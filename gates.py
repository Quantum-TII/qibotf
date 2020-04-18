# -*- coding: utf-8 -*-
# @authors: S. Efthymiou
import numpy as np
import tensorflow as tf
from qibo.base import gates as base_gates
from qibo.config import einsum, matrices, DTYPEINT, DTYPE, GPU_MEASUREMENT_CUTOFF, CPU_NAME
from typing import List, Optional, Sequence, Tuple


class _ControlCache:
    """Helper tools for `controlled_by` gates.

    This class contains:
      A) an `order` that is used to transpose `state`
         so that control legs are moved in the front
      B) a `targets` list which is equivalent to the
         `target_qubits` tuple but each index is reduced
         by the amount of control qubits that preceed it.
    This method is called by the `nqubits` setter so that the loop runs
    once per gate (and not every time the gate is called).
    """

    def __init__(self, gate: base_gates.Gate):
        self.ncontrol = len(gate.control_qubits)
        self._order, self.targets = self.calculate(gate)
        # Calculate the reverse order for transposing the state legs so that
        # control qubits are back to their original positions
        self._reverse = self.revert(self._order)

        self._order_dm = None
        self._reverse_dm = None

    def order(self, is_density_matrix: bool = False):
        if not is_density_matrix:
            return self._order

        if self._order_dm is None:
            self.calculate_dm()
        return self._order_dm

    def reverse(self, is_density_matrix: bool = False):
        if not is_density_matrix:
            return self._reverse

        if self._reverse_dm is None:
            self.calculate_dm()
        return self._reverse_dm

    @staticmethod
    def calculate(gate: base_gates.Gate):
        loop_start = 0
        order = list(gate.control_qubits)
        targets = list(gate.target_qubits)
        for control in gate.control_qubits:
            for i in range(loop_start, control):
                order.append(i)
            loop_start = control + 1

            for i, t in enumerate(gate.target_qubits):
                if t > control:
                    targets[i] -= 1
        for i in range(loop_start, gate.nqubits):
            order.append(i)

        return order, targets

    def calculate_dm(self):
        additional_order = np.array(self._order) + len(self._order)
        self._order_dm = (self._order[:self.ncontrol] +
                          list(additional_order[:self.ncontrol]) +
                          self._order[self.ncontrol:] +
                          list(additional_order[self.ncontrol:]))
        self._reverse_dm = self.revert(self._order_dm)

    @staticmethod
    def revert(transpose_order) -> List[int]:
        reverse_order = len(transpose_order) * [0]
        for i, r in enumerate(transpose_order):
            reverse_order[r] = i
        return reverse_order


