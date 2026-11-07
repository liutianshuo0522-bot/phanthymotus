#!/usr/bin/env python3
"""Record, summarize and compare NVIDIA Jetson tegrastats logs."""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import statistics
import subprocess
import time
from pathlib import Path


_RAM_RE = re.compile(r"\bRAM\s+(\d+)/(\d+)MB")
_SWAP_RE = re.compile(r"\bSWAP\s+(\d+)/(\d+)MB")
_CPU_RE = re.compile(r"\bCPU\s+\[([^]]*)]")
_GPU_RE = re.compile(r"\bGR3D_FREQ\s+(\d+(?:\.\d+)?)%")
_TEMP_RE = re.compile(r"\b([A-Za-z][\w-]*)@(-?\d+(?:\.\d+)?)C")
_POWER_RE = re.compile(r"\b([A-Z][A-Z0-9_]+)\s+(\d+)mW/(\d+)mW")


def parse_tegrastats_line(line: str) -> dict | None:
    """Parse stable resource fields while tolerating JetPack-specific extras."""
    ram = _RAM_RE.search(line)
    cpu = _CPU_RE.search(line)
    gpu = _GPU_RE.search(line)
    if not any((ram, cpu, gpu)):
        return None
    result: dict = {"raw": line.rstrip("\n")}
    if ram:
        result.update(ram_used_mb=int(ram.group(1)), ram_total_mb=int(ram.group(2)))
    swap = _SWAP_RE.search(line)
    if swap:
        result.update(
            swap_used_mb=int(swap.group(1)), swap_total_mb=int(swap.group(2)))
    if gpu:
        result["gpu_percent"] = float(gpu.group(1))
    if cpu:
        percentages = []
        for entry in cpu.group(1).split(","):
            match = re.search(r"(\d+(?:\.\d+)?)%", entry)
            if match:
                percentages.append(float(match.group(1)))
        result["cpu_core_percent"] = percentages
        result["cpu_total_percent"] = sum(percentages)
        result["cpu_mean_active_percent"] = (
            statistics.fmean(percentages) if percentages else 0.0)
        result["cpu_active_cores"] = len(percentages)
    temperatures = {
        name: float(value) for name, value in _TEMP_RE.findall(line)
    }
    if temperatures:
        result["temperatures_c"] = temperatures
    power = {
        name: {"current_mw": int(current), "average_mw": int(average)}
        for name, current, average in _POWER_RE.findall(line)
    }
    if power:
        result["power"] = power
    return result


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def _metric(values: list[float]) -> dict | None:
    if not values:
        return None
    return {
        "mean": round(statistics.fmean(values), 3),
        "p50": round(_percentile(values, 0.50), 3),
        "p95": round(_percentile(values, 0.95), 3),
        "max": round(max(values), 3),
    }


def summarize_samples(samples: list[dict]) -> dict:
    summary = {
        "samples": len(samples),
        "gpu_percent": _metric([
            item["gpu_percent"] for item in samples if "gpu_percent" in item]),
        "cpu_total_percent": _metric([
            item["cpu_total_percent"]
            for item in samples if "cpu_total_percent" in item]),
        "cpu_mean_active_percent": _metric([
            item["cpu_mean_active_percent"]
            for item in samples if "cpu_mean_active_percent" in item]),
        "ram_used_mb": _metric([
            item["ram_used_mb"] for item in samples if "ram_used_mb" in item]),
        "swap_used_mb": _metric([
            item["swap_used_mb"] for item in samples if "swap_used_mb" in item]),
    }
    temperature_names = sorted({
        name for item in samples for name in item.get("temperatures_c", {})
    })
    summary["temperatures_c"] = {
        name: _metric([
            item["temperatures_c"][name]
            for item in samples if name in item.get("temperatures_c", {})])
        for name in temperature_names
    }
    power_names = sorted({
        name for item in samples for name in item.get("power", {})
    })
    summary["power_current_mw"] = {
        name: _metric([
            item["power"][name]["current_mw"]
            for item in samples if name in item.get("power", {})])
        for name in power_names
    }
    return summary


def load_log(path: str | Path) -> list[dict]:
    samples = []
    with Path(path).open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            sample = parse_tegrastats_line(line)
            if sample is not None:
                samples.append(sample)
    if not samples:
        raise ValueError(f"no tegrastats samples found in {path}")
    return samples


