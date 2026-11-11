#!/usr/bin/env python3
"""Run a local browser-microphone VAD/ASR demo without ROS2.

The browser captures one microphone continuously and sends PCM16 audio in
1024-byte blocks.  This server keeps one VAD/ASR session warm, returns completed
utterances immediately, and exposes a robot-style wake/background routing
decision for the demo UI.
"""

from __future__ import annotations

import argparse
import json
import logging
import struct
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"
PERCEPTION_ROOT = HERE.parents[1]
TOOLS_ROOT = HERE.parent
for candidate in (PERCEPTION_ROOT, TOOLS_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from plugins.vad_preroll import PcmHistory  # noqa: E402
from tools.asr_eval import (  # noqa: E402
    _load_replay_vad,
    _load_runtime_adapter,
    _pcm16_to_wav,
)
from utils.asr_vad import BUFFER_S  # noqa: E402


SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2
CHUNK_BYTES = 1_024
CHUNK_SAMPLES = CHUNK_BYTES // SAMPLE_WIDTH
MAX_BATCH_BYTES = CHUNK_BYTES * 64
MAX_BODY_BYTES = 64 * 1024

log = logging.getLogger("asr-live")

try:
    from pypinyin import Style, lazy_pinyin
except ImportError:  # exact matching remains available without the optional helper
    Style = None
    lazy_pinyin = None


def _normalized_chars(text: str) -> tuple[list[str], list[int]]:
    """Return comparable characters and their exclusive offsets in ``text``."""
    chars: list[str] = []
    ends: list[int] = []
    for index, source in enumerate(str(text or "")):
        folded = unicodedata.normalize("NFKC", source).casefold()
        for character in folded:
            if character.isalnum():
                chars.append(character)
                ends.append(index + 1)
    return chars, ends


def _edit_distance(left: list[str], right: list[str]) -> float:
    if not left or not right:
        return 1.0
    previous = list(range(len(right) + 1))
    for row, left_value in enumerate(left, 1):
        current = [row]
        for column, right_value in enumerate(right, 1):
            current.append(min(
                current[-1] + 1,
                previous[column] + 1,
                previous[column - 1] + (left_value != right_value),
            ))
        previous = current
    return previous[-1] / max(len(left), len(right))


def _pinyin_units(chars: list[str]) -> list[str] | None:
    if lazy_pinyin is None or Style is None:
        return None
    try:
        return lazy_pinyin(
            "".join(chars), style=Style.NORMAL, neutral_tone_with_five=False,
            errors=lambda value: list(value.casefold()),
        )
    except Exception:  # optional demo aid must never break ASR
        return None


def wake_aliases(wake_word: str) -> list[str]:
    """Return single/repeated wake aliases, longest first.

    The default ``小范小范`` therefore also accepts ``小范``.  If a user
    configures the shorter form instead, its repeated form is accepted too.
    Longest-first ordering is important: ``小范小范请前进`` must strip both
    wake repetitions instead of routing ``小范请前进`` to core.
    """
    chars, _ = _normalized_chars(wake_word)
    if not chars:
        return []
    variants = [chars]
    midpoint = len(chars) // 2
    if len(chars) % 2 == 0 and chars[:midpoint] == chars[midpoint:]:
        variants.append(chars[:midpoint])
    else:
        variants.append(chars + chars)

    unique = {"".join(variant) for variant in variants if variant}
    return sorted(unique, key=lambda value: (-len(value), value))


def route_transcript(text: str, wake_word: str,
                     threshold: float = 0.3) -> dict:
    """Route one transcript to core/background using text or pinyin matching.

    Production uses the plugin's IPA matcher.  The local Mac demo uses exact
    normalized text first and optional pinyin matching second, which handles the
    common 小范/小贩/小凡 homophone without requiring the ROS2 image's espeak
    runtime.  The method is included in the result so the distinction is visible.
    """
    raw_text = str(text or "").strip()
    text_chars, text_ends = _normalized_chars(raw_text)
    aliases = wake_aliases(wake_word)
    if not aliases:
        return {
            "raw_text": raw_text,
            "wake_word": "",
            "wake_matched": True,
            "wake_match_method": "disabled",
            "command_text": raw_text,
            "route": "core" if raw_text else "none",
            "send_to_core": bool(raw_text),
        }

    # Try the longest alias first.  A shorter exact match must not win before
    # an acceptable repeated-form match, otherwise one wake repetition leaks
    # into the command sent to core.
    best: tuple[float, int, str, str] | None = None
    for alias in aliases:
        keyword_chars = list(alias)
        length = len(keyword_chars)
        keyword_pinyin = _pinyin_units(keyword_chars)
        alias_best: tuple[float, int, str, str] | None = None
        for begin in range(0, len(text_chars) - length + 1):
            end = begin + length
            window = text_chars[begin:end]
            if window == keyword_chars:
                alias_best = (0.0, end, "exact", alias)
                break
            window_pinyin = _pinyin_units(window)
            if keyword_pinyin and window_pinyin:
                distance = _edit_distance(window_pinyin, keyword_pinyin)
                candidate = (distance, end, "pinyin_fuzzy", alias)
                if alias_best is None or candidate[:2] < alias_best[:2]:
                    alias_best = candidate
        if alias_best and alias_best[0] <= threshold:
            best = alias_best
            break

    if best is None:
        return {
            "raw_text": raw_text,
            "wake_word": wake_word,
            "wake_matched": False,
            "wake_match_method": "none",
            "command_text": "",
            "route": "background" if raw_text else "none",
            "send_to_core": False,
        }

    cut = text_ends[best[1] - 1]
    command = raw_text[cut:].lstrip("，。！？、；：,.!?;: ")
    return {
        "raw_text": raw_text,
        "wake_word": wake_word,
        "wake_matched": True,
        "wake_match_method": best[2],
        "wake_distance": round(best[0], 3),
        "wake_variant": best[3],
        "wake_aliases": aliases,
        "command_text": command,
        "route": "core" if command else "wake_only",
        "send_to_core": bool(command),
    }


def local_text_reply(route: dict) -> str:
    """Return an offline text-only acknowledgement for the Mac demo.

    This is deliberately identified as a local dialogue policy, not an LLM.
    A fixed wake acknowledgement should not add a cloud/model dependency to an
    ASR evaluation, and the field can later be replaced by agent-core output.
    """
    if route.get("route_reason") == "waiting_command":
        return "在呢，需要我做什么吗？"
    if route.get("send_to_core"):
        command = str(route.get("command_text") or "").strip()
        command = command.lstrip("，。！？、；：,.!?;: ")
        if command.startswith("请"):
            command = command[1:].lstrip()
        if command:
            return f"好的，已收到任务：{command.rstrip('，。！？、；：,.!?;: ')}。"
    return ""


@dataclass
class LiveSettings:
    wake_word: str = "小范小范"
    wake_threshold: float = 0.3
    vad_silence_ms: int = 400
    vad_pre_roll_ms: int = 500
    flush_ms: int = 2_000
    wake_hold_ms: int = 10_000
    language: str = "zh-CN"


class LiveRecognizer:
    """Stateful microphone stream using the same VAD and ASR adapters as replay."""

    def __init__(self, adapter, vad, settings: LiveSettings,
                 capture_started_at: float | None = None,
                 clock=None):
        self.adapter = adapter
        self.vad = vad
        self.settings = settings
        self.clock = clock or time.time
        self.capture_started_at = (
            capture_started_at
            if capture_started_at is not None
            else self.clock()
        )
        self.history = PcmHistory(SAMPLE_RATE * (BUFFER_S + 1))
        self.pre_roll_samples = int(
            SAMPLE_RATE * settings.vad_pre_roll_ms / 1000)
        self.silence_samples = int(
            SAMPLE_RATE * settings.vad_silence_ms / 1000)
        self.vad.reset()
        self.stream_samples = 0
        self.input_samples = 0
        self.chunks_received = 0
        self.flush_chunks = 0
        self.bytes_received = 0
        self.segment_count = 0
        self.discarded_short_segments = 0
        self.speech_active = False
        self.wake_pending_until_s = 0.0

    def snapshot(self) -> dict:
        audio_time_s = self.stream_samples / SAMPLE_RATE
        wake_pending = self.wake_pending_until_s > audio_time_s
        return {
            "active": True,
            "speech_active": self.speech_active,
            "chunks_received": self.chunks_received,
            "bytes_received": self.bytes_received,
            "audio_duration_s": round(self.input_samples / SAMPLE_RATE, 3),
            "segment_count": self.segment_count,
            "discarded_short_segments": self.discarded_short_segments,
            "flush_chunks": self.flush_chunks,
            "wake_pending": wake_pending,
            "wake_remaining_ms": round(max(
                0.0, (self.wake_pending_until_s - audio_time_s) * 1000), 1),
            "wake_hold_ms": self.settings.wake_hold_ms,
            "chunk_bytes": CHUNK_BYTES,
            "chunk_duration_ms": 32,
        }

    def accept_batch(self, payload: bytes) -> list[dict]:
        if not payload:
            raise ValueError("audio batch is empty")
        if len(payload) > MAX_BATCH_BYTES:
            raise ValueError(
                f"audio batch exceeds {MAX_BATCH_BYTES} bytes")
        if len(payload) % CHUNK_BYTES:
            raise ValueError(
                f"audio batch must contain exact {CHUNK_BYTES}-byte chunks")
        events: list[dict] = []
        for offset in range(0, len(payload), CHUNK_BYTES):
            events.extend(self._accept_chunk(payload[offset:offset + CHUNK_BYTES]))
        return events

    def _accept_chunk(self, pcm: bytes, *, artificial: bool = False) -> list[dict]:
        if len(pcm) != CHUNK_BYTES:
            raise ValueError(f"live chunk must be exactly {CHUNK_BYTES} bytes")
        samples = struct.unpack(f"<{CHUNK_SAMPLES}h", pcm)
        self.history.append(pcm)
        self.vad.accept_waveform([sample / 32768.0 for sample in samples])
        self.stream_samples += CHUNK_SAMPLES
        if artificial:
            self.flush_chunks += 1
        else:
            self.input_samples += CHUNK_SAMPLES
            self.chunks_received += 1
            self.bytes_received += len(pcm)

        events: list[dict] = []
        detected = bool(self.vad.is_speech_detected())
        if detected and not self.speech_active:
            events.append({
                "type": "speech_start",
                "audio_time_s": round(self.stream_samples / SAMPLE_RATE, 3),
            })
        self.speech_active = detected
        events.extend(self._drain_segments())
        self.speech_active = bool(self.vad.is_speech_detected())
        return events

    def _drain_segments(self) -> list[dict]:
        events: list[dict] = []
        while not self.vad.empty():
            segment = self.vad.front
            float_samples = list(segment.samples)
            segment_start = getattr(segment, "start", None)
            if segment_start is None:
                segment_start = max(
                    0,
                    self.stream_samples - len(float_samples) - self.silence_samples,
                )
            segment_start = int(segment_start)
            segment_end = segment_start + len(float_samples)
            segment_pcm = struct.pack(
                f"<{len(float_samples)}h",
                *[
                    int(max(-32768, min(32767, sample * 32768)))
                    for sample in float_samples
                ],
            )
            pre_roll = self.history.slice(
                segment_start - self.pre_roll_samples, segment_start)
            utterance = pre_roll + segment_pcm
            self.vad.pop()

            # Match the production worker's >500 ms gate.
            if len(utterance) <= SAMPLE_RATE:
                self.discarded_short_segments += 1
                continue

            decode_started = self.clock()
            text = self.adapter.transcribe(
                _pcm16_to_wav(utterance), self.settings.language)
            decode_finished = self.clock()
            emitted_at_s = self.stream_samples / SAMPLE_RATE
            route = self._route_with_wake_session(
                str(text or ""),
                segment_start / SAMPLE_RATE,
                emitted_at_s,
            )
            speech_end_ts = self.capture_started_at + segment_end / SAMPLE_RATE
            self.segment_count += 1
            events.append({
                "type": "transcript",
                "index": self.segment_count,
                **route,
                "speech_start_s": round(segment_start / SAMPLE_RATE, 3),
                "speech_end_s": round(segment_end / SAMPLE_RATE, 3),
                "utterance_duration_s": round(
                    len(utterance) / SAMPLE_WIDTH / SAMPLE_RATE, 3),
                "pre_roll_ms": round(
                    len(pre_roll) / SAMPLE_WIDTH / SAMPLE_RATE * 1000, 1),
                "decode_ms": round((decode_finished - decode_started) * 1000, 1),
                "endpoint_latency_ms": round(max(
                    0.0, (decode_finished - speech_end_ts) * 1000), 1),
            })
        return events

    def _route_with_wake_session(
        self,
        text: str,
        segment_start_s: float,
        emitted_at_s: float,
    ) -> dict:
        """Apply one-command wake memory on top of transcript matching."""
        route = route_transcript(
            text, self.settings.wake_word, self.settings.wake_threshold)
        pending_at_speech_start = (
            self.wake_pending_until_s > 0
            and segment_start_s <= self.wake_pending_until_s
        )

        if route["wake_matched"]:
            if route["route"] == "wake_only":
                self.wake_pending_until_s = (
                    emitted_at_s + self.settings.wake_hold_ms / 1000)
                route.update({
                    "wake_session_active": True,
                    "wake_session_remaining_ms": self.settings.wake_hold_ms,
                    "route_reason": "waiting_command",
                })
            else:
                # A command in the same utterance consumes the wake immediately.
                self.wake_pending_until_s = 0.0
                route.update({
                    "wake_session_active": False,
                    "wake_session_remaining_ms": 0,
                    "route_reason": "wake_and_command",
                })
            return self._with_text_reply(route)

        if route["raw_text"] and pending_at_speech_start:
            # Consume the pending wake after exactly one non-empty command.
            self.wake_pending_until_s = 0.0
            route.update({
                "wake_session_active": True,
                "wake_session_remaining_ms": 0,
                "wake_match_method": "wake_session",
                "command_text": route["raw_text"],
                "route": "core",
                "send_to_core": True,
                "route_reason": "deferred_command",
            })
            return self._with_text_reply(route)

        if emitted_at_s > self.wake_pending_until_s:
            self.wake_pending_until_s = 0.0
        route.update({
            "wake_session_active": False,
            "wake_session_remaining_ms": 0,
            "route_reason": "no_wake",
        })
        return self._with_text_reply(route)

    @staticmethod
    def _with_text_reply(route: dict) -> dict:
        reply = local_text_reply(route)
        route["assistant_reply"] = reply
        route["reply_source"] = "local_dialogue_policy" if reply else "none"
        return route

    def flush(self) -> list[dict]:
        """Close a pending VAD segment after microphone capture stops."""
        events: list[dict] = []
        if not self.vad.is_speech_detected():
            return events
        limit = max(0, int(
            (self.settings.flush_ms + 31) // 32))
        silence = b"\x00" * CHUNK_BYTES
        for _ in range(limit):
            events.extend(self._accept_chunk(silence, artificial=True))
            if not self.vad.is_speech_detected():
                break
        return events


class LiveASRService:
    def __init__(self, adapter, vad, settings: LiveSettings):
        self.adapter = adapter
        self.vad = vad
        self.settings = settings
        self.session: LiveRecognizer | None = None
        self.lock = threading.RLock()

    def state(self) -> dict:
        with self.lock:
            snapshot = self.session.snapshot() if self.session else {"active": False}
            return {
                "ready": True,
                "sample_rate": SAMPLE_RATE,
                "sample_width": SAMPLE_WIDTH,
                "channels": 1,
                "chunk_bytes": CHUNK_BYTES,
                "chunk_duration_ms": 32,
                "wake_word": self.settings.wake_word,
                "wake_aliases": wake_aliases(self.settings.wake_word),
                "wake_threshold": self.settings.wake_threshold,
                "wake_hold_ms": self.settings.wake_hold_ms,
                "pinyin_available": lazy_pinyin is not None,
                "session": snapshot,
            }

    def start(self, wake_word: str,
              capture_started_at: float | None) -> dict:
        with self.lock:
            settings = LiveSettings(**vars(self.settings))
            settings.wake_word = wake_word.strip()
            self.session = LiveRecognizer(
                self.adapter, self.vad, settings, capture_started_at)
            return self.state()

    def accept(self, payload: bytes) -> dict:
        with self.lock:
            if self.session is None:
                raise RuntimeError("live session has not been started")
            events = self.session.accept_batch(payload)
            return {"events": events, "session": self.session.snapshot()}

    def stop(self) -> dict:
        with self.lock:
            if self.session is None:
                return {"events": [], "session": {"active": False}}
            events = self.session.flush()
            snapshot = self.session.snapshot()
            snapshot["active"] = False
            self.session = None
            return {"events": events, "session": snapshot}


class LiveHTTPServer(ThreadingHTTPServer):
    service: LiveASRService


class LiveHandler(BaseHTTPRequestHandler):
    server_version = "RobotASRLive/1.0"

    def _json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("invalid Content-Length") from error
        if length < 0 or length > MAX_BODY_BYTES:
            raise ValueError("request body is too large")
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("request body must be JSON") from error
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def _binary_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("invalid Content-Length") from error
        if length <= 0:
            raise ValueError("audio request body is empty")
        if length > MAX_BATCH_BYTES:
            raise ValueError("audio request body is too large")
        return self.rfile.read(length)

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
            self._send_json(self.server.service.state())
            return
        files = {
            "/": (STATIC_DIR / "index.html", "text/html; charset=utf-8"),
            "/index.html": (STATIC_DIR / "index.html", "text/html; charset=utf-8"),
            "/app.js": (STATIC_DIR / "app.js", "text/javascript; charset=utf-8"),
            "/styles.css": (STATIC_DIR / "styles.css", "text/css; charset=utf-8"),
        }
        entry = files.get(route)
        if entry is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._send_file(*entry)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        route = urlparse(self.path).path
        try:
            if route == "/api/live/start":
                body = self._json_body()
                wake_word = str(body.get(
                    "wake_word", self.server.service.settings.wake_word))
                capture_ms = body.get("capture_started_at_ms")
                capture_started_at = (
                    float(capture_ms) / 1000 if capture_ms is not None else None)
                self._send_json({
                    "ok": True,
                    **self.server.service.start(wake_word, capture_started_at),
                })
                return
            if route == "/api/live/chunks":
                self._send_json({
                    "ok": True,
                    **self.server.service.accept(self._binary_body()),
                })
                return
            if route == "/api/live/stop":
                self._json_body()
                self._send_json({"ok": True, **self.server.service.stop()})
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except (RuntimeError, ValueError) as error:
            self._send_json(
                {"ok": False, "error": str(error)}, HTTPStatus.BAD_REQUEST)
        except Exception as error:  # keep the local server alive for a retry
            log.exception("live request failed")
            self._send_json(
                {"ok": False, "error": str(error)},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def log_message(self, message: str, *args) -> None:
        log.debug("%s - %s", self.client_address[0], message % args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--model", default="zipformer-zh-14m")
    parser.add_argument("--model-dir")
    parser.add_argument("--vad-model")
    parser.add_argument("--num-threads", type=int, default=4)
    parser.add_argument("--vad-threshold", type=float, default=0.5)
    parser.add_argument("--vad-silence-ms", type=int, default=400)
    parser.add_argument("--vad-pre-roll-ms", type=int, default=500)
    parser.add_argument("--wake-word", default="小范小范")
    parser.add_argument("--wake-threshold", type=float, default=0.3)
    parser.add_argument("--wake-hold-ms", type=int, default=10_000)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.num_threads < 1:
        raise ValueError("num-threads must be at least 1")
    if not 0 <= args.vad_threshold <= 1:
        raise ValueError("vad-threshold must be between 0 and 1")
    if not 0 <= args.wake_threshold <= 1:
        raise ValueError("wake-threshold must be between 0 and 1")
    if args.wake_hold_ms < 0:
        raise ValueError("wake-hold-ms must be non-negative")

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print("正在加载 ASR 模型…")
    load_started = time.perf_counter()
    adapter = _load_runtime_adapter(
        args.model, args.model_dir, args.num_threads)
    asr_load_ms = (time.perf_counter() - load_started) * 1000
    print("正在加载 VAD 模型…")
    load_started = time.perf_counter()
    vad = _load_replay_vad(
        args.vad_model, args.vad_threshold, args.vad_silence_ms)
    vad_load_ms = (time.perf_counter() - load_started) * 1000

    settings = LiveSettings(
        wake_word=args.wake_word,
        wake_threshold=args.wake_threshold,
        wake_hold_ms=args.wake_hold_ms,
        vad_silence_ms=args.vad_silence_ms,
        vad_pre_roll_ms=args.vad_pre_roll_ms,
    )
    service = LiveASRService(adapter, vad, settings)
    server = LiveHTTPServer((args.host, args.port), LiveHandler)
    server.service = service
    print("Robot ASR 实时 Demo 已启动")
    print(f"本地地址: http://{args.host}:{args.port}")
    print(f"ASR 加载: {asr_load_ms:.0f} ms · VAD 加载: {vad_load_ms:.0f} ms")
    print("音频契约: 16 kHz · 单声道 · PCM16 · 1024 bytes/chunk")
    if lazy_pinyin is None:
        print("提示: 未安装 pypinyin，唤醒词仅做规范化精确匹配")
    print("按 Ctrl+C 停止服务")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n实时 Demo 已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
