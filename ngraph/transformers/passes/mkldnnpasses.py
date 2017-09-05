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
from __future__ import division

from ngraph.op_graph.convolution import ConvolutionOp, bprop_conv, update_conv
from ngraph.op_graph.op_graph import Op, MapRolesOp, TensorOp, ComputationOp, \
    Flatten, Unflatten, unflatten, ReorderAxes, DotLowDimension, Add, ContiguousOp, ReturnOp
from ngraph.op_graph.pooling import PoolingOp, BpropPoolOp
from ngraph.transformers.cpu.batchnorm import BatchnormOp, BpropBatchnormOp
from ngraph.op_graph.axes import Axes, FlattenedAxis
from ngraph.transformers.cpu.relu import ReluOp, BpropReluOp
from ngraph.transformers.passes.passes import PeepholeGraphPass
from ngraph.util.generics import generic_method

import ctypes as ct
import numpy as np
import collections
from operator import itemgetter
from orderedset import OrderedSet


class MklReorderOp(TensorOp):
    '''
    Converts op value tensor from MKL layouts to "native" layout
    '''

    def __init__(self, arg, in_layout, out_layout, **kwargs):
        super(MklReorderOp, self).__init__(args=(arg,), axes=arg.axes, **kwargs)
        self.in_layout = in_layout
        self.out_layout = out_layout


def get_order_from_axes(axes, sub_axes):
    order = []
    for a in sub_axes:
        found = False
        for (index, b) in enumerate(axes):
            if b.name == a.name:
                order.append(index)
                found = True
                continue
        if not found:
            assert False, "Axis not found"
    return order


def get_axes_mkl_order(axes, order):
    axes_list = []
    flattend_axis_flag = False
    for axis in axes:
        if(axis.is_flattened and len(order) > 2):
            unflattend_axis = unflatten(axis).axes
            for indx in range(len(unflattend_axis)):
                axes_list.append(unflattend_axis[indx])
                flattend_axis_flag = True
        else:
            axes_list.append(axis)
    if flattend_axis_flag:
        return [axes_list[index] for index in order]
    else:
        return [axes[index] for index in order]


def get_size_mkl_order(axes, order):
    return [a.length for a in get_axes_mkl_order(axes, order)]


def get_strides_mkl_order(td, order):
    strides_list = []
    flattend_axis_flag = False
    for axis in td.axes:
        if (axis.is_flattened and len(order) > 2):
            flattend_axis_flag = True
    if flattend_axis_flag:
        for each_stride in td.full_strides:
            if isinstance(each_stride, tuple):
                for stride_val in each_stride:
                    strides_list.append(stride_val)
            else:
                strides_list.append(each_stride)
        return [strides_list[index] for index in order]
    else:
        return [td.strides[index] for index in order]


