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

import numpy as np
from builtins import range
import math

from neon.backends.layer_cpu import ConvLayer

from geon.op_graph.op_graph import AllocationOp
from geon.op_graph import arrayaxes
from geon.transformers.base import Transformer


class proxy_backend(object):
    """ a fake neon backend to make ConvLayer not raise an exception. """
    # TODO: refactor away

    def check_caffe_compat(self):
        """ no caffe compat for now """
        return False

    def output_dim(self, X, S, padding, strides, pooling=False):
        """
        Compute along 1 dimension, with these sizes, what will be the output dimension.

        Arguments:
            X (int): input data dimension
            S (int): filter dimension
            padding (int): padding on each side
            strides (int): striding
            pooling (bool): flag for setting pooling layer size
        """
        if X < S:
            raise ValueError((
                'filter dimension {S} can not be large than input data '
                'dimension {X}'
            ).format(S=S, X=X))

        if self.check_caffe_compat() and pooling:
            size = int(math.ceil((float(X - S + 2 * padding) / strides))) + 1
            if padding > 0 and (size - 1) * strides >= X + padding:
                # decrement size if last pooling op is completely in padding
                size -= 1
        else:
            # normal neon output size determination
            size = ((X - S + 2 * padding) // strides) + 1

        if pooling and padding >= S:
            raise ValueError("Padding dim %d incompatible with filter size %d" % (padding, S))

        return size


class proxy_tensor(object):
    """ A fake CPUTensor to make old neon implementation of ConvLayer happy """
    # TODO: refactor away
    def __init__(self, tensor):
        self._tensor = tensor


class NumPyTransformer(Transformer):
    """
    Transformer for executing graphs on a CPU, backed by numpy.

    Given a list of ops you want to compute the results of, this transformer
    will compile the graph required to compute those results and exposes an
    evaluate method to execute the compiled graph.
    """

    # allocators
    def make_raw_buffer(self, size):
        """
        TODO.

        Arguments:
          size: TODO

        Returns:
          TODO
        """
        return bytearray(size)

    def tensor_view(self, tensor_description):
        """
        TODO.

        Arguments:
          tensor_description: TODO

        Returns:
          TODO
        """
        return np.ndarray(
            shape=tensor_description.shape,
            dtype=tensor_description.dtype,
            buffer=tensor_description.buffer.data,
            offset=tensor_description.offset,
            strides=tensor_description.strides
        )

    def nparray(self, array):
        """
        TODO.

        Arguments:
          array: TODO

        Returns:
          TODO
        """
        return array

    def rng(self, seed=None):
        """
        TODO.

        Arguments:
          seed: TODO

        Returns:
          TODO
        """
        return np.random.RandomState(seed=seed)

    def rng_normal_tensor(self, rng, tensor_description, loc, scale):
        """
        TODO.

        Arguments:
          rng: TODO
          tensor_description: TODO
          loc: TODO
          scale: TODO

        Returns:
          TODO
        """
        return rng.normal(
            loc, scale, tensor_description.sizes).astype(
            tensor_description.dtype)

    def rng_uniform_tensor(self, rng, tensor_description, low, high):
        """
        TODO.

        Arguments:
          rng: TODO
          tensor_description: TODO
          low: TODO
          high: TODO

        Returns:
          TODO
        """
        return rng.uniform(
            low, high, tensor_description.sizes).astype(
            tensor_description.dtype)

    # Side-effects
    def fill(self, out, value):
        """
        TODO.

        Arguments:
          out: TODO
          value: TODO
        """
        out.fill(value)

    def set_item(self, tensor, item, value):
        """
        TODO.

        Arguments:
          tensor: TODO
          item: TODO
          value: TODO
        """
        tensor.__setitem__(item, value)

    # Operations
    def absolute(self, x, out):
        """
        TODO.

        Arguments:
          x: TODO
          out: TODO
        """
        np.abs(x, out=out)

    def add(self, x, y, out):
        """
        TODO.

        Arguments:
          x: TODO
          y: TODO
          out: TODO
        """
        np.add(x, y, out=out)

    def argmax(self, x, out):
        """
        TODO.

        Arguments:
          x: TODO
          out: TODO
        """
        np.ndarray.argmax(x, 0, out)

    def argmin(self, x, out):
        """
        TODO.

        Arguments:
          x: TODO
          out: TODO
        """
        np.ndarray.argmin(x, 0, out)

    def cos(self, x, out):
        """
        TODO.

        Arguments:
          x: TODO
          out: TODO
        """
        np.cos(x, out=out)

    def divide(self, x, y, out):
        """
        TODO.

        Arguments:
          x: TODO
          y: TODO
          out: TODO
        """
        np.divide(x, y, out=out)

    def dot(self, x, y, out):
        """
        TODO.

        Arguments:
          x: TODO
          y: TODO
          out: TODO
        """
        if not out.flags.c_contiguous:
            t = x
            x = y.T
            y = t.T
            out = out.T
        np.dot(x, y, out)

    def equal(self, x, y, out):
        """
        TODO.

        Arguments:
          x: TODO
          y: TODO
          out: TODO
        """
        np.equal(x, y, out=out)

    def exp(self, x, out):
        """
        TODO.

        Arguments:
          x: TODO
          out: TODO
        """
        np.exp(x, out=out)

    def greater(self, x, y, out):
        """
        TODO.

        Arguments:
          x: TODO
          y: TODO
          out: TODO
        """
        np.greater(x, y, out=out)

    def greater_equal(self, x, y, out):
        """
        TODO.

        Arguments:
          x: TODO
          y: TODO
          out: TODO
        """
        np.greater_equal(x, y, out=out)

    def less(self, x, y, out):
        """
        TODO.

        Arguments:
          x: TODO
          y: TODO
          out: TODO
        """
        np.less(x, y, out=out)

    def less_equal(self, x, y, out):
        """
        TODO.

        Arguments:
          x: TODO
          y: TODO
          out: TODO
        """
        np.less_equal(x, y, out=out)

    def log(self, x, out):
        """
        TODO.

        Arguments:
          x: TODO
          out: TODO
        """
        np.log(x, out=out)

    def max(self, x, axis, out):
        """
        TODO.

        Arguments:
          x: TODO
          axis: TODO
          out: TODO
        """
        np.max(x, axis, out=out)

    def maximum(self, x, y, out):
        """
        TODO.

        Arguments:
          x: TODO
          y: TODO
          out: TODO

        Returns:

        """
        np.maximum(x, y, out=out)

    def min(self, x, axis, out):
        """
        TODO.

        Arguments:
          x: TODO
          axis: TODO
          out: TODO

        Returns:

        """
        np.min(x, axis, out=out)

    def minimum(self, x, y, out):
        """
        TODO.

        Arguments:
          x: TODO
          y: TODO
          out: TODO

        Returns:

        """
        np.minimum(x, y, out=out)

    def multiply(self, x, y, out):
        """
        TODO.

        Arguments:
          x: TODO
          y: TODO
          out: TODO

        Returns:

        """
        np.multiply(x, y, out=out)

    def negative(self, x, out):
        """
        TODO.

        Arguments:
          x: TODO
          out: TODO

        Returns:

        """
        np.negative(x, out=out)

    def not_equal(self, x, y, out):
        """
        TODO.

        Arguments:
          x: TODO
          y: TODO
          out: TODO

        Returns:

        """
        np.not_equal(x, y, out=out)

    def onehot(self, x, out):
        """
        TODO.

        Arguments:
          x: TODO
          out: TODO

        Returns:

        """
        out[:] = 0
        for i in range(len(x)):
            out[x[i], i] = 1

    def reciprocal(self, x, out):
        """
        TODO.

        Arguments:
          x: TODO
          out: TODO

        Returns:

        """
        np.reciprocal(x, out=out)

    def sign(self, x, out):
        """
        TODO.

        Arguments:
          x: TODO
          out: TODO

        Returns:

        """
        np.sign(x, out=out)

    def sin(self, x, out):
        """
        TODO.

        Arguments:
          x: TODO
          out: TODO

        Returns:

        """
        np.sin(x, out=out)

    def sqrt(self, x, out):
        """
        TODO.

        Arguments:
          x: TODO
          out: TODO

        Returns:

        """
        np.sqrt(x, out=out)

    def square(self, x, out):
        """
        TODO.

        Arguments:
          x: TODO
          out: TODO

        Returns:

        """
        np.square(x, out=out)

    def subtract(self, x, y, out):
        """
        TODO.

        Arguments:
          x: TODO
          y: TODO
          out: TODO

        Returns:

        """
        np.subtract(x, y, out=out)

    def sum(self, x, axis, out):
        """
        TODO.

        Arguments:
          x: TODO
          axis: TODO
          out: TODO

        Returns:

        """
        np.sum(x, axis=axis, out=out)

    def tanh(self, x, out):
        """
        TODO.

        Arguments:
          x: TODO
          out: TODO

        Returns:

        """
        np.tanh(x, out=out)

    def conv(self, input, filter, output, input_shape, filter_shape, padding, strides):
        # TODO: change args to ConvLayer to meaningful names instead of random
        # upper case letters.

        # TODO: only create ConvLayer once per op per session so that things
        # like autotune only need to be run once per session.

        # TODO: fork ConvLayer and refactor into here/conv op.

        neon_conv_layer = ConvLayer(
            proxy_backend(), output.dtype,
            N=arrayaxes.get_batch_axis().length,
            C=input_shape[0],
            D=input_shape[1],
            H=input_shape[2],
            W=input_shape[3],

            K=filter_shape[0],
            T=filter_shape[1],
            R=filter_shape[2],
            S=filter_shape[3],

            pad_d=padding[0], pad_h=padding[1], pad_w=padding[2],
            str_d=strides[0], str_h=strides[1], str_w=strides[2],
        )

        # neon_conv_layer...
        neon_conv_layer.xprop_conv(
            proxy_tensor(input),
            proxy_tensor(filter),
            proxy_tensor(output),
        )


class NPNormal(AllocationOp):
    """TODO."""

    def __init__(self, rng, loc, scale, **kwargs):
        super(NPNormal, self).__init__(args=(rng,), **kwargs)
        self.loc = loc
        self.scale = scale

    def compute_tensor_axes_info(self):
        """TODO."""
        rng, = self.args
        tensor_axes_info = super(NPNormal, self).compute_tensor_axes_info()
        tensor_axes_info.alloc = lambda evaluator, tensor_description: evaluator.rng_normal_tensor(
            rng, tensor_description, self.loc, self.scale)


class NPUniform(AllocationOp):
    """TODO."""

    def __init__(self, rng, low, high, **kwargs):
        super(NPUniform, self).__init__(args=(rng,), **kwargs)
        self.low = low
        self.high = high

    def compute_tensor_axes_info(self):
        """TODO."""
        rng, = self.args
        tensor_axes_info = super(NPUniform, self).compute_tensor_axes_info()
        tensor_axes_info.alloc = lambda evaluator, tensor_description: \
            evaluator.rng_uniform_tensor(rng, tensor_description, self.low, self.high)
