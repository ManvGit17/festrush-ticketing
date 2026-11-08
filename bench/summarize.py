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


EXPERIMENTS = {
    "e2": ("E2: the payment provider (each charge takes 300 ms)", ["inprocess", "service"]),
    "e3": ("E3: where the confirmation goes (each message takes 300 ms)", ["outbox", "inline"]),
    "e4": (
        "E4: a one-second read cache in the gateway (students refresh the page 6 times)",
        ["nocache", "cache"],
    ),
}


def stat(run: dict[str, Any], name: str, key: str) -> float | None:
    entry = run["locust"].get(name)
    return None if entry is None else entry[key] / 1000


def experiments_table(runs: list[dict[str, Any]], code: str) -> str:
    title, variants = EXPERIMENTS[code]
    rows = [
        f"#### {title}",
        "",
        "| Variant | Runs | Whole run (s) | Reserve p50 (s) | Order p50 (s) | Pay p50 (s) | Pay p95 (s) | Page refresh p50 (s) | Oversold |",
        "| :-- | --: | --: | --: | --: | --: | --: | --: | --: |",
    ]
    for variant in variants:
        rs = [r for r in runs if r["label"].startswith(f"exp-{code}-{variant}-r")]
        if not rs:
            continue

        def col(name: str, key: str, digits: int = 1, rs: list[dict[str, Any]] = rs) -> str:
            vals = [v for r in rs if (v := stat(r, name, key)) is not None]
            return spread(vals, digits=digits) if vals else "n/a"

        reserve, order = "POST /api/events/[id]/reserve", "POST /api/orders"
        pay, refresh = "POST /api/orders/[id]/pay", "GET /api/events/[id] (refresh)"
        rows.append(
            f"| `{variant}` | {len(rs)} | {spread([r['load_seconds'] for r in rs], digits=1)} "
            f"| {col(reserve, 'p50_ms')} | {col(order, 'p50_ms')} | {col(pay, 'p50_ms')} | {col(pay, 'p95_ms')} "
            f"| {col(refresh, 'p50_ms', 2)} | {spread([r['database']['oversold_seats'] for r in rs])} |"
        )
    return "\n".join(rows)


def by_label(runs: list[dict[str, Any]], labels: list[str]) -> list[dict[str, Any]]:
    wanted = {name: i for i, name in enumerate(labels)}
    return sorted((r for r in runs if r["label"] in wanted), key=lambda r: wanted[r["label"]])


def ms(run: dict[str, Any], name: str, key: str) -> str:
    entry = run["locust"].get(name)
    return "n/a" if entry is None else f"{entry[key] / 1000:.1f}"


def ceiling_table(runs: list[dict[str, Any]]) -> str:
    """E0: what limits throughput? The same 5,000-student spike with k6, varying the web tier."""
    rows = [
        "| Web processes | Threads each | Students | Requests/s overall | Reserve p50 (s) |",
        "| --: | --: | --: | --: | --: |",
    ]
    for r in by_label(runs, ["k6-w1", "k6-w4-t10", "k6-w4-t40", "k6-w8-t10", "k6-w16-t5"]):
        o, agg = r["options"], r["locust"]["Aggregated"]
        rows.append(
            f"| {o['web_workers']} | {o.get('threads', 40)} | {o['users']:,} "
            f"| {agg['requests'] / r['load_seconds']:.0f} | {ms(r, RESERVE, 'p50_ms')} |"
        )
    return "\n".join(rows)


def gateway_table(runs: list[dict[str, Any]]) -> str:
    rows = [
        "| Setup | Whole run (s) | Reserve attempt p50 / p95 (s) | Order p50 (s) | Pay p50 (s) | Rejected with 429 | Seats sold | Oversold |",
        "| :-- | --: | --: | --: | --: | --: | --: | --: |",
    ]
    names = {
        "e-direct": "no gateway",
        "e-gateway": "gateway, 300 claims/s",
        "e-gateway-100": "gateway, 100 claims/s",
    }
    for r in by_label(runs, list(names)):
        extra = r["locust"].get("_extra", {})
        rows.append(
            f"| {names[r['label']]} | {r['load_seconds']:.0f} | {ms(r, RESERVE, 'p50_ms')} / {ms(r, RESERVE, 'p95_ms')} "
            f"| {ms(r, 'POST /api/orders', 'p50_ms')} | {ms(r, 'POST /api/orders/[id]/pay', 'p50_ms')} "
            f"| {extra.get('shed_429', 0):,} | {r['database']['reservations'].get('confirmed', 0)} "
            f"| {r['database']['oversold_seats']} |"
        )
    return "\n".join(rows)


def waiting_room_table(runs: list[dict[str, Any]]) -> str:
    rows = [
        "| Admission rate | Whole run (s) | Join p50 (s) | Status poll p50 (s) | Reserve p50 (s) | Status polls | Seats sold | Oversold |",
        "| --: | --: | --: | --: | --: | --: | --: | --: |",
    ]
    for r in by_label(runs, ["wr-fifo-100", "wr-fifo-300"]):
        rows.append(
            f"| {r['options']['admit_per_second']}/s | {r['load_seconds']:.0f} "
            f"| {ms(r, 'POST /api/queue/join', 'p50_ms')} | {ms(r, 'GET /api/queue/status', 'p50_ms')} "
            f"| {ms(r, RESERVE, 'p50_ms')} | {r['locust'].get('GET /api/queue/status', {}).get('requests', 0):,} "
            f"| {r['database']['reservations'].get('confirmed', 0)} | {r['database']['oversold_seats']} |"
        )
    return "\n".join(rows)


def chaos_table(runs: list[dict[str, Any]]) -> str:
    rows = [
        "| Fault | What was broken | Students | Seats sold | Messages | Failed requests | Oversold | Invariants | App back after |",
        "| :-- | :-- | --: | --: | --: | --: | --: | :-- | --: |",
    ]
    names = {
        "chaos-kill-worker": "kill the worker",
        "chaos-kill-payments": "kill the payments service",
        "chaos-restart-db": "stop PostgreSQL for 5 s",
        "chaos-slow-provider": "slow, lossy provider",
    }
    for r in by_label(runs, list(names)):
        o, ch = r["options"], r.get("chaos", {})
        broken = (
            "800 ms latency, 20% lost answers, 10% declines"
            if r["label"] == "chaos-slow-provider"
            else "; ".join(ch.get("events", [])) or "n/a"
        )
        back = ch.get("recovery_seconds")
        rows.append(
            f"| {names[r['label']]} | {broken} | {o['users']} | {r['database']['reservations'].get('confirmed', 0)} "
            f"| {r['database']['messages']} | {r['locust']['Aggregated']['failures']} | {r['database']['oversold_seats']} "
            f"| {'all hold' if not r['database']['violations'] else r['database']['violations']} "
            f"| {'n/a' if back is None else f'{back} s'} |"
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
    for title, builder, marker in (
        ("### E0: what limits throughput (5,000 students, k6)", ceiling_table, "k6-w"),
        ("### The gateway: rate limiting a 5,000-student spike", gateway_table, "e-gateway"),
        ("### The waiting room in front of the app", waiting_room_table, "wr-fifo"),
        ("### Chaos: break something while 300 students buy 30 seats", chaos_table, "chaos-"),
    ):
        if any(r["label"].startswith(marker) for r in runs):
            parts += [title, builder(runs)]
    for code in EXPERIMENTS:
        table = experiments_table(runs, code)
        if table.count("\n") > 3:
            parts.append(table)
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
