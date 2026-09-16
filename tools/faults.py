#!/usr/bin/env python3
"""Schedule the faults a run will suffer, from the policies the domains already declare.

The vehicle has 128 declared faults and, until this file, **no way to fail**. Every domain carries
a `fault_policy.yaml` naming what can break, what it looks like, what the service does about it
without asking, and how it seeds — and nothing read the seeding. A challenge whose vehicle never
breaks is a piloting exercise, and the 118 entries were the adversity sitting unused.

Three things make this a schedule rather than a dice roll:

  - **the hazard rates are the domains' own.** 66 faults seed on `{hazard, unit: per_h}` and the
    rate is a number a domain argued for in its provenance, not one chosen here.
  - **the streams are name-keyed**, per `simulator-design.md` §3.4: "Every stream derives by name:
    `(master_seed, domain, component_id, purpose)`. ... Name-keyed means **adding a component is not
    class-breaking**." That is the property `--check` tests, and it is the difference between a
    schedule a prior run can be compared against and one that silently changes when a domain
    lands.
  - **the conditional faults are reported as armed and not scheduled.** 62 of the 128 seed on
    `on_demand_p` or `coupled_to` rather than on a rate, because they can only occur when a trigger
    holds — "any fault that removes a publisher's input without removing the publisher", "any
    redundant group with two or more live members". Sampling those from a rate would be inventing a
    rate the domain deliberately did not give; they are listed with their triggers instead, and the
    plant arms them when the trigger holds.

    python3 tools/faults.py --seed 20260912                # the run's schedule
    python3 tools/faults.py --seed 20260912 --json         # the same, as data
    python3 tools/faults.py --check                        # name-keying is not class-breaking
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_vehicle import in_seeding_pool  # noqa: E402  (the linter is the bottom of the graph)
from plant import Unconfigured  # noqa: E402  (a sibling tool, not a package)

# The mission's own duration, from `mission.yaml`'s phase ladder: eight phases summing to exactly
# 192.0 h. Read from the file rather than repeated, because a schedule over a duration the mission
# does not have is a schedule for a different mission.
DEFAULT_HOURS = 192.0


@dataclass
class Fault:
    """One declared fault, with the policy fields the schedule needs."""

    id: str
    domain: str
    component: str
    kind: str
    seeding: dict[str, Any]
    perturbs: list[str]
    mechanism: str
    response: dict[str, Any]

    @property
    def hazard(self) -> float | None:
        """The per-hour rate, or None when the fault is conditional rather than stochastic."""
        if self.seeding.get("unit") == "per_h" and isinstance(
            self.seeding.get("hazard"), (int, float)
        ):
            return float(self.seeding["hazard"])
        return None

    @property
    def trigger(self) -> str | None:
        return self.seeding.get("trigger") or self.seeding.get("coupled_to")


def load_faults(root: Path) -> list[Fault]:
    faults: list[Fault] = []
    for path in sorted(p for p in (root / "domains").iterdir() if p.is_dir()):
        policy = yaml.safe_load((path / "fault_policy.yaml").read_text()) or {}
        for entry in policy.get("faults") or []:
            faults.append(
                Fault(
                    id=str(entry.get("id")),
                    domain=path.name,
                    component=str(entry.get("component", "")),
                    kind=str(entry.get("kind", "")),
                    seeding=entry.get("seeding") or {},
                    perturbs=[str(c) for c in entry.get("perturbs") or []],
                    mechanism=str(entry.get("mechanism", "")),
                    response=entry.get("response") or {},
                )
            )
    return faults


def describe_pool(pool: dict[str, Any]) -> str:
    """A pool's selectors, as a label for the schedule: what was placed, and by what rule."""
    parts = []
    if pool.get("kinds"):
        parts.append("kinds " + "/".join(str(k) for k in pool["kinds"]))
    if pool.get("hazards"):
        parts.append("hazards " + "/".join(str(h) for h in pool["hazards"]))
    return " or ".join(parts) if parts else "nothing"


