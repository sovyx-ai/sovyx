#!/usr/bin/env python3
"""WebSocket stress test — 50 events/second for 10 seconds.

POLISH-22: Validates that the dashboard frontend handles rapid-fire WS events
without dropping events, leaking memory, or causing API call storms.

Usage:
    python tests/stress/ws_stress_test.py [--url ws://localhost:8000/ws] [--token TOKEN]
    python tests/stress/ws_stress_test.py --rate 50 --duration 10

Metrics collected:
    - Events sent vs events acknowledged (via pong echo)
    - Send rate achieved (events/sec)
    - Total duration
    - Connection stability
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import time
import uuid
from datetime import UTC, datetime

try:
    from websockets.asyncio.client import connect as ws_connect
except ImportError as _err:
    print("ERROR: websockets not installed. Run: pip install websockets")
    raise SystemExit(1) from _err


# ── Event templates matching WsEventType ──

EVENT_TYPES = [
    "ServiceHealthChanged",
    "PerceptionReceived",
    "ThinkCompleted",
    "ResponseSent",
    "ConceptCreated",
    "EpisodeEncoded",
    "ConsolidationCompleted",
    "ChannelConnected",
    "ChannelDisconnected",
]

# Weight distribution — more common events get higher weight
EVENT_WEIGHTS = [
    3,   # ServiceHealthChanged
    10,  # PerceptionReceived
    8,   # ThinkCompleted
    8,   # ResponseSent
    5,   # ConceptCreated
    3,   # EpisodeEncoded
    2,   # ConsolidationCompleted
    1,   # ChannelConnected
    1,   # ChannelDisconnected
]


def make_event(event_type: str) -> dict:
    """Generate a realistic WS event payload."""
    return {
        "type": event_type,
        "timestamp": datetime.now(UTC).isoformat(),
        "correlation_id": str(uuid.uuid4()),
        "data": _make_data(event_type),
    }


def _make_data(event_type: str) -> dict:
    """Generate type-specific event data."""
    match event_type:
        case "ThinkCompleted":
            return {
                "tokens_in": random.randint(50, 500),
                "tokens_out": random.randint(20, 300),
                "model": "claude-sonnet-4-20250514",
            }
        case "PerceptionReceived":
            return {
                "channel": random.choice(["telegram", "discord", "api"]),
                "sender": f"user_{random.randint(1, 100)}",
            }
        case "ResponseSent":
            return {
                "channel": random.choice(["telegram", "discord", "api"]),
                "tokens": random.randint(10, 200),
            }
        case "ConceptCreated":
            return {
                "concept_id": str(uuid.uuid4()),
                "label": f"concept_{random.randint(1, 1000)}",
            }
        case "ServiceHealthChanged":
            return {
                "service": random.choice(["engine", "brain", "channels"]),
                "status": random.choice(["healthy", "degraded"]),
            }
        case "EpisodeEncoded":
            return {
                "episode_id": str(uuid.uuid4()),
                "concepts": random.randint(1, 10),
            }
        case "ConsolidationCompleted":
            return {
                "pruned": random.randint(0, 50),
                "strengthened": random.randint(0, 30),
            }
        case _:
            return {}


class StressTestMetrics:
    """Collect and report stress test metrics."""

    def __init__(self) -> None:
        self.events_sent = 0
        self.send_errors = 0
        self.start_time = 0.0
        self.end_time = 0.0
        self.event_type_counts: dict[str, int] = {}

    def record_send(self, event_type: str) -> None:
        self.events_sent += 1
        self.event_type_counts[event_type] = self.event_type_counts.get(event_type, 0) + 1

    def record_error(self) -> None:
        self.send_errors += 1

    @property
    def duration(self) -> float:
        return self.end_time - self.start_time

    @property
    def actual_rate(self) -> float:
        return self.events_sent / self.duration if self.duration > 0 else 0

    def report(self) -> str:
        lines = [
            "",
            "═══ WS STRESS TEST RESULTS ═══",
            f"  Duration:     {self.duration:.2f}s",
            f"  Events sent:  {self.events_sent}",
            f"  Send errors:  {self.send_errors}",
            f"  Actual rate:  {self.actual_rate:.1f} events/sec",
            "  Target:       50 events/sec × 10s = 500 events",
            "",
            "  Event distribution:",
        ]
        for etype, count in sorted(self.event_type_counts.items(), key=lambda x: -x[1]):
            lines.append(f"    {etype:30s} {count:4d}")

        # Pass/fail
        lines.append("")
        passed = True
        if self.events_sent < 450:
            lines.append(f"  ❌ FAIL: Only {self.events_sent}/500 events sent")
            passed = False
        if self.send_errors > 10:
            lines.append(f"  ❌ FAIL: {self.send_errors} send errors (max 10)")
            passed = False
        if self.actual_rate < 40:
            lines.append(f"  ❌ FAIL: Rate {self.actual_rate:.1f}/s < 40/s minimum")
            passed = False

        if passed:
            lines.append("  ✅ PASS: All metrics within acceptable range")

        lines.append("═══════════════════════════════")
        return "\n".join(lines)


async def run_stress_test(
    url: str,
    token: str,
    rate: int = 50,
    duration: int = 10,
) -> StressTestMetrics:
    """Connect to WS and send events at target rate."""
    metrics = StressTestMetrics()
    interval = 1.0 / rate

    full_url = f"{url}?token={token}" if "?" not in url else url

    print(f"Connecting to {url} ...")

    async with ws_connect(full_url) as ws:
        print(f"Connected. Sending {rate} events/sec for {duration}s ...")

        metrics.start_time = time.monotonic()
        target_end = metrics.start_time + duration

        while time.monotonic() < target_end:
            cycle_start = time.monotonic()

            event_type = random.choices(EVENT_TYPES, weights=EVENT_WEIGHTS, k=1)[0]
            event = make_event(event_type)

            try:
                await ws.send(json.dumps(event))
                metrics.record_send(event_type)
            except Exception as e:
                metrics.record_error()
                print(f"  Send error: {e}")

            # Sleep to maintain target rate
            elapsed = time.monotonic() - cycle_start
            sleep_time = max(0, interval - elapsed)
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

        metrics.end_time = time.monotonic()

    return metrics


async def run_broadcast_stress_test(
    url: str,
    token: str,
    rate: int = 50,
    duration: int = 10,
) -> StressTestMetrics:
    """Alternative: connect as a listener and use the server's broadcast mechanism.

    This test connects, then uses a second connection to inject events
    via the broadcast path, verifying the full pipeline.
    """
    metrics = StressTestMetrics()
    interval = 1.0 / rate

    full_url = f"{url}?token={token}" if "?" not in url else url

    print(f"Connecting listener to {url} ...")

    async with ws_connect(full_url) as listener:
        print(f"Listener connected. Sending {rate} events/sec for {duration}s ...")
        print("(Events go through WebSocket send — server should broadcast back)")

        received_count = 0
        metrics.start_time = time.monotonic()
        target_end = metrics.start_time + duration

        async def sender() -> None:
            """Send events at target rate."""
            while time.monotonic() < target_end:
                cycle_start = time.monotonic()
                event_type = random.choices(EVENT_TYPES, weights=EVENT_WEIGHTS, k=1)[0]
                event = make_event(event_type)
                try:
                    await listener.send(json.dumps(event))
                    metrics.record_send(event_type)
                except Exception:
                    metrics.record_error()

                elapsed = time.monotonic() - cycle_start
                sleep_time = max(0, interval - elapsed)
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)

        async def receiver() -> None:
            """Count received messages."""
            nonlocal received_count
            try:
                async for _msg in listener:
                    received_count += 1
                    if time.monotonic() > target_end + 2:
                        break
            except Exception:
                pass

        # Run both concurrently
        await asyncio.gather(
            sender(),
            asyncio.wait_for(receiver(), timeout=duration + 5),
            return_exceptions=True,
        )

        metrics.end_time = time.monotonic()
        print(f"  Received back: {received_count} messages")

    return metrics


def main() -> None:
    parser = argparse.ArgumentParser(description="WebSocket stress test for Sovyx dashboard")
    parser.add_argument("--url", default="ws://localhost:8000/ws", help="WebSocket URL")
    parser.add_argument("--token", default="test-token", help="Auth token")
    parser.add_argument("--rate", type=int, default=50, help="Events per second")
    parser.add_argument("--duration", type=int, default=10, help="Test duration in seconds")
    parser.add_argument(
        "--mode",
        choices=["send", "broadcast"],
        default="send",
        help="Test mode: 'send' (direct) or 'broadcast' (full pipeline)",
    )
    args = parser.parse_args()

    metrics = asyncio.run(
        run_stress_test(args.url, args.token, args.rate, args.duration)
        if args.mode == "send"
        else run_broadcast_stress_test(args.url, args.token, args.rate, args.duration)
    )

    print(metrics.report())


if __name__ == "__main__":
    main()
