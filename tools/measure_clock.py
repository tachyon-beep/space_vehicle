#!/usr/bin/env python3
"""Measure what the mission clock and a private checkpoint would cost on this engine, today.

`docs/decisions/0002-mission-clock-and-continuity.md` prices its options — a multiplier, a
publication cadence, a checkpoint cadence — against figures it measured rather than guessed: the
cost of one `plant.step`, of the per-tick compare-point over the truth, of one executive cycle with
`n` windows, and of serialising and durably writing a checkpoint-shaped payload. AGENTS.md's rule is
that a figure in a document needs a reader in the same commit, because a declaration no tool reads
has already drifted; this is the reader. Run it and compare with the record's table; where they
disagree, the record is what moved.

Two shapes of the truth are measured, and the difference between them is the point. The coolant
transport delay (`plant.md` §3) is a tick-indexed ring born at tick one with one slot per tick of
its declared transit, and for the first many thousand ticks most of those slots are `None`; a `None`
encodes in a handful of bytes where a float encodes in two dozen. A measurement taken at tick ten is
therefore a measurement of an empty ring, and `--full-ring` fills every slot with a float so the
steady state of a mission that has run past one transit is what is timed.

The compare-point is timed under three encoders, and the three rows are the record's evidence
consequence 1 kept side by side. `plant.state_hash` is the §6 contract: since WP08 child 6 it is a
SHA-256 over `json.dumps(truth, sort_keys=True, separators=(",", ":"), allow_nan=False)` with a
`default` that raises on a non-JSON value and an explicit refusal of any non-string key. The
**floor** row is the same `json.dumps` with `default=repr` and no key check — the cost of writing
the truth out at all, which on a full ring is the `repr` of 52,100 floats; the difference between
the two rows is the price of the key check. The **tagged** row is the encoder child 6 retired,
kept here verbatim and nowhere else (`tagged_hash`): it walked the truth in Python and wrapped every
scalar in a one-key map naming its type, and that walk, not the ring, was the 20–26 ms the record
measured. Every run still reports `all_string_keys` so that a truth the adopted encoder would refuse
is visible as a fact about the truth rather than as a crash in the timing loop.

The durable record (ADR 0002 J, WP08 child 4) is priced the same way. One appended tick row and
its `fsync` are timed alone, and `Executive.cycle` is timed again with a state directory, so that
every cycle appends its row and `fsync`s it before publishing: a *quiet* cycle (no command anywhere,
one `fsync`: the row carries the published-tick marks) and a *verdict* cycle (one refused command per
window, so every window writes a result and the `results_written` note costs a second `fsync`). The
`fsync`s of one quiet and one verdict cycle are counted rather than assumed, and the two rows' sizes
are reported. The difference between these rows and `Executive.cycle` without a record is J's cost.

    python3 tools/measure_clock.py                         # the empty-ring table
    python3 tools/measure_clock.py --full-ring             # the steady-state table
    python3 tools/measure_clock.py --windows 1 2 10 --json # as data, for a record's appendix

Nothing here is a pin: the figures are this machine's and this commit's, and the output carries
both so that two runs can be told apart.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console import Executive  # noqa: E402
from plant import (  # noqa: E402
    canonical_state,
    initial_values,
    load_world,
    mission_ladder,
    state_hash,
    step,
    tick_seconds,
)

# How the ring is filled under `--full-ring`: a plausible coolant temperature in kelvin, cycling
# through 97 distinct floats so that the encoding writes every slot out rather than one repeated value.
FILL_BASE_K = 280.35
FILL_STEP_K = 0.0137


def timed(fn: Any, samples: int) -> dict[str, float]:
    """p50, p95 and max of `samples` calls, in microseconds (p95 means little below ~20 samples)."""
    xs: list[float] = []
    for _ in range(samples):
        t0 = time.perf_counter()
        fn()
        xs.append((time.perf_counter() - t0) * 1e6)
    xs.sort()
    return {
        "p50_us": round(statistics.median(xs), 1),
        "p95_us": round(xs[max(0, int(len(xs) * 0.95) - 1)], 1),
        "max_us": round(xs[-1], 1),
    }


def plain_hash(values: dict[str, Any]) -> str:
    """The floor: sorted keys, no whitespace, `repr` for anything JSON cannot say, no key check.

    Not an encoder anything adopts — `default=repr` would collide a non-JSON value with an equal
    `str` — but the cheapest way to write the same bytes, so `state_hash` minus this row is what the
    adopted encoder's refusals cost.
    """
    text = json.dumps(values, sort_keys=True, separators=(",", ":"), default=repr)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def tagged_canonical(value: Any) -> Any:
    """The encoder WP08 child 6 retired, verbatim, so the record's before and after share a table.

    Every scalar became a one-key map naming its type (`{"float": repr(x)}`, `{"int": n}`,
    `{"null": None}`, …), every mapping a `{"map": …}` with its keys coerced by `str`, every sequence
    a `{"seq": […]}`, and anything else `{"other": repr(x)}`. The tags kept `0` and `False` apart —
    which plain JSON does on its own — and the Python walk that applied them visited each of the
    coolant transport ring's 52,100 slots as a function call.
    """
    if isinstance(value, bool):
        return {"bool": value}
    if isinstance(value, int):
        return {"int": value}
    if isinstance(value, float):
        return {"float": repr(value)}
    if isinstance(value, str):
        return {"str": value}
    if isinstance(value, dict):
        return {"map": {str(key): tagged_canonical(item) for key, item in value.items()}}
    if isinstance(value, (list, tuple)):
        return {"seq": [tagged_canonical(item) for item in value]}
    if value is None:
        return {"null": None}
    return {"other": repr(value)}


def tagged_state(values: dict[str, Any]) -> str:
    return json.dumps(tagged_canonical(values), sort_keys=True, separators=(",", ":"))


def tagged_hash(values: dict[str, Any]) -> str:
    return hashlib.sha256(tagged_state(values).encode("utf-8")).hexdigest()[:16]


def all_string_keys(value: Any) -> bool:
    if isinstance(value, dict):
        return all(isinstance(k, str) and all_string_keys(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return all(all_string_keys(v) for v in value)
    return True


def fill_rings(values: dict[str, Any]) -> dict[str, Any]:
    """Every `<state>__delay` ring with each `None` slot replaced by one of 97 distinct floats."""
    filled = dict(values)
    for key, ring in values.items():
        if not (key.endswith("__delay") and isinstance(ring, dict) and "slots" in ring):
            continue
        slots = [
            x if x is not None else FILL_BASE_K + (i % 97) * FILL_STEP_K
            for i, x in enumerate(ring["slots"])
        ]
        filled[key] = {**ring, "slots": slots}
    return filled


def checkpoint_shaped(truth: dict[str, Any], windows: int) -> dict[str, Any]:
    """A payload with the fields ADR 0002 §G lists, so the size and the write are priced honestly."""
    return {
        "format": "vehicle.checkpoint.measure",
        "engine": "0" * 40,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "world_id": "0" * 32,
        "scenario": "nominal",
        "seed": 0,
        "clock": {"m": 1.0, "k": 5, "n": 50, "lag_ceiling_s": 0.0},
        "tick": 12_345,
        "phase": "translunar_coast",
        "phase_entry_seq": 1,
        "tripped_interlocks": [],
        "truth": truth,
        "dwell": {f"state_{i}": {"at_us": i * 20_000, "left": 1.0} for i in range(40)},
        "lineage_head": "0" * 64,
        "receipt": 1_000,
        "rng": {},
        "segments": [{"boot_id": "b" * 32, "wall_start": "2026-10-09T00:00:00+00:00", "tick": 0}],
        "windows": {
            f"slug{i:02d}": {
                "ring_slots": 300,
                "seq": 100,
                "receipts": 50,
                "spend": {"version": 1, "ticks": [1, 2, 3]},
                "deferred": [],
                "arms": {},
                "published_tick": 12_300,
            }
            for i in range(windows)
        },
    }


def durable_writer(directory: Path, blob: bytes, *, fsync: bool) -> Any:
    """Write, fsync and replace: a *lower bound* on ADR 0002 §I's sequence, which also renames the
    previous generation aside and fsyncs the directory."""

    def write() -> None:
        tmp = directory / "checkpoint.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, blob)
            if fsync:
                os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, directory / "checkpoint.json")

    return write


def submit_refusal(diode: Path, slugs: list[str]) -> None:
    """One command no vehicle knows, in every window: a verdict, and a result, everywhere."""
    for slug in slugs:
        (diode / slug / "console.json").write_text(json.dumps({"commands": ["zzz_measure"], "variables": {}}))


def counted_fsyncs(fn: Any) -> int:
    """How many `os.fsync` calls one call of `fn` makes."""
    real = os.fsync
    calls = [0]

    def counting(fd: int) -> None:
        calls[0] += 1
        real(fd)

    os.fsync = counting
    try:
        fn()
    finally:
        os.fsync = real
    return calls[0]


def verdict_cycles(executive: Any, diode: Path, slugs: list[str], samples: int) -> dict[str, float]:
    """`Executive.cycle` with one refused command in every window, the console's write not timed."""
    xs: list[float] = []
    for _ in range(samples):
        submit_refusal(diode, slugs)
        t0 = time.perf_counter()
        executive.cycle()
        xs.append((time.perf_counter() - t0) * 1e6)
    xs.sort()
    return {
        "p50_us": round(statistics.median(xs), 1),
        "p95_us": round(xs[max(0, int(len(xs) * 0.95) - 1)], 1),
        "max_us": round(xs[-1], 1),
    }