def stream(master_seed: int, fault: Fault, purpose: str = "seeding") -> random.Random:
    """The fault's own generator, derived by name rather than by position.

    `simulator-design.md:190-198` fixes the key as `(master_seed, domain, component_id, purpose)`
    and gives the reason: "Twelve domains arrive incrementally over months. With a shared generator
    — or index-keyed derivation — adding thruster 9 reshuffles every existing stream and silently
    invalidates every prior run."

    The digest is over the four parts joined by a separator that cannot appear in a domain or
    component name, so `("a", "bc")` and `("ab", "c")` cannot collide into one stream. SHA-256
    rather than `hash()`, because `hash()` is salted per process and this number has to be the same
    in the run that schedules a fault and the rerun that explains it.
    """
    key = "\x1f".join([str(master_seed), fault.domain, fault.component, purpose])
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def arrivals(hazard: float, hours: float, rng: random.Random) -> list[float]:
    """A Poisson process at `hazard` per hour over `hours`, as a list of event times.

    Exponential inter-arrivals, which is what a constant hazard rate *means* — the rate is
    memoryless, so the waiting time to the next event does not depend on how long it has been since
    the last. Sampling a count and scattering it uniformly would be the easy mistake and it is a
    different process: it makes events regular, and regularity is exactly what a fleet should not
    be able to rely on.
    """
    times: list[float] = []
    now = 0.0
    while True:
        # `random()` can return 0.0, and `log(0)` is a domain error; the smallest positive double
        # keeps the draw finite and is a far more likely event than the draw it replaces.
        now += -math.log(max(rng.random(), sys.float_info.min)) / hazard
        if now > hours:
            return times
        times.append(now)


@dataclass
class Posture:
    """One row of `apollo_diode.md:370-374`'s difficulty scaling, as `mission.yaml` declares it."""

    id: str
    critical_hazard_per_h: float
    noncritical_hazard_per_h: float
    failure_on_demand_p: float
    seeded_faults: str
    gm_disposition: str = ""
    # What this posture *places*, as `mission.yaml` declares it: a selector over fault kinds and
    # hazard classes. The classes are named, not spelled as rates — `critical` means "hazard equal
    # to the nominal posture's critical baseline", and that baseline is a declaration rather than a
    # constant in this file.
    guaranteed_pool: dict[str, Any] = field(default_factory=dict)
    optional_pool: dict[str, Any] = field(default_factory=dict)

    def hazard_factor(self, nominal: Posture) -> float:
        """The multiplier the posture applies to every spontaneous hazard rate.

        **It is one number, and that is a property of the table rather than a convenience.** The
        critical and noncritical rows are separate columns because they are separate baselines —
        2e-5 and 2e-4 — but *both* move by ×5 at `degraded` and ×10 at `crisis`, so a fault's
        scaling does not depend on knowing which class it is in. That matters because the 118
        policies declare a bare `hazard` and nothing else: 39 sit exactly on a baseline and 19 sit
        between them (COM-09 at 1e-4 is 5× critical and 0.5× noncritical), so a class-dependent
        factor would be undefined for a third of the corpus. `check_vehicle` refuses a posture
        table whose two rows do not move together, because that is the day this stops being true.
        """
        factors = {
            self.critical_hazard_per_h / nominal.critical_hazard_per_h,
            self.noncritical_hazard_per_h / nominal.noncritical_hazard_per_h,
        }
        if len(factors) != 1:
            raise Unconfigured(
                "mission.yaml#scenario_postures",
                f"posture {self.id!r} scales its critical and noncritical hazards by different "
                f"factors ({sorted(factors)}), so a fault's new rate depends on a class the fault "
                "policies do not declare",
            )
        return factors.pop()

    def on_demand_factor(self, nominal: Posture) -> float:
        """Demand-failure probability scales by its own factor: ×5 at `degraded`, ×20 at `crisis`.

        Separate from the hazard factor because apollo's table says so — `1e-4 → 2e-3` is ×20 where
        the hazard columns move ×10 — and folding the two together would quietly halve the crisis
        posture's demand failures.
        """
        return self.failure_on_demand_p / nominal.failure_on_demand_p


def load_postures(root: Path) -> dict[str, Posture]:
    mission = yaml.safe_load((root / "mission.yaml").read_text()) or {}
    postures: dict[str, Posture] = {}
    for row in mission.get("scenario_postures") or []:
        seeds = row.get("seeds") or {}
        postures[str(row.get("id"))] = Posture(
            guaranteed_pool=dict(seeds.get("guaranteed") or {}),
            optional_pool=dict(seeds.get("optional") or {}),
            id=str(row.get("id")),
            critical_hazard_per_h=float(row.get("critical_hazard_per_h", 0.0)),
            noncritical_hazard_per_h=float(row.get("noncritical_hazard_per_h", 0.0)),
            failure_on_demand_p=float(row.get("failure_on_demand_p", 0.0)),
            seeded_faults=str(row.get("seeded_faults", "")),
            gm_disposition=str(row.get("gm_disposition", "")),
        )
    return postures


