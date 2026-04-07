"""Startup benchmarks: cold start time, app creation."""

from __future__ import annotations

import json
import time


def bench_create_app() -> dict[str, object]:
    """Benchmark dashboard app creation time (cold start)."""
    start = time.perf_counter()
    from sovyx.dashboard.server import create_app

    app = create_app()
    elapsed = time.perf_counter() - start

    return {
        "benchmark": "create_app_cold",
        "duration_ms": round(elapsed * 1000, 2),
        "app_routes": len(app.routes),
    }


def bench_import_sovyx() -> dict[str, object]:
    """Benchmark top-level sovyx import time."""
    import importlib
    import sys

    # Clear cached module
    modules_to_clear = [k for k in sys.modules if k.startswith("sovyx")]
    for m in modules_to_clear:
        del sys.modules[m]

    start = time.perf_counter()
    importlib.import_module("sovyx")
    elapsed = time.perf_counter() - start

    return {
        "benchmark": "import_sovyx",
        "duration_ms": round(elapsed * 1000, 2),
    }


def run_all() -> list[dict[str, object]]:
    """Run all startup benchmarks."""
    return [
        bench_import_sovyx(),
        bench_create_app(),
    ]


if __name__ == "__main__":
    for r in run_all():
        print(json.dumps(r))
