#!/usr/bin/env python3
"""Reproducible utilities for the Chinese robot-ASR demo.

This module deliberately keeps validation and scoring free of ROS2 and
sherpa-onnx imports, so dataset preparation and result checking work on a
developer laptop. Model transcription and VAD are imported lazily only by the
commands that need them; the compact Zipformer replay also has a no-ROS laptop
path.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import statistics
import struct
import subprocess
import sys
import time
import unicodedata
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable


SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH = 2
MIN_CHUNK_BYTES = 1_024
MAX_REPO_FILE_BYTES = 1_000_000


@dataclass(frozen=True)
class WavInfo:
    path: str
    sample_rate: int
    channels: int
    sample_width: int
    frames: int
    duration_s: float


def inspect_wav(path: str | Path) -> WavInfo:
    """Return WAV metadata and enforce the platform's audio contract."""
    audio_path = Path(path)
    try:
        with wave.open(str(audio_path), "rb") as stream:
            channels = stream.getnchannels()
            sample_width = stream.getsampwidth()
            sample_rate = stream.getframerate()
            frames = stream.getnframes()
            compression = stream.getcomptype()
    except (OSError, EOFError, wave.Error) as error:
        raise ValueError(f"{audio_path}: unreadable WAV: {error}") from error

    problems = []
    if sample_rate != SAMPLE_RATE:
        problems.append(f"sample_rate={sample_rate}, expected {SAMPLE_RATE}")
    if channels != CHANNELS:
        problems.append(f"channels={channels}, expected {CHANNELS}")
    if sample_width != SAMPLE_WIDTH:
        problems.append(f"sample_width={sample_width}, expected {SAMPLE_WIDTH}")
    if compression != "NONE":
        problems.append(f"compression={compression}, expected PCM")
    if frames <= 0:
        problems.append("audio is empty")
    if problems:
        raise ValueError(f"{audio_path}: " + "; ".join(problems))

    return WavInfo(
        path=str(audio_path),
        sample_rate=sample_rate,
        channels=channels,
        sample_width=sample_width,
        frames=frames,
        duration_s=frames / sample_rate,
    )


def iter_pcm_chunks(path: str | Path,
                    chunk_bytes: int = MIN_CHUNK_BYTES) -> Iterable[bytes]:
    """Yield fixed-size PCM chunks, padding the final chunk with silence.

    The platform contract requires at least 1024 bytes per AudioChunk.  A WAV's
    final read is normally shorter, so publishing it as-is would violate the
    contract; zero-padding preserves the signal and provides useful VAD tail.
    """
    if chunk_bytes < MIN_CHUNK_BYTES or chunk_bytes % SAMPLE_WIDTH:
        raise ValueError(
            f"chunk_bytes must be even and >= {MIN_CHUNK_BYTES}: {chunk_bytes}")
    inspect_wav(path)
    with wave.open(str(path), "rb") as stream:
        frames_per_chunk = chunk_bytes // SAMPLE_WIDTH
        while True:
            chunk = stream.readframes(frames_per_chunk)
            if not chunk:
                break
            if len(chunk) < chunk_bytes:
                chunk += b"\x00" * (chunk_bytes - len(chunk))
            yield chunk


def normalize_text(text: str) -> str:
    """Normalize Chinese ASR text for CER while retaining letters and digits."""
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return "".join(character for character in normalized if character.isalnum())


def edit_counts(reference: str, hypothesis: str) -> dict[str, int]:
    """Return Levenshtein substitutions/deletions/insertions."""
    ref = list(reference)
    hyp = list(hypothesis)
    costs = [[0] * (len(hyp) + 1) for _ in range(len(ref) + 1)]
    for index in range(len(ref) + 1):
        costs[index][0] = index
    for index in range(len(hyp) + 1):
        costs[0][index] = index
    for row in range(1, len(ref) + 1):
        for column in range(1, len(hyp) + 1):
            substitution = costs[row - 1][column - 1] + (
                ref[row - 1] != hyp[column - 1])
            deletion = costs[row - 1][column] + 1
            insertion = costs[row][column - 1] + 1
            costs[row][column] = min(substitution, deletion, insertion)

    substitutions = deletions = insertions = 0
    row, column = len(ref), len(hyp)
    while row or column:
        if row and column:
            penalty = int(ref[row - 1] != hyp[column - 1])
            if costs[row][column] == costs[row - 1][column - 1] + penalty:
                substitutions += penalty
                row -= 1
                column -= 1
                continue
        if row and costs[row][column] == costs[row - 1][column] + 1:
            deletions += 1
            row -= 1
            continue
        insertions += 1
        column -= 1
    return {
        "substitutions": substitutions,
        "deletions": deletions,
        "insertions": insertions,
        "errors": substitutions + deletions + insertions,
    }


def _read_jsonl(path: str | Path) -> list[dict]:
    records = []
    with Path(path).open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {error}") from error
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: expected a JSON object")
            records.append(value)
    return records