def record_costs(world: Any, tmpdir: Path, windows: list[int], samples: int, full_ring: bool) -> dict[str, Any]:
    """The durable record's price: one row appended and `fsync`ed, and a cycle with and without verdicts."""
    result: dict[str, Any] = {}
    row = {
        "tick": 12_345,
        "world_id": "0" * 32,
        "boot_id": "b" * 32,
        "lineage": "1" * 64,
        "state_hash": "2" * 64,
        "effects": [],
        "receipts": [],
        "windows": {},
        "published": {f"slug{i:02d}": {"tick": 12_345, "seq": 12_000} for i in range(max(windows) if windows else 10)},
        "failures": 0,
    }
    line = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(tmpdir / "record.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:

        def append_fsync() -> None:
            os.write(fd, line)
            os.fsync(fd)

        result["row_bytes"] = len(line)
        result["append_fsync"] = timed(append_fsync, min(samples, 100))
        result["append"] = timed(lambda: os.write(fd, line), min(samples, 100))
    finally:
        os.close(fd)
    cycles: dict[str, Any] = {}
    for n in windows:
        slugs = [f"w{i:02d}" for i in range(n)]
        # The same verdict cycle with no record, so the record's share can be read off one run.
        bare_diode = tmpdir / f"bare-diode{n}"
        bare = Executive(world, bare_diode, phase="translunar_coast", scenario="nominal", seed=0)
        try:
            for slug in slugs:
                bare.attach(slug)
            for _ in range(2):
                bare.cycle()
            if full_ring:
                bare.truth = fill_rings(bare.truth)
            bare_verdict = verdict_cycles(bare, bare_diode, slugs, min(samples, 60))
        finally:
            bare.close()
        diode, state = tmpdir / f"record-diode{n}", tmpdir / f"record-state{n}"
        executive = Executive(world, diode, phase="translunar_coast", scenario="nominal", seed=0, state_dir=state)
        try:
            for slug in slugs:
                executive.attach(slug)
            for _ in range(2):
                executive.cycle()
            if full_ring:
                executive.truth = fill_rings(executive.truth)
            quiet_fsyncs = counted_fsyncs(executive.cycle)
            submit_refusal(diode, slugs)
            verdict_fsyncs = counted_fsyncs(executive.cycle)
            quiet = timed(executive.cycle, min(samples, 60))
            verdict = verdict_cycles(executive, diode, slugs, min(samples, 60))
            rows = [json.loads(raw) for raw in executive.journal.read_bytes().splitlines()]
            ticks = [r for r in rows if "event" not in r]
            sizes = {bool(r["receipts"]): len(json.dumps(r, sort_keys=True, separators=(",", ":"))) + 1 for r in ticks}
            cycles[str(n)] = {
                "quiet": quiet,
                "verdict": verdict,
                "verdict_without_record": bare_verdict,
                "quiet_fsyncs": quiet_fsyncs,
                "verdict_fsyncs": verdict_fsyncs,
                "quiet_row_bytes": sizes.get(False),
                "verdict_row_bytes": sizes.get(True),
            }
        finally:
            executive.close()
    result["cycle_by_windows"] = cycles
    return result


def engine_identity(root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=False, timeout=5,
        )
        head = out.stdout.strip() or "unknown"
        dirty = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True, text=True, check=False, timeout=5,
        )
        return f"{head}-dirty" if dirty.stdout.strip() else head
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def measure(root: Path, *, windows: list[int], samples: int, full_ring: bool, warm_ticks: int) -> dict[str, Any]:
    world = load_world(root)
    dt = tick_seconds(world)
    t0 = initial_values(world)
    truth = t0
    for _ in range(warm_ticks):
        truth = step(world, truth, dt, None, [])
    if full_ring:
        truth = fill_rings(truth)
    rings = {
        key: len(ring["slots"])
        for key, ring in truth.items()
        if key.endswith("__delay") and isinstance(ring, dict)
    }
    filled = {
        key: sum(1 for x in truth[key]["slots"] if x is not None) for key in rings
    }
    result: dict[str, Any] = {
        "engine": engine_identity(root),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "dt_s": dt,
        "warm_ticks": warm_ticks,
        "full_ring": full_ring,
        "truth_keys_t0": len(t0),
        "truth_keys": len(truth),
        "delay_rings": {k: {"slots": rings[k], "filled": filled[k]} for k in rings},
        "all_string_keys": all_string_keys(truth),
        "canonical_bytes": len(canonical_state(truth).encode("utf-8")),
        "tagged_bytes": len(tagged_state(truth).encode("utf-8")),
        "state_hash": timed(lambda: state_hash(truth), samples),
        "plain_hash": timed(lambda: plain_hash(truth), samples),
        "tagged_hash": timed(lambda: tagged_hash(truth), samples),
        "step": timed(lambda: step(world, truth, dt, None, []), samples),
    }
    ladder = mission_ladder(world)
    result["mission"] = {
        "phases": len(ladder),
        "ticks": ladder[-1].last_tick,
        "shortest_phase_ticks": min(p.last_tick - p.first_tick for p in ladder),
    }
    with tempfile.TemporaryDirectory(prefix="measure_clock_") as tmp:
        tmpdir = Path(tmp)
        payload = checkpoint_shaped(truth, max(windows) if windows else 10)
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=repr).encode("utf-8")
        result["checkpoint"] = {
            "windows": max(windows) if windows else 10,
            "bytes": len(blob),
            "dumps": timed(
                lambda: json.dumps(payload, sort_keys=True, separators=(",", ":"), default=repr),
                samples,
            ),
            "write_fsync_rename": timed(durable_writer(tmpdir, blob, fsync=True), min(samples, 100)),
            "write_rename": timed(durable_writer(tmpdir, blob, fsync=False), min(samples, 100)),
        }
        cycles: dict[str, Any] = {}
        for n in windows:
            diode = tmpdir / f"diode{n}"
            executive = Executive(world, diode, phase="translunar_coast", scenario="nominal", seed=0)
            try:
                for i in range(n):
                    executive.attach(f"w{i:02d}")
                for _ in range(2):
                    executive.cycle()
                if full_ring:
                    executive.truth = fill_rings(executive.truth)
                cycles[str(n)] = timed(executive.cycle, min(samples, 60))
            finally:
                executive.close()
        result["cycle_by_windows"] = cycles
        result["record"] = record_costs(world, tmpdir, windows, samples, full_ring)
    return result


