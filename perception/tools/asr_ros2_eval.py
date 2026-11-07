#!/usr/bin/env python3
"""Replay an ASR manifest through the real ROS2 AudioChunk transport.

Run this inside the perception/ROS2 environment.  Pass ``--mcp-url`` to start
and snapshot the ASR card automatically, or start it on ``--input-topic``
beforehand.  Imports of rclpy and audio_msgs are deliberately lazy so result
shaping and scoring remain unit-testable on a developer laptop.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from tools import asr_eval
except ImportError:  # direct execution: this script's directory is sys.path[0]
    import asr_eval


@dataclass(frozen=True)
class CapturedResult:
    topic: str
    payload: dict
    received_ts: float
    received_monotonic: float


def _number(value) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if math.isfinite(number):
            return number
    return None


def _span_duration_ms(payload: dict, name: str) -> float:
    total = 0.0
    for span in payload.get("spans", ()):
        if not isinstance(span, dict) or span.get("span") != name:
            continue
        start = _number(span.get("start_ts"))
        end = _number(span.get("end_ts"))
        if start is not None and end is not None and end >= start:
            total += (end - start) * 1000
    return total


def _segment_from_result(result: CapturedResult, index: int) -> dict:
    payload = result.payload
    audio_start = _number(payload.get("audio_start_ts"))
    audio_end = _number(payload.get("audio_end_ts"))
    complete = _number(payload.get("asr_complete_ts"))
    endpoint_ms = (
        max(0.0, (result.received_ts - audio_end) * 1000)
        if audio_end is not None else None
    )
    delivery_ms = (
        max(0.0, (result.received_ts - complete) * 1000)
        if complete is not None else None
    )
    return {
        "index": index,
        "topic": result.topic,
        "text": str(payload.get("text", "")).strip(),
        "background": bool(payload.get("background")),
        "kws_triggered": bool(payload.get("kws_triggered")),
        "audio_start_ts": audio_start,
        "audio_end_ts": audio_end,
        "asr_complete_ts": complete,
        "received_ts": result.received_ts,
        "decode_ms": round(_span_duration_ms(payload, "asr_transcribe"), 3),
        "endpoint_latency_ms": (
            round(endpoint_ms, 3) if endpoint_ms is not None else None
        ),
        "ros_delivery_ms": (
            round(delivery_ms, 3) if delivery_ms is not None else None
        ),
    }


def prediction_from_results(item: dict, results: list[CapturedResult],
                            publish_stats: dict) -> dict:
    """Turn captured ROS payloads into the common asr_eval prediction schema."""
    segments = [
        _segment_from_result(result, index)
        for index, result in enumerate(results)
    ]
    texts = [segment["text"] for segment in segments if segment["text"]]
    endpoint_values = [
        segment["endpoint_latency_ms"] for segment in segments
        if segment["endpoint_latency_ms"] is not None
    ]
    delivery_values = [
        segment["ros_delivery_ms"] for segment in segments
        if segment["ros_delivery_ms"] is not None
    ]
    raw_comparable = all(
        not segment["background"] and not segment["kws_triggered"]
        for segment in segments
    )
    return {
        "id": item["id"],
        "text": "".join(texts),
        "status": "ok" if segments else "no_result",
        "audio_duration_s": asr_eval.inspect_wav(
            item["_audio_path"]).duration_s,
        "segment_count": len(segments),
        "segments": segments,
        "decode_ms": round(sum(segment["decode_ms"] for segment in segments), 3),
        "endpoint_latency_ms": max(endpoint_values) if endpoint_values else None,
        "ros_delivery_ms": max(delivery_values) if delivery_values else None,
        "raw_cer_comparable": raw_comparable,
        **publish_stats,
    }


def _matches_capture_window(result: CapturedResult, start_ts: float,
                            end_ts: float) -> bool:
    """Reject a late result from the previous WAV instead of misattributing it."""
    payload_end = _number(result.payload.get("audio_end_ts"))
    if payload_end is None:
        return result.received_ts >= start_ts
    return start_ts - 0.75 <= payload_end <= end_ts + 0.75


def _group_summaries(manifest: list[dict], predictions: list[dict],
                     wake_word: str) -> dict:
    by_id = {prediction["id"]: prediction for prediction in predictions}
    summaries = {}
    groups = dict.fromkeys(
        str(item.get("split", "")).strip() for item in manifest)
    for group in groups:
        if not group:
            continue
        subset = [
            item for item in manifest
            if str(item.get("split", "")).strip() == group
        ]
        summaries[group] = asr_eval.score(
            subset, [by_id[item["id"]] for item in subset], wake_word)[0]
    return summaries


def _decode_mcp_response(body: bytes) -> dict:
    try:
        envelope = json.loads(body)
        if not isinstance(envelope, dict):
            raise ValueError("JSON-RPC response is not an object")
        if "error" in envelope:
            error = envelope["error"]
            message = error.get("message") if isinstance(error, dict) else error
            raise RuntimeError(f"perception MCP error: {message}")
        content = envelope["result"]["content"]
        payload = json.loads(content[0]["text"])
        if not isinstance(payload, dict):
            raise ValueError("ASR MCP result is not an object")
        return payload
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("invalid perception MCP JSON-RPC response") from error


class PerceptionMcpClient:
    """Small JSON-RPC client used only to manage/snapshot the ASR instance."""

    def __init__(self, url: str, timeout_s: float):
        self.url = url
        self.timeout_s = timeout_s
        self._request_id = 0
        self._id_lock = threading.Lock()

    def call_asr(self, **arguments) -> dict:
        with self._id_lock:
            self._request_id += 1
            request_id = self._request_id
        request_body = json.dumps({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": "asr", "arguments": arguments},
        }).encode("utf-8")
        request = Request(
            self.url,
            data=request_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_s) as response:
                return _decode_mcp_response(response.read())
        except (HTTPError, URLError, TimeoutError) as error:
            raise RuntimeError(
                f"cannot call perception MCP at {self.url}: {error}") from error


def _transport_delta(before: dict, after: dict) -> dict | None:
    keys = (
        "received_chunks", "received_bytes", "enqueued_chunks",
        "enqueued_bytes", "queue_dropped_chunks", "queue_dropped_bytes",
        "rejected_chunks", "rejected_bytes",
    )
    if not all(key in before and key in after for key in keys):
        return None
    delta = {key: int(after[key]) - int(before[key]) for key in keys}
    if any(value < 0 for value in delta.values()):
        return None
    return delta


def _run_ros(args) -> int:
    try:
        import rclpy
        from audio_msgs.msg import AudioChunk
        from rclpy.node import Node
        from rclpy.qos import (
            DurabilityPolicy,
            HistoryPolicy,
            QoSProfile,
            ReliabilityPolicy,
        )
        from std_msgs.msg import String
    except ImportError as error:
        raise RuntimeError(
            "ROS2 replay must run inside the perception environment with "
            "rclpy, std_msgs and audio_msgs installed"
        ) from error

    manifest = asr_eval.load_manifest(args.manifest)
    for item in manifest:
        asr_eval.inspect_wav(item["_audio_path"])
    if args.chunk_bytes != asr_eval.MIN_CHUNK_BYTES:
        raise ValueError("ROS2 acceptance replay requires exactly 1024-byte chunks")
    if args.flush_ms < 0 or args.result_timeout_ms < 0 or args.settle_ms < 0:
        raise ValueError("timing arguments must be non-negative")
    if args.qos_depth < 1:
        raise ValueError("qos_depth must be positive")
    if (args.subscriber_timeout_s <= 0 or args.mcp_ready_timeout_s <= 0
            or args.mcp_timeout_s <= 0):
        raise ValueError("subscriber and MCP timeouts must be positive")

    qos = QoSProfile(
        reliability=ReliabilityPolicy.BEST_EFFORT,
        history=HistoryPolicy.KEEP_LAST,
        depth=args.qos_depth,
        durability=DurabilityPolicy.VOLATILE,
    )

    class ReplayNode(Node):
        def __init__(self):
            super().__init__(f"asr_eval_{os.getpid()}")
            self.publisher = self.create_publisher(
                AudioChunk, args.input_topic, qos)
            self._lock = threading.Lock()
            self._results: list[CapturedResult] = []
            self.create_subscription(
                String, args.output_topic, self._capture(args.output_topic), qos)
            self.create_subscription(
                String, args.background_topic,
                self._capture(args.background_topic), qos)

        def _capture(self, topic: str):
            def callback(message) -> None:
                try:
                    payload = json.loads(message.data)
                    if not isinstance(payload, dict):
                        return
                except (TypeError, ValueError):
                    return
                result = CapturedResult(
                    topic=topic,
                    payload=payload,
                    received_ts=time.time(),
                    received_monotonic=time.monotonic(),
                )
                with self._lock:
                    self._results.append(result)
            return callback

        def snapshot(self) -> list[CapturedResult]:
            with self._lock:
                return list(self._results)

    rclpy.init(args=None)
    node = ReplayNode()
    spin_thread = threading.Thread(
        target=rclpy.spin, args=(node,), name="asr-eval-spin", daemon=True)
    spin_thread.start()
    mcp = (PerceptionMcpClient(args.mcp_url, args.mcp_timeout_s)
           if args.mcp_url else None)
    mcp_start_thread = None
    mcp_start_result: dict = {}
    transport_before: dict = {}
    started_by_tool = False
    try:
        if mcp is not None:
            ready_deadline = time.monotonic() + args.mcp_ready_timeout_s
            info = mcp.call_asr(
                action="info", instance_id=args.instance_id,
                input_topic=args.input_topic)
            while info.get("state") == "loading" and time.monotonic() < ready_deadline:
                time.sleep(1)
                info = mcp.call_asr(
                    action="info", instance_id=args.instance_id,
                    input_topic=args.input_topic)
            if info.get("state") == "loading":
                raise RuntimeError(
                    f"ASR model was still loading after {args.mcp_ready_timeout_s}s")
            if info.get("state") == "error":
                raise RuntimeError(f"ASR MCP reports error: {info.get('desc', info)}")
            if info.get("state") != "running":
                started_by_tool = True

                def start_asr() -> None:
                    try:
                        mcp_start_result["result"] = mcp.call_asr(
                            action="start", instance_id=args.instance_id,
                            input_topic=args.input_topic)
                    except Exception as error:  # relayed on the replay thread
                        mcp_start_result["error"] = error

                mcp_start_thread = threading.Thread(
                    target=start_asr, name="asr-eval-mcp-start", daemon=True)
                mcp_start_thread.start()

        subscriber_deadline = time.monotonic() + args.subscriber_timeout_s

        def ros_graph_ready() -> bool:
            return (
                node.publisher.get_subscription_count() >= 1
                and node.count_publishers(args.output_topic) >= 1
                and node.count_publishers(args.background_topic) >= 1
            )

        while not ros_graph_ready() and time.monotonic() < subscriber_deadline:
            if "error" in mcp_start_result:
                raise RuntimeError(
                    f"ASR start failed: {mcp_start_result['error']}")
            time.sleep(0.05)
        if node.publisher.get_subscription_count() < 1:
            start_detail = mcp_start_result.get("result")
            raise RuntimeError(
                f"no ASR subscriber on {args.input_topic!r}; start the ASR "
                f"card on this exact topic before replay (start={start_detail})")
        if not ros_graph_ready():
            raise RuntimeError(
                "ASR input subscriber was found, but its output publishers "
                f"were not discovered on {args.output_topic!r} and "
                f"{args.background_topic!r}")
        if mcp is not None:
            info = mcp.call_asr(
                action="info", instance_id=args.instance_id,
                input_topic=args.input_topic)
            transport_before = dict(info.get("transport") or {})

        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        predictions = []
        orphan_results = []
        with output_path.open("w", encoding="utf-8") as output:
            for item in manifest:
                before = node.snapshot()
                start_index = len(before)
                capture_start_ns = time.time_ns()
                capture_start_ts = capture_start_ns / 1_000_000_000
                replay_started = time.monotonic()
                published = publish_errors = late_chunks = 0
                max_lag_ms = 0.0
                chunk_duration_s = (
                    args.chunk_bytes / asr_eval.SAMPLE_WIDTH /
                    asr_eval.SAMPLE_RATE)
                chunks = list(asr_eval.iter_pcm_chunks(
                    item["_audio_path"], args.chunk_bytes))
                flush_chunks = math.ceil(
                    args.flush_ms / (chunk_duration_s * 1000))
                chunks.extend([b"\x00" * args.chunk_bytes] * flush_chunks)

                for index, chunk in enumerate(chunks):
                    deadline = replay_started + index * chunk_duration_s
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        time.sleep(remaining)
                    lag_ms = max(0.0, (time.monotonic() - deadline) * 1000)
                    max_lag_ms = max(max_lag_ms, lag_ms)
                    if lag_ms > 1.0:
                        late_chunks += 1
                    stamp_ns = capture_start_ns + round(
                        index * chunk_duration_s * 1_000_000_000)
                    stamp_sec, stamp_nanosec = divmod(
                        stamp_ns, 1_000_000_000)
                    message = AudioChunk()
                    message.header.stamp.sec = stamp_sec
                    message.header.stamp.nanosec = stamp_nanosec
                    message.header.frame_id = item["id"]
                    message.format = "audio/pcm-16k"
                    message.data = list(chunk)
                    try:
                        node.publisher.publish(message)
                        published += 1
                    except Exception:  # rclpy publish errors are sample-local
                        publish_errors += 1

                capture_end_ts = capture_start_ts + len(chunks) * chunk_duration_s
                expected_speech = bool(asr_eval.normalize_text(item.get("text", "")))
                result_deadline = (
                    time.monotonic() + args.result_timeout_ms / 1000)
                matched: list[CapturedResult] = []
                while time.monotonic() < result_deadline:
                    candidates = node.snapshot()[start_index:]
                    matched = [
                        result for result in candidates
                        if _matches_capture_window(
                            result, capture_start_ts, capture_end_ts)
                    ]
                    if matched and expected_speech:
                        quiet_s = time.monotonic() - matched[-1].received_monotonic
                        if quiet_s >= args.settle_ms / 1000:
                            break
                    time.sleep(0.02)

                candidates = node.snapshot()[start_index:]
                matched = [
                    result for result in candidates
                    if _matches_capture_window(
                        result, capture_start_ts, capture_end_ts)
                ]
                orphan_results.extend(
                    result for result in candidates if result not in matched)
                stats = {
                    "input_chunks": len(chunks) - flush_chunks,
                    "flush_chunks": flush_chunks,
                    "published_chunks": published,
                    "publisher_errors": publish_errors,
                    "late_chunks": late_chunks,
                    "max_replay_lag_ms": round(max_lag_ms, 3),
                    "ros_replay_wall_ms": round(
                        (time.monotonic() - replay_started) * 1000, 3),
                    "replay_wall_ms": round(
                        (time.monotonic() - replay_started) * 1000, 3),
                    "input_topic": args.input_topic,
                }
                prediction = prediction_from_results(item, matched, stats)
                predictions.append(prediction)
                output.write(json.dumps(prediction, ensure_ascii=False) + "\n")
                output.flush()
                print(json.dumps(prediction, ensure_ascii=False))

        summary, details = asr_eval.score(
            manifest, predictions, args.wake_word)
        delivery = [
            segment["ros_delivery_ms"]
            for prediction in predictions
            for segment in prediction["segments"]
            if segment["ros_delivery_ms"] is not None
        ]
        published_chunks = sum(
            prediction["published_chunks"] for prediction in predictions)
        publisher_errors = sum(
            prediction["publisher_errors"] for prediction in predictions)
        summary.update({
            "transport": "ros2",
            "input_topic": args.input_topic,
            "output_topic": args.output_topic,
            "background_topic": args.background_topic,
            "chunk_bytes": args.chunk_bytes,
            "flush_ms": args.flush_ms,
            "published_chunks": published_chunks,
            "publisher_errors": publisher_errors,
            "ros_delivery_ms_p50": asr_eval._percentile(delivery, 0.50),
            "ros_delivery_ms_p95": asr_eval._percentile(delivery, 0.95),
            "raw_cer_comparable": all(
                prediction["raw_cer_comparable"] for prediction in predictions),
            "orphan_results": len(orphan_results),
        })
        transport_after = {}
        transport_observation = None
        if mcp is not None:
            info = mcp.call_asr(
                action="info", instance_id=args.instance_id,
                input_topic=args.input_topic)
            transport_after = dict(info.get("transport") or {})
            delta = _transport_delta(transport_before, transport_after)
            if delta is not None:
                delivery_gap = published_chunks - delta["received_chunks"]
                measured_drops = (
                    delivery_gap + delta["queue_dropped_chunks"]
                    + delta["rejected_chunks"]
                    if delivery_gap >= 0 else None
                )
                transport_observation = {
                    "before": transport_before,
                    "after": transport_after,
                    "delta": delta,
                    "publisher_to_subscriber_gap_chunks": delivery_gap,
                    "isolated_topic_assumption_met": delivery_gap >= 0,
                    "transport_ok": (
                        publisher_errors == 0 and delivery_gap == 0
                        and delta["queue_dropped_chunks"] == 0
                        and delta["rejected_chunks"] == 0
                    ),
                }
                summary.update({
                    "subscriber_received_chunks": delta["received_chunks"],
                    "subscriber_enqueued_chunks": delta["enqueued_chunks"],
                    "publisher_to_subscriber_gap_chunks": delivery_gap,
                    "queue_dropped_chunks": delta["queue_dropped_chunks"],
                    "rejected_chunks": delta["rejected_chunks"],
                    "dropped_chunks": measured_drops,
                    "transport_ok": transport_observation["transport_ok"],
                })
        if transport_observation is None:
            summary.update({
                "subscriber_received_chunks": None,
                "subscriber_enqueued_chunks": None,
                "publisher_to_subscriber_gap_chunks": None,
                "queue_dropped_chunks": None,
                "rejected_chunks": None,
                "dropped_chunks": None,
                "transport_ok": None,
            })
        groups = _group_summaries(manifest, predictions, args.wake_word)
        report = {
            "summary": summary,
            "groups": groups,
            "transport_observation": transport_observation,
            "details": details,
            "predictions": predictions,
        }
        print(json.dumps({"summary": summary, "groups": groups},
                         ensure_ascii=False, indent=2))
        if args.report:
            asr_eval._write_json(args.report, report)
        if not summary["raw_cer_comparable"] and not args.allow_kws_results:
            raise RuntimeError(
                "received KWS-trimmed or background results; configure the ASR "
                "card with trigger_mode=vad for raw-CER acceptance, or pass "
                "--allow-kws-results for a non-comparable transport smoke test")
        return int(
            bool(summary["publisher_errors"])
            or summary["transport_ok"] is False)
    finally:
        if mcp_start_thread is not None:
            mcp_start_thread.join(timeout=max(2, args.mcp_timeout_s))
        if mcp is not None and args.stop_after and started_by_tool:
            try:
                mcp.call_asr(action="stop", instance_id=args.instance_id)
            except Exception as error:
                print(f"warning: failed to stop ASR instance: {error}")
        node.destroy_node()
        rclpy.shutdown()
        spin_thread.join(timeout=2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--input-topic", required=True)
    parser.add_argument("--output-topic")
    parser.add_argument("--background-topic")
    parser.add_argument("--output", required=True)
    parser.add_argument("--report")
    parser.add_argument("--chunk-bytes", type=int,
                        default=asr_eval.MIN_CHUNK_BYTES)
    parser.add_argument("--flush-ms", type=int, default=2_000)
    parser.add_argument("--result-timeout-ms", type=int, default=2_000)
    parser.add_argument("--settle-ms", type=int, default=500)
    parser.add_argument("--subscriber-timeout-s", type=float, default=10)
    parser.add_argument("--qos-depth", type=int, default=50)
    parser.add_argument("--wake-word", default="小范小范")
    parser.add_argument("--allow-kws-results", action="store_true")
    parser.add_argument(
        "--mcp-url",
        help="optional perception MCP URL; starts/snapshots ASR transport counters")
    parser.add_argument("--instance-id", default="asr-eval")
    parser.add_argument("--mcp-timeout-s", type=float, default=30)
    parser.add_argument("--mcp-ready-timeout-s", type=float, default=300)
    parser.add_argument(
        "--stop-after", action="store_true",
        help="stop the ASR instance after replay if this tool started it")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.output_topic = args.output_topic or f"{args.input_topic}/asr"
    args.background_topic = (
        args.background_topic or f"{args.input_topic}/asr_background")
    try:
        return _run_ros(args)
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(2, f"error: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
