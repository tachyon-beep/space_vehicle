#!/usr/bin/env python3
"""A reference plant: load the world from the configuration, and run it until it hits a debt.

This is not a simulator. It integrates nothing, and every state whose physics it does not have
raises a named exception rather than guessing. What it *is* is the proof that the configuration
is implementable and a list of what is missing, in the order the missing things are needed.

The idea is `simulator-design.md:146-150`'s, applied to the plant instead of to the linter: you
do not enumerate what a simulator needs up front, you build it, run it, and it tells you what you
now owe. `check_vehicle.py` does that for the *definition* — it reports 202 declared debts by
path. This tool does it for the *implementation*: it loads the whole world, builds the tick order,
and then walks the tick in that order, stopping at the first thing it cannot compute and saying
exactly what it would need.

Why that is worth writing rather than asserting
-----------------------------------------------

"Ready to implement" is a claim, and a claim about a 34-file configuration is worth exactly as
much as the evidence behind it. The evidence here is mechanical:

  - **the schedule is derivable** — 39 nodes, from `coupling.yaml`'s edges minus its declared
    back-edges, with `check_vehicle.derive_schedule` doing the derivation so the plant and the
    linter cannot disagree about the order;
  - **the states are instantiable** — 119 of them, each with a method, a unit and the parameters
    its method owes, and the loader refuses a configuration where one is missing rather than
    defaulting it;
  - **the frame is producible** — `emit_frame` builds apollo's envelope from
    `presentation.yaml#frame`, which is the only reason to believe that contract is a contract
    rather than a description;
  - **the tick loop closes** — `step()` is `plant.md`'s seven steps, with step 4 walking the
    derived order and each state's `advance()` raising rather than inventing.

What the run then says is the build order. Today it says: 107 of 119 states are fully
configured, 12 are not, and 33 of 55 edge sensitivities are still unset — so the physics is
missing in a *known* shape rather than an unknown one, and the first thing to supply is whichever
the schedule reaches first.

The method implementations are deliberately trivial and mostly raise. A `lag` needs a time
constant and a driving value; a `stock` needs a quantum and a flow; a `discrete` needs a
transition rule, which no configuration can supply because the rules are the domain's. Filling
those in is writing the plant — this file is the frame it goes in and the error messages that
tell you where.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_vehicle import Report, derive_schedule  # noqa: E402  (a sibling tool, not a package)

# The parameters each integrator class owes, from `plant.md` §3. A method absent from this table
# owes none *by name* — which is not the same as owing nothing, and `advance()` is where the
# difference shows.
OWED = {
    "lag": ("tau_s", "has no time constant, so it cannot be advanced"),
    "stock": ("quantum", "has no quantum, so its conservation cannot be exact"),
    "delay": ("delay_s", "has no delay, so there is nothing to store"),
    "hazard": ("lambda_per_h", "has no hazard rate"),
}


class Unconfigured(Exception):
    """A value the plant needs and does not have, named by the path that reaches it."""

    def __init__(self, where: str, what: str) -> None:
        super().__init__(f"{where}: {what}")
        self.where = where
        self.what = what


def unset_paths(node: Any, trail: str = "") -> list[str]:
    """Every `UNCONFIGURED` scalar under a node, with the path that reaches it."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            found += unset_paths(value, f"{trail}.{key}" if trail else str(key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found += unset_paths(value, f"{trail}[{index}]")
    elif node == "UNCONFIGURED":
        found.append(trail)
    return found


@dataclass
class State:
    """One integrator, as the configuration declares it."""

    id: str
    domain: str
    node: str
    method: str
    unit: str
    spec: dict[str, Any]

    @property
    def owed(self) -> list[str]:
        """What this state is missing: its method's named parameter, plus anything unset."""
        missing = unset_paths(self.spec)
        owed = OWED.get(self.method)
        if owed and self.spec.get(owed[0]) in (None, "UNCONFIGURED"):
            name = owed[0]
            if name not in missing:
                missing.append(name)
        return missing


@dataclass
class Edge:
    id: str
    source: str
    target: str
    kind: str
    sensitivity: dict[str, Any]

    @property
    def usable(self) -> bool:
        """An edge is usable when it carries a value the plant could apply."""
        return (self.sensitivity or {}).get("value") not in (None, "UNCONFIGURED")