def schedule(
    faults: list[Fault],
    master_seed: int,
    hours: float,
    *,
    posture: Posture | None = None,
    nominal: Posture | None = None,
) -> tuple[list[dict], list[Fault]]:
    """Every fault's events over the mission, and the faults that cannot be scheduled.

    `posture` scales the rates, and until it existed the difficulty knob did nothing: `mission.yaml`
    declares three postures differing by 10× in critical hazard and 20× in demand failure, the fault
    policies carry the *nominal* baselines as if they were absolute, and the scheduler read the
    policies and never the postures. A `crisis` run and a `nominal` run produced the same faults.
    """
    factor = 1.0
    # The on-demand factor is *not* applied here. An `on_demand_p` fault is conditional — it fires
    # when its trigger holds — and this scheduler does not evaluate triggers, so there is no event
    # for a scaled probability to change. It is applied where a demand draw actually happens:
    # `optional_sensor_defect`, and the plant when it arms the conditional half.
    if posture is not None and nominal is not None:
        factor = posture.hazard_factor(nominal)
    events: list[dict] = []
    armed: list[Fault] = []
    for fault in faults:
        rate = fault.hazard
        if rate is None:
            armed.append(fault)
            continue
        for moment in arrivals(rate * factor, hours, stream(master_seed, fault)):
            events.append(
                {
                    "met_h": round(moment, 6),
                    "fault": fault.id,
                    "domain": fault.domain,
                    "component": fault.component,
                    "kind": fault.kind,
                    "response": fault.response.get("kind"),
                    "perturbs": fault.perturbs,
                }
            )
    events.sort(key=lambda e: (e["met_h"], e["fault"]))
    return events, armed


def guaranteed_seed(
    faults: list[Fault],
    posture: Posture,
    master_seed: int,
    hours: float,
    *,
    nominal: Posture | None = None,
) -> dict[str, Any] | None:
    """The fault a posture promises will happen, placed rather than sampled.

    `mission.yaml#scenario_postures.seeded_faults` is prose — "none", "one latent or noncritical
    primary", "one guaranteed major primary plus an optional latent sensor defect" — and
    `apollo_diode.md:368` says why the mechanism exists at all: a common-cause event is "explicitly
    seeded rather than relying on tiny random probability". At the nominal critical hazard a
    192-hour mission expects 0.0038 events from any one fault, so a posture that promised a major
    failure and then sampled for it would deliver an empty mission most of the time.

    **Two readings are being made here and both are stated so they can be argued with.** "Major"
    is read as the *critical class*, which is the only severity the corpus declares. "Primary" is
    read as the fault itself rather than its chain — and the chains *are* linked to faults now:
    `coupling.yaml#failure_chains[].realised_by` names them and `check_chain_faults` holds each one
    to perturbing a clue of the chain it realises. What is still a reading rather than a
    declaration is **which chain the seed produces**: the pool is the class, so a `crisis` run
    seeds a critical fault and not the crisis a chain names, and the corpus records that in
    `mission.yaml#open_debts` rather than pretending the two are the same. "Latent sensor defect"
    is read as `kind: instrument`, which is the corpus's own name for a measurement that is wrong
    while the system is fine.
    """
    if nominal is None or not posture.guaranteed_pool:
        return None
    rng = random.Random(
        int.from_bytes(hashlib.sha256(f"{master_seed}:guaranteed".encode()).digest()[:8], "big")
    )
    baselines = {
        "critical": nominal.critical_hazard_per_h,
        "noncritical": nominal.noncritical_hazard_per_h,
    }
    pool = [
        f for f in faults if in_seeding_pool(f.kind, f.hazard, posture.guaranteed_pool, baselines)
    ]
    label = f"guaranteed seed: {describe_pool(posture.guaranteed_pool)}"
    if not pool:
        return None
    chosen = pool[rng.randrange(len(pool))]
    return {
        "met_h": round(rng.uniform(0.1 * hours, 0.9 * hours), 6),
        "fault": chosen.id,
        "domain": chosen.domain,
        "component": chosen.component,
        "kind": chosen.kind,
        "response": chosen.response.get("kind"),
        "perturbs": chosen.perturbs,
        "guaranteed": label,
    }


