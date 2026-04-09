"""CLI command: brain analyze — score distribution analysis.

Provides ``sovyx brain analyze <mind_id>`` command that reports
importance and confidence distribution statistics, entropy, quartiles,
and flags potential issues (drift, concentration).
"""

from __future__ import annotations

import json
import math

import typer
from rich.console import Console
from rich.table import Table

console = Console()
analyze_app = typer.Typer(name="analyze", help="Analyze brain score distributions")


def _shannon_entropy(values: list[float], bins: int = 20) -> float:
    """Compute Shannon entropy of a score distribution."""
    if len(values) < 2:  # noqa: PLR2004
        return 0.0
    counts = [0] * bins
    for v in values:
        idx = min(int(v * bins), bins - 1)
        counts[idx] += 1
    n = len(values)
    entropy = 0.0
    for count in counts:
        if count > 0:
            p = count / n
            entropy -= p * math.log2(p)
    return entropy


def _quartiles(values: list[float]) -> tuple[float, float, float]:
    """Q1, median, Q3."""
    s = sorted(values)
    n = len(s)
    if n == 0:
        return 0.0, 0.0, 0.0
    q1 = s[n // 4]
    median = s[n // 2]
    q3 = s[3 * n // 4]
    return q1, median, q3


def _analyze_scores(
    values: list[float],
    label: str,
) -> dict[str, float | str]:
    """Analyze a score distribution and return metrics."""
    if not values:
        return {"label": label, "count": 0}

    q1, median, q3 = _quartiles(values)
    entropy = _shannon_entropy(values)
    iqr = q3 - q1
    spread = max(values) - min(values)

    # Health assessment
    if entropy < 1.0:
        health = "🔴 CRITICAL — collapsed"
    elif entropy < 1.5:  # noqa: PLR2004
        health = "🟡 WARNING — concentrating"
    else:
        health = "🟢 healthy"

    return {
        "label": label,
        "count": len(values),
        "mean": round(sum(values) / len(values), 3),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
        "q1": round(q1, 3),
        "median": round(median, 3),
        "q3": round(q3, 3),
        "iqr": round(iqr, 3),
        "spread": round(spread, 3),
        "entropy": round(entropy, 3),
        "health": health,
    }


@analyze_app.command("scores")
def analyze_scores(
    mind_id: str = typer.Argument(help="Mind ID to analyze"),
    output_json: bool = typer.Option(False, "--json", help="JSON output"),
    db_path: str = typer.Option(
        "",
        "--db",
        help="Database path (default: ~/.sovyx/data/<mind_id>/brain.db)",
    ),
) -> None:
    """Analyze importance and confidence score distributions.

    Reports: mean, quartiles, entropy, spread, and health status.
    """
    import asyncio

    asyncio.run(_analyze_scores_async(mind_id, output_json, db_path))


async def _analyze_scores_async(
    mind_id: str,
    output_json: bool,
    db_path: str,
) -> None:
    """Async implementation of score analysis."""
    from pathlib import Path

    import aiosqlite

    if not db_path:
        db_path = str(Path.home() / ".sovyx" / "data" / mind_id / "brain.db")

    path = Path(db_path)
    if not path.exists():
        console.print(f"[red]Database not found:[/red] {db_path}")
        raise typer.Exit(1)

    async with aiosqlite.connect(str(path)) as db:
        cursor = await db.execute(
            "SELECT importance, confidence FROM concepts WHERE mind_id = ?",
            (mind_id,),
        )
        rows = list(await cursor.fetchall())

    if not rows:
        console.print(f"[yellow]No concepts found for mind '{mind_id}'[/yellow]")
        raise typer.Exit(0)

    importances = [float(r[0]) for r in rows]
    confidences = [float(r[1]) for r in rows]

    imp_stats = _analyze_scores(importances, "importance")
    conf_stats = _analyze_scores(confidences, "confidence")

    if output_json:
        console.print(json.dumps({
            "mind_id": mind_id,
            "concepts": len(rows),
            "importance": imp_stats,
            "confidence": conf_stats,
        }, indent=2))
        return

    # Rich table output
    console.print(f"\n[bold]Score Distribution — {mind_id}[/bold]")
    console.print(f"Concepts: {len(rows)}\n")

    table = Table(title="Distribution Metrics")
    table.add_column("Metric", style="bold")
    table.add_column("Importance", justify="right")
    table.add_column("Confidence", justify="right")

    for key in ("mean", "min", "max", "q1", "median", "q3", "iqr", "spread", "entropy"):
        table.add_row(
            key.upper(),
            str(imp_stats.get(key, "")),
            str(conf_stats.get(key, "")),
        )

    console.print(table)
    console.print(f"\nImportance: {imp_stats.get('health', '')}")
    console.print(f"Confidence: {conf_stats.get('health', '')}")