def render(result: dict[str, Any]) -> str:
    def us(row: dict[str, float]) -> str:
        return f"{row['p50_us'] / 1000:.2f} ms p50, {row['p95_us'] / 1000:.2f} p95, {row['max_us'] / 1000:.2f} max"

    lines = [
        f"engine {result['engine']}  python {result['python']}  {result['platform']}",
        f"dt {result['dt_s']} s; warmed {result['warm_ticks']} tick(s); ring {'full' if result['full_ring'] else 'as warmed'}",
        f"truth keys: {result['truth_keys_t0']} at t=0, {result['truth_keys']} now; all keys are strings: {result['all_string_keys']}",
    ]
    for key, ring in result["delay_rings"].items():
        lines.append(f"delay ring {key}: {ring['slots']} slots, {ring['filled']} filled")
    lines += [
        f"canonical_state bytes: {result['canonical_bytes']:,} (plain sorted JSON); the retired tagged encoding: {result['tagged_bytes']:,}",
        f"state_hash (§6, sorted compact JSON since WP08.6): {us(result['state_hash'])}",
        f"sha256(plain sorted JSON, default=repr) — the floor: {us(result['plain_hash'])}",
        f"state_hash as tagged before WP08.6 (reference):    {us(result['tagged_hash'])}",
        f"plant.step, no effects:                            {us(result['step'])}",
    ]
    ck = result["checkpoint"]
    lines += [
        f"checkpoint-shaped payload, {ck['windows']} window(s): {ck['bytes']:,} bytes; json.dumps {us(ck['dumps'])}",
        f"  write + fsync + rename: {us(ck['write_fsync_rename'])}",
        f"  write + rename:         {us(ck['write_rename'])}",
    ]
    for n, row in result["cycle_by_windows"].items():
        lines.append(f"Executive.cycle, {n} window(s): {us(row)}")
    record = result["record"]
    lines += [
        f"record: one tick row ({record['row_bytes']:,} bytes) appended + fsync: {us(record['append_fsync'])}",
        f"  appended alone:                         {us(record['append'])}",
    ]
    for n, row in record["cycle_by_windows"].items():
        lines += [
            f"Executive.cycle with the record, {n} window(s): quiet {us(row['quiet'])} ({row['quiet_fsyncs']} fsync, "
            f"row {row['quiet_row_bytes']:,} B)",
            f"  verdict in every window:              {us(row['verdict'])} ({row['verdict_fsyncs']} fsync, "
            f"row {row['verdict_row_bytes']:,} B)",
            f"  the same verdict cycle, no record:    {us(row['verdict_without_record'])}",
        ]
    m = result["mission"]
    lines.append(f"mission: {m['phases']} phases, {m['ticks']:,} ticks; shortest phase {m['shortest_phase_ticks']:,} ticks")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dir", default=str(Path(__file__).resolve().parent.parent), help="the vehicle root")
    parser.add_argument("--windows", type=int, nargs="*", default=[1, 2, 10], help="window counts to cycle with")
    parser.add_argument("--samples", type=int, default=200, help="timing samples per quantity")
    parser.add_argument("--warm-ticks", type=int, default=10, help="ticks to step before measuring")
    parser.add_argument("--full-ring", action="store_true", help="fill every delay-ring slot with a float first")
    parser.add_argument("--json", action="store_true", help="emit the measurements as JSON")
    args = parser.parse_args(argv)
    result = measure(
        Path(args.dir),
        windows=list(args.windows),
        samples=max(1, args.samples),
        full_ring=args.full_ring,
        warm_ticks=max(1, args.warm_ticks),
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