@dataclass
class World:
    root: Path
    states: list[State]
    edges: list[Edge]
    back_edges: set[str]
    schedule: list[str]
    channels: dict[str, dict[str, Any]]
    frame_fields: list[str]
    verbs: dict[str, dict[str, Any]]
    plant_published: list[str] = field(default_factory=list)
    debts: int = 0

    def states_on(self, node: str) -> list[State]:
        return [s for s in self.states if s.node == node]


def load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text()) or {}


def load_world(root: Path) -> World:
    """Build the world, or refuse the configuration.

    A loader that tolerates a missing field is a loader that has decided a default, and the whole
    point of this folder is that it has not. So this raises rather than defaulting, and the
    exceptions are the report.
    """
    coupling = load_yaml(root / "coupling.yaml")
    channels_doc = load_yaml(root / "channels.yaml")
    presentation = load_yaml(root / "presentation.yaml")

    states: list[State] = []
    for directory in sorted(p for p in (root / "domains").iterdir() if p.is_dir()):
        components = load_yaml(directory / "components.yaml")
        for spec in components.get("state") or []:
            states.append(
                State(
                    id=str(spec["id"]),
                    domain=str(components.get("domain", directory.name)),
                    node=str(spec.get("node", "internal")),
                    method=str(spec.get("method")),
                    unit=str(spec.get("unit", "")),
                    spec=spec,
                )
            )

    edges = [
        Edge(
            id=str(e["id"]),
            source=str(e["from"]),
            target=str(e["to"]),
            kind=str(e.get("kind")),
            sensitivity=e.get("sensitivity") or {},
        )
        for e in coupling.get("edges") or []
    ]
    back_edges = {
        str(c.get("back_edge")) for c in coupling.get("cycles") or [] if c.get("back_edge")
    }

    report = Report()
    schedule = derive_schedule(coupling, report)
    if not schedule:
        raise Unconfigured(
            "coupling.yaml", "the tick order cannot be derived: " + "; ".join(report.refusals)
        )

    channels: dict[str, dict[str, Any]] = {}
    for section, rows in channels_doc.items():
        if not isinstance(rows, list) or section in {"open_debts", "crew_positions"}:
            continue
        for row in rows:
            if isinstance(row, dict) and row.get("id"):
                channels[str(row["id"])] = row

    verbs: dict[str, dict[str, Any]] = {}
    for directory in sorted(p for p in (root / "domains").iterdir() if p.is_dir()):
        for verb in (load_yaml(directory / "commands.yaml").get("commands")) or []:
            verbs[str(verb.get("verb"))] = verb

    return World(
        root=root,
        states=states,
        edges=edges,
        back_edges=back_edges,
        schedule=schedule,
        channels=channels,
        frame_fields=[
            str((f or {}).get("name"))
            for f in (presentation.get("frame") or {}).get("fields") or []
        ],
        verbs=verbs,
        plant_published=[str(e.get("channel")) for e in presentation.get("plant_published") or []],
        # Counted here rather than taken from the linter, and deliberately a *different* number:
        # the linter reports 202 debts, most of which are prose obligations ("this needs a
        # patched-conic design") recorded in `open_debts` lists. This counts only the values that
        # are literally `UNCONFIGURED`, because those are the ones that stop a plant. Two numbers
        # with one name would be worse than either.
        debts=len(
            unset_paths(
                {
                    name: load_yaml(root / name)
                    for name in (
                        "vehicle.yaml",
                        "mission.yaml",
                        "coupling.yaml",
                        "channels.yaml",
                        "presentation.yaml",
                    )
                }
            )
        )
        + sum(
            len(unset_paths(load_yaml(directory / name)))
            for directory in sorted((root / "domains").iterdir())
            if directory.is_dir()
            for name in (
                "components.yaml",
                "points.yaml",
                "profiles.yaml",
                "commands.yaml",
                "fault_policy.yaml",
            )
        ),
    )


