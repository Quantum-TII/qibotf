import itertools
import numpy as np
import tensorflow as tf
from qibo.config import raise_error, EINSUM_CHARS
from qibo.base import hamiltonians


NUMERIC_TYPES = (np.int, np.float, np.complex,
                 np.int32, np.int64, np.float32,
                 np.float64, np.complex64, np.complex128)
ARRAY_TYPES = (tf.Tensor, np.ndarray)


class TensorflowHamiltonian(hamiltonians.Hamiltonian):
    """TensorFlow implementation of :class:`qibo.base.hamiltonians.Hamiltonian`."""
    NUMERIC_TYPES = NUMERIC_TYPES
    ARRAY_TYPES = ARRAY_TYPES
    K = tf

    def _calculate_exp(self, a):
        if self._eigenvectors is None:
            return tf.linalg.expm(-1j * a * self.matrix)
        else:
            expd = tf.linalg.diag(tf.exp(-1j * a * self._eigenvalues))
            ud = tf.transpose(tf.math.conj(self._eigenvectors))
            return tf.matmul(self._eigenvectors, tf.matmul(expd, ud))

    def expectation(self, state, normalize=False):
        statec = tf.math.conj(state)
        hstate = self @ state
        ev = tf.math.real(tf.reduce_sum(statec * hstate))
        if normalize:
            norm = tf.reduce_sum(tf.square(tf.abs(state)))
            return ev / norm
        return ev

    def _real(self, o):
        if isinstance(o, tf.Tensor):
            return np.array(o).real
        return super(TensorflowHamiltonian, self)._real(o)

    def __mul__(self, o):
        """Multiplication to scalar operator."""
        if isinstance(o, tf.Tensor):
            o = tf.cast(o, dtype=self.matrix.dtype)
        return super(TensorflowHamiltonian, self).__mul__(o)


class NumpyHamiltonian(TensorflowHamiltonian):
    """Numpy implementation of :class:`qibo.base.hamiltonians.Hamiltonian`."""
    import scipy
    K = np

    def _calculate_exp(self, a):
        if self._eigenvectors is None:
            return self.scipy.linalg.expm(-1j * a * self.matrix)
        else:
            expd = np.diag(np.exp(-1j * a * self._eigenvalues))
            ud = np.transpose(np.conj(self._eigenvectors))
            return self._eigenvectors @ (expd @ ud)

    def expectation(self, state, normalize=False):
        statec = np.conj(state)
        hstate = self @ state
        ev = np.sum(statec * hstate).real
        if normalize:
            return ev / (np.abs(state) ** 2).sum()
        return ev


class TensorflowTrotterHamiltonian(hamiltonians.TrotterHamiltonian):
    """TensorFlow implementation of :class:`qibo.base.hamiltonians.TrotterHamiltonian`."""

    def expectation(self, state, normalize=False):
        raise_error(NotImplementedError)

    def dense_hamiltonian(self):
        if 2 * self.nqubits > len(EINSUM_CHARS): # pragma: no cover
            # case not tested because it only happens in large examples
            raise_error(NotImplementedError, "Not enough einsum characters.")

        matrix = np.zeros(2 * self.nqubits * (2,), dtype=self.dtype)
        chars = EINSUM_CHARS[:2 * self.nqubits]
        for targets, term in self:
            tmat = term.matrix.reshape(2 * term.nqubits * (2,))
            n = self.nqubits - len(targets)
            emat = np.eye(2 ** n, dtype=self.dtype).reshape(2 * n * (2,))
            gen = lambda x: (chars[i + x] for i in targets)
            tc = "".join(itertools.chain(gen(0), gen(self.nqubits)))
            ec = "".join((c for c in chars if c not in tc))
            matrix += np.einsum(f"{tc},{ec}->{chars}", tmat, emat)

        matrix = matrix.reshape(2 * (2 ** self.nqubits,))
        return self.dense_class(self.nqubits, matrix)
