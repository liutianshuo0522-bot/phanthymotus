#!/usr/bin/env python3
"""Local-only browser recorder for the robot-ASR evaluation dataset."""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import tempfile
import threading
import wave
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse


HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"
PROMPTS_PATH = HERE / "prompts.json"
MAX_AUDIO_BYTES = 32 * 1024 * 1024
SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH = 2
ID_PATTERN = re.compile(r"[a-z0-9-]+")

log = logging.getLogger("asr-recorder")


def load_prompts(path: Path = PROMPTS_PATH) -> list[dict]:
    prompts = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(prompts, list) or not prompts:
        raise ValueError("prompts.json must contain a non-empty list")
    seen = set()
    for index, prompt in enumerate(prompts, 1):
        if not isinstance(prompt, dict):
            raise ValueError(f"prompt {index} must be an object")
        prompt_id = str(prompt.get("id", ""))
        if not ID_PATTERN.fullmatch(prompt_id):
            raise ValueError(f"prompt {index} has an unsafe id: {prompt_id!r}")
        if prompt_id in seen:
            raise ValueError(f"duplicate prompt id: {prompt_id}")
        seen.add(prompt_id)
        for field in ("text", "split", "category", "hint"):
            if field not in prompt or not isinstance(prompt[field], str):
                raise ValueError(f"prompt {prompt_id} requires string field {field}")
    return prompts


def inspect_wav_bytes(payload: bytes) -> dict:
    try:
        with wave.open(io.BytesIO(payload), "rb") as stream:
            channels = stream.getnchannels()
            sample_width = stream.getsampwidth()
            sample_rate = stream.getframerate()
            frames = stream.getnframes()
            compression = stream.getcomptype()
    except (EOFError, wave.Error) as error:
        raise ValueError(f"invalid WAV file: {error}") from error

    problems = []
    if sample_rate != SAMPLE_RATE:
        problems.append(f"sample rate is {sample_rate}, expected {SAMPLE_RATE}")
    if channels != CHANNELS:
        problems.append(f"channels is {channels}, expected {CHANNELS}")
    if sample_width != SAMPLE_WIDTH:
        problems.append(f"sample width is {sample_width}, expected {SAMPLE_WIDTH}")
    if compression != "NONE":
        problems.append(f"compression is {compression}, expected PCM")
    if frames <= 0:
        problems.append("recording is empty")
    if problems:
        raise ValueError("; ".join(problems))
    return {
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_width": sample_width,
        "frames": frames,
        "duration_s": frames / sample_rate,
    }


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