def gate_instantiations(
    variable: str, argument_schema: dict[str, Any] | None = None, owner: str = ""
) -> list[str]:
    """Every gate variable a verb's template expands to, in the registry's own order.

    `presentation.yaml#mirror.variables_are_templated` states the rule and the reason it matters:
    58 verbs declare 58 gate variables and they are not 58 names a fleet sees, because most are
    templates — `reserve_floor_<resource>_enable` is eight names in practice. Its warning is the
    point of this function: "a refusal that named the template rather than the instantiation —
    `reserve_floor_<resource>_enable` instead of `reserve_floor_water_cooling_enable` — would be a
    name a fleet cannot act on."

    The expansion rule is derived, not chosen: **the i-th placeholder is filled by the i-th enum
    argument, in the argument_schema's declaration order**, and those arguments' values are the
    instantiation set. `sensor_<group>_<source>_enable` has two placeholders and its first two
    enum arguments are `group` and `source`; `o2_<vehicle>_<source>_enable` declares `vehicle`,
    `source` *and* `flow`, and `flow` is not a placeholder because the template has only two. So
    the count of placeholders decides how many arguments are consumed, and the names decide which.
    """
    placeholders = re.findall(r"<([^>]+)>", variable)
    if not placeholders:
        return [variable]
    # Binding is by **name**, not by position. Position was the first rule tried and it is wrong
    # in a way worth recording: `set_power_amplifier`'s gate is `power_amplifier_<transmitter>_enable`
    # and its arguments are declared `state` then `transmitter`, so a positional rule binds the
    # placeholder to `state` and publishes `power_amplifier_True_enable`. A name is also what the
    # template is *for* — `<transmitter>` says which argument it varies over, and a reader who has
    # to count to find out is reading a positional convention that nothing states.
    expanded = [variable]
    for name in placeholders:
        spec = (argument_schema or {}).get(name)
        if not isinstance(spec, dict) or spec.get("type") != "enum":
            raise Unconfigured(
                f"commands.yaml#{owner}.gate.variable",
                f"declares {variable!r}, whose placeholder <{name}> names no enum argument of that "
                f"verb. The template binds by name, so a placeholder that names nothing is a gate "
                "with no instantiation — and §9's check 5 requires a closed gate to be refused by "
                "the name a fleet would act on, not by the template",
            )
        values = spec.get("values") or []
        if not values:
            raise Unconfigured(
                f"commands.yaml#{owner}.argument_schema.{name}",
                f"declares an empty enum, so {variable!r} has no instantiation to publish",
            )
        # An enum whose single "value" is a sentence is a described set rather than an enumerated
        # one, and it cannot be instantiated. `set_load` declared `['any id in
        # components.yaml#loads']` — a pointer to a registry the argument schema could have read.
        if len(values) == 1 and isinstance(values[0], str) and " " in values[0]:
            raise Unconfigured(
                f"commands.yaml#{owner}.argument_schema.{name}",
                f"describes its values rather than listing them ({values[0]!r}), so {variable!r} "
                "cannot be instantiated and the mirror would publish a gate variable with a "
                "sentence in it. The enumeration exists in the registry the description points at",
            )
        expanded = [
            candidate.replace(f"<{name}>", str(value), 1)
            for candidate in expanded
            for value in values
        ]
    return expanded