def optional_sensor_defect(
    faults: list[Fault],
    posture: Posture,
    master_seed: int,
    hours: float,
    *,
    nominal: Posture | None = None,
    include: bool = False,
) -> dict[str, Any] | None:
    """The crisis posture's "optional latent sensor defect" — offered, and included only on request.

    **"Optional" is read as the GM's decision rather than a probability draw, and the reason is in
    the posture's own row.** Crisis carries `gm_disposition: white_team`, and apollo's table gives
    the clause as a *feature of the posture* — "one guaranteed major primary **plus an optional**
    latent sensor defect" — beside a `seeded_faults` column whose other entries are "none" and "one
    latent or noncritical primary". Sampling it instead would mean choosing a probability, and the
    only rate the posture declares is `failure_on_demand_p`, which apollo defines as "Bernoulli
    p per activation" — a per-*activation* rate, not a per-mission one. Using it per mission would
    understate by the number of activations in 192 hours, which is thousands; inventing a per-
    mission number would be inventing a number. So the scheduler offers it, names the pool, and
    includes one only when a caller asks.
    """
    if nominal is None or not posture.optional_pool:
        return None
    baselines = {
        "critical": nominal.critical_hazard_per_h,
        "noncritical": nominal.noncritical_hazard_per_h,
    }
    pool = [
        f for f in faults if in_seeding_pool(f.kind, f.hazard, posture.optional_pool, baselines)
    ]
    if not include:
        return None
    rng = random.Random(
        int.from_bytes(hashlib.sha256(f"{master_seed}:sensor".encode()).digest()[:8], "big")
    )
    if not pool:
        return None
    chosen = pool[rng.randrange(len(pool))]
    return {
        "met_h": round(rng.uniform(0.1 * hours, 0.9 * hours), 6),
        "fault": chosen.id,
        "domain": chosen.domain,
        "component": chosen.component,
        "kind": chosen.kind,
        "response": chosen.response.get("kind"),
        "perturbs": chosen.perturbs,
        "guaranteed": f"optional seed: {describe_pool(posture.optional_pool)}",
    }


