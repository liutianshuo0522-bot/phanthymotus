from __future__ import annotations

import sys
from pathlib import Path

import pytest


PERCEPTION_ROOT = Path(__file__).resolve().parents[1]
if str(PERCEPTION_ROOT) not in sys.path:
    sys.path.insert(0, str(PERCEPTION_ROOT))

from tools.asr_live import server as asr_live  # noqa: E402


class _Segment:
    def __init__(self, samples, start=0):
        self.samples = samples
        self.start = start


class _TailVad:
    """Stay active, then emit one segment after two artificial tail chunks."""

    def __init__(self, input_chunks=1):
        self.input_chunks = input_chunks
        self.chunk_sizes = []
        self.reset()

    def reset(self):
        self.calls = 0
        self.speaking = False
        self.queued = []

    def accept_waveform(self, samples):
        self.calls += 1
        self.chunk_sizes.append(len(samples))
        self.speaking = True
        if self.calls == self.input_chunks + 2:
            self.queued.append(_Segment([0.25] * 16_000))
            self.speaking = False

    def is_speech_detected(self):
        return self.speaking

    def empty(self):
        return not self.queued

    @property
    def front(self):
        return self.queued[0]

    def pop(self):
        self.queued.pop(0)


class _Adapter:
    def __init__(self, text="小范小范向前走"):
        self.text = text
        self.calls = []

    def transcribe(self, wav_bytes, language):
        assert wav_bytes.startswith(b"RIFF")
        self.calls.append((wav_bytes, language))
        return self.text


def test_route_transcript_strips_an_exact_wake_word_and_punctuation():
    result = asr_live.route_transcript("小范，小范！向前走", "小范小范")

    assert result["wake_matched"] is True
    assert result["wake_match_method"] == "exact"
    assert result["wake_variant"] == "小范小范"
    assert result["command_text"] == "向前走"
    assert result["route"] == "core"
    assert result["send_to_core"] is True


def test_route_transcript_accepts_single_form_of_repeated_wake_word():
    result = asr_live.route_transcript("小范请向前走", "小范小范")

    assert result["wake_matched"] is True
    assert result["wake_variant"] == "小范"
    assert result["command_text"] == "请向前走"
    assert result["route"] == "core"


def test_route_transcript_prefers_long_form_without_leaking_second_wake():
    result = asr_live.route_transcript("小范小范请向前走", "小范")

    assert result["wake_variant"] == "小范小范"
    assert result["command_text"] == "请向前走"
    assert result["wake_aliases"] == ["小范小范", "小范"]


def test_wake_only_opens_window_and_next_utterance_becomes_core():
    recognizer = asr_live.LiveRecognizer(
        _Adapter(), _TailVad(),
        asr_live.LiveSettings(wake_hold_ms=10_000),
    )

    wake = recognizer._route_with_wake_session("小范", 1.0, 1.5)
    command = recognizer._route_with_wake_session("请向前走", 5.0, 5.8)

    assert wake["route"] == "wake_only"
    assert wake["wake_session_active"] is True
    assert wake["route_reason"] == "waiting_command"
    assert wake["assistant_reply"] == "在呢，需要我做什么吗？"
    assert wake["reply_source"] == "local_dialogue_policy"
    assert command["wake_matched"] is False
    assert command["wake_session_active"] is True
    assert command["wake_match_method"] == "wake_session"
    assert command["command_text"] == "请向前走"
    assert command["route"] == "core"
    assert command["send_to_core"] is True
    assert command["assistant_reply"] == "好的，已收到任务：向前走。"
    assert recognizer.wake_pending_until_s == 0


def test_wake_window_expires_before_late_command():
    recognizer = asr_live.LiveRecognizer(
        _Adapter(), _TailVad(),
        asr_live.LiveSettings(wake_hold_ms=10_000),
    )

    recognizer._route_with_wake_session("小范", 1.0, 1.5)
    late = recognizer._route_with_wake_session("请向前走", 12.0, 12.8)

    assert late["route"] == "background"
    assert late["send_to_core"] is False
    assert late["wake_session_active"] is False
    assert late["assistant_reply"] == ""


def test_wake_and_command_in_one_utterance_does_not_leave_pending_state():
    recognizer = asr_live.LiveRecognizer(
        _Adapter(), _TailVad(), asr_live.LiveSettings())

    result = recognizer._route_with_wake_session(
        "小范请向前走", 1.0, 2.0)

    assert result["route"] == "core"
    assert result["route_reason"] == "wake_and_command"
    assert result["assistant_reply"] == "好的，已收到任务：向前走。"
    assert recognizer.wake_pending_until_s == 0


def test_route_transcript_keeps_non_wake_speech_in_background():
    result = asr_live.route_transcript("今天天气不错", "小范小范")

    assert result["wake_matched"] is False
    assert result["command_text"] == ""
    assert result["route"] == "background"
    assert result["send_to_core"] is False


def test_route_transcript_can_use_pinyin_for_a_homophone(monkeypatch):
    pronunciations = {
        "小": "xiao", "范": "fan", "贩": "fan", "向": "xiang",
        "前": "qian", "走": "zou",
    }
    monkeypatch.setattr(
        asr_live,
        "_pinyin_units",
        lambda chars: [pronunciations.get(char, char) for char in chars],
    )

    result = asr_live.route_transcript("小贩小范向前走", "小范小范")

    assert result["wake_matched"] is True
    assert result["wake_match_method"] == "pinyin_fuzzy"
    assert result["command_text"] == "向前走"
    assert result["route"] == "core"


def test_live_recognizer_rejects_non_1024_byte_input():
    recognizer = asr_live.LiveRecognizer(
        _Adapter(), _TailVad(), asr_live.LiveSettings())

    with pytest.raises(ValueError, match="exact 1024-byte chunks"):
        recognizer.accept_batch(b"\x00" * 1023)


def test_live_recognizer_runs_vad_asr_and_route_on_continuous_chunks():
    times = iter([101.1, 101.2])
    adapter = _Adapter()
    vad = _TailVad(input_chunks=1)
    recognizer = asr_live.LiveRecognizer(
        adapter,
        vad,
        asr_live.LiveSettings(flush_ms=64, vad_pre_roll_ms=0),
        capture_started_at=100.0,
        clock=times.__next__,
    )

    first_events = recognizer.accept_batch(b"\x01\x00" * 512)
    final_events = recognizer.flush()
    transcript = next(
        event for event in final_events if event["type"] == "transcript")
    snapshot = recognizer.snapshot()

    assert first_events[0]["type"] == "speech_start"
    assert vad.chunk_sizes == [512, 512, 512]
    assert len(adapter.calls) == 1
    assert adapter.calls[0][1] == "zh-CN"
    assert transcript["raw_text"] == "小范小范向前走"
    assert transcript["command_text"] == "向前走"
    assert transcript["route"] == "core"
    assert transcript["decode_ms"] == pytest.approx(100)
    assert transcript["endpoint_latency_ms"] == pytest.approx(200)
    assert snapshot["chunks_received"] == 1
    assert snapshot["bytes_received"] == 1024
    assert snapshot["audio_duration_s"] == pytest.approx(0.032)
    assert snapshot["flush_chunks"] == 2
    assert snapshot["segment_count"] == 1