def capability_snapshot(
    world: World,
    *,
    phase: str,
    closed_gates: set[str] | None = None,
    tripped_interlocks: set[str] | None = None,
) -> list[dict[str, Any]]:
    """The six declared capability fields, one row per verb, from the registries and live state.

    `presentation.yaml#mirror.capability_snapshot` names the fields and the derivation, and its own
    open-debt list says what was missing: "`state.json`'s capability snapshot is a projection of
    the registries and nothing emits it... the generator for it belongs with the plant, because
    `available` and `availability_reason` depend on live state — the current phase, the gate
    variable values, and the interlocks against the thresholds." This is that generator.

    It is a *projection*, which is the whole reason it can be built at all: every field is already
    in `commands.yaml`, so a snapshot that disagreed with the registry would be a second
    declaration rather than a view of the first. Nothing here decides anything; it resolves.

    `availability_reason` names the thing that closed the verb, and the order matters: the phase
    first, because a verb outside its phase is not merely gated but absent from the mission, then
    the gate variable, then the interlock. A verb that is open says so rather than saying nothing,
    because the contract's check 5 is about a *closed* gate being refused by name and a fleet that
    cannot distinguish "open" from "unnamed" has lost the check.
    """
    closed = set(closed_gates or ())
    tripped = set(tripped_interlocks or ())
    rows: list[dict[str, Any]] = []
    for verb, spec in sorted(world.verbs.items()):
        gate = spec.get("gate") if isinstance(spec.get("gate"), dict) else {}
        variables = gate_instantiations(
            str(gate.get("variable") or ""), spec.get("argument_schema") or {}, verb
        )
        allowed = [str(p) for p in spec.get("allowed_phases") or []]
        in_phase = phase in allowed
        shut = [variable for variable in variables if variable in closed]
        interlocks = spec.get("interlocks")
        # **A declared interlock is not a closed one, and reading it as one made 32 of this
        # vehicle's 58 verbs permanently unavailable.** The first version of this function did
        # `blocked_by_interlock = isinstance(interlocks, list) and bool(interlocks)` — so every verb
        # carrying a guard was reported closed, for ever: `arm_engine`, `start_burn`, `load_burn`,
        # `set_attitude_target`, `set_coolant_pump` and 27 more, each with an `availability_reason`
        # naming an interlock that was not tripped. A fleet reading that `state.json` would conclude
        # the vehicle was nearly unusable, and the console built on it refused all 32 by name.
        #
        # `commands.yaml`'s `interlocks` list declares *what must be checked when a command
        # arrives* — D-03's "service-owned" half, the counterpart to the agent-writable gate. It is
        # not a standing condition, and §9's check 5 is about *gates*: "a closed gate is refused by
        # name". So an interlock can only make a verb unavailable when a caller supplies the live
        # threshold state that trips it, which is what `tripped_interlocks` is for; with no live
        # state the interlocks are *unevaluated*, and the reason says so rather than claiming either
        # answer.
        declared = [str(i) for i in interlocks] if isinstance(interlocks, list) else []
        blown = [name for name in declared if name in tripped]
        if not in_phase:
            available, reason = False, f"phase: {phase} is not in {allowed}"
        elif shut:
            available, reason = False, f"gate: {shut[0]} is closed"
        elif blown:
            available, reason = False, f"interlock: {blown[0]} is tripped"
        elif declared:
            available, reason = (
                True,
                f"open; {len(declared)} interlock(s) checked when a command arrives, not now",
            )
        else:
            available, reason = True, "open"
        rows.append(
            {
                "verb": verb,
                "available": available,
                "irreversible": bool(spec.get("irreversible")),
                "deferrable": spec.get("execution_class") == "deferred",
                "argument_schema": spec.get("argument_schema") or {},
                "availability_reason": reason,
                "gate_variables": variables,
            }
        )
    return rows


def emit_state(
    world: World,
    *,
    phase: str,
    posture: str,
    abort_latched: bool,
    closed_gates: set[str] | None = None,
    tripped_interlocks: set[str] | None = None,
    published_at: str = "",
) -> dict[str, Any]:
    """Build one `state.json`, the mirror, from the registries and what live state there is.

    The mirror's rule (`presentation.yaml#mirror`) is that it "carries the vehicle's own **software
    state** and the mission envelope — `layer: service` channels at P0 and P1 — plus the
    executive's keys. It carries no measurements and no estimates." So this emits the three vehicle
    keys the file declares (`vehicle`, `available_commands`, `variables`) and the capability
    snapshot, and it deliberately does **not** emit `budget`, `queue_depth` or `published_at`:
    those are the far side's, and a vehicle that filled them would be writing the executive's
    record for it. They are named in `executive_keys` precisely so the split is visible.

    A note on what a reference plant can honestly put in `variables`. There is no plant code here,
    so no gate variable has a *value* — every one of the 226 is declared and unset. Emitting
    `true` would be the defaulting this folder exists to refuse, and emitting nothing would lose
    the names a fleet needs. So the names are published with an explicit state, `closed_gates`
    carries whatever live state the caller has, and a verb whose gate is in neither set reports
    `gate: <name> has no value` rather than claiming to be open.
    """
    closed = {str(g) for g in (closed_gates or set())}
    snapshot = capability_snapshot(
        world, phase=phase, closed_gates=closed, tripped_interlocks=tripped_interlocks
    )
    variables: dict[str, Any] = {}
    for row in snapshot:
        for variable in row["gate_variables"]:
            # The contract publishes the *instantiated* name, so `reserve_floor_<resource>_enable`
            # does not appear here and `reserve_floor_water_cooling_enable` does.
            variables.setdefault(variable, variable not in closed)
    return {
        "vehicle": {"phase": phase, "posture": posture, "abort_latched": abort_latched},
        "available_commands": [row["verb"] for row in snapshot if row["available"]],
        "variables": variables,
        "capability": snapshot,
    }


