from __future__ import annotations

import sys
import wave
from pathlib import Path
import json

import pytest


PERCEPTION_ROOT = Path(__file__).resolve().parents[1]
if str(PERCEPTION_ROOT) not in sys.path:
    sys.path.insert(0, str(PERCEPTION_ROOT))

from tools import asr_ros2_eval  # noqa: E402


def _wav(path: Path, frames: int = 16000) -> Path:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\0" * frames * 2)
    return path


def _result(payload: dict, *, topic: str = "/mic/asr",
            received_ts: float = 12.0) -> asr_ros2_eval.CapturedResult:
    return asr_ros2_eval.CapturedResult(
        topic=topic,
        payload=payload,
        received_ts=received_ts,
        received_monotonic=20.0,
    )


def test_segment_metrics_use_capture_and_completion_timestamps():
    captured = _result({
        "text": "机器人前进",
        "audio_start_ts": 10.0,
        "audio_end_ts": 11.5,
        "asr_complete_ts": 11.9,
        "spans": [
            {"span": "asr_transcribe", "start_ts": 11.6, "end_ts": 11.8},
            {"span": "asr_transcribe", "start_ts": 11.8, "end_ts": 11.85},
            {"span": "kws", "start_ts": 11.85, "end_ts": 11.9},
        ],
    })

    segment = asr_ros2_eval._segment_from_result(captured, 3)

    assert segment["index"] == 3
    assert segment["decode_ms"] == 250.0
    assert segment["endpoint_latency_ms"] == 500.0
    assert segment["ros_delivery_ms"] == pytest.approx(100.0)


def test_prediction_joins_segments_and_marks_kws_output_non_comparable(tmp_path):
    audio = _wav(tmp_path / "one.wav")
    item = {"id": "one", "_audio_path": audio, "text": "小范前进"}
    results = [
        _result({"text": "小范", "audio_end_ts": 11.0,
                 "asr_complete_ts": 11.5}, received_ts=11.6),
        _result({"text": "前进", "audio_end_ts": 11.8,
                 "asr_complete_ts": 11.9, "kws_triggered": True},
                received_ts=12.0),
    ]

    prediction = asr_ros2_eval.prediction_from_results(
        item, results, {"published_chunks": 32})

    assert prediction["text"] == "小范前进"
    assert prediction["segment_count"] == 2
    assert prediction["audio_duration_s"] == 1.0
    assert prediction["endpoint_latency_ms"] == 600.0
    assert prediction["published_chunks"] == 32
    assert prediction["raw_cer_comparable"] is False


def test_capture_window_uses_payload_audio_time_when_available():
    previous = _result({"audio_end_ts": 90.0}, received_ts=101.0)
    current = _result({"audio_end_ts": 101.5}, received_ts=102.0)
    unstamped = _result({}, received_ts=100.5)

    assert not asr_ros2_eval._matches_capture_window(previous, 100.0, 103.0)
    assert asr_ros2_eval._matches_capture_window(current, 100.0, 103.0)
    assert asr_ros2_eval._matches_capture_window(unstamped, 100.0, 103.0)


def test_group_summaries_keep_manifest_splits_separate(tmp_path):
    audio = _wav(tmp_path / "one.wav")
    manifest = [
        {"id": "dev", "split": "dev", "text": "前进", "_audio_path": audio},
        {"id": "test", "split": "test", "text": "停止", "_audio_path": audio},
    ]
    predictions = [
        {"id": "dev", "text": "前进", "audio_duration_s": 1,
         "decode_ms": 10},
        {"id": "test", "text": "停", "audio_duration_s": 1,
         "decode_ms": 10},
    ]

    groups = asr_ros2_eval._group_summaries(manifest, predictions, "小范小范")

    assert groups["dev"]["cer"] == 0
    assert groups["test"]["cer"] == pytest.approx(0.5)


def test_cli_defaults_to_derived_output_topics():
    parser = asr_ros2_eval.build_parser()
    args = parser.parse_args([
        "--manifest", "manifest.jsonl", "--input-topic", "/mic/audio",
        "--output", "predictions.jsonl",
    ])
    # main() derives these after parsing; keeping them unset here prevents a
    # stale hard-coded topic when callers choose a different input topic.
    assert args.output_topic is None
    assert args.background_topic is None
    assert args.chunk_bytes == 1024


def test_decode_mcp_response_extracts_tool_payload():
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{
            "type": "text",
            "text": json.dumps({"state": "running", "transport": {"x": 1}}),
        }]},
    }).encode()

    assert asr_ros2_eval._decode_mcp_response(body) == {
        "state": "running", "transport": {"x": 1}}


def test_transport_delta_rejects_resets_and_reports_valid_run():
    before = {
        "received_chunks": 2, "received_bytes": 2048,
        "enqueued_chunks": 2, "enqueued_bytes": 2048,
        "queue_dropped_chunks": 0, "queue_dropped_bytes": 0,
        "rejected_chunks": 0, "rejected_bytes": 0,
    }
    after = {
        "received_chunks": 12, "received_bytes": 12288,
        "enqueued_chunks": 11, "enqueued_bytes": 11264,
        "queue_dropped_chunks": 1, "queue_dropped_bytes": 1024,
        "rejected_chunks": 0, "rejected_bytes": 0,
    }

    delta = asr_ros2_eval._transport_delta(before, after)
    assert delta["received_chunks"] == 10
    assert delta["enqueued_chunks"] == 9
    assert delta["queue_dropped_chunks"] == 1
    assert asr_ros2_eval._transport_delta(after, before) is None