def compare_logs(idle_path: str | Path, load_path: str | Path,
                 gpu_limit: float,
                 loaded_idle_path: str | Path | None = None) -> dict:
    idle = summarize_samples(load_log(idle_path))
    load = summarize_samples(load_log(load_path))
    loaded_idle = (
        summarize_samples(load_log(loaded_idle_path))
        if loaded_idle_path is not None else None
    )
    comparison_baseline = loaded_idle or idle
    baseline_gpu = comparison_baseline.get("gpu_percent") or {}
    load_gpu = load.get("gpu_percent") or {}
    load_ram = load.get("ram_used_mb") or {}
    baseline_ram = comparison_baseline.get("ram_used_mb") or {}
    idle_ram = idle.get("ram_used_mb") or {}
    loaded_ram = (loaded_idle or {}).get("ram_used_mb") or {}
    return {
        "idle": idle,
        "loaded_idle": loaded_idle,
        "replay": load,
        "comparison": {
            "runtime_baseline": (
                "loaded_idle" if loaded_idle is not None else "idle"),
            "gpu_limit_percent": gpu_limit,
            "gpu_p95_below_limit": (
                load_gpu.get("p95") < gpu_limit
                if load_gpu.get("p95") is not None else None
            ),
            "gpu_max_below_limit": (
                load_gpu.get("max") < gpu_limit
                if load_gpu.get("max") is not None else None
            ),
            "gpu_mean_delta_percent": (
                round(load_gpu["mean"] - baseline_gpu["mean"], 3)
                if "mean" in load_gpu and "mean" in baseline_gpu else None
            ),
            "ram_peak_delta_mb": (
                round(load_ram["max"] - baseline_ram["max"], 3)
                if "max" in load_ram and "max" in baseline_ram else None
            ),
            "model_loaded_ram_peak_delta_mb": (
                round(loaded_ram["max"] - idle_ram["max"], 3)
                if "max" in loaded_ram and "max" in idle_ram else None
            ),
            "replay_ram_peak_delta_from_loaded_mb": (
                round(load_ram["max"] - loaded_ram["max"], 3)
                if "max" in load_ram and "max" in loaded_ram else None
            ),
        },
    }


def _write_json(path: str | Path, value) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def command_record(args) -> int:
    binary = shutil.which(args.tegrastats)
    if binary is None:
        raise RuntimeError(f"tegrastats executable not found: {args.tegrastats}")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with output.open("w", encoding="utf-8") as stream:
        process = subprocess.Popen(
            [binary, "--interval", str(args.interval_ms)],
            stdout=stream,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            if args.duration_s > 0:
                deadline = time.monotonic() + args.duration_s
                while time.monotonic() < deadline and process.poll() is None:
                    time.sleep(min(0.5, deadline - time.monotonic()))
                if process.poll() is not None and time.monotonic() < deadline:
                    raise RuntimeError(
                        f"tegrastats exited early with code {process.returncode}")
            else:
                while process.poll() is None:
                    time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
    result = {
        "log": str(output.resolve()),
        "duration_s": round(time.monotonic() - started, 3),
        "interval_ms": args.interval_ms,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_summarize(args) -> int:
    report = {"summary": summarize_samples(load_log(args.log))}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.output:
        _write_json(args.output, report)
    return 0


def command_compare(args) -> int:
    report = compare_logs(
        args.idle_log, args.replay_log, args.gpu_limit,
        loaded_idle_path=args.loaded_idle_log)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.output:
        _write_json(args.output, report)
    p95_pass = report["comparison"]["gpu_p95_below_limit"]
    max_pass = report["comparison"]["gpu_max_below_limit"]
    return int(p95_pass is False or max_pass is False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    record = commands.add_parser("record", help="record tegrastats stdout")
    record.add_argument("--output", required=True)
    record.add_argument("--duration-s", type=float, default=0,
                        help="0 records until Ctrl-C")
    record.add_argument("--interval-ms", type=int, default=1_000)
    record.add_argument("--tegrastats", default="tegrastats")
    record.set_defaults(handler=command_record)

    summarize = commands.add_parser("summarize", help="summarize one log")
    summarize.add_argument("--log", required=True)
    summarize.add_argument("--output")
    summarize.set_defaults(handler=command_summarize)

    compare = commands.add_parser("compare", help="compare idle and replay logs")
    compare.add_argument("--idle-log", required=True)
    compare.add_argument(
        "--loaded-idle-log",
        help="optional ASR-model-loaded idle phase for resident/runtime deltas")
    compare.add_argument("--replay-log", required=True)
    compare.add_argument("--gpu-limit", type=float, default=10.0)
    compare.add_argument("--output")
    compare.set_defaults(handler=command_compare)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if getattr(args, "duration_s", 0) < 0:
            raise ValueError("duration_s must be non-negative")
        if getattr(args, "interval_ms", 1) < 1:
            raise ValueError("interval_ms must be positive")
        if getattr(args, "gpu_limit", 1) <= 0:
            raise ValueError("gpu_limit must be positive")
        return args.handler(args)
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(2, f"error: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