def check_name_keying(faults: list[Fault], master_seed: int, hours: float) -> int:
    """Adding a fault must not move any existing fault's events.

    This is `simulator-design.md:190-198`'s "adding a component is not class-breaking" as an
    assertion rather than a remark, and it is the property that makes a recorded run comparable to
    a later one. A mutation is *not* applied to the corpus: the check appends a synthetic fault and
    compares the other faults' schedules, which is the same question without editing a domain.
    """
    before, _ = schedule(faults, master_seed, hours)
    synthetic = Fault(
        id="ZZZ-99-name-keying-probe",
        domain="gnu",
        component="nonexistent",
        kind="discrete",
        seeding={"hazard": 1.0, "unit": "per_h"},
        perturbs=[],
        mechanism="a fault that exists only to prove the streams are keyed by name",
        response={"kind": "advisory"},
    )
    after, _ = schedule(faults + [synthetic], master_seed, hours)
    kept = [e for e in after if e["fault"] != synthetic.id]
    same = [(a["met_h"], a["fault"]) for a in before] == [(b["met_h"], b["fault"]) for b in kept]
    probe_hits = len([e for e in after if e["fault"] == synthetic.id])
    print(f"  {len(before)} event(s) before, {len(kept)} after adding one fault")
    print(f"  the synthetic fault itself drew {probe_hits} event(s), so its stream is live")
    if not same:
        print("  NAME-KEYING BROKEN: adding a fault moved an existing fault's events")
        return 1
    print("  NAME-KEYING HOLDS: no existing fault's events moved")
    if probe_hits == 0:
        print("  but the probe drew nothing, so the comparison proved nothing")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dir", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--seed", type=int, default=0, help="the run's master seed")
    parser.add_argument("--hours", type=float, default=DEFAULT_HOURS)
    parser.add_argument("--json", action="store_true", help="emit the schedule as JSON")
    parser.add_argument("--list", action="store_true", help="list every fault and its seeding form")
    parser.add_argument("--check", action="store_true", help="assert the streams are name-keyed")
    parser.add_argument(
        "--sensor-defect",
        action="store_true",
        help="include the crisis posture's optional latent sensor defect (the GM's call, not a draw)",
    )
    parser.add_argument(
        "--posture",
        default="nominal",
        help="the scenario posture to scale the rates by: nominal, degraded or crisis",
    )
    args = parser.parse_args(argv)

    root = Path(args.dir)
    mission = yaml.safe_load((root / "mission.yaml").read_text()) or {}
    phases = [
        (str(p.get("id")), float(p.get("duration_h", 0))) for p in mission.get("phases") or []
    ]
    if args.hours == DEFAULT_HOURS and phases:
        args.hours = sum(d for _, d in phases)
    # A phase ladder that does not reach the schedule's horizon means the schedule covers a
    # mission this vehicle does not fly, so the two are checked against each other rather than
    # assumed equal.
    ladder = sum(d for _, d in phases)
    if phases and abs(ladder - args.hours) > 1e-9:
        sys.stderr.write(
            f"the phase ladder sums to {ladder} h and the schedule runs {args.hours} h\n"
        )
        return 3

    faults = load_faults(root)
    if not faults:
        sys.stderr.write(f"no faults under {root / 'domains'}\n")
        return 3

    if args.check:
        print(f"fault stream derivation, master seed {args.seed} over {args.hours} h")
        return check_name_keying(faults, args.seed, args.hours)

    if args.list:
        # **The magnitude, which nothing read at all until round 35.** The `seeding` block declares
        # a fault in two halves — when it fires and how far it moves the channel it perturbs — and
        # only the first was read. The second had twenty spellings (`bias_walk` and five suffixed
        # variants, four `drift_*`, `rate_kg_per_h` beside `rate_per_s`, and four sentences in the
        # field meant for a number), the unit was inside the key name rather than in a field, and
        # nothing compared it with the channel. `magnitude`, `magnitude_unit` and
        # `magnitude_channel` are the one pair and its host, and this prints them: a fault is now
        # something the adversary can size, not only schedule.
        for fault in faults:
            form = "hazard" if fault.hazard is not None else "armed"
            rate = f"{fault.hazard:g}/h" if fault.hazard is not None else f"when {fault.trigger}"
            seeding = fault.seeding
            amount = seeding.get("magnitude")
            if isinstance(amount, (int, float)):
                unit_text = seeding.get("magnitude_unit")
                if unit_text == "UNCONFIGURED":
                    # A number whose unit nobody has stated. Printed as the owe it is rather than
                    # with a `?` where a unit would go, because the two owe different things.
                    size = f"{amount:g} with unit owed"
                else:
                    size = f"{amount:g} {unit_text} on {seeding.get('magnitude_channel', '?')}"
            elif amount == "UNCONFIGURED":
                size = "magnitude owed"
                if seeding.get("magnitude_unit") == "UNCONFIGURED":
                    size += " (unit too)"
            else:
                size = seeding.get("condition") or "-"
            condition = seeding.get("condition")
            if condition and isinstance(amount, (int, float)):
                size = f"{size} ({str(condition)[:28]})"
            print(f"  {fault.id:38} {fault.domain:12} {fault.kind:22} {form:6} {rate:14} {size}")
        return 0

    postures = load_postures(root)
    if args.posture not in postures:
        sys.stderr.write(
            f"posture {args.posture!r} is not one of {sorted(postures)} in mission.yaml\n"
        )
        return 3
    posture = postures[args.posture]
    nominal = postures.get("nominal", posture)
    try:
        events, armed = schedule(faults, args.seed, args.hours, posture=posture, nominal=nominal)
        extras = [
            found
            for found in (
                guaranteed_seed(faults, posture, args.seed, args.hours, nominal=nominal),
                optional_sensor_defect(
                    faults,
                    posture,
                    args.seed,
                    args.hours,
                    nominal=nominal,
                    include=args.sensor_defect,
                ),
            )
            if found
        ]
        for extra in extras:
            extra["scaled_by"] = args.posture
            events.append(extra)
    except Unconfigured as exc:
        sys.stderr.write(f"{exc}\n")
        return 3
    events.sort(key=lambda e: (e["met_h"], e["fault"]))
    if args.json:
        json.dump(
            {
                "master_seed": args.seed,
                "hours": args.hours,
                "posture": args.posture,
                "hazard_factor": posture.hazard_factor(nominal),
                "on_demand_factor": posture.on_demand_factor(nominal),
                "seeded_faults": posture.seeded_faults,
                "events": events,
                "armed": [{"fault": f.id, "domain": f.domain, "trigger": f.trigger} for f in armed],
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        return 0

    by_domain = collections.Counter(e["domain"] for e in events)
    print(f"master seed {args.seed} over {args.hours} h, posture {args.posture!r}")
    print(
        f"  hazard x{posture.hazard_factor(nominal):g}, demand x{posture.on_demand_factor(nominal):g}"
        f"  ·  seeded faults: {posture.seeded_faults}"
    )
    print(
        f"  {len(faults)} declared faults: {len(faults) - len(armed)} stochastic, {len(armed)} armed"
    )
    print(f"  {len(events)} scheduled event(s) across {len(by_domain)} domain(s)")
    if posture.id == "crisis":
        offered = sum(1 for f in faults if f.kind == "instrument")
        print(
            f"  the crisis posture also offers one latent sensor defect from {offered} instrument "
            f"fault(s); pass --sensor-defect to seat it"
        )
    if events:
        print()
        for event in events:
            mark = " *" if event.get("guaranteed") else "  "
            print(
                f" {mark}MET {event['met_h']:>8.3f} h  {event['fault']:38} {event['kind']:22} "
                f"{event['response'] or '-':9} -> {', '.join(event['perturbs'][:3])}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
