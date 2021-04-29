"""TensorFlow custom operator for Tensor initial state."""
import logging
log = logging.getLogger(__name__)

_custom_operators_loaded = False
try:
    import tensorflow as tf
    try:
        from qibotf.custom_operators.python.ops.qibo_tf_custom_operators import initial_state
        from qibotf.custom_operators.python.ops.qibo_tf_custom_operators import transpose_state
        from qibotf.custom_operators.python.ops.qibo_tf_custom_operators import swap_pieces
        from qibotf.custom_operators.python.ops.qibo_tf_custom_operators import apply_gate
        from qibotf.custom_operators.python.ops.qibo_tf_custom_operators import apply_x
        from qibotf.custom_operators.python.ops.qibo_tf_custom_operators import apply_y
        from qibotf.custom_operators.python.ops.qibo_tf_custom_operators import apply_z
        from qibotf.custom_operators.python.ops.qibo_tf_custom_operators import apply_z_pow
        from qibotf.custom_operators.python.ops.qibo_tf_custom_operators import apply_two_qubit_gate
        from qibotf.custom_operators.python.ops.qibo_tf_custom_operators import apply_fsim
        from qibotf.custom_operators.python.ops.qibo_tf_custom_operators import apply_swap
        from qibotf.custom_operators.python.ops.qibo_tf_custom_operators import collapse_state
        from qibotf.custom_operators.python.ops.qibo_tf_custom_operators import measure_frequencies
        _custom_operators_loaded = True
    except tf.errors.NotFoundError:  # pragma: no cover
        log.warning("Custom operators not found, skipping custom operators load.")
except ModuleNotFoundError:  # pragma: no cover
    # case not tested because CI has tf installed
    log.warning("Tensorflow is not installed. Skipping custom operators load.")
