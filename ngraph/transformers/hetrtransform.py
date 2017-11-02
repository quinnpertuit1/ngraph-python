# ----------------------------------------------------------------------------
# Copyright 2016 Nervana Systems Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ----------------------------------------------------------------------------
import collections
import os

from orderedset import OrderedSet
from six import itervalues, iteritems
from ngraph.op_graph.comm_nodes import ResultOp
from ngraph.op_graph.op_graph import Op, TensorValueOp
from ngraph.transformers.base import Computation
from ngraph.transformers.base import ComputationGraphTransformer
from ngraph.transformers.base import make_transformer_factory
from ngraph.transformers.hetr.mpilauncher import MPILauncher
from ngraph.transformers.passes.hetrpasses import CommunicationPass
from ngraph.transformers.passes.hetrpasses import DeviceAssignPass
from ngraph.transformers.passes.hetrpasses import AxesUpdatePass
from ngraph.op_graph.serde.serde import op_to_protobuf, add_edges
import logging


_OPS_PER_MSG = 10
logger = logging.getLogger(__name__)


def build_transformer(name, comm=None):
    """

    :param results: the graph nodes that we care about, for the computation
    :return: the dictionary of transformers, with names matching the graph node hints
    """
    if 'cpu' in name:
        transformer = make_transformer_factory('cpu', comm=comm)()
    elif 'gpu' in name:
        try:
            from ngraph.transformers.gputransform import GPUTransformer  # noqa
            transformer = make_transformer_factory('gpu', device_id=comm.Get_rank(), comm=comm)()
        except ImportError:
            assert False, "Fatal: Unable to initialize GPU, " \
                          "but GPU transformer was requested."
    else:
        assert False, "Unknown device!"

    return transformer


class HetrComputation(Computation):
    """
    Lightweight wrapper class for handling runtime execution of child computations for Hetr
    """

    def __init__(self, hetr, computation_op):
        self.child_computations = dict()
        self.transformer = hetr
        self.send_nodes = hetr.send_nodes
        self.computation_op = computation_op

        # self.returns could be replaced by comp_op.returns if it were expressed as a set
        self.returns = OrderedSet()
        if isinstance(computation_op.returns, collections.Container):
            self.returns.update(list(computation_op.returns))
        elif isinstance(computation_op.returns, Op):
            self.returns.update(list([computation_op.returns]))

        # if one of the requested results is marked as distributed across devices,
        # wrap it in a ResultOp to facilitate DistributedPass inserting a gather operation
        new_returns = OrderedSet()
        for op in self.returns:
            if 'device_id' in op.metadata and \
                    isinstance(op.metadata['device_id'], (list, tuple)):
                op.metadata['is_split_op'] = True
                new_result = ResultOp(device_id=0, args=tuple([op]))
                op.metadata['hetr_replaced_by'] = new_result
                new_result.metadata['replaces_op'] = op
                new_returns.add(new_result)
            else:
                new_returns.add(op)

        # Do Hetr passes
        pass_ops = new_returns | OrderedSet(self.computation_op.parameters)
        for graph_pass in self.transformer.graph_passes:
            pass_ops = pass_ops | OrderedSet(hetr.send_nodes)
            graph_pass.do_pass(ops=pass_ops)

        # hack around new TensorValueOp that wraps AssignableTensorOp
        # autogenerated by creating a ComputationOp:
        for p in self.computation_op.parameters:
            if isinstance(p, TensorValueOp):
                p.metadata.update(p.states_read[0].metadata)

        # assume all children are the same type
        # and all GPUs are in one chassis
        num_process = len(self.transformer.child_transformers)
        ppn = 1 if self.transformer.default_device == 'cpu' else num_process
        self.transformer.mpilauncher.launch(num_process, ppn)
        self.transformer.setup_child_transformers(num_process)

        def is_my_op(op, name):
            op_trans = op.metadata['transformer']
            return name == op_trans or name in op_trans

        # build whole_graph once to avoid slow serialization once per worker
        # split whole pb message into list of smaller chunks
        # gRPC prefers sending smaller messages
        placeholders = [p for p in self.computation_op.parameters]
        all_returns = [o for o in self.send_nodes | new_returns]
        transform_returns = [o.args[0] if isinstance(o, ResultOp) else o for o in all_returns]
        whole_graph = Op.all_op_references(transform_returns + placeholders)

        pb_whole_graph = []
        pb_ops, pb_edges = [], []
        for i, o in enumerate(whole_graph):
            pb_ops.append(op_to_protobuf(o))
            add_edges(pb_edges, pb_ops, o)
            if (i != 0 and i % _OPS_PER_MSG == 0) or (i == len(whole_graph) - 1):
                pb_whole_graph.append((pb_ops, pb_edges))
                pb_ops, pb_edges = [], []

        t_placeholders, t_returns = {}, {}
        for t_name in self.transformer.child_transformers.keys():
            t_placeholders[t_name] = [p for p in placeholders if is_my_op(p, t_name)]
            t_returns[t_name] = [r for r in all_returns if is_my_op(r, t_name)]

        # create_computation is an async call using gPRC future
        # allowing child transformers to create computation simultaneously
        # get_computation waits the corresponding request to finish
        logger.info('Start preparing the distributed graph.'),
        for t_name, trans in iteritems(self.transformer.child_transformers):
            logger.debug('child transformer: {}'.format(t_name))
            trans.build_transformer()
            transform_ops = [
                r.args[0] if isinstance(r, ResultOp) else r for r in t_returns[t_name]]
            trans.create_computation(pb_whole_graph, transform_ops, t_placeholders[t_name])

        for t_name, trans in iteritems(self.transformer.child_transformers):
            comp = trans.get_computation()
            comp.param_idx = [g_pos for g_pos, p in enumerate(self.computation_op.parameters)
                              if is_my_op(p, t_name)]

            # when there is a ResultOp, hack around it
            comp.returns = dict()
            for i, op in enumerate(t_returns[t_name]):
                if op in self.returns and 'hetr_replaced_by' not in op.metadata:
                    comp.returns[op] = i
                elif 'replaces_op' in op.metadata and op.metadata['replaces_op'] in self.returns:
                    comp.returns[op.metadata['replaces_op']] = i
            self.child_computations[t_name] = comp
        logger.info('Finished preparing the distributed graph.'),

    def __call__(self, *args, **kwargs):
        """
        Executes child computations in parallel.

        :arg args: list of values to the placeholders specified in __init__ *args

        :return: tuple of return values, one per return specified in __init__ returns list.
        """
        args = self.unpack_args_or_feed_dict(args, kwargs)
        for child in itervalues(self.child_computations):
            child.feed_input([args[i] for i in child.param_idx])

        return_vals = dict()
        for child in itervalues(self.child_computations):
            return_vals.update(child.get_results())
        if isinstance(self.computation_op.returns, Op):
            return return_vals[self.computation_op.returns]
        elif isinstance(self.computation_op.returns, (collections.Sequence, OrderedSet)):
            return tuple(return_vals[op] for op in self.computation_op.returns)
        elif isinstance(self.computation_op.returns, collections.Set):
            return return_vals
        else:
            return None