def load_manifest(path: str | Path) -> list[dict]:
    manifest_path = Path(path).resolve()
    records = _read_jsonl(manifest_path)
    seen = set()
    for index, record in enumerate(records, 1):
        sample_id = str(record.get("id", "")).strip()
        audio = str(record.get("audio", "")).strip()
        if not sample_id or not audio or "text" not in record:
            raise ValueError(
                f"{manifest_path}:{index}: id, audio and text are required")
        if sample_id in seen:
            raise ValueError(f"{manifest_path}:{index}: duplicate id {sample_id!r}")
        seen.add(sample_id)
        audio_path = Path(audio)
        if not audio_path.is_absolute():
            audio_path = manifest_path.parent / audio_path
        record["id"] = sample_id
        record["_audio_path"] = audio_path.resolve()
    return records


def _number(record: dict, key: str) -> float | None:
    value = record.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = float(value)
        if math.isfinite(value) and value >= 0:
            return value
    return None


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def score(manifest: list[dict], predictions: list[dict],
          wake_word: str = "") -> tuple[dict, list[dict]]:
    prediction_map = {}
    duplicate_predictions = 0
    for prediction in predictions:
        sample_id = str(prediction.get("id", "")).strip()
        if not sample_id:
            raise ValueError("every prediction requires a non-empty id")
        if sample_id in prediction_map:
            duplicate_predictions += 1
        prediction_map[sample_id] = prediction

    totals = {key: 0 for key in (
        "substitutions", "deletions", "insertions", "errors")}
    total_reference_chars = 0
    total_audio_s = 0.0
    total_decode_ms = 0.0
    timed_audio_s = 0.0
    endpoint_latencies = []
    missing = silence_false_outputs = empty_hypotheses = error_predictions = 0
    expected_utterances = detected_utterances = 0
    missed_utterances = duplicate_utterances = unexpected_utterances = 0
    pending_eof_files = tail_flush_files = tail_dropped_files = 0
    discarded_short_segments = 0
    input_chunks = flush_chunks = dropped_chunks = late_chunks = 0
    max_replay_lag_ms = 0.0
    total_pipeline_ms = total_replay_wall_ms = 0.0
    pipeline_audio_s = replay_wall_audio_s = 0.0
    replay_metrics_present = False
    normalized_wake_word = normalize_text(wake_word)
    wake_references = wake_hits = 0
    command_totals = {key: 0 for key in totals}
    command_reference_chars = 0
    details = []

    for item in manifest:
        sample_id = item["id"]
        prediction = prediction_map.get(sample_id)
        if prediction is None:
            missing += 1
            prediction = {"id": sample_id, "text": "", "status": "missing"}
        if prediction.get("status") == "error":
            error_predictions += 1
        reference = str(item.get("text", ""))
        hypothesis = str(prediction.get("text", ""))
        normalized_reference = normalize_text(reference)
        normalized_hypothesis = normalize_text(hypothesis)
        counts = edit_counts(normalized_reference, normalized_hypothesis)
        for key in totals:
            totals[key] += counts[key]
        total_reference_chars += len(normalized_reference)

        duration_s = _number(prediction, "audio_duration_s")
        if duration_s is None:
            duration_s = inspect_wav(item["_audio_path"]).duration_s
        total_audio_s += duration_s
        decode_ms = _number(prediction, "decode_ms")
        if decode_ms is not None:
            total_decode_ms += decode_ms
            timed_audio_s += duration_s
        segments = prediction.get("segments")
        segment_latencies = []
        if isinstance(segments, list):
            for segment in segments:
                if not isinstance(segment, dict):
                    continue
                value = _number(segment, "endpoint_latency_ms")
                if value is not None:
                    segment_latencies.append(value)
        endpoint_latency_ms = _number(prediction, "endpoint_latency_ms")
        if segment_latencies:
            endpoint_latencies.extend(segment_latencies)
        elif endpoint_latency_ms is not None:
            endpoint_latencies.append(endpoint_latency_ms)

        segment_count_value = _number(prediction, "segment_count")
        if segment_count_value is None and isinstance(segments, list):
            segment_count_value = len(segments)
        if segment_count_value is not None:
            replay_metrics_present = True
            segment_count = int(segment_count_value)
            expected = int(bool(normalized_reference))
            expected_utterances += expected
            detected_utterances += segment_count
            missed_utterances += max(0, expected - segment_count)
            if expected:
                duplicate_utterances += max(0, segment_count - expected)
            else:
                unexpected_utterances += segment_count
        else:
            segment_count = None

        for key, target in (
            ("input_chunks", "input"),
            ("flush_chunks", "flush"),
            ("dropped_chunks", "dropped"),
            ("discarded_short_segments", "discarded"),
        ):
            value = _number(prediction, key)
            if value is None:
                continue
            replay_metrics_present = True
            if target == "input":
                input_chunks += int(value)
            elif target == "flush":
                flush_chunks += int(value)
            elif target == "dropped":
                dropped_chunks += int(value)
            else:
                discarded_short_segments += int(value)
        if prediction.get("tail_flush_emitted"):
            replay_metrics_present = True
            tail_flush_files += 1
        if prediction.get("pending_speech_at_eof"):
            replay_metrics_present = True
            pending_eof_files += 1
        if prediction.get("tail_dropped"):
            replay_metrics_present = True
            tail_dropped_files += 1
        late_value = _number(prediction, "late_chunks")
        if late_value is not None:
            replay_metrics_present = True
            late_chunks += int(late_value)
        lag_value = _number(prediction, "max_replay_lag_ms")
        if lag_value is not None:
            replay_metrics_present = True
            max_replay_lag_ms = max(max_replay_lag_ms, lag_value)

        pipeline_ms = _number(prediction, "pipeline_ms")
        if pipeline_ms is not None:
            replay_metrics_present = True
            total_pipeline_ms += pipeline_ms
            pipeline_audio_s += duration_s
        replay_wall_ms = _number(prediction, "replay_wall_ms")
        if replay_wall_ms is not None:
            replay_metrics_present = True
            total_replay_wall_ms += replay_wall_ms
            replay_wall_audio_s += duration_s

        wake_expected = False
        wake_hit = None
        command_counts = None
        command_reference = command_hypothesis = None
        if normalized_wake_word and normalized_reference.startswith(normalized_wake_word):
            wake_expected = True
            wake_references += 1
            wake_hit = normalized_hypothesis.startswith(normalized_wake_word)
            wake_hits += int(wake_hit)
            # Measure the command independently from wake-word spelling.  Both
            # prefixes occupy the same expected slot, while exact wake success
            # remains visible in wake_hit_rate.
            command_reference = normalized_reference[len(normalized_wake_word):]
            command_hypothesis = normalized_hypothesis[len(normalized_wake_word):]
            command_counts = edit_counts(command_reference, command_hypothesis)
            command_reference_chars += len(command_reference)
            for key in command_totals:
                command_totals[key] += command_counts[key]

        if not normalized_reference and normalized_hypothesis:
            silence_false_outputs += 1
        if normalized_reference and not normalized_hypothesis:
            empty_hypotheses += 1
        details.append({
            "id": sample_id,
            "reference": reference,
            "hypothesis": hypothesis,
            "normalized_reference": normalized_reference,
            "normalized_hypothesis": normalized_hypothesis,
            "reference_chars": len(normalized_reference),
            "cer": (
                counts["errors"] / len(normalized_reference)
                if normalized_reference else None
            ),
            "audio_duration_s": duration_s,
            "decode_ms": decode_ms,
            "endpoint_latency_ms": endpoint_latency_ms,
            "segment_count": segment_count,
            "wake_expected": wake_expected,
            "wake_hit": wake_hit,
            "command_reference": command_reference,
            "command_hypothesis": command_hypothesis,
            "command_cer": (
                command_counts["errors"] / len(command_reference)
                if command_counts is not None and command_reference else None
            ),
            "status": prediction.get("status", "ok"),
            **counts,
        })

    manifest_ids = {item["id"] for item in manifest}
    extra_predictions = len(set(prediction_map) - manifest_ids)
    summary = {
        "samples": len(manifest),
        "reference_chars": total_reference_chars,
        **totals,
        "cer": (
            totals["errors"] / total_reference_chars
            if total_reference_chars else None
        ),
        "total_audio_s": round(total_audio_s, 6),
        "total_decode_ms": round(total_decode_ms, 3),
        "rtf": (
            total_decode_ms / 1000 / timed_audio_s if timed_audio_s else None
        ),
        "endpoint_latency_ms_p50": _percentile(endpoint_latencies, 0.50),
        "endpoint_latency_ms_p95": _percentile(endpoint_latencies, 0.95),
        "missing_predictions": missing,
        "extra_predictions": extra_predictions,
        "duplicate_predictions": duplicate_predictions,
        "error_predictions": error_predictions,
        "empty_hypotheses": empty_hypotheses,
        "silence_false_outputs": silence_false_outputs,
    }
    if replay_metrics_present:
        summary.update({
            "expected_utterances": expected_utterances,
            "detected_utterances": detected_utterances,
            "missed_utterances": missed_utterances,
            "duplicate_utterances": duplicate_utterances,
            "unexpected_utterances": unexpected_utterances,
            "pending_speech_at_eof_files": pending_eof_files,
            "tail_flush_files": tail_flush_files,
            "tail_dropped_files": tail_dropped_files,
            "discarded_short_segments": discarded_short_segments,
            "input_chunks": input_chunks,
            "flush_chunks": flush_chunks,
            "dropped_chunks": dropped_chunks,
            "late_chunks": late_chunks,
            "max_replay_lag_ms": round(max_replay_lag_ms, 3),
            "total_pipeline_ms": round(total_pipeline_ms, 3),
            "full_chain_rtf": (
                total_pipeline_ms / 1000 / pipeline_audio_s
                if pipeline_audio_s else None
            ),
            "total_replay_wall_ms": round(total_replay_wall_ms, 3),
            "replay_wall_rtf": (
                total_replay_wall_ms / 1000 / replay_wall_audio_s
                if replay_wall_audio_s else None
            ),
        })
    if normalized_wake_word:
        summary.update({
            "wake_word": wake_word,
            "wake_references": wake_references,
            "wake_hits": wake_hits,
            "wake_hit_rate": (
                wake_hits / wake_references if wake_references else None
            ),
            "command_reference_chars": command_reference_chars,
            **{f"command_{key}": value
               for key, value in command_totals.items()},
            "command_cer": (
                command_totals["errors"] / command_reference_chars
                if command_reference_chars else None
            ),
        })
    return summary, details