def get_native_layout(mkldnn, td, order, use_formats=True):
    '''
    Create an MKL layout object in transformer-visible layout
    :param td: tensor description of the op. Currently owns tensor layout info in graph
    :param order: order in which axes need to be specified to MKL
    :param use_formats: Optimization to identify canned MKL formats
    :return: MKL layout object
    '''
    op_axes = td.axes
    mkl_shape = get_size_mkl_order(op_axes, order)
    data_type = mkldnn.datatype[td.dtype.type]
    elem_size = td.dtype.itemsize
    mkl_strides = [stride // elem_size for stride in get_strides_mkl_order(td, order)]
    # TODO(jbobba) - Handle views for tensors that are not fully materialized
    mkl_axes = [axis for axis in get_axes_mkl_order(op_axes, order)]
    memory_format = mkldnn.memory_format['blocked']
    if use_formats:
        # Look for canned formats
        if len(mkl_strides) == 4:
            [N, C, H, W] = mkl_strides
            stride_order = sorted([N, C, H, W], reverse=True)
            if (stride_order == [C, H, W, N]):
                memory_format = mkldnn.memory_format['chwn']
            elif (stride_order == [N, C, H, W]):
                memory_format = mkldnn.memory_format['nchw']
        elif len(mkl_strides) == 2:
            [N, C] = mkl_strides
            stride_order = sorted([N, C], reverse=True)
            if stride_order == [N, C]:
                memory_format = mkldnn.memory_format['nc']

    native_layout = mkldnn.create_layout_md(
        mkldnn.mkldnn_engine,
        len(mkl_shape), get_ctypes_arg(mkl_shape),
        get_ctypes_arg(mkl_strides), data_type, memory_format)
    mkldnn.native_layouts += [native_layout]
    return (native_layout, mkl_axes)


def get_mkl_layout(mkldnn, op, order, use_formats=True):
    if op.name in mkldnn.op_layouts:
        return mkldnn.op_layouts[op.name]
    else:
        return get_native_layout(mkldnn, op.tensor_description(), order, use_formats)


def dbg_print_kernel(mkldnn, op, op_id):
    if (mkldnn.mkldnn_verbose):
        print
        print(op_id, op.name, op.axes)
        mkldnn.print_kernel(mkldnn.kernels[op.name])


def get_ctypes_arg(x):
    return ((ct.c_int) * len(x))(*x) if x else None


def get_flattened_axes(x):
    """
    Ordered list of axis visible to MKLDNN
    """
    return [axis for axis in Axes.as_flattened_list(x) if axis.name != '__NG_DEPTH']


def get_rotated_layout(mkldnn, in_layout, from_axes, to_axes):
    permute_order = [from_axes.index(axis) for axis in to_axes]
    new_layout = mkldnn.layout_reorder(in_layout, get_ctypes_arg(permute_order))
    mkldnn.native_layouts += [new_layout]
    return new_layout

def get_arg_output_idx(exop, arg_exop):
    for i, output_decl in enumerate(arg_exop.output_decls):
        for input_decl in output_decl.user_input_decls:
            if input_decl.exop == exop:
                # Assumes only arg comes from arg_exop to exop
                return i
    # assert False
    return 0


class MklCreateOpDescriptors(PeepholeGraphPass):
    """
    Creates MKL-DNN op kernels for ops in the graph that have an MKL-DNN implementation.
    Most MKL-DNN op kernels produce tensors in MKL-DNN layout that is tracked and propagated
    to downstream ops. Index ops such as ReorderAxes and 'Meta' ops such as MapRolesOp
    update and propagate MKL-DNN tensor layout information. MKL-DNN conversion ops to convert
    tensors from MKL-DNN layout to a graph-visible layout are inserted in a subsequent pass.

    Steps for creating an op kernel
    1) Check if op is supported by MKL-DNN
    2) Marshall op parameters from graph to pass on to MKL-DNN
    3) Create or extract MKL-DNN layouts for inputs
    4) Create MKL-DNN op kernel
    5) Remember output MKL-layout and MKL-visible axes for use by ops downstream

    """

    def __init__(self, mkldnn, **kwargs):
        super(MklCreateOpDescriptors, self).__init__(**kwargs)
        assert mkldnn.enabled
        self.mkldnn = mkldnn
    
    def get_exop(self, op):
        return self.op_accessor.computation_decl.get_exop(op)

    def set_mkl_layout(self, op, mkl_axes, index=0):
        exop = self.get_exop(op)
        layout = self.mkldnn.output_layout(self.mkldnn.kernels[op.name], index)
        if layout:
            self.mkldnn.op_layouts[op.name] = (layout, mkl_axes)
            exop.output_decls[index].tensor_view_decl.mkl_layout = (layout, mkl_axes)
    
    def get_arg_shape_and_layout(self, op, arg, mkl_order):
        arg_idx = get_arg_output_idx(self.get_exop(op), self.get_exop(arg))
        return self.get_op_shape_and_layout(arg, mkl_order, arg_idx)

    def get_op_shape_and_layout(self, op, mkl_order, index=0):
        exop = self.get_exop(op)
        mkl_layout = exop.output_decls[index].tensor_view_decl.mkl_layout
        op_axes_mkl = [op.axes[idx] for idx in mkl_order]
        mkl_shape = [a.length for a in op_axes_mkl]
        if op.name in self.mkldnn.op_layouts:
            temp_layout = self.mkldnn.op_layouts[op.name]
            assert mkl_layout != None and mkl_layout == temp_layout
            in_layout, in_axes = self.mkldnn.op_layouts[op.name]
            # Check if we need to rotate axes in the MKL layout object
            if op_axes_mkl != in_axes:
                assert Axes(
                    get_flattened_axes(in_axes)).is_equal_set(
                    Axes(
                        get_flattened_axes(op_axes_mkl)))
                mkl_layout = get_rotated_layout(
                    self.mkldnn,
                    in_layout,
                    get_flattened_axes(in_axes),
                    get_flattened_axes(op_axes_mkl))
            else:
                mkl_layout = in_layout
        else:
            mkl_layout = get_native_layout(self.mkldnn, op.tensor_description(), mkl_order, True)[0]

        return mkl_shape, mkl_layout

    def move_child_exops(self, exop, after_exop):
        """
        Recursively move exop's children/deps to after after_exop if needed
        """
        for output_decl in exop.output_decls:
            for child_input_decl in output_decl.user_input_decls:
                child_exop = child_input_decl.exop
                n_exop = child_exop
                while n_exop != after_exop and not n_exop.is_exop_end_of_list:
                    n_exop = n_exop.next_exop
                if n_exop == after_exop and child_exop != after_exop:
                    self.op_accessor.exop_block.move_exop_to_after_exop(child_exop, after_exop)
                    self.move_child_exops(child_exop, child_exop)


    def replace_exop(self, new_op, old_op):
        """
        1) Replace old_op's output decl with a new output_decl from new_op
        2) Adjust the position of old_op's children
        3) Delete old_op's exop
        """
        new_exop = self.op_accessor.computation_decl.get_exop(new_op)
        old_exop = self.op_accessor.computation_decl.get_exop(old_op)
        old_td = old_op.tensor_description()
        old_tensor_decl = self.op_accessor.computation_decl.get_tensor_decl(old_op)
        new_output_decl = new_exop.add_output_decl(old_tensor_decl, old_td)
        self.move_child_exops(old_exop, new_exop)
        self.op_accessor.exop_block.replace_output_decl(old_exop.output_decls[0], new_output_decl)
        self.op_accessor.exop_block.remove_exop(old_exop)

    @generic_method(dispatch_base_type=Op)
    def visit(self, op, *args):
        pass

    @visit.on_type(BatchnormOp)
    def visit(self, op, inputs, gamma, bias, epsilon, mean, variance):
        inputs_f = get_flattened_axes(inputs.axes)
        # unflatten the inputs and extract C H W N params
        if isinstance(inputs, Flatten):
            unflatten_inputs = unflatten(inputs)
            # Sanity check tensor shapes
            if (len(unflatten_inputs.axes.lengths) != 5):
                return
        else:
            if (len(inputs.axes.lengths) != 5):
                return
        # Only single precision float supported for now
        if op.dtype != np.float32:
            return
        # Depth needs to be 1 for MKLDNN. Need channels to be a multiple of 8
        if inputs.axes[1].length != 1 or inputs.axes[0].length % 8 != 0:
            return

        mkl_order = [4, 0, 2, 3]
        data_type = self.mkldnn.datatype[op.dtype.type]
        inputs_shape = get_size_mkl_order(inputs.axes, mkl_order)
        mean_size = mean.axes.lengths[0]
        mean_dims = 1
        gamma_shape = gamma.axes.lengths[0]
        bias_shape = bias.axes.lengths[0]
        variance_size = variance.axes.lengths[0]
        variance_dims = 1
        outputs_shape = get_size_mkl_order(op.axes, mkl_order)

        # weights is 2 dimensional, 1-st dimension contains gamma parameter, 2-nd
        # dimension contains beta parameter.
        weights_shape = [gamma_shape, bias_shape]

        (inputs_shape, inputs_layout) = self.get_arg_shape_and_layout(op, inputs, mkl_order)
        # (inputs_layout, mkl_axes) = get_mkl_layout(
        #    self.mkldnn, inputs, mkl_order, True)
        mean_layout = None
        variance_layout = None

        op_id = len(self.mkldnn.kernels)
        self.mkldnn.kernels[op.name] = self.mkldnn.create_empty_kernel(op_id)

        self.mkldnn.batchnorm_fprop_kernel(
            self.mkldnn.mkldnn_engine,
            len(inputs_shape),
            len(outputs_shape),
            len(weights_shape),
            mean_dims,
            variance_dims,
            mean_size,
            variance_size,
            get_ctypes_arg(inputs_shape),
            get_ctypes_arg(weights_shape),
            get_ctypes_arg(outputs_shape),
            op.eps,
            inputs_layout,
            None,
            mean_layout,
            variance_layout,
            data_type,
            self.mkldnn.kernels[
                op.name])

        out_axes = get_axes_mkl_order(op.axes, mkl_order)
        self.set_mkl_layout(op, out_axes)
        dbg_print_kernel(self.mkldnn, op, op_id)

        self.replace_exop(op, mean)
        self.replace_exop(op, variance)


    @visit.on_type(BpropBatchnormOp)
    def visit(self, op, delta, fprop_src, dgamma, dbeta, gamma, bias, mean, variance):
        axis_len_5d = False
        # Only single precision float supported for now
        if op.dtype != np.float32:
            return
        # Sanity check tensor shapes
        if (len(op.axes.lengths) == 2):
            if isinstance(op.axes[1], FlattenedAxis):
                C, Flatten_axis = op.axes
                if (len(unflatten(Flatten_axis).axes.lengths) != 4):
                    return
            else:
                return
        elif len(op.axes.lengths) != 5:
            return

        outputs_shape = get_size_mkl_order(op.axes, [4, 0, 2, 3])
        axis_len_5d = True

        data_type = self.mkldnn.datatype[op.dtype.type]
        mean_dims = 1
        variance_dims = 1
        mean_size = mean.axes.lengths[0]
        variance_size = variance.axes.lengths[0]

        delta_shape = get_size_mkl_order(delta.axes, [4, 0, 2, 3])

        # weights is 2 dimensional, 1-st dimension contains gamma parameter, 2-nd
        # dimension contains beta parameter.
        gamma_shape = gamma.axes.lengths[0]
        bias_shape = bias.axes.lengths[0]
        weights_shape = [gamma_shape, bias_shape]

        if (axis_len_5d):
            (delta_layout, mkl_axes) = get_mkl_layout(
                self.mkldnn, unflatten(delta), [4, 0, 2, 3], True)
        if (axis_len_5d):
            (fprop_src_layout, _) = get_mkl_layout(
                self.mkldnn, unflatten(fprop_src), [4, 0, 2, 3], True)

        if fprop_src.name in self.mkldnn.op_layouts:
            (fprop_src_layout, _) = self.mkldnn.op_layouts.get(fprop_src.name)

        (delta_shape, delta_layout) = self.get_arg_shape_and_layout(op, delta, [4, 0, 2 ,3])
        mean_layout = None
        # if mean.name in self.mkldnn.kernels:
        #    mean_layout = self.mkldnn.op_layouts[mean.name]

        variance_layout = None
        # if variance.name in self.mkldnn.kernels:
        #     variance_layout = self.mkldnn.op_layouts[variance.name]

        op_id = len(self.mkldnn.kernels)
        self.mkldnn.kernels[op.name] = self.mkldnn.create_empty_kernel(op_id)

        self.mkldnn.batchnorm_bprop_kernel(
            self.mkldnn.mkldnn_engine,
            len(delta_shape),
            len(outputs_shape),
            len(weights_shape),
            mean_dims,
            variance_dims,
            get_ctypes_arg(delta_shape),
            get_ctypes_arg(outputs_shape),
            get_ctypes_arg(weights_shape),
            mean_size,
            variance_size,
            op.fprop.eps,
            fprop_src_layout,
            None,
            mean_layout,
            variance_layout,
            delta_layout,
            data_type,
            self.mkldnn.kernels[
                op.fprop.forwarded.name],
            self.mkldnn.kernels[
                op.name])
        # mkl_order = get_order_from_axes(unflatten(delta).axes, mkl_axes)
        out_axes = get_axes_mkl_order(op.axes, [4, 0, 2, 3])
        self.set_mkl_layout(op, out_axes)
        dbg_print_kernel(self.mkldnn, op, op_id)

        self.replace_exop(op, dgamma)
        self.replace_exop(op, dbeta)

    @visit.on_type(ConvolutionOp)
    def visit(self, op, input, filter, bias=None):

        # Only 2D convolution supported in MKLDNN for now
        if (input.axes.find_by_name('__NG_DEPTH').size != 1):
            return
        # Only single precision float supported for now
        if (op.dtype.type != np.float32):
            return

        data_type = self.mkldnn.datatype[op.dtype.type]
        # Assumes (C, D, H, W, N) for convolution axes and (I, D, H, W, O) for filter axes

        (input_shape, input_layout) = self.get_arg_shape_and_layout(op, input, [4, 0, 2, 3])
        (filter_shape, filter_layout) = self.get_arg_shape_and_layout(op, filter, [4, 0, 2, 3])
        bias_shape = get_size_mkl_order(bias.axes, [0]) if bias else None
        output_shape = get_size_mkl_order(op.axes, [4, 0, 2, 3])
        out_axes = get_axes_mkl_order(op.axes, [4, 0, 2, 3])
        pad_d, pad_h, pad_w = itemgetter(
            *('pad_' + s for s in ('d', 'h', 'w')))(op.conv_params)
        str_d, str_h, str_w = itemgetter(
            *('str_' + s for s in ('d', 'h', 'w')))(op.conv_params)
        pad = [pad_h, pad_w]
        stride = [str_h, str_w]

        op_id = len(self.mkldnn.kernels)
        self.mkldnn.kernels[op.name] = self.mkldnn.create_empty_kernel(op_id)
        self.mkldnn.conv_fprop_kernel(
            self.mkldnn.mkldnn_engine,
            len(input_shape),
            len(filter_shape),
            len(bias_shape) if bias_shape else 0,
            len(output_shape),
            get_ctypes_arg(input_shape),
            get_ctypes_arg(filter_shape),
            get_ctypes_arg(bias_shape),
            get_ctypes_arg(output_shape),
            get_ctypes_arg(stride),
            get_ctypes_arg(pad),
            input_layout,
            filter_layout,
            data_type,
            self.mkldnn.kernels[
                op.name])

        self.set_mkl_layout(op, out_axes)
        dbg_print_kernel(self.mkldnn, op, op_id)

    @visit.on_type(bprop_conv)
    def visit(self, op, input, filter):
        # Only 2D convolution supported in MKLDNN for now
        if (input.axes.find_by_name('__NG_DEPTH').size != 1):
            return
        # Only single precision float supported for now
        if (op.dtype.type != np.float32):
            return

        data_type = self.mkldnn.datatype[op.dtype.type]
        # Assumes (C, D, H, W, N) for convolution axes and (I, D, H, W, O) for filter axes
        (input_shape, input_layout) = self.get_arg_shape_and_layout(op, input, [4, 0, 2, 3])
        (filter_shape, filter_layout) = self.get_arg_shape_and_layout(op, filter, [4, 0, 2, 3])
        output_shape = get_size_mkl_order(op.axes, [4, 0, 2, 3])
        out_axes = get_axes_mkl_order(op.axes, [4, 0, 2, 3])
        pad_d, pad_h, pad_w = itemgetter(
            *('pad_' + s for s in ('d', 'h', 'w')))(op.conv_params)
        str_d, str_h, str_w = itemgetter(
            *('str_' + s for s in ('d', 'h', 'w')))(op.conv_params)
        pad = [pad_h, pad_w]
        stride = [str_h, str_w]

        op_id = len(self.mkldnn.kernels)
        self.mkldnn.kernels[op.name] = self.mkldnn.create_empty_kernel(op_id)
        self.mkldnn.conv_bprop_kernel(
            self.mkldnn.mkldnn_engine,
            len(input_shape),
            len(filter_shape),
            len(output_shape),
            get_ctypes_arg(input_shape),
            get_ctypes_arg(filter_shape),
            get_ctypes_arg(output_shape),
            get_ctypes_arg(stride),
            get_ctypes_arg(pad),
            input_layout,
            filter_layout,
            data_type,
            self.mkldnn.kernels[
                op.name])

        self.set_mkl_layout(op, out_axes)
        dbg_print_kernel(self.mkldnn, op, op_id)

    @visit.on_type(update_conv)
    def visit(self, op, delta, inputs):
        # Only 2D convolution supported in MKLDNN for now
        if (delta.axes.find_by_name('__NG_DEPTH').size != 1):
            return
        # Only single precision float supported for now
        if (op.dtype.type != np.float32):
            return

        data_type = self.mkldnn.datatype[op.dtype.type]
        # Assumes (C, D, H, W, N) for convolution axes and (I, D, H, W, O) for filter axes
        (delta_shape, delta_layout) = self.get_arg_shape_and_layout(op, delta, [4, 0, 2, 3])
        (inputs_shape, inputs_layout) = self.get_arg_shape_and_layout(op, inputs, [4, 0, 2, 3])
        # Output
        (filter_shape, filter_layout) = self.get_op_shape_and_layout(op, [4, 0, 2, 3], 0)
        pad_d, pad_h, pad_w = itemgetter(
            *('pad_' + s for s in ('d', 'h', 'w')))(op.conv_params)
        str_d, str_h, str_w = itemgetter(
            *('str_' + s for s in ('d', 'h', 'w')))(op.conv_params)
        pad = [pad_h, pad_w]
        stride = [str_h, str_w]

        op_id = len(self.mkldnn.kernels)
        self.mkldnn.kernels[op.name] = self.mkldnn.create_empty_kernel(op_id)
        self.mkldnn.update_conv_kernel(
            self.mkldnn.mkldnn_engine,
            len(delta_shape),
            len(filter_shape),
            len(inputs_shape),
            get_ctypes_arg(delta_shape),
            get_ctypes_arg(filter_shape),
            get_ctypes_arg(inputs_shape),
            get_ctypes_arg(stride),
            get_ctypes_arg(pad),
            delta_layout,
            filter_layout,
            inputs_layout,
            data_type,
            self.mkldnn.kernels[
                op.name])
        # Output is in ngraph layout. We dont need set_mkl_layout
        dbg_print_kernel(self.mkldnn, op, op_id)

    @visit.on_type(ReluOp)
    def visit(self, op, input):
        if (op.dtype.type != np.float32):
            return
        if (len(op.axes) != 5 and len(op.axes) != 2):
            return

        data_type = self.mkldnn.datatype[op.dtype.type]
        if len(op.axes) == 5:
            (input_shape, input_layout) = self.get_arg_shape_and_layout(op, input, [4, 0, 2, 3])
            out_axes = get_axes_mkl_order(op.axes, [4, 0, 2, 3])
        elif len(op.axes) == 2:
            (input_shape, input_layout) = self.get_arg_shape_and_layout(op, input, [1, 0])
            out_axes = get_axes_mkl_order(op.axes, [1, 0])

        input_size = np.prod(input.axes.lengths)
        op_id = len(self.mkldnn.kernels)
        self.mkldnn.kernels[op.name] = self.mkldnn.create_empty_kernel(op_id)
        self.mkldnn.relu_fprop_kernel(
            self.mkldnn.mkldnn_engine,
            input_size, op.slope,
            input_layout,
            data_type,
            self.mkldnn.kernels[op.name])

        self.set_mkl_layout(op, out_axes)
        dbg_print_kernel(self.mkldnn, op, op_id)

    @visit.on_type(BpropReluOp)
    def visit(self, op, delta, fprop_src):
        if (op.dtype.type != np.float32):
            return
        if (len(op.axes) != 5 and len(op.axes) != 2):
            return

        data_type = self.mkldnn.datatype[op.dtype.type]
        if len(op.axes) == 5:
            (delta_shape, delta_layout) = self.get_arg_shape_and_layout(op, delta, [4, 0, 2, 3])
            (fprop_src_shape, fprop_src_layout) = self.get_arg_shape_and_layout(op, fprop_src, [4, 0, 2, 3])
            out_axes = get_axes_mkl_order(op.axes, [4, 0, 2, 3])
        elif len(op.axes) == 2:
            (delta_shape, delta_layout) = self.get_arg_shape_and_layout(op, delta, [1, 0])
            (fprop_src_shape, fprop_src_layout) = self.get_arg_shape_and_layout(op, fprop_src, [1, 0])
            out_axes = get_axes_mkl_order(op.axes, [1, 0])

        input_size = np.prod(delta.axes.lengths)
        op_id = len(self.mkldnn.kernels)
        self.mkldnn.kernels[op.name] = self.mkldnn.create_empty_kernel(op_id)
        self.mkldnn.relu_bprop_kernel(
            self.mkldnn.mkldnn_engine,
            input_size, op.fprop.forwarded.slope,
            fprop_src_layout, delta_layout,
            data_type,
            self.mkldnn.kernels[op.name])

        self.set_mkl_layout(op, out_axes)
        dbg_print_kernel(self.mkldnn, op, op_id)

    @visit.on_type(PoolingOp)
    def visit(self, op, input):
        # Only 2D pooling supported in MKLDNN for now
        if (input.axes.find_by_name('__NG_DEPTH').size != 1):
            return
        if (op.pool_params['J'] != 1 or op.pool_params['T'] != 1):
            return
        # Only single precision float supported for now
        if op.dtype != np.float32:
            return
        # Sanity check tensor shapes
        if (len(op.axes.lengths) != 5):
            return

        data_type = self.mkldnn.datatype[op.dtype.type]
        # Assumes (C, D, H, W, N) for pooling axes
        (input_shape, input_layout) = self.get_arg_shape_and_layout(op, input, [4, 0, 2, 3])
        output_shape = get_size_mkl_order(op.axes, [4, 0, 2, 3])
        out_axes = get_axes_mkl_order(op.axes, [4, 0, 2, 3])
        kernel = [op.pool_params['R'], op.pool_params['S']]
        pad_d, pad_h, pad_w = itemgetter(*('pad_' + s for s in ('d', 'h', 'w')))(op.pool_params)
        str_d, str_h, str_w = itemgetter(*('str_' + s for s in ('d', 'h', 'w')))(op.pool_params)
        pad = [pad_h, pad_w]
        stride = [str_h, str_w]
        op_type = op.pool_params
        pool_type = 0
        if op_type['op'] == 'avg':
            pool_type = 1

        op_id = len(self.mkldnn.kernels)
        self.mkldnn.kernels[op.name] = self.mkldnn.create_empty_kernel(op_id)
        self.mkldnn.pool_fprop_kernel(
            self.mkldnn.mkldnn_engine,
            len(input_shape), len(output_shape),
            get_ctypes_arg(input_shape), get_ctypes_arg(kernel), get_ctypes_arg(output_shape),
            get_ctypes_arg(stride), get_ctypes_arg(pad), pool_type,
            input_layout, data_type, self.mkldnn.kernels[op.name])

        self.set_mkl_layout(op, out_axes)
        dbg_print_kernel(self.mkldnn, op, op_id)

    @visit.on_type(BpropPoolOp)
    def visit(self, op, input):
        # Only 2D pooling supported in MKLDNN for now
        if (input.axes.find_by_name('__NG_DEPTH').size != 1):
            return
        if (op.pool_params['J'] != 1 or op.pool_params['T'] != 1):
            return
        # Only single precision float supported for now
        if op.dtype != np.float32:
            return
        # Sanity check tensor shapes
        if (len(op.axes.lengths) != 5):
            return

        data_type = self.mkldnn.datatype[op.dtype.type]
        # Assumes (C, D, H, W, N) for pooling axes
        (input_shape, input_layout) = self.get_arg_shape_and_layout(op, input, [4, 0, 2, 3])
        output_shape = get_size_mkl_order(op.axes, [4, 0, 2, 3])
        out_axes = get_axes_mkl_order(op.axes, [4, 0, 2, 3])
        kernel = [op.pool_params['R'], op.pool_params['S']]
        pad_d, pad_h, pad_w = itemgetter(*('pad_' + s for s in ('d', 'h', 'w')))(op.pool_params)
        str_d, str_h, str_w = itemgetter(*('str_' + s for s in ('d', 'h', 'w')))(op.pool_params)
        pad = [pad_h, pad_w]
        stride = [str_h, str_w]
        op_type = op.pool_params
        pool_type = 0
        if op_type['op'] == 'avg':
            pool_type = 1

        op_id = len(self.mkldnn.kernels)
        self.mkldnn.kernels[op.name] = self.mkldnn.create_empty_kernel(op_id)
        self.mkldnn.pool_bprop_kernel(
            self.mkldnn.mkldnn_engine,
            len(input_shape), len(output_shape),
            get_ctypes_arg(input_shape), get_ctypes_arg(kernel), get_ctypes_arg(output_shape),
            get_ctypes_arg(stride), get_ctypes_arg(pad), pool_type,
            input_layout, data_type,
            self.mkldnn.kernels[op.fprop.forwarded.name],
            self.mkldnn.kernels[op.name])

        self.set_mkl_layout(op, out_axes)
        dbg_print_kernel(self.mkldnn, op, op_id)

    @visit.on_type(DotLowDimension)
    def visit(self, op, x, y, bias=None):

        # Sanity check tensor shapes
        if (len(x.axes.lengths) != 2) or (len(y.axes.lengths) != 2):
            return
        # Only single precision float supported for now
        if op.dtype != np.float32:
            return

        (x_shape, x_layout) = self.get_arg_shape_and_layout(op, x, [0, 1])
        (y_shape, y_layout) = self.get_arg_shape_and_layout(op, y, [1, 0])

        o_shape = get_size_mkl_order(op.axes, [1, 0])
        bias_shape = [o_shape[1]] if bias else None

        bias_layout = None
        data_type = self.mkldnn.datatype[op.dtype.type]

        op_id = len(self.mkldnn.kernels)
        self.mkldnn.kernels[op.name] = self.mkldnn.create_empty_kernel(op_id)
        self.mkldnn.innerproduct_fprop_kernel(
            self.mkldnn.mkldnn_engine,
            len(x_shape), len(y_shape), 1, len(o_shape),
            get_ctypes_arg(x_shape), get_ctypes_arg(y_shape),
            get_ctypes_arg(bias_shape), get_ctypes_arg(o_shape),
            x_layout, y_layout, bias_layout,
            data_type, self.mkldnn.kernels[op.name])

        out_axes = get_axes_mkl_order(op.axes, [1, 0])
        self.set_mkl_layout(op, out_axes)
        dbg_print_kernel(self.mkldnn, op, op_id)

    @visit.on_type(Add)
    def visit(self, op, x, y):
        # Disable for now since we are seeing perf slowdowns
        # return

        # Sanity check for tensor shapes
        if (op.dtype.type != np.float32):
            return
        if len(x.shape) != 5 or len(y.shape) != 5:
            return

        arg_idx = get_arg_output_idx(self.get_exop(op), self.get_exop(x))
        if self.get_exop(x).output_decls[arg_idx].tensor_view_decl.mkl_layout is None:
	          return

        (_, input_axes) = self.get_exop(x).output_decls[arg_idx].tensor_view_decl.mkl_layout
        mkl_order = get_order_from_axes(op.axes, input_axes)
        data_type = self.mkldnn.datatype[op.dtype.type]
        (x_shape, x_layout) = self.get_arg_shape_and_layout(op, x, mkl_order)
        (y_shape, y_layout) = self.get_arg_shape_and_layout(op, y, mkl_order)
        out_shape = op.axes.lengths

        op_id = len(self.mkldnn.kernels)
        self.mkldnn.kernels[op.name] = self.mkldnn.create_empty_kernel(op_id)
        self.mkldnn.add_kernel(
            self.mkldnn.mkldnn_engine,
            len(x_shape), len(y_shape), len(out_shape),
            get_ctypes_arg(x_shape), get_ctypes_arg(y_shape),
            get_ctypes_arg(out_shape),
            x_layout, y_layout,
            2,
            data_type, self.mkldnn.kernels[op.name])
        
        out_axes = get_axes_mkl_order(op.axes, mkl_order)
        self.set_mkl_layout(op, out_axes)
        dbg_print_kernel(self.mkldnn, op, op_id)

    @visit.on_type(ContiguousOp)
    def visit(self, op, arg):
        if arg.name in self.mkldnn.op_layouts:
            self.mkldnn.op_layouts[op.name] = self.mkldnn.op_layouts[arg.name]
            self.get_exop(op).output_decls[0].tensor_view_decl.mkl_layout = self.mkldnn.op_layouts[arg.name]
        elif (not arg.tensor_description().c_contiguous and
                all(stride != 0 for stride in arg.tensor_description().strides)):
            # TODO(jbobba): Disabled until MKLDNN coversion bug is fixed
            return
            ndims = len(op.axes)
            order = list(range(ndims))
            (in_shape, in_layout) = self.get_arg_shape_and_layout(op, arg, order)
            (out_shape, out_layout) = self.get_op_shape_and_layout(op, order, 0)

            op_id = len(self.mkldnn.kernels)
            self.mkldnn.kernels[op.name] = self.mkldnn.create_empty_kernel(op_id)
            self.mkldnn.reorder_kernel(
                self.mkldnn.mkldnn_engine,
                ndims, get_ctypes_arg(in_shape),
                self.mkldnn.datatype[op.dtype.type],
                in_layout, out_layout,
                self.mkldnn.kernels[op.name]
            )
            dbg_print_kernel(self.mkldnn, op, op_id)

    @visit.on_type(MapRolesOp)
    def visit(self, op, arg):
        if arg.name in self.mkldnn.op_layouts:
            (mkl_layout, mkl_axes) = self.mkldnn.op_layouts[arg.name]
            order = get_order_from_axes(arg.axes, mkl_axes)
            new_axes = get_axes_mkl_order(op.axes, order)
            self.mkldnn.op_layouts[op.name] = (mkl_layout, new_axes)
            self.get_exop(op).output_decls[0].tensor_view_decl.mkl_layout = (mkl_layout, new_axes)

    @visit.on_type(ReorderAxes)
    def visit(self, op, arg):
        if arg.name in self.mkldnn.op_layouts:
            self.mkldnn.op_layouts[op.name] = self.mkldnn.op_layouts[arg.name]
            self.get_exop(op).output_decls[0].tensor_view_decl.mkl_layout = self.mkldnn.op_layouts[arg.name]

    @visit.on_type(Unflatten)
    def visit(self, op, arg):
        if arg.name in self.mkldnn.op_layouts:
            if (len(arg.axes) == len(op.axes)):
                (mkl_layout, mkl_axes) = self.mkldnn.op_layouts[arg.name]
                order = get_order_from_axes(arg.axes, mkl_axes)
                new_axes = get_axes_mkl_order(op.axes, order)
                self.mkldnn.op_layouts[op.name] = (mkl_layout, new_axes)
                self.get_exop(op).output_decls[0].tensor_view_decl.mkl_layout = (mkl_layout, new_axes)

    @visit.on_type(Flatten)
    def visit(self, op, arg):
        if arg.name in self.mkldnn.op_layouts:
            if (len(arg.axes) == len(op.axes)):
                (mkl_layout, mkl_axes) = self.mkldnn.op_layouts[arg.name]
                order = get_order_from_axes(arg.axes, mkl_axes)
                new_axes = get_axes_mkl_order(op.axes, order)
                self.mkldnn.op_layouts[op.name] = (mkl_layout, new_axes)
                self.get_exop(op).output_decls[0].tensor_view_decl.mkl_layout = (mkl_layout, new_axes)

class MklAddLayoutConversions(PeepholeGraphPass):
    """
    Adds layout conversion nodes when an MKLDNN tensor is utilized by a
    non-MKL op
    """

    def __init__(self, mkldnn, **kwargs):
        super(MklAddLayoutConversions, self).__init__(**kwargs)
        self.mkldnn = mkldnn
        self.reorder_ops = dict()   # Maps op.name to reorder op

    def init_mkldnn_reorder(self, op):
        (mkl_layout, mkl_axes) = op.in_layout
        check_flatten = False
        mkl_flattend_axis = False
        # check if any one of axis in mkl_axes is Flattend
        for axis in mkl_axes:
            if isinstance(axis, FlattenedAxis):
                mkl_flattend_axis = True

        for axis_indx, each_axis in enumerate(op.axes):
            if isinstance(each_axis, FlattenedAxis) and not(mkl_axes[axis_indx].is_flattened) \
                    and not(mkl_flattend_axis):
                check_flatten = True

        if check_flatten:
            mkl_axes_order = get_order_from_axes(unflatten(op).axes, mkl_axes)
        else:
            mkl_axes_order = get_order_from_axes(op.axes, mkl_axes)
        (out_layout, _) = get_mkl_layout(self.mkldnn, op, mkl_axes_order, True)
        ndims = len(mkl_axes)
        if check_flatten:
            dims = get_size_mkl_order(unflatten(op).axes, mkl_axes_order)
        else:
            dims = get_size_mkl_order(op.axes, mkl_axes_order)
        op_id = len(self.mkldnn.kernels)
        self.mkldnn.kernels[op.name] = self.mkldnn.create_empty_kernel(op_id)
        self.mkldnn.reorder_kernel(
            self.mkldnn.mkldnn_engine,
            ndims, get_ctypes_arg(dims),
            self.mkldnn.datatype[op.dtype.type],
            mkl_layout, out_layout,
            self.mkldnn.kernels[op.name]
        )
        dbg_print_kernel(self.mkldnn, op, op_id)

    def get_reorder_op(self, op):
        if op.name in self.reorder_ops:
            return self.reorder_ops[op.name]
        else:
            mkl_layout = self.get_exop(op).output_decls[0].tensor_view_decl.mkl_layout
            assert mkl_layout == self.mkldnn.op_layouts[op.name]
            reorder_op = MklReorderOp(
                op, in_layout=mkl_layout, out_layout=None)
            self.reorder_ops[op.name] = reorder_op
            self.init_mkldnn_reorder(reorder_op)
            return reorder_op

    def get_exop(self, op):
        return self.op_accessor.computation_decl.get_exop(op)

    def get_arg_mkl_layout(self, op, arg):
        arg_idx = get_arg_output_idx(self.get_exop(op), self.get_exop(arg))
        return self.get_exop(arg).output_decls[arg_idx].tensor_view_decl.mkl_layout

    def is_mkl_pass_through(self, op):
        if isinstance(op, (Flatten, Unflatten, ReorderAxes, ContiguousOp)) \
            and self.get_exop(op).output_decls[0].tensor_view_decl.mkl_layout is not None:
            return True
        else:
            return False

    @generic_method(dispatch_base_type=Op)
    def visit(self, op, *args):
        # if op.name in self.mkldnn.kernels or op.name in self.mkldnn.op_layouts:
        if op.name in self.mkldnn.kernels or self.is_mkl_pass_through(op):
            # MKL Op or an MKL layout pass-through op
            return
        replace = False
        new_args = []
        for arg in args:
            mkl_layout = self.get_arg_mkl_layout(op, arg)
            if mkl_layout is not None:
                assert arg.name in self.mkldnn.op_layouts
                reorder_op = self.get_reorder_op(arg)
                new_args.append(reorder_op)
                replace = True
            else:
                new_args.append(arg)
        if replace:
            new_op = op.copy_with_new_args(new_args)
            self.replace_op(op, new_op)

    @visit.on_type(ContiguousOp)
    def visit(self, op, arg):
        mkl_layout = self.get_arg_mkl_layout(op, arg)
        if mkl_layout is not None:
            assert op.name in self.mkldnn.op_layouts
            # Expect downstream ops to handle MKL layout or insert explicit conversions
            self.replace_op(op, arg)
        elif isinstance(arg, MklReorderOp):
            # TODO(jbobba) - Can we eliminate ContiguousOp here?
            self.replace_op(op, arg)

    @visit.on_type(MapRolesOp)
    def visit(self, op, arg):
        pass

    @visit.on_type(MklReorderOp)
    def visit(self, op, arg):
        pass

    @visit.on_type(ComputationOp)
    def visit(self, op):
        # this version only runs with the op-graph transformer
        if isinstance(op.returns, Op) and op.returns.forwarded.name in self.mkldnn.op_layouts:
            reorder_op = self.get_reorder_op(op.returns.forwarded)
            op.returns = reorder_op
            op.add_control_dep(reorder_op)
        elif isinstance(op.returns, (collections.Sequence, OrderedSet)):
            returns = op.returns
            op.returns = []
            for orig_op in returns:
                if orig_op.forwarded.name in self.mkldnn.op_layouts:
                    reorder_op = self.get_reorder_op(orig_op.forwarded)
                    op.returns.append(reorder_op)
                    op.add_control_dep(reorder_op)
                else:
                    op.returns.append(orig_op)
        elif isinstance(op.returns, collections.Set):
            # TODO(jbobba): Verify this case
            returns = op.returns
            op.returns = OrderedSet()
            for orig_op in returns:
                if orig_op.forwarded.name in self.mkldnn.op_layouts:
                    reorder_op = self.get_reorder_op(orig_op.forwarded)
                    op.returns.add(reorder_op)
                    op.add_control_dep(reorder_op)
                else:
                    op.returns.add(orig_op)
        else:
            pass

    @visit.on_type(ReturnOp)
    def visit(self, op, *returns):
        # This version only runs with the exec-graph transformer
        return_exop = self.get_exop(op)
        for i, input_decl in enumerate(return_exop.input_decls):
            mkl_layout = input_decl.source_output_decl.tensor_view_decl.mkl_layout
            if mkl_layout is not None:
                input_op = input_decl.source_output_decl.exop.op
                input_exop = input_decl.source_output_decl.exop
                reorder_op = self.get_reorder_op(input_op)
                self.op_accessor.computation_decl.exop_block.add_ops([reorder_op], input_exop)
                reorder_exop = self.get_exop(reorder_op)
                return_exop.input_decls[i].source_output_decl = reorder_exop.output_decls[0]
                reorder_exop.output_decls[0].user_input_decls.add(return_exop.input_decls[i])
