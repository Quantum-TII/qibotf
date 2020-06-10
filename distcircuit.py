# -*- coding: utf-8 -*-
# @authors: S. Efthymiou
import copy
import numpy as np
import tensorflow as tf
import joblib
from qibo.config import DTYPECPX, DTYPEINT
from qibo.tensorflow import circuit, measurements, callbacks
from qibo.tensorflow import custom_operators as op
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union


class TensorflowDistributedCircuit(circuit.TensorflowCircuit):
    """Implementation of :class:`qibo.base.circuit.BaseCircuit` in Tensorflow.

    Args:
        nqubits (int): Total number of qubits in the circuit.
        calc_devices (dict): Dictionary from device names to the number of
            times each device will be used.
            For example if ``calc_devices = {'/GPU:0': 2, '/GPU:1': 2}``
            then two distinct GPUs will be used twice each for a total of 4
            logical devices. The number of logical devices must be a power of 2.
        memory_device (str): Name of the device where the full state will be
            saved. This is usually the CPU.
        dtype: Tensorflow type for complex numbers.
            Read automatically from `config`.
    """

    def __init__(self,
                 nqubits: int,
                 calc_devices: Dict[str, int],
                 memory_device: str = "/CPU:0",
                 dtype=DTYPECPX):
        super(TensorflowDistributedCircuit, self).__init__(nqubits, dtype)
        self._init_kwargs.update({"calc_devices": calc_devices,
                                  "memory_device": memory_device})
        self.ndevices = sum(calc_devices.values())
        self.nglobal = np.log2(self.ndevices)
        if not self.nglobal.is_integer():
            raise ValueError("Number of calculation devices should be a power "
                             "of 2 but is {}.".format(self.ndevices))
        self.nglobal = int(self.nglobal)

        self.memory_device = memory_device
        self.calc_devices = calc_devices

        self.queues = {d: [] for d in self.calc_devices.keys()}
        self.global_qubits_list = []

        self.pieces = None
        self._global_qubits = None
        self._local_qubits = None

        n = self.nqubits - self.nglobal
        self.device_shape = tf.cast((self.ndevices, 2 ** n), dtype=DTYPEINT)
        self.full_shape = tf.cast((2 ** self.nqubits,), dtype=DTYPEINT)
        self.tensor_shape = self.nqubits * (2,)

        self.local_full_shape = tf.cast((2 ** n,), dtype=DTYPEINT)
        self.local_tensor_shape = n * (2,)

    @property
    def global_qubits(self) -> List[int]:
        if self._global_qubits is None:
            raise ValueError("Cannot access global qubits before being set.")
        return sorted(self._global_qubits)

    @global_qubits.setter
    def global_qubits(self, x: Sequence[int]):
        if len(x) != self.nglobal:
            raise ValueError("Invalid number of global qubits {} for using {} "
                             "calculation devices.".format(len(x), self.ndevices))
        self._global_qubits = set(x)
        self._local_qubits = [i for i in range(self.nqubits) if i not in self._global_qubits]

        self.transpose_order = list(sorted(self._global_qubits)) + self._local_qubits
        self.reverse_transpose_order = self.nqubits * [0]
        for i, v in enumerate(self.transpose_order):
            self.reverse_transpose_order[v] = i

    def _set_nqubits(self, gate):
        # Do not set ``gate.nqubits`` during gate addition because this will
        # be set by the ``set_gates`` method once all gates are known.
        pass

    def with_noise(self, noise_map, measurement_noise):
        raise NotImplementedError("Distributed circuit does not support "
                                  "density matrices yet.")

    def set_gates(self):
        if not self.queue:
            raise RuntimeError("No gates available to set for distributed run.")

        all_qubits = set(range(self.nqubits))
        queues = [[]]

        global_qubits = set(all_qubits)
        queue = iter(self.queue)
        try:
            gate = next(queue)
            while True:
                target_qubits = set(gate.target_qubits)
                global_qubits -= target_qubits
                while len(global_qubits) > self.nglobal:
                    queues[-1].append(gate)
                    gate = next(queue)
                    target_qubits = set(gate.target_qubits)
                    global_qubits -= target_qubits

                if len(global_qubits) == self.nglobal:
                    queues[-1].append(gate)
                    gate = next(queue)
                    while not set(gate.target_qubits) & global_qubits:
                        queues[-1].append(gate)
                        gate = next(queue)
                else:
                    # must be len(global_qubits) < self.nglobal
                    free_qubits = list(sorted(target_qubits))
                    global_qubits |= set(free_qubits[self.nglobal - len(global_qubits):])

                queues.append([])
                self.global_qubits_list.append(list(sorted(global_qubits)))
                global_qubits = set(all_qubits)

        except StopIteration:
            if len(global_qubits) > self.nglobal:
                global_qubits = list(sorted(global_qubits))[:self.nglobal]
            self.global_qubits_list.append(list(sorted(global_qubits)))

        # "Compile" actual gates
        nlocal = self.nqubits - self.nglobal
        for global_qubits, queue in zip(self.global_qubits_list, queues):
            for device in self.calc_devices.keys():
                self.queues[device].append([])

            for gate in queue:
                for device in self.calc_devices.keys():
                    # TODO: Move this copy functionality to `gates.py`
                    calc_gate = copy.copy(gate)
                    calc_gate.reduce(global_qubits)
                    calc_gate.original_gate = gate
                    # Gate matrix should be constructed in the calculation device
                    with tf.device(device):
                        calc_gate.nqubits = nlocal
                    self.queues[device][-1].append(calc_gate)

    def compile(self, callback: Optional[callbacks.Callback] = None):
        """Compiles the circuit as a Tensorflow graph.

        Args:
            callback: A Callback to calculate during circuit execution.
                See :class:`qibo.tensorflow.callbacks.Callback` for more details.
                User can give a single callback or list of callbacks here.
        """
        raise RuntimeError("Cannot compile circuit that uses custom operators.")

    def _device_execute(self, state: tf.Tensor, gates: List["TensorflowGate"]) -> tf.Tensor:
        for gate in gates:
            state = gate(state)
        return state

    # Old casting on CPU after runs finish. Not used because it leads to
    # GPU memory errors
    #def _cast_results(self, results: List[List[tf.Tensor]]):
    #    i = 0
    #    for result in results:
    #        for s in result:
    #            self.pieces[i].assign(s)
    #            i += 1

    def _joblib_config(self) -> Tuple[Iterable[int], str]:
        start = 0
        for device, n in self.calc_devices.items():
            stop = start + n
            yield range(start, stop), device
            start = stop

    def _joblib_execute(self, group: int):
        def _device_job(ids, device):
            for i in ids:
                with tf.device(device):
                    state = self._device_execute(
                        self.pieces[i], self.queues[device][group])
                    self.pieces[i].assign(state)
                    del(state)

        pool = joblib.Parallel(n_jobs=len(self.calc_devices),
                               prefer="threads")
        pool(joblib.delayed(_device_job)(ids, device)
             for ids, device in self._joblib_config())

    # Sequential execution without `joblib` (not used)
    #def _sequential_execute(self, group):
    #    i = 0
    #    for device in self.calc_devices.keys():
    #        for _ in range(self.calc_devices[device]):
    #            with tf.device(device):
    #                result = self._device_execute(self.pieces[i], self.queues[device][group])
    #            self.pieces[i].assign(result)
    #            i += 1

    @property
    def using_tfgates(self) -> bool:
        """Determines if we are using Tensorflow native or custom gates."""
        from qibo.tensorflow import gates
        return gates.TensorflowGate == self.gate_module.TensorflowGate

    def execute(self,
                initial_state: Optional[Union[np.ndarray, tf.Tensor]] = None,
                nshots: Optional[int] = None,
                callback: Optional[callbacks.Callback] = None
                ) -> Union[tf.Tensor, measurements.CircuitResult]:
        """Propagates the state through the circuit applying the corresponding gates.

        In default usage the full final state vector or density matrix is returned.
        If the circuit contains measurement gates and `nshots` is given, then
        the final state is sampled and the samples are returned.

        Args:
            initial_state (np.ndarray): Initial state vector as a numpy array of shape ``(2 ** nqubits,)``
                or a density matrix of shape ``(2 ** nqubits, 2 ** nqubits)``.
                A Tensorflow tensor with shape ``nqubits * (2,)`` (or ``2 * nqubits * (2,)`` for density matrices)
                is also allowed as an initial state but must have the `dtype` of the circuit.
                If ``initial_state`` is ``None`` the |000...0> state will be used.
            nshots (int): Number of shots to sample if the circuit contains
                measurement gates.
                If ``nshots`` None the measurement gates will be ignored.
            callback: A Callback to calculate during circuit execution.
                See :class:`qibo.tensorflow.callbacks.Callback` for more details.
                User can give a single callback or list of callbacks here.
                Note that if the Circuit is compiled then all callbacks should
                be passed when ``compile`` is called, not during execution.
                Otherwise an ``RuntimeError`` will be raised.

        Returns:
            If ``nshots`` is given and the circuit contains measurements
                A :class:`qibo.base.measurements.CircuitResult` object that contains the measured bitstrings.
            If ``nshots`` is ``None`` or the circuit does not contain measurements.
                The final state vector as a Tensorflow tensor of shape ``(2 ** nqubits,)`` or a density matrix of shape ``(2 ** nqubits, 2 ** nqubits)``.
        """
        if not self.global_qubits_list:
            self.set_gates()
        self.global_qubits = self.global_qubits_list[0]
        self._cast_initial_state(initial_state)

        #self._add_callbacks(callback)
        for group, global_qubits in enumerate(self.global_qubits_list):
            if group > 0:
                self._swap(global_qubits)
            #self._sequential_execute(group)
            self._joblib_execute(group)

        # Append callback results to callbacks
        #for callback, result in zip(self.callbacks, callback_results):
        #    callback.append(result)

        if self.measurement_gate is None or nshots is None:
            return self.final_state

        raise NotImplementedError("Measurements are not implemented for "
                                  "distributed circuits.")
        #samples = self.measurement_gate(state, nshots, samples_only=True,
        #                                is_density_matrix=self.using_density_matrix)
        #self._final_state = state

        #self.measurement_gate_result = measurements.GateResult(
        #    self.measurement_gate.qubits, state, decimal_samples=samples)
        #return measurements.CircuitResult(
        #    self.measurement_tuples, self.measurement_gate_result)

    @property
    def final_state(self) -> tf.Tensor:
        """Final state as a Tensorflow tensor of shape (2 ** nqubits,).

        The circuit has to be executed at least once before accessing this
        property, otherwise a `ValueError` is raised. If the circuit is
        executed more than once, only the last final state is returned.
        """
        if self.pieces is None:
            raise ValueError("Cannot access the state tensor before being set.")
        return self._merge(self.pieces)

    def _default_global_qubits(self) -> List[int]:
        """Returns a list with the last qubits to cast them as global."""
        return list(range(self.nqubits - self.nglobal, self.nqubits))

    def _default_initial_piece(self) -> tf.Tensor:
        """Returns the 0th piece for the |000...0> state."""
        zeros = tf.zeros(2 ** (self.nqubits - self.nglobal), dtype=self.dtype)
        return op.initial_state(zeros)

    def _create_pieces(self):
        n = 2 ** (self.nqubits - self.nglobal)
        with tf.device(self.memory_device):
            self.pieces = [tf.Variable(tf.zeros(n, dtype=self.dtype))
                           for _ in range(self.ndevices)]

    def _default_initial_state(self) -> tf.Tensor:
        """Creates the |000...0> state for default initialization."""
        self._create_pieces()
        with tf.device(self.memory_device):
            self.pieces[0].assign(self._default_initial_piece())

    def _cast_initial_state(self, initial_state: Optional[Union[np.ndarray, tf.Tensor]] = None) -> tf.Tensor:
        """Checks and casts initial state given by user."""
        if self.pieces is not None:
            raise RuntimeError("Attempting to initialize distributed circuit "
                               "state that is already initialized.")

        if self._global_qubits is None:
            self.global_qubits = self._default_global_qubits()

        self.buffer = tf.zeros(self.full_shape, dtype=self.dtype)
        if initial_state is None:
            return self._default_initial_state()

        state = super(TensorflowDistributedCircuit, self)._cast_initial_state(initial_state)
        self._create_pieces()
        self._split(state)

    def _add_callbacks(self, callback: callbacks.Callback):
        """Adds callbacks in the circuit."""
        raise NotImplementedError("Callbacks are not implemented for "
                                  "distributed circuits.")
        #n = len(self.callbacks)
        #if isinstance(callback, list):
        #    self.callbacks += callback
        #elif isinstance(callback, callbacks.Callback):
        #    self.callbacks.append(callback)
        # Set number of qubits in new callbacks
        #for cb in self.callbacks[n:]:
        #    cb.nqubits = self.nqubits

    def _split(self, state: tf.Tensor):
        with tf.device(self.memory_device):
            state = op.transpose_state(state, self.nqubits, self.transpose_order)
            state = tf.reshape(state, self.device_shape)
            for i in range(self.ndevices):
                self.pieces[i].assign(state[i])

    def _merge(self, states: List[tf.Tensor]) -> tf.Tensor:
        with tf.device(self.memory_device):
            state = tf.concat([s[tf.newaxis] for s in states], axis=0)
            state = op.transpose_state(state, self.nqubits, self.reverse_transpose_order)
            return tf.reshape(state, self.full_shape)

    def _swap(self, new_global_qubits: Sequence[int]):
        with tf.device(self.memory_device):
            state = tf.concat([s[tf.newaxis] for s in self.pieces], axis=0)

            order = list(self.reverse_transpose_order)
            self.global_qubits = new_global_qubits
            order = [order[v] for v in self.transpose_order]

            self.buffer = op.transpose_state(state, self.nqubits, order, self.buffer)
            self.buffer = tf.reshape(self.buffer, self.device_shape)
            for i in range(self.ndevices):
                self.pieces[i].assign(self.buffer[i])