def _write_json(path: str | Path, value) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def command_validate(args) -> int:
    manifest = load_manifest(args.manifest)
    infos = [inspect_wav(item["_audio_path"]) for item in manifest]
    summary = {
        "samples": len(infos),
        "total_audio_s": round(sum(info.duration_s for info in infos), 3),
        "audio_contract": "16 kHz, mono, signed PCM16 little-endian",
        "chunk_bytes": args.chunk_bytes,
    }
    if args.chunk_bytes < MIN_CHUNK_BYTES or args.chunk_bytes % SAMPLE_WIDTH:
        raise ValueError(
            f"chunk_bytes must be even and >= {MIN_CHUNK_BYTES}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def command_score(args) -> int:
    manifest = load_manifest(args.manifest)
    predictions = _read_jsonl(args.predictions)
    summary, details = score(manifest, predictions, args.wake_word)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.output:
        _write_json(args.output, {"summary": summary, "details": details})
    return 0


class _StandaloneZipformerAdapter:
    """Minimal model-only adapter for laptops that do not have ROS2 installed."""

    _ENCODER = "encoder-epoch-99-avg-1.int8.onnx"
    _DECODER = "decoder-epoch-99-avg-1.onnx"
    _JOINER = "joiner-epoch-99-avg-1.int8.onnx"
    _TAIL_S = 0.66

    def __init__(self, model_dir: Path, num_threads: int):
        try:
            import sherpa_onnx
        except ImportError as error:
            raise RuntimeError(
                "local demo requires sherpa-onnx; install it in a virtual "
                "environment with: pip install sherpa-onnx==1.13.6"
            ) from error

        self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(model_dir / "tokens.txt"),
            encoder=str(model_dir / self._ENCODER),
            decoder=str(model_dir / self._DECODER),
            joiner=str(model_dir / self._JOINER),
            num_threads=num_threads,
            provider="cpu",
            sample_rate=SAMPLE_RATE,
            feature_dim=80,
            decoding_method="greedy_search",
            enable_endpoint_detection=False,
        )

    def transcribe(self, wav_bytes: bytes, language: str) -> str:
        del language
        with wave.open(io.BytesIO(wav_bytes), "rb") as stream:
            pcm = stream.readframes(stream.getnframes())
        sample_count = len(pcm) // SAMPLE_WIDTH
        samples = struct.unpack(f"<{sample_count}h", pcm)
        float_samples = [sample / 32768.0 for sample in samples]

        stream = self._recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, float_samples)
        stream.accept_waveform(
            SAMPLE_RATE, [0.0] * int(SAMPLE_RATE * self._TAIL_S))
        stream.input_finished()
        while self._recognizer.is_ready(stream):
            self._recognizer.decode_stream(stream)
        result = self._recognizer.get_result(stream)
        text = result.text if hasattr(result, "text") else str(result)
        return text.strip()