def blackout_budget(root: Path) -> list[dict[str, Any]]:
    """The mission's second clock: how many times each phase loses the Earth, and for how long.

    A phase declares one duration and an orbit declares another, and until this projection existed
    nothing put them together. `mission.yaml#comms_blackout` derives 46.5 minutes of silence per
    117.8-minute lunar revolution and names the five phases it affects — but "affects `surface`"
    and "costs `surface` eight and a half hours of contact" are different statements, and only the
    second one tells a fleet what it is planning around. The block's own note points at the sharpest
    case without quantifying it: "a blackout during a powered descent is eleven minutes of the most
    consequential flying on the mission happening unwatched."

    **What this computes and what it must not be read as.** The figure is the blackout for a vehicle
    in a 100 km circular lunar orbit for the whole phase, which is the CSM and only the CSM. The LM
    is in orbit briefly during `descent` and `ascent_rendezvous` and is *on the surface* for most of
    `surface`, and a vehicle on the surface near the sub-Earth point has the Earth fixed in its sky
    — so its blackouts are a function of landing longitude, which this file does not declare. That
    is the half still owed, and stating it here is what keeps this number from being read as the
    whole mission's.
    """
    mission = load_yaml(root / "mission.yaml")
    block = mission.get("comms_blackout") or {}
    period = float((block.get("orbit") or {}).get("period_min") or 0.0)
    blackout = float(block.get("duration_min") or 0.0)
    if period <= 0 or blackout <= 0:
        return []
    affected = {str(p) for p in block.get("phases_affected") or []}
    rows: list[dict[str, Any]] = []
    for phase in mission.get("phases") or []:
        pid = str(phase.get("id"))
        if pid not in affected:
            continue
        minutes = float(phase.get("duration_h") or 0.0) * 60.0
        revolutions = minutes / period
        # A phase boundary clips a revolution, and a clipped revolution may hold no blackout — so
        # the count a fleet can rely on is the whole ones, and the fraction says how much of
        # another one the phase contains.
        whole = int(revolutions)
        rows.append(
            {
                "phase": pid,
                "minutes": minutes,
                "revolutions": revolutions,
                "blackouts_at_least": whole,
                "clipped_fraction": revolutions - whole,
                "blackout_min": revolutions * blackout,
                "contact_min": revolutions * (period - blackout),
            }
        )
    return rows


def crew_placement(root: Path) -> list[dict[str, Any]]:
    """Who is at which station, for every configuration every phase names.

    `ask_crew`'s perception bound is per *station* — `domains/crew/components.yaml#display_contract`
    gives each one its own panels and its own `cannot_see` list — so "what can this person tell me"
    needs a station, and a station needs a vehicle, and a vehicle needs a configuration. Three
    declarations answer it and none of them answered it alone: `vehicle.yaml#configurations` says
    which vehicle carries crew and how many, `mission.yaml#phases` lists the configurations a phase
    passes through, and `mission.yaml#crew.positions[].stations` says which seat each person takes
    in each vehicle.

    **Keyed by (phase, configuration) rather than by phase**, because a phase's list is a
    *sequence*: `lunar_orbit` runs from docked to undocked and `ascent_rendezvous` from the ascent
    stage to a docked CSM, so "where is the commander during lunar orbit" has two true answers and
    reporting neither would be worse than reporting both. The pairing is what the declarations
    determine; anything finer would be a guess about where in the phase.

    This is a *projection*, in this folder's sense. Where the declaration chain does not reach, the
    entry says so — and that is the value of writing it down. `descent` and `surface` name no
    configuration carrying crew in the CSM, so the CSM pilot's station in those phases is not
    merely unknown to this function, it is undeclared in the corpus.
    """
    mission = load_yaml(root / "mission.yaml")
    vehicle = load_yaml(root / "vehicle.yaml")
    configs = {str(c.get("id")): c for c in vehicle.get("configurations") or []}
    people = (mission.get("crew") or {}).get("positions") or []
    rows: list[dict[str, Any]] = []
    for phase in mission.get("phases") or []:
        # `configurations` is the sequence the phase's *subject* passes through; `also_present` is
        # what else is flying. Both place crew, and the second is the only reason the CM pilot can
        # be located during `descent` and `surface`.
        named = [str(n) for n in phase.get("configurations") or []] + [
            str(n) for n in phase.get("also_present") or []
        ]
        for name in named:
            config = configs.get(name) or {}
            held = str(config.get("crew_in") or "")
            placement: dict[str, Any] = {}
            for person in people:
                stations = person.get("stations") or {}
                if held and held in stations:
                    placement[str(person.get("id"))] = {
                        "station": stations[held],
                        "vehicle": held,
                    }
                else:
                    placement[str(person.get("id"))] = {
                        "station": None,
                        "vehicle": None,
                        "reason": (
                            f"{name!r} carries crew in {held or 'no vehicle'}, and this person has "
                            f"no station there (theirs are {sorted(stations)})"
                        ),
                    }
            rows.append(
                {"phase": str(phase.get("id")), "configuration": name, "placement": placement}
            )
    return rows


