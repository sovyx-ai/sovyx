"""Memory benchmarks: RSS baseline after startup."""

from __future__ import annotations

import json
import os


def bench_rss_after_import() -> dict[str, object]:
    """Measure RSS after importing sovyx core modules."""
    import resource

    # Import core modules
    import sovyx.brain  # noqa: F401
    import sovyx.cognitive  # noqa: F401
    import sovyx.context  # noqa: F401
    import sovyx.engine  # noqa: F401
    import sovyx.llm  # noqa: F401
    import sovyx.observability  # noqa: F401
    import sovyx.persistence  # noqa: F401

    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_mb = rss_kb / 1024  # Linux returns KB

    return {
        "benchmark": "rss_after_import",
        "rss_mb": round(rss_mb, 1),
        "pid": os.getpid(),
    }


def bench_rss_after_create_app() -> dict[str, object]:
    """Measure RSS after creating the dashboard app."""
    import resource

    from sovyx.dashboard.server import create_app

    _ = create_app()

    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_mb = rss_kb / 1024

    return {
        "benchmark": "rss_after_create_app",
        "rss_mb": round(rss_mb, 1),
        "pid": os.getpid(),
    }


def run_all() -> list[dict[str, object]]:
    """Run all memory benchmarks."""
    return [
        bench_rss_after_import(),
        bench_rss_after_create_app(),
    ]


if __name__ == "__main__":
    for r in run_all():
        print(json.dumps(r))