def _standalone_model_dir(model_dir: str | None) -> Path:
    if model_dir:
        return Path(model_dir).expanduser().resolve()
    cache_home = Path(os.environ.get(
        "XDG_CACHE_HOME", Path.home() / ".cache"))
    return cache_home / "robot-asr" / "models" / "zipformer-zh-14m"


def _load_standalone_zipformer(model_dir: str | None, num_threads: int):
    perception_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(perception_root))
    from utils.model_downloader import ensure_model

    resolved_dir = _standalone_model_dir(model_dir)
    ensure_model("asr_zipformer_zh_14m", str(resolved_dir))
    adapter = _StandaloneZipformerAdapter(resolved_dir, num_threads)

    # Match the plugin's warm path so the timed demo runs exclude first-call
    # graph setup. Model download, load and warmup remain visible as load time.
    silence = io.BytesIO()
    with wave.open(silence, "wb") as stream:
        stream.setnchannels(CHANNELS)
        stream.setsampwidth(SAMPLE_WIDTH)
        stream.setframerate(SAMPLE_RATE)
        stream.writeframes(b"\x00\x00" * SAMPLE_RATE)
    adapter.transcribe(silence.getvalue(), "zh-CN")
    return adapter


def _load_runtime_adapter(model: str, model_dir: str | None,
                          num_threads: int):
    """Load the plugin adapter, with a no-ROS fallback for the demo model."""
    perception_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(perception_root))
    try:
        from plugins import asr  # pylint: disable=import-outside-toplevel
    except ModuleNotFoundError as error:
        if error.name in {"rclpy", "std_msgs"} and model == "zipformer-zh-14m":
            return _load_standalone_zipformer(model_dir, num_threads)
        raise RuntimeError(
            "this model requires the perception runtime with ROS2, audio_msgs "
            "and sherpa-onnx installed; run it in the Jetson container"
        ) from error

    config = {
        "asr_model": model,
        "device": "cpu",
        "num_threads": num_threads,
        "warmup": True,
    }
    if model_dir:
        config["model_dir"] = model_dir
    return asr._build_asr_adapter(config)  # noqa: SLF001 - benchmark real path