class TensorflowGate(base_gates.Gate):
    """The base Tensorflow gate.

    **Properties:**
        matrix: The matrix that represents the gate to be applied.
            This is (2, 2) for 1-qubit gates and (4, 4) for 2-qubit gates.
        qubits: List with the qubits that the gate is applied to.
    """

    dtype = matrices.dtype
    einsum = einsum

    def __init__(self):
        self.calculation_cache = None
        # For `controlled_by` gates (see `_ControlCache` for more details)
        self.control_cache = None
        # Gate matrices
        self.matrix = None
        self._matrix_dagger = None # TODO: Remove this if it is not needed

    def with_backend(self, einsum_choice: str) -> "TensorflowGate":
        """Uses a different einsum backend than the one defined in config.

        Useful for testing.

        Args:
            einsum_choice: Which einsum backend to use.
                One of `DefaultEinsum` or `MatmulEinsum`.

        Returns:
            The gate object with the calculation backend switched to the
            selection.
        """
        from qibo.tensorflow import einsum
        self.einsum = getattr(einsum, einsum_choice)()
        return self

    @base_gates.Gate.nqubits.setter
    def nqubits(self, n: int):
        """Sets the number of qubit that this gate acts on.

        This is called automatically by the `Circuit.add` method if the gate
        is used on a `Circuit`. If the gate is called on a state then `nqubits`
        is set during the first `__call__`.
        When `nqubits` is set we also calculate the einsum string so that it
        is calculated only once per gate.
        """
        base_gates.Gate.nqubits.fset(self, n)
        if self.is_controlled_by:
            self.control_cache = _ControlCache(self)
            nactive = n - len(self.control_qubits)
            targets = self.control_cache.targets
            self.calculation_cache = self.einsum.create_cache(targets, nactive)
        else:
            self.calculation_cache = self.einsum.create_cache(self.qubits, n)

    @property
    def matrix_dagger(self):
        # TODO: Remove this if it is not needed for `MatmulEinsum`.
        if self._matrix_dagger is not None:
            return self._matrix_dagger

        n = len(tuple(self.matrix.shape)) // 2
        ids = tuple(range(n, 2 * n)) + tuple(range(n))
        self._matrix_dagger = tf.math.conj(tf.transpose(self.matrix, ids))
        return self._matrix_dagger

    def __call__(self, state: tf.Tensor, is_density_matrix: bool = False
                 ) -> tf.Tensor:
        """Implements the `Gate` on a given state."""
        if self._nqubits is None:
            if is_density_matrix:
                self.nqubits = len(tuple(state.shape)) // 2
            else:
                self.nqubits = len(tuple(state.shape))

        if self.is_controlled_by:
            return self._controlled_by_call(state, is_density_matrix)

        if is_density_matrix:
            cache = self.calculation_cache.density_matrix()
            state = self.einsum(cache["left"], state, self.matrix)
            return self.einsum(cache["right"], state, tf.math.conj(self.matrix))

        return self.einsum(self.calculation_cache.vector, state, self.matrix)

    def _controlled_by_call(self, state: tf.Tensor,
                            is_density_matrix: bool = False) -> tf.Tensor:
        """Gate __call__ method for `controlled_by` gates."""
        ncontrol = len(self.control_qubits)
        nactive = self.nqubits - ncontrol

        transpose_order = self.control_cache.order(is_density_matrix)
        reverse_transpose_order = self.control_cache.reverse(is_density_matrix)

        state = tf.transpose(state, transpose_order)
        if is_density_matrix:
            cache = self.calculation_cache.density_matrix(is_controlled_by=True)
            state = tf.reshape(state, 2 * (2 ** ncontrol,) + 2 * nactive * (2,))

            #shape = ((2 ** ncontrol - 1) ** 2,) + 2 * nactive * (2,)
            #updates00 = tf.reshape(state[:-1, :-1], shape)

            updates01 = self.einsum(cache["right0"], state[:-1, -1],
                                    tf.math.conj(self.matrix))
            updates10 = self.einsum(cache["left0"], state[-1, :-1],
                                    self.matrix)

            updates11 = self.einsum(cache["left"], state[-1, -1], self.matrix)
            updates11 = self.einsum(cache["right"], updates11,
                                    tf.math.conj(self.matrix))

            updates01 = tf.concat([state[:-1, :-1], updates01[:, tf.newaxis]], axis=1)
            updates10 = tf.concat([updates10, updates11[tf.newaxis]], axis=0)
            state = tf.concat([updates01, updates10[tf.newaxis]], axis=0)
            state = tf.reshape(state, 2 * self.nqubits * (2,))

        else:
            # Apply `einsum` only to the part of the state where all controls
            # are active. This should be `state[-1]`
            state = tf.reshape(state, (2 ** ncontrol,) + nactive * (2,))
            updates = self.einsum(self.calculation_cache.vector, state[-1],
                                  self.matrix)
            # Concatenate the updated part of the state `updates` with the
            # part of of the state that remained unaffected `state[:-1]`.
            state = tf.concat([state[:-1], updates[tf.newaxis]], axis=0)
            state = tf.reshape(state, self.nqubits * (2,))

        return tf.transpose(state, reverse_transpose_order)


class H(TensorflowGate, base_gates.H):

    def __init__(self, q):
        base_gates.H.__init__(self, q)
        TensorflowGate.__init__(self)
        self.matrix = matrices.H


class X(TensorflowGate, base_gates.X):

    def __init__(self, q):
        base_gates.X.__init__(self, q)
        TensorflowGate.__init__(self)
        self.matrix = matrices.X

    def controlled_by(self, *q):
        """Fall back to CNOT and Toffoli if controls are one or two."""
        if len(q) == 1:
            gate = CNOT(q[0], self.target_qubits[0])
        elif len(q) == 2:
            gate = TOFFOLI(q[0], q[1], self.target_qubits[0])
        else:
            gate = super(X, self).controlled_by(*q)

        gate.einsum = self.einsum
        return gate


class Y(TensorflowGate, base_gates.Y):

    def __init__(self, q):
        base_gates.Y.__init__(self, q)
        TensorflowGate.__init__(self)
        self.matrix = matrices.Y


