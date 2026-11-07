from __future__ import annotations

import io
import json
import sys
import wave
from pathlib import Path

import pytest


PERCEPTION_ROOT = Path(__file__).resolve().parents[1]
if str(PERCEPTION_ROOT) not in sys.path:
    sys.path.insert(0, str(PERCEPTION_ROOT))

from tools.asr_recorder import server  # noqa: E402


def _wav_bytes(sample_rate=16000, channels=1, sample_width=2, frames=1600):
    payload = io.BytesIO()
    with wave.open(payload, "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(sample_width)
        stream.setframerate(sample_rate)
        stream.writeframes(b"\x00" * frames * channels * sample_width)
    return payload.getvalue()


def _prompts():
    return [
        {
            "id": "dev-001",
            "split": "dev",
            "category": "移动指令",
            "text": "小范小范请向前走一步",
            "hint": "正常语速",
        },
        {
            "id": "test-001",
            "split": "test",
            "category": "静音测试",
            "text": "",
            "hint": "保持安静",
        },
    ]


def test_prompts_are_valid_and_have_fixed_dev_test_split():
    prompts = server.load_prompts()
    assert len(prompts) == 30
    assert sum(prompt["split"] == "dev" for prompt in prompts) == 10
    assert sum(prompt["split"] == "test" for prompt in prompts) == 20
    assert len({prompt["id"] for prompt in prompts}) == 30


def test_inspect_wav_accepts_platform_audio_contract():
    info = server.inspect_wav_bytes(_wav_bytes())
    assert info["sample_rate"] == 16000
    assert info["channels"] == 1
    assert info["duration_s"] == pytest.approx(0.1)


@pytest.mark.parametrize("kwargs, message", [
    ({"sample_rate": 48000}, "sample rate"),
    ({"channels": 2}, "channels"),
    ({"sample_width": 1}, "sample width"),
])
def test_inspect_wav_rejects_wrong_format(kwargs, message):
    with pytest.raises(ValueError, match=message):
        server.inspect_wav_bytes(_wav_bytes(**kwargs))


def test_store_writes_audio_and_reproducible_manifest(tmp_path):
    store = server.RecordingStore(tmp_path, _prompts())
    record = store.save("dev-001", _wav_bytes(frames=3200))

    assert record["duration_s"] == pytest.approx(0.2)
    assert (tmp_path / "audio" / "dev-001.wav").is_file()
    manifest = [
        json.loads(line)
        for line in (tmp_path / "manifest.jsonl").read_text(
            encoding="utf-8").splitlines()
    ]
    assert len(manifest) == 1
    assert manifest[0]["id"] == "dev-001"
    assert manifest[0]["audio"] == "audio/dev-001.wav"
    assert manifest[0]["text"] == "小范小范请向前走一步"


def test_store_overwrites_one_prompt_without_duplicate_manifest_rows(tmp_path):
    store = server.RecordingStore(tmp_path, _prompts())
    store.save("dev-001", _wav_bytes(frames=1600))
    store.save("dev-001", _wav_bytes(frames=4800))

    lines = (tmp_path / "manifest.jsonl").read_text(
        encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["duration_s"] == pytest.approx(0.3)


def test_store_rejects_unknown_prompt(tmp_path):
    store = server.RecordingStore(tmp_path, _prompts())
    with pytest.raises(KeyError, match="unknown prompt"):
        store.save("../../escape", _wav_bytes())
