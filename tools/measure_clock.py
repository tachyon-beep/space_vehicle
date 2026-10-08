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

The compare-point is timed under two encoders: `plant.state_hash`, which is the §6 contract today,
and a SHA-256 over `json.dumps(truth, sort_keys=True, separators=(",", ":"), default=repr)`. The
second is what ADR 0002 asks the maintainer to consider adopting: it distinguishes every pair the
first's tags exist to separate (`0` and `False`, `1` and `1.0`, `0.0` and `-0.0`, `"1"` and `1`,
`None` and `0`), collides a list with a tuple exactly where the first does, and requires every key
to be a string — which every run checks (`all_string_keys`) rather than assumes. `default=repr` is
used here only to price the encoding; an adopted encoder must raise on a non-JSON value instead, or
such a value would encode as a string and collide with an equal `str`.

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
    """The candidate encoder: sorted keys, no whitespace, `repr` for anything JSON cannot say."""
    text = json.dumps(values, sort_keys=True, separators=(",", ":"), default=repr)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


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
        "plain_bytes": len(json.dumps(truth, sort_keys=True, separators=(",", ":"), default=repr).encode("utf-8")),
        "state_hash": timed(lambda: state_hash(truth), samples),
        "plain_hash": timed(lambda: plain_hash(truth), samples),
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
        f"canonical_state bytes: {result['canonical_bytes']:,}; plain sorted JSON bytes: {result['plain_bytes']:,}",
        f"state_hash (canonical, §6 today): {us(result['state_hash'])}",
        f"sha256(plain sorted JSON):        {us(result['plain_hash'])}",
        f"plant.step, no effects:           {us(result['step'])}",
    ]
    ck = result["checkpoint"]
    lines += [
        f"checkpoint-shaped payload, {ck['windows']} window(s): {ck['bytes']:,} bytes; json.dumps {us(ck['dumps'])}",
        f"  write + fsync + rename: {us(ck['write_fsync_rename'])}",
        f"  write + rename:         {us(ck['write_rename'])}",
    ]
    for n, row in result["cycle_by_windows"].items():
        lines.append(f"Executive.cycle, {n} window(s): {us(row)}")
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