class HetrTransformer(ComputationGraphTransformer):
    """
    Transformer for executing graphs on a CPU, backed by numpy.

    Given a list of ops you want to compute the results of, this transformer
    will compile the graph required to compute those results and exposes an
    evaluate method to execute the compiled graph.
    """

    transformer_name = "hetr"

    default_rtol = 1e-05
    default_atol = 1e-08

    def __init__(self, device='cpu', **kwargs):
        super(HetrTransformer, self).__init__(**kwargs)

        self.default_device = device
        self.my_pid = os.getpid()
        self.is_closed = False
        self.child_transformers = dict()
        self.send_nodes = OrderedSet()
        self.graph_passes = [DeviceAssignPass(hetr=self,
                                              default_device=device,
                                              default_device_id=0),
                             CommunicationPass(self.send_nodes),
                             AxesUpdatePass()]
        self.mpilauncher = MPILauncher()

    def close(self):
        if self.is_closed:
            return
        if self.my_pid != os.getpid():
            # Only close once, and don't close if this is a copy in a child process
            return
        for t in self.child_transformers.values():
            t.close_transformer()
        for t in self.child_transformers.values():
            t.close()
        self.mpilauncher.close()
        super(HetrTransformer, self).close()
        self.is_closed = True

    def register_transformer(self, tname):
        # TODO: Issue #1866 change from using tname string to using (ttype, dev_id, host) tuple
        if tname not in self.child_transformers:
            if 'cpu' in tname or 'gpu' in tname:
                from ngraph.transformers.hetr.rpc_client import RPCTransformerClient
                # TODO: use dev_id from tuple
                dev_id = int(tname[3:])
                logger.debug("register_transformer: dev_id %d", dev_id)
                trans_client = RPCTransformerClient(tname)
            self.child_transformers[tname] = trans_client

    def setup_child_transformers(self, num_servers):
        # expect that all child transformers have been already registered
        for tname, trans in iteritems(self.child_transformers):
            dev_id = int(tname[3:])
            server_address = self.mpilauncher.get_address_by_rank(dev_id, num_servers)
            trans.set_server_address(server_address)
            logger.debug("setup_child_transformers: dev_id %d, server_address %s",
                         dev_id, server_address)

    def transformer(self, tname):
        assert tname in self.child_transformers, "register transformer {} before use".format(tname)
        return self.child_transformers[tname]

    def add_computation(self, computation):
        return self.make_computation(computation)

    def make_computation(self, computation):
        """
        Build a heterogeneous computation object that implements
        communication and synchronization between subgraphs run
        on child transformers.

        Arguments:
            computation: A computation Op.

        Returns:
            Callable.
        """
        hetr_comp = HetrComputation(self, computation)
        return hetr_comp

    """
    These APIs are internally used between regular transformers and
    their computations.  HeTr has no use or need for them but is
    required to provide the functions by the metaclass in order
    to be a 'Transformer', which it wants to be in order to expose
    the user-facing parts of the Transformer API.
    """
    # TODO: Refer Issue #978
    def initialize(self):
        pass

    def device_buffer_storage(self, bytes, dtype, name):
        assert False, "Should not be used, TODO cleanup"

    def start_transform_allocate(self):
        assert False, "Should not be used, TODO cleanup"

    def transform_allocate_ops(self, all_ops):
        assert False, "Should not be used, TODO cleanup"

    def finish_transform_allocate(self):
        assert False, "Should not be used, TODO cleanup"

    def transform_ordered_ops(self, ordered_ops, name):
        pass

    def finish_transform(self):
        assert False, "Should not be used, TODO cleanup"

    def allocate_storage(self):
        assert False, "Should not be used, TODO cleanup"

    def add_initialization_ops(self, ops):
        pass

    def state_initializations(self, states):
        pass