class RecordingStore:
    def __init__(self, output_dir: Path, prompts: list[dict]):
        self.output_dir = output_dir.expanduser().resolve()
        self.audio_dir = self.output_dir / "audio"
        self.records_path = self.output_dir / "records.json"
        self.manifest_path = self.output_dir / "manifest.jsonl"
        self.prompts = prompts
        self.prompt_by_id = {prompt["id"]: prompt for prompt in prompts}
        self.prompt_order = {
            prompt["id"]: index for index, prompt in enumerate(prompts)}
        self._lock = threading.Lock()
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.records = self._load_records()

    def _load_records(self) -> dict[str, dict]:
        if not self.records_path.exists():
            return {}
        try:
            values = json.loads(self.records_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"cannot read {self.records_path}: {error}") from error
        if not isinstance(values, list):
            raise ValueError(f"{self.records_path} must contain a list")
        return {
            value["id"]: value for value in values
            if isinstance(value, dict) and value.get("id") in self.prompt_by_id
        }

    def _ordered_records(self) -> list[dict]:
        return sorted(
            self.records.values(),
            key=lambda record: self.prompt_order[record["id"]],
        )

    def _write_metadata(self) -> None:
        records = self._ordered_records()
        _atomic_write_text(
            self.records_path,
            json.dumps(records, ensure_ascii=False, indent=2) + "\n",
        )
        manifest_lines = [
            json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            for record in records
        ]
        _atomic_write_text(
            self.manifest_path,
            "\n".join(manifest_lines) + ("\n" if manifest_lines else ""),
        )

    def save(self, prompt_id: str, payload: bytes) -> dict:
        prompt = self.prompt_by_id.get(prompt_id)
        if prompt is None:
            raise KeyError(f"unknown prompt: {prompt_id}")
        info = inspect_wav_bytes(payload)
        if info["duration_s"] > 90:
            raise ValueError("recording is longer than 90 seconds")

        filename = f"{prompt_id}.wav"
        destination = self.audio_dir / filename
        with self._lock:
            descriptor, temporary = tempfile.mkstemp(
                dir=self.audio_dir, prefix=f".{prompt_id}.", suffix=".wav.tmp")
            temporary_path = Path(temporary)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_path, destination)
            finally:
                temporary_path.unlink(missing_ok=True)

            record = {
                "id": prompt_id,
                "audio": f"audio/{filename}",
                "text": prompt["text"],
                "split": prompt["split"],
                "category": prompt["category"],
                "duration_s": round(info["duration_s"], 6),
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
            self.records[prompt_id] = record
            self._write_metadata()
        return record

    def state(self) -> dict:
        with self._lock:
            return {
                "prompts": self.prompts,
                "saved": self._ordered_records(),
                "output_dir": str(self.output_dir),
                "manifest": str(self.manifest_path),
                "audio_contract": "16 kHz, mono, signed PCM16 little-endian WAV",
            }


class RecorderHTTPServer(ThreadingHTTPServer):
    store: RecordingStore


class RecorderHandler(BaseHTTPRequestHandler):
    server_version = "RobotASRRecorder/1.0"

    def _send_json(self, value, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        payload = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        route = urlparse(self.path).path
        if route == "/api/state":
            self._send_json(self.server.store.state())
            return
        match = re.fullmatch(r"/api/recordings/([a-z0-9-]+)", route)
        if match:
            prompt_id = match.group(1)
            if prompt_id not in self.server.store.records:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._send_file(
                self.server.store.audio_dir / f"{prompt_id}.wav", "audio/wav")
            return
        files = {
            "/": (STATIC_DIR / "index.html", "text/html; charset=utf-8"),
            "/index.html": (STATIC_DIR / "index.html", "text/html; charset=utf-8"),
            "/styles.css": (STATIC_DIR / "styles.css", "text/css; charset=utf-8"),
            "/app.js": (STATIC_DIR / "app.js", "text/javascript; charset=utf-8"),
        }
        entry = files.get(route)
        if entry is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._send_file(*entry)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        route = unquote(urlparse(self.path).path)
        match = re.fullmatch(r"/api/recordings/([a-z0-9-]+)", route)
        if not match:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if content_length <= 0:
            self._send_json({"error": "empty request body"}, HTTPStatus.BAD_REQUEST)
            return
        if content_length > MAX_AUDIO_BYTES:
            self._send_json(
                {"error": "recording exceeds 32 MB"},
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
            return
        payload = self.rfile.read(content_length)
        try:
            record = self.server.store.save(match.group(1), payload)
        except KeyError as error:
            self._send_json({"error": str(error)}, HTTPStatus.NOT_FOUND)
            return
        except ValueError as error:
            self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json(
            {
                "ok": True,
                "record": record,
                "path": str(
                    self.server.store.audio_dir / f"{record['id']}.wav"),
            },
            HTTPStatus.CREATED,
        )

    def log_message(self, message: str, *args) -> None:
        log.info("%s - %s", self.client_address[0], message % args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.home() / "Documents" / "robot-asr-dataset",
        help="directory for WAV files and manifest.jsonl",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    prompts = load_prompts()
    store = RecordingStore(args.output_dir, prompts)
    server = RecorderHTTPServer((args.host, args.port), RecorderHandler)
    server.store = store
    print("Robot ASR 录音采集器已启动")
    print(f"本地地址: http://{args.host}:{args.port}")
    print(f"录音目录: {store.output_dir}")
    print("按 Ctrl+C 停止服务")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n录音采集器已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