def command_demo(args) -> int:
    """Transcribe one WAV and print a compact, recording-friendly report."""
    info = inspect_wav(args.audio)
    if args.repeat < 1:
        raise ValueError("repeat must be at least 1")

    load_started = time.perf_counter()
    adapter = _load_runtime_adapter(
        args.model, args.model_dir, args.num_threads)
    load_ms = (time.perf_counter() - load_started) * 1000
    wav_bytes = Path(args.audio).read_bytes()

    texts = []
    decode_times_ms = []
    for _ in range(args.repeat):
        decode_started = time.perf_counter()
        text = adapter.transcribe(wav_bytes, args.language)
        decode_times_ms.append(
            (time.perf_counter() - decode_started) * 1000)
        texts.append(text)

    median_decode_ms = statistics.median(decode_times_ms)
    result = {
        "audio": str(Path(args.audio).resolve()),
        "audio_duration_s": round(info.duration_s, 6),
        "model": args.model,
        "device": "cpu",
        "num_threads": args.num_threads,
        "text": texts[-1],
        "repeat": args.repeat,
        "transcript_consistent": len(set(texts)) == 1,
        "model_load_ms": round(load_ms, 3),
        "decode_ms_median": round(median_decode_ms, 3),
        "decode_ms_runs": [round(value, 3) for value in decode_times_ms],
        "rtf_median": round(median_decode_ms / 1000 / info.duration_s, 6),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.output:
        _write_json(args.output, result)
    return 0


def command_transcribe(args) -> int:
    """Run model-only decoding; VAD/ROS end-to-end latency is measured later."""
    manifest = load_manifest(args.manifest)
    for item in manifest:
        inspect_wav(item["_audio_path"])

    adapter = _load_runtime_adapter(
        args.model, args.model_dir, args.num_threads)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as output:
        for item in manifest:
            info = inspect_wav(item["_audio_path"])
            started = time.perf_counter()
            result = {
                "id": item["id"],
                "model": args.model,
                "audio_duration_s": info.duration_s,
            }
            try:
                text = adapter.transcribe(
                    item["_audio_path"].read_bytes(), "zh-CN")
                result.update(text=text, status="ok")
            except Exception as error:  # keep the rest of the evaluation usable
                result.update(text="", status="error", error=str(error))
            result["decode_ms"] = round(
                (time.perf_counter() - started) * 1000, 3)
            output.write(json.dumps(result, ensure_ascii=False) + "\n")
            output.flush()
            print(json.dumps(result, ensure_ascii=False))
    return 0


def _pcm16_to_wav(pcm: bytes) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(CHANNELS)
        stream.setsampwidth(SAMPLE_WIDTH)
        stream.setframerate(SAMPLE_RATE)
        stream.writeframes(pcm)
    return output.getvalue()


def _standalone_vad_model_path(model_path: str | None) -> Path:
    """Resolve/download Silero VAD outside the repository."""
    if model_path:
        supplied = Path(model_path).expanduser().resolve()
        if supplied.is_file():
            return supplied
        if supplied.suffix.lower() == ".onnx":
            raise ValueError(f"VAD model file does not exist: {supplied}")
        model_dir = supplied
    else:
        cache_home = Path(os.environ.get(
            "XDG_CACHE_HOME", Path.home() / ".cache"))
        model_dir = cache_home / "robot-asr" / "models" / "vad"

    perception_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(perception_root))
    from utils.model_downloader import ensure_model

    ensure_model("vad", str(model_dir))
    resolved = model_dir / "silero_vad.onnx"
    if not resolved.is_file():
        raise RuntimeError(f"VAD model download did not create {resolved}")
    return resolved


def _load_replay_vad(model_path: str | None, threshold: float,
                     silence_ms: int):
    perception_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(perception_root))
    from utils.asr_vad import build_sherpa_vad

    return build_sherpa_vad(
        str(_standalone_vad_model_path(model_path)), threshold, silence_ms)