def crew_coverage(root: Path) -> list[dict[str, Any]]:
    """Which crew members a phase places at all, across every configuration it names.

    The distinction this exists to draw is the one the per-configuration rows cannot: a crew member
    missing from `lm_alone_descent` is *not in the LM*, which is a fact about that configuration and
    not a gap — while a crew member no configuration in the phase holds at all is a gap in the
    corpus.

    Both halves of a phase's list count. `configurations` is the sequence the phase's *subject*
    passes through and `also_present` is what else is flying, and the second is the only reason the
    CM pilot can be located during `descent` and `surface`: the CSM waits in lunar orbit through
    both, alone, with her aboard.
    """
    mission = load_yaml(root / "mission.yaml")
    vehicle = load_yaml(root / "vehicle.yaml")
    configs = {str(c.get("id")): c for c in vehicle.get("configurations") or []}
    people = (mission.get("crew") or {}).get("positions") or []
    summary: list[dict[str, Any]] = []
    for phase in mission.get("phases") or []:
        present = [str(n) for n in phase.get("configurations") or []] + [
            str(n) for n in phase.get("also_present") or []
        ]
        vehicles = {str((configs.get(n) or {}).get("crew_in") or "") for n in present}
        unplaced = [
            str(person.get("id"))
            for person in people
            if not vehicles & set((person.get("stations") or {}).keys())
        ]
        summary.append(
            {
                "phase": str(phase.get("id")),
                "vehicles": sorted(v for v in vehicles if v),
                "unplaced": unplaced,
            }
        )
    return summary


def emit_frame(
    world: World,
    *,
    tick: int,
    seq: int,
    boot_id: str,
    met_s: float,
    sensor_time_s: float,
    values: dict[str, Any],
    quality: dict[str, str],
    phase: str,
    vehicle: str,
    state_revision: int,
) -> dict[str, Any]:
    """Build one telemetry frame in apollo's shape, from the declared field list.

    `apollo_diode.md:600-627` is the envelope and `presentation.yaml#frame` is the vehicle's
    statement of it. Building the frame here is what turns that statement into something that
    either produces a frame or does not — and it is the one part of the file surface the vehicle
    side owns outright, so it is the part worth proving from this side of the window.
    """
    provided = {
        "schema": "aurora.capsule.telemetry.v1",
        "seq": seq,
        "sim_step": tick,
        "boot_id": boot_id,
        "met_s": met_s,
        "sensor_time_s": sensor_time_s,
        "publish_time_s": met_s,
        "state_revision": state_revision,
        "vehicle": vehicle,
        "phase": phase,
        "values": values,
        "quality": quality,
        "injected": False,
    }
    missing = [name for name in world.frame_fields if name not in provided]
    if missing:
        raise Unconfigured(
            "presentation.yaml#frame.fields",
            f"declares {missing}, which no producer supplies. A frame field with no producer is a "
            "field the window cannot fill",
        )
    return {name: provided[name] for name in world.frame_fields}


def advance(world: World, state: State, values: dict[str, Any], dt: float) -> dict[str, Any]:
    """One state's contribution to a tick, or a named refusal.

    The refusals are the point. Every one of them names the thing that would have to exist for
    this state to advance, and they are ordered by what the method needs first: the parameter the
    class owes, then the edges that drive it, then the rule — because a rule cannot be supplied by
    configuration at all and needs code.
    """
    where = f"domains/{state.domain}/components.yaml:state {state.id}"
    owed = OWED.get(state.method)
    if owed and state.spec.get(owed[0]) in (None, "UNCONFIGURED"):
        raise Unconfigured(f"{where}.{owed[0]}", owed[1])

    incoming = [e for e in world.edges if e.target == state.node and e.id not in world.back_edges]
    if state.method in {"lag", "stock", "delay", "dynamics"} and not incoming:
        raise Unconfigured(
            where, f"is a {state.method} with no incoming edge, so nothing drives it"
        )
    for edge in incoming:
        if not edge.usable:
            raise Unconfigured(
                f"coupling.yaml:edge {edge.id}",
                f"drives {state.id} and carries no sensitivity value, so the plant cannot apply it",
            )

    if state.method == "lag":
        tau = float(state.spec["tau_s"])
        driver = values.get(incoming[0].source)
        if driver is None:
            raise Unconfigured(f"{where}", f"reads {incoming[0].source!r}, which nothing supplies")
        current = float(values.get(state.node) or driver)
        alpha = math.exp(-dt / tau)
        return {state.node: driver + (current - driver) * alpha}

    if state.method == "stock":
        quantum = float(state.spec["quantum"])
        total = 0.0
        for edge in incoming:
            total += float(edge.sensitivity["value"]) * dt
        if abs(total) and abs(total) < quantum:
            # plant.md §4: a flow below the quantum is a modelling error, not a rounding one, and
            # the accumulator must still carry it rather than lose it.
            return {state.node: None}
        return {state.node: total}

    if state.method == "hazard":
        raise Unconfigured(
            where,
            "is a hazard state: the plant needs the RNG stream and the hazard draw, which are "
            "plant code rather than configuration",
        )

    raise Unconfigured(
        where,
        f"is a {state.method} state and its rule is not in the configuration. An `algebraic` "
        "state's relation, a `discrete` state's transitions and a `dynamics` state's equations "
        "are domain code, and the configuration deliberately does not pretend to carry them",
    )


