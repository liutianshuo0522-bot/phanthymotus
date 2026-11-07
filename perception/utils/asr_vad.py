"""Shared sherpa-onnx VAD configuration for live ASR and offline replay."""

from __future__ import annotations


SAMPLE_RATE = 16_000
WINDOW_SIZE = 512
MIN_CHUNK_BYTES = WINDOW_SIZE * 2
MAX_SPEECH_S = 30
BUFFER_S = 35


def build_sherpa_vad(model_path: str, threshold: float, silence_ms: int):
    """Build the VAD used by both the ROS worker and the replay evaluator.

    Keeping this constructor in a ROS-free module lets the laptop evaluator
    exercise the production segmentation configuration without importing
    ``rclpy`` or creating a ROS node.
    """
    if not 0 <= threshold <= 1:
        raise ValueError(f"vad threshold must be between 0 and 1: {threshold}")
    if silence_ms < 0:
        raise ValueError(f"vad silence_ms must be non-negative: {silence_ms}")

    import sherpa_onnx

    config = sherpa_onnx.VadModelConfig(
        silero_vad=sherpa_onnx.SileroVadModelConfig(
            model=str(model_path),
            threshold=float(threshold),
            min_silence_duration=silence_ms / 1000.0,
            min_speech_duration=0.1,
            window_size=WINDOW_SIZE,
            max_speech_duration=MAX_SPEECH_S,
        ),
        sample_rate=SAMPLE_RATE,
        num_threads=1,
        # One 32 ms Silero window is too small to amortise CUDA launch and
        # transfer overhead.  This also prevents the VAD subprocess from
        # holding a second CUDA context beside the ASR model.
        provider="cpu",
    )
    return sherpa_onnx.VoiceActivityDetector(
        config, buffer_size_in_seconds=BUFFER_S)