def replay_wav(
    path: str | Path,
    adapter,
    vad,
    *,
    chunk_bytes: int = MIN_CHUNK_BYTES,
    flush_ms: int = 2_000,
    silence_ms: int = 400,
    pre_roll_ms: int = 500,
    realtime: bool = False,
    language: str = "zh-CN",
    clock: Callable[[], float] | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> dict:
    """Replay one WAV through VAD and ASR using AudioChunk-sized blocks.

    ``pipeline_ms`` measures active VAD + ASR wall time and excludes deliberate
    real-time sleeps.  ``replay_wall_ms`` includes those sleeps.  Endpoint
    latency is measured from the VAD segment's speech end through segmentation
    and ASR completion; in real-time mode it additionally captures scheduling
    lag, while fast mode uses the audio clock plus measured decode time.
    """
    if chunk_bytes < MIN_CHUNK_BYTES or chunk_bytes % SAMPLE_WIDTH:
        raise ValueError(
            f"chunk_bytes must be even and >= {MIN_CHUNK_BYTES}: {chunk_bytes}")
    if flush_ms < 0:
        raise ValueError("flush_ms must be non-negative")
    if silence_ms < 0:
        raise ValueError("silence_ms must be non-negative")
    if pre_roll_ms < 0:
        raise ValueError("pre_roll_ms must be non-negative")

    now = clock or time.perf_counter
    sleep = sleeper or time.sleep
    info = inspect_wav(path)

    perception_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(perception_root))
    from plugins.vad_preroll import PcmHistory
    from utils.asr_vad import BUFFER_S

    vad.reset()
    history = PcmHistory(SAMPLE_RATE * (BUFFER_S + 1))
    pre_roll_samples = int(SAMPLE_RATE * pre_roll_ms / 1000)
    silence_samples = int(SAMPLE_RATE * silence_ms / 1000)
    stream_samples = 0
    active_s = 0.0
    segments = []
    discarded_short = 0
    speech_seen_during_input = False
    max_replay_lag_s = 0.0
    late_chunks = 0
    replay_started = now()

    def drain_segments(during_flush: bool) -> None:
        nonlocal discarded_short
        while not vad.empty():
            segment = vad.front
            float_samples = list(segment.samples)
            segment_start = getattr(segment, "start", None)
            if segment_start is None:
                segment_start = max(
                    0, stream_samples - len(float_samples) - silence_samples)
            segment_start = int(segment_start)
            segment_end = segment_start + len(float_samples)
            segment_pcm = struct.pack(
                f"<{len(float_samples)}h",
                *[
                    int(max(-32768, min(32767, sample * 32768)))
                    for sample in float_samples
                ],
            )
            pre_roll = history.slice(
                segment_start - pre_roll_samples, segment_start)
            utterance = pre_roll + segment_pcm
            vad.pop()

            # Match the production worker's >500 ms output gate.
            if len(utterance) <= SAMPLE_RATE:
                discarded_short += 1
                continue

            decode_started = now()
            text = adapter.transcribe(_pcm16_to_wav(utterance), language)
            decode_finished = now()
            decode_s = max(0.0, decode_finished - decode_started)
            speech_end_s = segment_end / SAMPLE_RATE
            emitted_at_audio_s = stream_samples / SAMPLE_RATE
            vad_endpoint_ms = max(
                0.0, (emitted_at_audio_s - speech_end_s) * 1000)
            if realtime:
                endpoint_ms = max(
                    0.0,
                    (decode_finished - replay_started - speech_end_s) * 1000,
                )
                endpoint_basis = "realtime_wall_clock"
            else:
                endpoint_ms = vad_endpoint_ms + decode_s * 1000
                endpoint_basis = "audio_clock_plus_decode"
            segments.append({
                "index": len(segments),
                "text": str(text or "").strip(),
                "speech_start_s": round(segment_start / SAMPLE_RATE, 6),
                "speech_end_s": round(speech_end_s, 6),
                "utterance_duration_s": round(
                    len(utterance) / SAMPLE_WIDTH / SAMPLE_RATE, 6),
                "pre_roll_ms": round(
                    len(pre_roll) / SAMPLE_WIDTH / SAMPLE_RATE * 1000, 3),
                "emitted_at_audio_s": round(emitted_at_audio_s, 6),
                "emitted_during_flush": during_flush,
                "vad_endpoint_ms": round(vad_endpoint_ms, 3),
                "decode_ms": round(decode_s * 1000, 3),
                "endpoint_latency_ms": round(endpoint_ms, 3),
                "endpoint_latency_basis": endpoint_basis,
            })

    def feed_chunk(pcm: bytes, during_flush: bool) -> None:
        nonlocal stream_samples, active_s, speech_seen_during_input
        nonlocal max_replay_lag_s, late_chunks
        if len(pcm) != chunk_bytes:
            raise ValueError(
                f"replay chunk has {len(pcm)} bytes, expected {chunk_bytes}")
        if realtime:
            deadline = replay_started + stream_samples / SAMPLE_RATE
            remaining = deadline - now()
            if remaining > 0:
                sleep(remaining)
            lag_s = max(0.0, now() - deadline)
            max_replay_lag_s = max(max_replay_lag_s, lag_s)
            if lag_s > 0.001:
                late_chunks += 1

        active_started = now()
        sample_count = len(pcm) // SAMPLE_WIDTH
        samples = struct.unpack(f"<{sample_count}h", pcm)
        history.append(pcm)
        vad.accept_waveform([sample / 32768.0 for sample in samples])
        stream_samples += sample_count
        if not during_flush and vad.is_speech_detected():
            speech_seen_during_input = True
        drain_segments(during_flush)
        active_s += max(0.0, now() - active_started)

    input_chunks = 0
    for chunk in iter_pcm_chunks(path, chunk_bytes):
        feed_chunk(chunk, False)
        input_chunks += 1

    pending_speech_at_eof = bool(vad.is_speech_detected())
    segments_before_flush = len(segments)
    chunk_ms = chunk_bytes / SAMPLE_WIDTH / SAMPLE_RATE * 1000
    flush_chunk_limit = math.ceil(flush_ms / chunk_ms) if flush_ms else 0
    flush_chunks = 0
    silence_chunk = b"\x00" * chunk_bytes
    # ``flush_ms`` is a safety ceiling, not unconditional dead air.  Once the
    # pending speech segment closes, more silence cannot improve that file and
    # would make real-time wall RTF measure harness padding instead of latency.
    for _ in range(flush_chunk_limit if pending_speech_at_eof else 0):
        feed_chunk(silence_chunk, True)
        flush_chunks += 1
        if not vad.is_speech_detected():
            break

    replay_wall_s = max(0.0, now() - replay_started)
    tail_flush_segments = len(segments) - segments_before_flush
    tail_dropped = pending_speech_at_eof and tail_flush_segments == 0
    endpoint_values = [segment["endpoint_latency_ms"] for segment in segments]
    input_pcm_bytes = info.frames * SAMPLE_WIDTH
    padded_input_bytes = input_chunks * chunk_bytes - input_pcm_bytes
    text = "".join(segment["text"] for segment in segments if segment["text"])
    return {
        "text": text,
        "status": "ok" if segments else "no_speech",
        "audio_duration_s": round(info.duration_s, 6),
        "chunk_bytes": chunk_bytes,
        "chunk_duration_ms": round(chunk_ms, 3),
        "input_chunks": input_chunks,
        "flush_chunks": flush_chunks,
        "flush_ms_requested": flush_ms,
        "flush_audio_ms": round(flush_chunks * chunk_ms, 3),
        "padded_input_bytes": padded_input_bytes,
        "dropped_chunks": 0,
        "late_chunks": late_chunks,
        "max_replay_lag_ms": round(max_replay_lag_s * 1000, 3),
        "speech_seen_during_input": speech_seen_during_input,
        "pending_speech_at_eof": pending_speech_at_eof,
        "tail_flush_emitted": tail_flush_segments > 0,
        "tail_flush_segments": tail_flush_segments,
        "tail_dropped": tail_dropped,
        "discarded_short_segments": discarded_short,
        "segment_count": len(segments),
        "segments": segments,
        "decode_ms": round(sum(s["decode_ms"] for s in segments), 3),
        "pipeline_ms": round(active_s * 1000, 3),
        "full_chain_rtf": active_s / info.duration_s,
        "replay_wall_ms": round(replay_wall_s * 1000, 3),
        "replay_wall_rtf": replay_wall_s / info.duration_s,
        "endpoint_latency_ms": max(endpoint_values) if endpoint_values else None,
    }


