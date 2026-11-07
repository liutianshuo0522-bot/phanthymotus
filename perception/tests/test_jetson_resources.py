from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


PERCEPTION_ROOT = Path(__file__).resolve().parents[1]
if str(PERCEPTION_ROOT) not in sys.path:
    sys.path.insert(0, str(PERCEPTION_ROOT))

from tools import jetson_resources  # noqa: E402


def _line(ram: int, gpu: int, cpu0: int, cpu1: int, power: int) -> str:
    return (
        f"RAM {ram}/7620MB (lfb 120x4MB) SWAP 0/3810MB (cached 0MB) "
        f"CPU [{cpu0}%@1190,{cpu1}%@1190,off,off,off,off] "
        f"EMC_FREQ 2%@1600 GR3D_FREQ {gpu}%@[306] "
        f"cpu@45.5C soc2@43C VDD_IN {power}mW/4800mW"
    )


def test_parse_tegrastats_extracts_stable_metrics():
    sample = jetson_resources.parse_tegrastats_line(
        _line(1234, 5, 10, 20, 5032))

    assert sample is not None
    assert sample["ram_used_mb"] == 1234
    assert sample["gpu_percent"] == 5
    assert sample["cpu_core_percent"] == [10, 20]
    assert sample["cpu_total_percent"] == 30
    assert sample["cpu_mean_active_percent"] == 15
    assert sample["temperatures_c"] == {"cpu": 45.5, "soc2": 43.0}
    assert sample["power"]["VDD_IN"]["current_mw"] == 5032


def test_parse_tegrastats_ignores_unrelated_lines():
    assert jetson_resources.parse_tegrastats_line("ASR startup complete") is None


def test_summary_uses_nearest_rank_percentiles():
    samples = [
        jetson_resources.parse_tegrastats_line(_line(1000 + i, i, i, i, 4000))
        for i in range(1, 21)
    ]
    summary = jetson_resources.summarize_samples(samples)

    assert summary["samples"] == 20
    assert summary["gpu_percent"] == {
        "mean": 10.5, "p50": 10.0, "p95": 19.0, "max": 20.0,
    }
    assert summary["ram_used_mb"]["max"] == 1020


def test_compare_reports_gpu_acceptance_and_idle_deltas(tmp_path):
    idle = tmp_path / "idle.log"
    replay = tmp_path / "replay.log"
    idle.write_text("\n".join([
        _line(1000, 1, 2, 3, 4000),
        _line(1005, 2, 2, 3, 4100),
    ]) + "\n")
    replay.write_text("\n".join([
        _line(1100, 4, 20, 25, 5000),
        _line(1120, 8, 25, 30, 5200),
    ]) + "\n")

    report = jetson_resources.compare_logs(idle, replay, gpu_limit=10)
    comparison = report["comparison"]

    assert comparison["gpu_p95_below_limit"] is True
    assert comparison["gpu_max_below_limit"] is True
    assert comparison["gpu_mean_delta_percent"] == 4.5
    assert comparison["ram_peak_delta_mb"] == 115


def test_empty_log_is_rejected(tmp_path):
    path = tmp_path / "empty.log"
    path.write_text("not tegrastats\n")
    with pytest.raises(ValueError, match="no tegrastats samples"):
        jetson_resources.load_log(path)


def test_three_phase_comparison_separates_resident_and_runtime_ram(tmp_path):
    idle = tmp_path / "idle.log"
    loaded = tmp_path / "loaded.log"
    replay = tmp_path / "replay.log"
    idle.write_text(_line(1000, 1, 2, 3, 4000) + "\n")
    loaded.write_text(_line(1080, 1, 2, 3, 4200) + "\n")
    replay.write_text(_line(1125, 7, 20, 25, 5100) + "\n")

    report = jetson_resources.compare_logs(
        idle, replay, gpu_limit=10, loaded_idle_path=loaded)
    comparison = report["comparison"]

    assert comparison["runtime_baseline"] == "loaded_idle"
    assert comparison["gpu_mean_delta_percent"] == 6
    assert comparison["model_loaded_ram_peak_delta_mb"] == 80
    assert comparison["replay_ram_peak_delta_from_loaded_mb"] == 45
    assert comparison["ram_peak_delta_mb"] == 45


def test_compare_command_fails_on_gpu_max_spike(monkeypatch, capsys):
    monkeypatch.setattr(jetson_resources, "compare_logs", lambda *args, **kwargs: {
        "comparison": {
            "gpu_p95_below_limit": True,
            "gpu_max_below_limit": False,
        },
    })
    args = SimpleNamespace(
        idle_log="idle.log", loaded_idle_log=None,
        replay_log="replay.log", gpu_limit=10, output=None)

    assert jetson_resources.command_compare(args) == 1
    assert "gpu_max_below_limit" in capsys.readouterr().out
