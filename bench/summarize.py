"""Turn bench/results/*.json into the tables used in docs/concurrency.md.

    python bench/summarize.py                 # print Markdown
    python bench/summarize.py --write         # also write bench/results/SUMMARY.md

Every number in the documentation is printed by this script from the raw run files, so anyone
can regenerate the tables from their own runs.
"""

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

RESULTS = Path(__file__).resolve().parent / "results"
ORDER = ["naive", "row_lock", "atomic_update", "skip_locked"]
RESERVE = "POST /api/events/[id]/reserve"


def load(directory: Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.json"))]


def spread(values: list[float], unit: str = "", digits: int = 0) -> str:
    """Median, and the min to max range when there is more than one run."""

    def fmt(x: float) -> str:
        return f"{x:,.{digits}f}"

    if not values:
        return "n/a"
    med = statistics.median(values)
    if len(values) == 1:
        return f"{fmt(med)}{unit}"
    return f"{fmt(med)}{unit} ({fmt(min(values))}-{fmt(max(values))})"


def seconds(ms: float) -> float:
    return ms / 1000


def group(runs: list[dict[str, Any]]) -> dict[tuple[str, str, int, int], list[dict[str, Any]]]:
    out: dict[tuple[str, str, int, int], list[dict[str, Any]]] = defaultdict(list)
    for r in runs:
        o = r["options"]
        out[(o["scenario"], o["strategy"], o["users"], o["seats"])].append(r)
    return out


def comparison_table(runs: list[dict[str, Any]], users: int, seats: int) -> str:
    groups = group(runs)
    rows = [
        "| Strategy | Runs | Seats sold (confirmed) | Oversold seats | Reserve p50 (s) | Reserve p95 (s) | Reserve req/s | Failed requests |",
        "| :-- | --: | --: | --: | --: | --: | --: | --: |",
    ]
    for strategy in ORDER:
        rs = groups.get(("spike", strategy, users, seats), [])
        if not rs:
            continue
        res = [r["locust"][RESERVE] for r in rs]
        sold = [r["database"]["reservations"].get("confirmed", 0) for r in rs]
        over = [r["database"]["oversold_seats"] for r in rs]
        fails = [r["locust"]["Aggregated"]["failures"] for r in rs]
        rows.append(
            f"| `{strategy}` | {len(rs)} | {spread(sold)} | {spread(over)} "
            f"| {spread([seconds(x['p50_ms']) for x in res], digits=1)} "
            f"| {spread([seconds(x['p95_ms']) for x in res], digits=1)} "
            f"| {spread([x['rps'] for x in res])} | {spread(fails)} |"
        )
    return "\n".join(rows)


def storm_table(runs: list[dict[str, Any]]) -> str:
    rows = [
        "| Strategy | Users | Seats | Writes sent | Seats sold | Oversold | Orders | Charges at provider | Duplicate answered differently | Failed requests |",
        "| :-- | --: | --: | --: | --: | --: | --: | --: | --: | --: |",
    ]
    for r in runs:
        o = r["options"]
        if o["scenario"] != "retry-storm":
            continue
        loc, db = r["locust"], r["database"]
        sent = sum(v["requests"] for k, v in loc.items() if k.startswith("POST"))
        differing = loc.get("duplicate request got a different answer", {}).get("failures", 0)
        orders = sum(db["orders"].values())
        rows.append(
            f"| `{o['strategy']}` | {o['users']:,} | {o['seats']} | {sent:,} "
            f"| {db['reservations'].get('confirmed', 0)} | {db['oversold_seats']} | {orders} "
            f"| {db['charges'].get('n', 0)} | {differing} | {loc['Aggregated']['failures'] - differing} |"
        )
    return "\n".join(rows)


def soak_table(runs: list[dict[str, Any]]) -> str:
    rows = [
        "| Strategy | Duration (s) | Requests | Failed | Reservations | Orders | Payments | Messages | Oversold | Violated invariants |",
        "| :-- | --: | --: | --: | :-- | :-- | :-- | --: | --: | :-- |",
    ]
    for r in runs:
        o = r["options"]
        if o["scenario"] != "soak":
            continue
        db, agg = r["database"], r["locust"]["Aggregated"]
        rows.append(
            f"| `{o['strategy']}` | {o['duration']} | {agg['requests']:,} | {agg['failures']} "
            f"| {db['reservations']} | {db['orders']} | {db['payments']} | {db['messages']} "
            f"| {db['oversold_seats']} | {db['violations'] or 'none'} |"
        )
    return "\n".join(rows)


def environment(runs: list[dict[str, Any]]) -> str:
    if not runs:
        return "no runs"
    m, o = runs[0]["machine"], runs[0]["options"]
    commits = sorted({r["commit"] for r in runs})
    return (
        f"- Machine: {m['cpu']} ({m['logical_cpus']} logical CPUs visible), {m['os']}\n"
        f"- PostgreSQL {m['postgres']}, Python {m['python']}\n"
        f"- {o['web_workers']} web processes, {o['pool_size']} database connections each, one worker process\n"
        f"- Commit(s): {', '.join(commits)}; {len(runs)} runs"
    )


def build(directory: Path) -> str:
    runs = load(directory)
    sizes = sorted(
        {(r["options"]["users"], r["options"]["seats"]) for r in runs if r["options"]["scenario"] == "spike"}
    )
    parts = ["### Environment", environment(runs)]
    for users, seats in sizes:
        parts += [f"### Spike: {users:,} students, {seats} seats", comparison_table(runs, users, seats)]
    storm = storm_table(runs)
    if storm.count("\n") > 1:
        parts += ["### Retry storm: every write sent twice with the same key", storm]
    soak = soak_table(runs)
    if soak.count("\n") > 1:
        parts += ["### Soak: slow, declining and forgetful payment provider, short holds", soak]
    parts.append("Median, with the lowest to highest of the repeated runs in brackets.")
    return "\n\n".join(parts) + "\n"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dir", type=Path, default=RESULTS)
    p.add_argument("--write", action="store_true", help="also write SUMMARY.md next to the results")
    args = p.parse_args()
    text = build(args.dir)
    print(text)
    if args.write:
        (args.dir / "SUMMARY.md").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