def command_replay(args) -> int:
    """Run 1024-byte replay through the production VAD and ASR settings."""
    if args.chunk_bytes < MIN_CHUNK_BYTES or args.chunk_bytes % SAMPLE_WIDTH:
        raise ValueError(
            f"chunk_bytes must be even and >= {MIN_CHUNK_BYTES}: "
            f"{args.chunk_bytes}")
    if args.flush_ms < 0 or args.vad_silence_ms < 0 or args.vad_pre_roll_ms < 0:
        raise ValueError("flush and VAD duration arguments must be non-negative")
    if not 0 <= args.vad_threshold <= 1:
        raise ValueError("vad_threshold must be between 0 and 1")
    manifest = load_manifest(args.manifest)
    for item in manifest:
        inspect_wav(item["_audio_path"])

    load_started = time.perf_counter()
    adapter = _load_runtime_adapter(
        args.model, args.model_dir, args.num_threads)
    asr_load_ms = (time.perf_counter() - load_started) * 1000
    load_started = time.perf_counter()
    vad = _load_replay_vad(
        args.vad_model, args.vad_threshold, args.vad_silence_ms)
    vad_load_ms = (time.perf_counter() - load_started) * 1000

    predictions = []
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as output:
        for item in manifest:
            result = {"id": item["id"], "model": args.model}
            try:
                result.update(replay_wav(
                    item["_audio_path"],
                    adapter,
                    vad,
                    chunk_bytes=args.chunk_bytes,
                    flush_ms=args.flush_ms,
                    silence_ms=args.vad_silence_ms,
                    pre_roll_ms=args.vad_pre_roll_ms,
                    realtime=args.realtime,
                    language=args.language,
                ))
            except Exception as error:  # keep later samples evaluable
                result.update({
                    "text": "",
                    "status": "error",
                    "error": str(error),
                    "audio_duration_s": inspect_wav(
                        item["_audio_path"]).duration_s,
                    "segment_count": 0,
                    "segments": [],
                    "dropped_chunks": 0,
                })
            predictions.append(result)
            output.write(json.dumps(result, ensure_ascii=False) + "\n")
            output.flush()
            print(json.dumps(result, ensure_ascii=False))

    summary, details = score(manifest, predictions, args.wake_word)
    summary.update({
        "model": args.model,
        "asr_load_ms": round(asr_load_ms, 3),
        "vad_load_ms": round(vad_load_ms, 3),
        "realtime": bool(args.realtime),
        "chunk_bytes": args.chunk_bytes,
        "vad_threshold": args.vad_threshold,
        "vad_silence_ms": args.vad_silence_ms,
        "vad_pre_roll_ms": args.vad_pre_roll_ms,
        "flush_ms": args.flush_ms,
    })
    group_summaries = {}
    prediction_by_id = {item["id"]: item for item in predictions}
    for group in dict.fromkeys(
            str(item.get("split", "")).strip() for item in manifest):
        if not group:
            continue
        group_manifest = [
            item for item in manifest
            if str(item.get("split", "")).strip() == group
        ]
        group_predictions = [
            prediction_by_id[item["id"]]
            for item in group_manifest if item["id"] in prediction_by_id
        ]
        group_summaries[group] = score(
            group_manifest, group_predictions, args.wake_word)[0]

    console_report = {"summary": summary}
    if group_summaries:
        console_report["groups"] = group_summaries
    print(json.dumps(console_report, ensure_ascii=False, indent=2))
    if args.report:
        _write_json(args.report, {
            "summary": summary,
            "groups": group_summaries,
            "details": details,
            "predictions": predictions,
        })
    return 0


