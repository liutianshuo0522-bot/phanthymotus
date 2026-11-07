#!/usr/bin/env python3
"""Reproducible utilities for the Chinese robot-ASR demo.

This module deliberately keeps validation and scoring free of ROS2 and
sherpa-onnx imports, so dataset preparation and result checking work on a
developer laptop.  The ``transcribe`` command imports the real plugin lazily and
is intended to run inside the perception/Jetson environment.
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
from typing import Iterable


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


def score(manifest: list[dict], predictions: list[dict]) -> tuple[dict, list[dict]]:
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
    missing = silence_false_outputs = empty_hypotheses = 0
    details = []

    for item in manifest:
        sample_id = item["id"]
        prediction = prediction_map.get(sample_id)
        if prediction is None:
            missing += 1
            prediction = {"id": sample_id, "text": "", "status": "missing"}
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
        endpoint_latency_ms = _number(prediction, "endpoint_latency_ms")
        if endpoint_latency_ms is not None:
            endpoint_latencies.append(endpoint_latency_ms)

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
        "empty_hypotheses": empty_hypotheses,
        "silence_false_outputs": silence_false_outputs,
    }
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
    summary, details = score(manifest, predictions)
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

    scorer = commands.add_parser("score", help="compute CER and latency summary")
    scorer.add_argument("--manifest", required=True)
    scorer.add_argument("--predictions", required=True)
    scorer.add_argument("--output")
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