class Z(TensorflowGate, base_gates.Z):

    def __init__(self, q):
        base_gates.Z.__init__(self, q)
        TensorflowGate.__init__(self)
        self.matrix = matrices.Z


class M(TensorflowGate, base_gates.M):
    from qibo.tensorflow import measurements

    def __init__(self, *q, register_name: Optional[str] = None):
        base_gates.M.__init__(self, *q, register_name=register_name)
        TensorflowGate.__init__(self)
        self._traceout = None

    @base_gates.Gate.nqubits.setter
    def nqubits(self, n: int):
        base_gates.Gate.nqubits.fset(self, n)

    @property
    def _traceout_str(self):
        """Einsum string used to trace out when state is density matrix."""
        if self._traceout is None:
            from qibo.tensorflow.einsum import DefaultEinsum
            qubits = set(self.unmeasured_qubits)
            self._traceout = DefaultEinsum.partialtrace_str(
              qubits, self.nqubits, measuring=True)
        return self._traceout

    def _calculate_probabilities(self, state: tf.Tensor,
                                 is_density_matrix: bool = False) -> tf.Tensor:
        """Calculates probabilities from state using Born's rule.

        Args:
            state: State vector of shape nqubits * (2,) or density matrix of
                shape 2 * nqubits * (2,).
            is_density_matrix: Flag that specifies whether `state` is a state
                vector or density matrix.

        Returns:
            Probabilities for measured qubits with shape len(target_qubits)* (2,).
        """
        # Trace out unmeasured qubits
        if is_density_matrix:
            print(self._traceout_str)
            probs = tf.cast(tf.einsum(self._traceout_str, state),
                            dtype=DTYPE)
        else:
            probs = tf.reduce_sum(tf.square(tf.abs(state)),
                                  axis=self.unmeasured_qubits)
        # Bring probs in the order specified by the user
        return tf.transpose(probs, perm=self.reduced_target_qubits)

    def __call__(self, state: tf.Tensor, nshots: int,
                 samples_only: bool = False,
                 is_density_matrix: bool = False) -> tf.Tensor:
        if self._nqubits is None:
            self.nqubits = len(tuple(state.shape)) // (1 + int(is_density_matrix))

        probs_dim = 2 ** len(self.target_qubits)
        probs = self._calculate_probabilities(state, is_density_matrix)
        logits = tf.math.log(tf.reshape(probs, (probs_dim,)))

        if nshots * probs_dim < GPU_MEASUREMENT_CUTOFF:
            # Use default device to perform sampling
            samples_dec = tf.random.categorical(logits[tf.newaxis], nshots,
                                                dtype=DTYPEINT)[0]
        else:
            # Force using CPU to perform sampling because if GPU is used
            # it will cause a `ResourceExhaustedError`
            if CPU_NAME is None:
                raise RuntimeError("Cannot find CPU device to use for sampling.")
            with tf.device(CPU_NAME):
                samples_dec = tf.random.categorical(logits[tf.newaxis], nshots,
                                                    dtype=DTYPEINT)[0]
        if samples_only:
            return samples_dec
        return self.measurements.GateResult(
            self.qubits, state, decimal_samples=samples_dec)


class RX(TensorflowGate, base_gates.RX):

    def __init__(self, q, theta):
        base_gates.RX.__init__(self, q, theta)
        TensorflowGate.__init__(self)

        theta = tf.cast(self.theta, dtype=self.dtype)
        phase = tf.exp(1j * np.pi * theta / 2.0)
        cos = tf.cast(tf.math.real(phase), dtype=self.dtype)
        sin = tf.cast(tf.math.imag(phase), dtype=self.dtype)
        self.matrix = phase * (cos * matrices.I - 1j * sin * matrices.X)


class RY(TensorflowGate, base_gates.RY):

    def __init__(self, q, theta):
        base_gates.RY.__init__(self, q, theta)
        TensorflowGate.__init__(self)

        theta = tf.cast(self.theta, dtype=self.dtype)
        phase = tf.exp(1j * np.pi * theta / 2.0)
        cos = tf.cast(tf.math.real(phase), dtype=self.dtype)
        sin = tf.cast(tf.math.imag(phase), dtype=self.dtype)
        self.matrix = phase * (cos * matrices.I - 1j * sin * matrices.Y)