def submission_violations(root: str | Path,
                          max_bytes: int = MAX_REPO_FILE_BYTES) -> list[dict]:
    repo = Path(root).resolve()
    process = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=repo, capture_output=True, check=True,
    )
    violations = []
    for raw in process.stdout.split(b"\0"):
        if not raw:
            continue
        relative = raw.decode("utf-8", errors="surrogateescape")
        path = repo / relative
        if path.is_file() and path.stat().st_size > max_bytes:
            violations.append({"path": relative, "bytes": path.stat().st_size})
    return sorted(violations, key=lambda item: item["bytes"], reverse=True)


def command_check_submission(args) -> int:
    violations = submission_violations(args.root, args.max_bytes)
    result = {
        "ok": not violations,
        "limit_bytes": args.max_bytes,
        "violations": violations,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(bool(violations))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="validate manifest WAV files")
    validate.add_argument("--manifest", required=True)
    validate.add_argument("--chunk-bytes", type=int, default=MIN_CHUNK_BYTES)
    validate.set_defaults(handler=command_validate)

    demo = commands.add_parser(
        "demo", help="transcribe one WAV and print text plus timing")
    demo.add_argument("--audio", required=True)
    demo.add_argument("--model", default="zipformer-zh-14m")
    demo.add_argument("--model-dir")
    demo.add_argument("--num-threads", type=int, default=2)
    demo.add_argument("--language", default="zh-CN")
    demo.add_argument("--repeat", type=int, default=3)
    demo.add_argument("--output")
    demo.set_defaults(handler=command_demo)

    transcribe = commands.add_parser(
        "transcribe", help="run model-only decoding in the perception runtime")
    transcribe.add_argument("--manifest", required=True)
    transcribe.add_argument("--output", required=True)
    transcribe.add_argument("--model", default="zipformer-zh-14m")
    transcribe.add_argument("--model-dir")
    transcribe.add_argument("--num-threads", type=int, default=2)
    transcribe.set_defaults(handler=command_transcribe)

    replay = commands.add_parser(
        "replay",
        help="replay fixed PCM chunks through VAD and ASR, then score the chain",
    )
    replay.add_argument("--manifest", required=True)
    replay.add_argument("--output", required=True,
                        help="per-file JSONL predictions and timing")
    replay.add_argument("--report", help="combined score and replay metrics JSON")
    replay.add_argument("--model", default="zipformer-zh-14m")
    replay.add_argument("--model-dir")
    replay.add_argument("--vad-model",
                        help="silero_vad.onnx or a cache directory")
    replay.add_argument("--num-threads", type=int, default=2)
    replay.add_argument("--language", default="zh-CN")
    replay.add_argument("--chunk-bytes", type=int, default=MIN_CHUNK_BYTES)
    replay.add_argument("--flush-ms", type=int, default=2_000)
    replay.add_argument("--vad-threshold", type=float, default=0.5)
    replay.add_argument("--vad-silence-ms", type=int, default=400)
    replay.add_argument("--vad-pre-roll-ms", type=int, default=500)
    replay.add_argument("--wake-word", default="小范小范")
    replay.add_argument(
        "--realtime", action="store_true",
        help="pace chunks against the wall clock; default runs as fast as possible",
    )
    replay.set_defaults(handler=command_replay)

    scorer = commands.add_parser("score", help="compute CER and latency summary")
    scorer.add_argument("--manifest", required=True)
    scorer.add_argument("--predictions", required=True)
    scorer.add_argument("--output")
    scorer.add_argument("--wake-word", default="")
    scorer.set_defaults(handler=command_score)

    check = commands.add_parser(
        "check-submission", help="reject repo files above the submission limit")
    check.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    check.add_argument("--max-bytes", type=int, default=MAX_REPO_FILE_BYTES)
    check.set_defaults(handler=command_check_submission)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(2, f"error: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