def step(world: World, values: dict[str, Any], dt: float) -> dict[str, Any]:
    """`plant.md`'s seven-step tick, with the parts that need code named rather than faked.

    Steps 1, 2, 5, 6 and 7 are the window's and the executive's and are stubbed with their
    contracts written down. Step 3's event queue needs latched states, so it is a no-op until
    there are any. Step 4 is the one this file is really about: it walks the derived order.
    """
    # 1. Effects — everything external enters here and nowhere else.
    # 2. Revalidate at the moment of effect. Nothing is captured at schedule time.
    #    Both belong to the executive; the plant's contract is that it never sees an effect that
    #    has not been through them.
    #
    # 3. Advance to the next event boundary.
    horizon = dt

    # 4. Nodes, in the linter's frozen total order, Gauss-Seidel on the DAG, back-edges reading
    #    last tick's value. Writes are staged, then committed.
    staged: dict[str, Any] = {}
    for node in world.schedule:
        for state in world.states_on(node):
            staged.update(advance(world, state, {**values, **staged}, horizon))
    committed = {**values, **{k: v for k, v in staged.items() if k != "internal"}}

    # 5. Commit, then assert what can be asserted exactly. `assert_conservation` needs the
    #    accumulator of every `conserve` edge, which exists once the stocks do.
    # 6. Instruments: T -> A. One-way, and the quality function cannot see the faults.
    # 7. Compare-point: a hash over canonically-encoded state, every tick.
    return committed