class RZ(TensorflowGate, base_gates.RZ):

    def __init__(self, q, theta):
        base_gates.RZ.__init__(self, q, theta)
        TensorflowGate.__init__(self)

        theta = tf.cast(self.theta, dtype=self.dtype)
        phase = tf.exp(1j * np.pi * theta)
        rz = tf.eye(2, dtype=self.dtype)
        self.matrix = tf.tensor_scatter_nd_update(rz, [[1, 1]], [phase])

    def controlled_by(self, *q):
        """Fall back to CRZ if control is one."""
        gate = super(RZ, self).controlled_by(*q)
        if len(q) == 1:
            return CRZ(q[0], self.target_qubits[0], self.theta)
        return gate


class CNOT(TensorflowGate, base_gates.CNOT):

    def __init__(self, q0, q1):
        base_gates.CNOT.__init__(self, q0, q1)
        TensorflowGate.__init__(self)
        self.matrix = matrices.CNOT


class CRZ(TensorflowGate, base_gates.CRZ):

    def __init__(self, q0, q1, theta):
        base_gates.CRZ.__init__(self, q0, q1, theta)
        TensorflowGate.__init__(self)

        theta = tf.cast(self.theta, dtype=self.dtype)
        phase = tf.exp(1j * np.pi * theta)
        crz = tf.eye(4, dtype=self.dtype)
        crz = tf.tensor_scatter_nd_update(crz, [[3, 3]], [phase])
        self.matrix = tf.reshape(crz, 4 * (2,))


class SWAP(TensorflowGate, base_gates.SWAP):

    def __init__(self, q0, q1):
        base_gates.SWAP.__init__(self, q0, q1)
        TensorflowGate.__init__(self)
        self.matrix = matrices.SWAP


class TOFFOLI(TensorflowGate, base_gates.TOFFOLI):

    def __init__(self, q0, q1, q2):
        base_gates.TOFFOLI.__init__(self, q0, q1, q2)
        TensorflowGate.__init__(self)
        self.matrix = matrices.TOFFOLI


class Unitary(TensorflowGate, base_gates.Unitary):

    def __init__(self, unitary, *q, name: Optional[str] = None):
        base_gates.Unitary.__init__(self, unitary, *q, name=name)
        TensorflowGate.__init__(self)

        rank = 2 * len(self.target_qubits)
        # This reshape will raise an error if the number of target qubits
        # given is incompatible to the shape of the given unitary.
        self.matrix = tf.convert_to_tensor(self.unitary, dtype=self.dtype)
        self.matrix = tf.reshape(self.matrix, rank * (2,))


class NoiseChannel(TensorflowGate, base_gates.NoiseChannel):

    def __init__(self, q: int, px: float = 0, py: float = 0, pz: float = 0):
        base_gates.NoiseChannel.__init__(self, q, px, py, pz)
        TensorflowGate.__init__(self)

        self.gates = []
        for p, cl in zip(self.p, (X, Y, Z)):
            if p > 0:
                gate = cl(q)
                if self._nqubits is not None:
                    gate.nqubits = self.nqubits
                self.gates.append((p, gate))

    def with_backend(self, einsum_choice: str) -> "NoiseChannel":
        TensorflowGate.with_backend(self, einsum_choice)
        for _, gate in self.gates:
            gate.einsum = self.einsum
        return self

    def __call__(self, state: tf.Tensor, is_density_matrix: bool = True
                 ) -> tf.Tensor:
        if not is_density_matrix:
            raise ValueError("Noise channel can only be applied to density "
                             "matrices.")
        if self._nqubits is None:
            self.nqubits = len(tuple(state.shape)) // 2

        new_state = tf.zeros_like(state)
        for p, gate in self.gates:
            new_state += p * gate(state, is_density_matrix=True)
        return (1 - self.total_p) * state + new_state


class Flatten(TensorflowGate, base_gates.Flatten):

    def __init__(self, coefficients):
        base_gates.Flatten.__init__(self, coefficients)

    def __call__(self, state: tf.Tensor, is_density_matrix: bool = False
                 ) -> tf.Tensor:
        if self.nqubits is None:
            if is_density_matrix:
                self.nqubits = len(tuple(state.shape)) // 2
            else:
                self.nqubits = len(tuple(state.shape))

        if is_density_matrix:
            shape = 2 * self.nqubits * (2,)
        else:
            shape = self.nqubits * (2,)

        _state = np.array(self.coefficients).reshape(shape)
        return tf.convert_to_tensor(_state, dtype=state.dtype)
