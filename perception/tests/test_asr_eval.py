from __future__ import annotations

import json
import sys
from types import SimpleNamespace
import wave
from pathlib import Path

import pytest


PERCEPTION_ROOT = Path(__file__).resolve().parents[1]
if str(PERCEPTION_ROOT) not in sys.path:
    sys.path.insert(0, str(PERCEPTION_ROOT))

from tools import asr_eval  # noqa: E402


def _wav(path: Path, frames: int = 1600, sample_rate: int = 16000,
         channels: int = 1, sample_width: int = 2) -> Path:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(sample_width)
        stream.setframerate(sample_rate)
        stream.writeframes(b"\x00" * frames * channels * sample_width)
    return path


def test_inspect_wav_accepts_the_platform_contract(tmp_path):
    info = asr_eval.inspect_wav(_wav(tmp_path / "ok.wav"))
    assert info.sample_rate == 16000
    assert info.channels == 1
    assert info.sample_width == 2
    assert info.duration_s == pytest.approx(0.1)


@pytest.mark.parametrize("kwargs, message", [
    ({"sample_rate": 8000}, "sample_rate=8000"),
    ({"channels": 2}, "channels=2"),
    ({"sample_width": 1}, "sample_width=1"),
])
def test_inspect_wav_rejects_wrong_audio(tmp_path, kwargs, message):
    with pytest.raises(ValueError, match=message):
        asr_eval.inspect_wav(_wav(tmp_path / "bad.wav", **kwargs))


def test_pcm_chunks_are_never_below_the_platform_minimum(tmp_path):
    path = _wav(tmp_path / "odd-end.wav", frames=600)
    chunks = list(asr_eval.iter_pcm_chunks(path))
    assert [len(chunk) for chunk in chunks] == [1024, 1024]
    assert chunks[-1].endswith(b"\x00" * (1024 - 176))


def test_normalize_text_is_explicit_and_repeatable():
    assert asr_eval.normalize_text(" 小范，小范！ＡI １２3。") == "小范小范ai123"


@pytest.mark.parametrize("reference, hypothesis, expected", [
    ("机器人", "机器人", (0, 0, 0)),
    ("机器人", "机气人", (1, 0, 0)),
    ("机器人", "机器", (0, 1, 0)),
    ("机器", "机器人", (0, 0, 1)),
])
def test_edit_counts(reference, hypothesis, expected):
    got = asr_eval.edit_counts(reference, hypothesis)
    assert (got["substitutions"], got["deletions"], got["insertions"]) == expected


def test_score_reports_micro_cer_rtf_latency_and_failures(tmp_path):
    audio = _wav(tmp_path / "sample.wav", frames=16000)
    manifest = [
        {"id": "speech", "audio": str(audio), "_audio_path": audio,
         "text": "机器人前进"},
        {"id": "silence", "audio": str(audio), "_audio_path": audio,
         "text": ""},
    ]
    predictions = [
        {"id": "speech", "text": "机器人前近", "decode_ms": 100,
         "audio_duration_s": 1, "endpoint_latency_ms": 250},
        {"id": "silence", "text": "你好", "decode_ms": 100,
         "audio_duration_s": 1, "endpoint_latency_ms": 500},
    ]
    summary, details = asr_eval.score(manifest, predictions)
    assert summary["reference_chars"] == 5
    assert summary["substitutions"] == 1
    assert summary["insertions"] == 2
    assert summary["cer"] == pytest.approx(0.6)
    assert summary["rtf"] == pytest.approx(0.1)
    assert summary["endpoint_latency_ms_p50"] == 250
    assert summary["endpoint_latency_ms_p95"] == 500
    assert summary["silence_false_outputs"] == 1
    assert len(details) == 2


def test_manifest_paths_are_relative_to_the_manifest(tmp_path):
    _wav(tmp_path / "audio.wav")
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        json.dumps({"id": "one", "audio": "audio.wav", "text": "你好"},
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    loaded = asr_eval.load_manifest(manifest)
    assert loaded[0]["_audio_path"] == (tmp_path / "audio.wav").resolve()


def test_submission_check_sees_unignored_large_files(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "small.txt").write_text("ok", encoding="utf-8")
    (tmp_path / "model.onnx").write_bytes(b"0" * 101)
    violations = asr_eval.submission_violations(tmp_path, max_bytes=100)
    assert violations == [{"path": "model.onnx", "bytes": 101}]


def test_demo_reports_transcript_latency_and_rtf(monkeypatch, tmp_path, capsys):
    audio = _wav(tmp_path / "demo.wav", frames=16000)

    class _Adapter:
        def transcribe(self, wav_bytes, language):
            assert wav_bytes.startswith(b"RIFF")
            assert language == "zh-CN"
            return "机器人向前走"

    monkeypatch.setattr(
        asr_eval, "_load_runtime_adapter",
        lambda model, model_dir, num_threads: _Adapter(),
    )
    monkeypatch.setattr(
        asr_eval.time, "perf_counter",
        iter([0.0, 0.1, 1.0, 1.05, 2.0, 2.06, 3.0, 3.07]).__next__,
    )
    args = SimpleNamespace(
        audio=str(audio), model="zipformer-zh-14m", model_dir=None,
        num_threads=2, language="zh-CN", repeat=3, output=None,
    )

    assert asr_eval.command_demo(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["text"] == "机器人向前走"
    assert result["model_load_ms"] == pytest.approx(100)
    assert result["decode_ms_runs"] == pytest.approx([50, 60, 70])
    assert result["decode_ms_median"] == pytest.approx(60)
    assert result["rtf_median"] == pytest.approx(0.06)
    assert result["transcript_consistent"] is True


def test_demo_rejects_zero_repeats(tmp_path):
    args = SimpleNamespace(
        audio=str(_wav(tmp_path / "demo.wav")), repeat=0,
    )
    with pytest.raises(ValueError, match="repeat must be at least 1"):
        asr_eval.command_demo(args)


def test_standalone_model_dir_uses_an_external_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert asr_eval._standalone_model_dir(None) == (
        tmp_path / "robot-asr" / "models" / "zipformer-zh-14m")


def test_standalone_model_dir_honours_an_explicit_path(tmp_path):
    expected = tmp_path / "model"
    assert asr_eval._standalone_model_dir(str(expected)) == expected.resolve()