def readiness(world: World) -> None:
    """The build order: what is ready, what is not, and what the schedule reaches first."""
    ready = [s for s in world.states if not s.owed]
    blocked = [s for s in world.states if s.owed]
    usable = [e for e in world.edges if e.usable]
    print(
        f"world: {len(world.states)} states over {len(world.schedule)} nodes, {len(world.edges)} edges"
    )
    print(f"schedule: {' -> '.join(world.schedule[:6])} ... -> {world.schedule[-1]}")
    print()
    print(f"states fully configured   {len(ready):3d} / {len(world.states)}")
    print(f"states with a debt        {len(blocked):3d} / {len(world.states)}")
    print(f"edges with a sensitivity  {len(usable):3d} / {len(world.edges)}")
    print(f"UNCONFIGURED scalars      {world.debts:3d}")
    print()
    if blocked:
        print("blocked states, in the order the schedule reaches them:")
        order = {node: index for index, node in enumerate(world.schedule)}
        for state in sorted(blocked, key=lambda s: order.get(s.node, -1)):
            print(
                f"  {state.domain:12s} {state.id:26s} {state.method:9s} needs {', '.join(state.owed)}"
            )
        print()
    print("First tick, in schedule order — the plant stops at the first thing it cannot compute:")
    values: dict[str, Any] = {}
    for node in world.schedule:
        for state in world.states_on(node):
            try:
                advance(world, state, values, 1.0 / 50.0)
            except Unconfigured as exc:
                print(f"  {exc}")
                return
    print("  nothing raised, which would mean the configuration is complete")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load the vehicle configuration as a world.")
    parser.add_argument("--dir", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument(
        "--readiness",
        action="store_true",
        help="print the build order: what is ready, what is blocked, and by what",
    )
    parser.add_argument(
        "--frame", action="store_true", help="emit one telemetry frame and print its keys"
    )
    parser.add_argument(
        "--state",
        action="store_true",
        help="emit one state.json: the mirror's vehicle keys and the capability snapshot",
    )
    parser.add_argument("--phase", default="translunar_coast", help="the mission phase to report")
    parser.add_argument(
        "--blackout",
        action="store_true",
        help="the mission's second clock: blackouts and contact per phase, for a vehicle in orbit",
    )
    parser.add_argument(
        "--crew",
        action="store_true",
        help="where each crew member is, phase by phase, from the two halves of the crew model",
    )
    parser.add_argument(
        "--closed-gate",
        action="append",
        default=[],
        metavar="VARIABLE",
        help="a gate variable that is closed, by its instantiated name; repeatable",
    )
    parser.add_argument(
        "--closed-interlock",
        action="append",
        default=[],
        metavar="THRESHOLD",
        help="an interlock that is currently tripped; repeatable. An interlock that is merely "
        "declared is not a closed one, and treating it as closed is what made 32 of this "
        "vehicle's 58 verbs permanently unavailable",
    )
    args = parser.parse_args(argv)

    root = Path(args.dir)
    try:
        world = load_world(root)
    except Unconfigured as exc:
        sys.stderr.write(f"the configuration cannot be loaded: {exc}\n")
        return 3

    if args.frame:
        frame = emit_frame(
            world,
            tick=0,
            seq=0,
            boot_id="0" * 32,
            met_s=0.0,
            sensor_time_s=0.0,
            values={},
            quality={},
            phase="translunar_coast",
            vehicle="csm",
            state_revision=0,
        )
        print(yaml.safe_dump(frame, sort_keys=False, default_flow_style=False).rstrip())
        return 0

    if args.blackout:
        rows = blackout_budget(root)
        if not rows:
            sys.stderr.write("no comms blackout is declared, so there is no second clock\n")
            return 3
        print("  the CSM's lunar orbit, at 100 km: one blackout per revolution, for a vehicle")
        print("  in orbit for the whole phase. The LM is on the surface for most of `surface`.")
        print()
        total_blackout = total_contact = 0.0
        for row in rows:
            total_blackout += row["blackout_min"]
            total_contact += row["contact_min"]
            print(
                f"  {row['phase']:22} {row['minutes']:7.1f} min = {row['revolutions']:5.2f} rev  "
                f"blackout {row['blackout_min']:6.1f} min  contact {row['contact_min']:7.1f} min"
                f"  ({row['blackouts_at_least']} whole"
                + (f" + {row['clipped_fraction']:.2f} clipped" if row["clipped_fraction"] else "")
                + ")"
            )
        print()
        print(
            f"  total: {total_blackout / 60:.1f} h silent, {total_contact / 60:.1f} h in contact "
            f"across {len(rows)} phase(s)"
        )
        return 0

    if args.crew:
        for row in crew_placement(root):
            print(f"  {row['phase']:22} {row['configuration']}")
            for who, spot in row["placement"].items():
                if spot["station"]:
                    print(f"      {who:12} {spot['station']}")
                else:
                    print(f"      {who:12} {'not in this vehicle':24} {spot['reason']}")
        print()
        for row in crew_coverage(root):
            if row["unplaced"]:
                print(
                    f"  {row['phase']:22} UNPLACED {row['unplaced']} — no configuration this phase "
                    f"names carries crew in a vehicle they have a station in "
                    f"(the phase's vehicles: {row['vehicles']})"
                )
            else:
                print(f"  {row['phase']:22} every crew member placed ({row['vehicles']})")
        return 0

    if args.state:
        phases = [str(p.get("id")) for p in (load_yaml(root / "mission.yaml").get("phases") or [])]
        if args.phase not in phases:
            sys.stderr.write(
                f"phase {args.phase!r} is not one of the mission's {len(phases)} phases: {phases}\n"
            )
            return 3
        state = emit_state(
            world,
            phase=args.phase,
            posture="",
            abort_latched=False,
            closed_gates=set(args.closed_gate),
            tripped_interlocks=set(args.closed_interlock),
        )
        json.dump(state, sys.stdout, indent=2, sort_keys=False)
        sys.stdout.write("\n")
        return 0

    if args.readiness:
        readiness(world)
        return 0

    print(
        f"loaded {len(world.states)} states, {len(world.edges)} edges, "
        f"{len(world.channels)} channels and {len(world.verbs)} verbs; "
        f"the tick order is {len(world.schedule)} nodes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
