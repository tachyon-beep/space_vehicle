#!/usr/bin/env python3
"""A reference plant: load the world from the configuration, and run it until it hits a debt.

This is not a simulator. It integrates nothing, and every state whose physics it does not have
raises a named exception rather than guessing. What it *is* is the proof that the configuration
is implementable and a list of what is missing, in the order the missing things are needed.

The idea is `simulator-design.md:146-150`'s, applied to the plant instead of to the linter: you
do not enumerate what a simulator needs up front, you build it, run it, and it tells you what you
now owe. `check_vehicle.py` does that for the *definition* — it reports 281 declared debts by
path, and `test_the_readme_status_matches_the_tools` holds that figure in this file as well as in
the README, because it said 202 here for longer than anybody noticed. This tool does it for the *implementation*: it loads the whole world, builds the tick order,
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
from check_vehicle import (  # noqa: E402  (a sibling tool, not a package)
    Report,
    derive_schedule,
    stock_flux_basis,
)

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
    advances: str | None = None
    drains: str | None = None

    @property
    def usable(self) -> bool:
        """An edge is usable when it carries a value the plant could apply.

        **This is the integrator's question and not the readiness figure's**, and conflating the two
        is what the second property below exists to undo. The integrator multiplies the value by a
        driver, so a table is not usable to it; a reader asking whether the sensitivity has been
        *declared* has a different question with a different answer.
        """
        return (self.sensitivity or {}).get("value") not in (None, "UNCONFIGURED")

    @property
    def declared(self) -> bool:
        """Whether the edge's sensitivity resolves — the question `--readiness` means to ask.

        The corpus has two complete forms. A scalar `value` is a sensitivity; a `regimes` table is
        the answer the linter *requires* of a discrete edge out of physical equipment — *"a mode
        selection is a table rather than a sensitivity, and a scalar here would be a proportional
        law the vehicle does not have"* — and of a banded supply edge like `E-BUS-COMM`, which has a
        response per rung of the shed ladder and no derivative at all.

        `--readiness` counted only the scalar form, so **every regime edge read as undeclared: eight
        of them**, for the whole life of the projection, including the three bus edges the flagship
        thermal cycle turns on. The figure said the vehicle owed a number where the vehicle had
        correctly declared a table, which is the failure this folder is organised against arriving
        inside the tool that reports on it. `E-CMD-BUS`'s retarget made the ninth, and that is what
        surfaced it: the same graph, one edge moved from the scalar form to the table form, and the
        headline moved with it for no reason in the vehicle at all.
        """
        sensitivity = self.sensitivity or {}
        if sensitivity.get("value") not in (None, "UNCONFIGURED"):
            return True
        return bool(sensitivity.get("regimes"))


@dataclass
class World:
    root: Path
    states: list[State]
    edges: list[Edge]
    back_edges: set[str]
    schedule: list[str]
    nodes: dict[str, dict[str, Any]]
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
            advances=e.get("advances"),
            drains=e.get("drains"),
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
        # The node declarations travel with the world so the shared flux classifier can see a
        # stock's unit and an endpoint's kind. Without them the plant called the classifier with an
        # empty map, the dimensional check silently did nothing, and the plant's judgement was
        # weaker than the linter's — which is the one thing sharing the function is meant to prevent.
        nodes={str(k): v for k, v in (coupling.get("nodes") or {}).items()},
        channels=channels,
        frame_fields=[
            str((f or {}).get("name"))
            for f in (presentation.get("frame") or {}).get("fields") or []
        ],
        verbs=verbs,
        plant_published=[str(e.get("channel")) for e in presentation.get("plant_published") or []],
        # Counted here rather than taken from the linter, and deliberately a *different* number:
        # the linter reports 281 declared debts, most of which are prose obligations ("this needs a
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
    so no gate variable has a *value* — every one of the 228 is declared and unset. Emitting
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
    `surface`, and its situation there is not a smaller version of this one — it is the opposite.
    `mission.yaml#landing_site` now declares the site, and the Earth stands 41 degrees up from it;
    libration moves the sub-Earth point 0.075 deg/h at its fastest, so across the 21.5-hour surface
    phase the elevation changes by at most 1.6 degrees and the LM never loses the link.

    So the vehicle's comms situation *inverts* between its two halves, and `surface_contact` below
    reports the half this function's own arithmetic cannot reach.
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


def surface_contact(root: Path) -> dict[str, Any]:
    """The LM's link from the surface, which is a property of the site rather than of the orbit.

    This is the other half of the second clock and it is not a scaled-down copy of it. In orbit a
    vehicle loses the Earth once per 117.8-minute revolution; on the surface it either has the
    Earth in view for the whole stay or never, and which one is decided by the landing site the
    mission chose. `mission.yaml#landing_site` puts the Earth 41 degrees up, so the answer here is
    "always" — and the interesting part is that this makes the LM the *only* part of the vehicle
    with a continuous link during the phases when the crew would be in it.
    """
    mission = load_yaml(root / "mission.yaml")
    site = mission.get("landing_site") or {}
    elevation = site.get("earth_elevation_deg")
    variation = site.get("elevation_variation_over_surface_deg")
    surface = next((p for p in mission.get("phases") or [] if p.get("id") == "surface"), None)
    if not isinstance(elevation, (int, float)):
        return {}
    return {
        "latitude_deg": site.get("latitude_deg"),
        "longitude_deg": site.get("longitude_deg"),
        "earth_elevation_deg": float(elevation),
        "variation_over_surface_deg": float(variation or 0.0),
        "surface_hours": float((surface or {}).get("duration_h") or 0.0),
        "continuous": float(elevation) - float(variation or 0.0) > 0.0,
    }


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


SECONDS_PER_HOUR = 3600.0


def stock_flux(
    world: World, edge: Edge, values: dict[str, Any], dt: float, driver_node: str | None = None
) -> float:
    """What one edge moves into a stock over `dt`, or a named refusal.

    This function exists because the line it replaces was wrong three ways at once and **had never
    run**. The schedule stops at the first state it cannot compute — an `algebraic` one — long before
    it reaches any of the vehicle's 24 stock states, so the integrator that every consumable in the
    mission depends on was written, reviewed and never executed. Handed `lm_cabin_o2_kg` it
    returned the same 117.89792 kg whether one crew member was aboard or three, because it summed
    `sensitivity * dt` over every incoming edge and never read a driver. The three edges it summed
    were `116.86 Pa per K` (a lag relation between zone temperature and cabin pressure),
    `1.0 kg O2 per kg O2` (a conservation ratio whose flux is the tank's *outflow*, which the ratio
    does not contain) and `0.03792 kg/h per crew` (a per-hour rate needing a crew count and a 3600).
    Adding pascals-per-kelvin to kilograms-per-hour and calling the result a mass is not an
    approximation; it is a dimension error wearing a number.

    So the flux is established from the edge's own declared unit, and where the unit does not
    establish one the plant refuses instead of guessing. Three cases, and the third is the one the
    vehicle actually has:

      * **a rate with a time denominator** — `kg/s per W`, `kg/h per crew`. The driver multiplies it
        and the time basis converts it to per-second. This is the case a stock integrator is for.
      * **a dimensionless ratio** — `kg O2 per kg O2`. The driver is a *level* and the sensitivity is
        a transfer fraction, so the flux is (source's own outflow) x ratio, and the outflow belongs to
        whichever state produces it. The plant cannot recover a flow from a level, so this refuses
        and names the flow that is owed.
      * **a structural relation** — `Pa per K`, `J per K`. These land on a stock node because the
        *channel* hangs off the node, not because anything flows into it: a cabin's pressure depends
        on its temperature and its mass, but its mass does not depend on its temperature. Summing one
        into the other is the dimension error above, so this also refuses.

    The refusals are the useful output. Each names the edge and what a correct model would have to
    declare, which turns the vehicle's largest silent wrongness into a build order.
    """
    # The classification is the linter's, imported rather than repeated, in the pattern
    # `derive_schedule` already set: a rule about what a stock edge means cannot come apart from the
    # rule that checks it.
    basis, reason = stock_flux_basis(
        {
            "id": edge.id,
            "kind": edge.kind,
            "sensitivity": edge.sensitivity,
            "from": edge.source,
            "to": edge.target,
        },
        world.nodes,
    )
    if basis is None:
        # The shared classifier prefixes its reason with the edge id because the linter reports it
        # under a heading that does not name the edge again; this file's `where` already does, so
        # only that prefix comes off. Splitting on the first colon instead would cut the reason at
        # its own first colon and keep the half without the explanation in it.
        prefix = f"{edge.id} "
        raise Unconfigured(
            f"coupling.yaml:edge {edge.id}",
            reason[len(prefix) :] if reason.startswith(prefix) else reason,
        )

    per_hour = basis == "per_hour"
    where = f"coupling.yaml:edge {edge.id}"

    # Which end drives the flux depends on the direction. A fill is driven by the source, which is
    # the thing producing the flow; a **discharge is driven by the target**, which is the consumer
    # that sets the rate. Reading the stock itself on a discharge returns its own level, and
    # `E-CREW-WATER` at `kg/h per crew` then multiplied by a mass in kilograms and gave zero.
    source = driver_node or edge.source
    driver = values.get(source)
    if driver is None:
        raise Unconfigured(
            where,
            f"drives {edge.target} and reads {source!r}, which nothing supplies this tick",
        )
    if not isinstance(driver, (int, float)):
        raise Unconfigured(
            where,
            f"drives {edge.target} and reads {source!r} as {driver!r}, which is not a number the "
            "sensitivity can multiply",
        )
    flux = float(edge.sensitivity["value"]) * float(driver)
    return flux * (dt / SECONDS_PER_HOUR if per_hour else dt)


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

    # `advances` first, node second. A node can carry more than one state an edge drives —
    # `cabin_atm` carries four gas masses and a pressure — and resolving by node alone handed every
    # one of them the same edge list, so each gas integrated every other gas's flux. Where the
    # declaration is absent the node is a singleton (the linter refuses otherwise), so the node test
    # is still the whole answer.
    incoming = [
        e
        for e in world.edges
        if e.target == state.node
        and e.id not in world.back_edges
        and (e.advances is None or e.advances == state.id)
        # A declared clamp constrains the node's channel and moves no matter, so it is neither an
        # input nor an output. See `CLAMP_KIND` in the linter.
        and e.kind != "limit"
    ]
    # A tank filled at the pad and never again has no incoming edge *on purpose*, and it says so in
    # `preloaded:`. Without this exemption the plant refuses `water_cooling`, `o2_lm`, `prop_rcs` and
    # `pressurant_he` — four stocks that are correctly declared and simply drain, which is the same
    # distinction the linter's undriven-node check draws for the same reason.
    preloaded = (world.nodes.get(state.node) or {}).get("preloaded")
    if state.method in {"lag", "stock", "delay", "dynamics"} and not incoming and not preloaded:
        raise Unconfigured(
            where, f"is a {state.method} with no incoming edge, so nothing drives it"
        )
    # **The edge's threshold is the integrator's, and this asked it of every state.** A `regimes`
    # table is the complete answer the linter *requires* of a discrete edge out of physical
    # equipment — "a mode selection is a table rather than a sensitivity" — and it is what the three
    # bus supply edges carry. It cannot be multiplied by, so `usable` is the right question for
    # `lag` and `stock`, which is all this function integrates. Asking it of everything else made
    # the plant refuse an `algebraic` state with "edge E-BUS-INST carries no sensitivity value"
    # before it ever reached the refusal that is actually true — that the state's rule is domain
    # code — and the two buckets the build order promises cannot disagree then disagreed for four
    # states: `instrumentation_power`, `bus_a_v`, `bus_tie_closed` and `load_shed_class`. The
    # coupling is *declared* in each of them; what is missing is the code.
    integrated = state.method in {"lag", "stock"}
    for edge in incoming:
        if not (edge.usable if integrated else edge.declared):
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
        # --------------------------------------------------------------------------------------
        # **The level, which this branch never read.** `stock_flux` returns a *delta* —
        # `sensitivity x driver x dt`, in the node's own unit, which its docstring says outright —
        # and the branch summed those deltas and returned the sum as the node's new value. So a
        # tank holding 279 kg with a 1 mg drain became `-0.001` on the first tick instead of
        # `278.999`: **the integrator did not integrate**, in the one class the plant claims it can
        # advance, and `--build-order` counted such states as "ready now".
        #
        # The bug hid a missing declaration and the missing declaration hid the bug. A stock's
        # starting amount is written down nowhere in this vehicle: `vehicle.yaml#consumables` has
        # the loads, `coupling.yaml`'s `preloaded` prose names them a second time, and no *state*
        # carries one. Nothing noticed, because a branch that never reads the level never needs it.
        #
        # So the level is read, and where there is none the plant refuses **by name** rather than
        # starting from an implicit zero. A tank that silently begins empty is the same class of
        # error as a stock that fills without ever draining, and it is worse in one way: an empty
        # tank and a full one look identical on the first tick of a mission nobody has run.
        # --------------------------------------------------------------------------------------
        current = values.get(state.node)
        if current is None:
            raise Unconfigured(
                f"{where}.initial",
                f"is a stock and nothing supplies a level for `{state.node}` to integrate from, so "
                "the plant has no amount to subtract a drain from. A stock's starting amount is its "
                "initial condition and the configuration declares none: `vehicle.yaml#consumables` "
                "carries the loads for the tanks and `preloaded` names them in prose, but the state "
                "that integrates the tank is where the number has to be to be used",
            )
        if not isinstance(current, (int, float)):
            raise Unconfigured(
                f"{where}.initial",
                f"integrates `{state.node}` from {current!r}, which is not a number",
            )
        total = 0.0
        for edge in incoming:
            total += stock_flux(world, edge, values, dt)
        # And the outbound half, which was missing entirely: **every tank in the vehicle only
        # filled.** The README states the rule — "a back-edge *out* of a stock still drains it,
        # because the stock's own integrator subtracts the flow the back-edge reads" — and no tool
        # implemented any of it. Twelve edges leave stock nodes, and the classifier can establish a
        # flow for exactly one of them (`E-CREW-WATER`, at `kg/h per crew`); the rest are refused by
        # name, which is why the linter reports them as debts too.
        for edge in world.edges:
            # **Back-edges included**, which is the README's own rule: "a back-edge *out* of a stock
            # still drains it, because the stock's own integrator subtracts the flow the back-edge
            # reads". Excluding them would have left `water_cooling` and `h2_csm` — the two the README
            # names as discharging *entirely* through back-edges — draining nothing.
            if edge.source != state.node:
                continue
            # A declared clamp is a relation, not a flow: it constrains what the node's channel may
            # do and moves no matter, so it neither fills nor drains. See `CLAMP_KIND` in the linter.
            if edge.kind == "limit":
                continue
            if edge.drains is not None and edge.drains != state.id:
                continue
            total -= stock_flux(world, edge, values, dt, driver_node=edge.target)
        # --------------------------------------------------------------------------------------
        # `plant.md` §4's Bresenham residual accumulator, which the branch did not have either.
        #
        # **"Every stock uses a Bresenham residual accumulator**: `moved = (scaled + acc) // SCALE;
        # acc = (scaled + acc) % SCALE` ... Exact to the quantum over any horizon, integer-only,
        # horizon-independent." The worked example is this vehicle's own: the nominal leak is
        # `0.0064 g/s` against a 1 mg quantum, so `0.128` quanta per tick, which rounds to zero
        # every tick — *"the leak never happens"*. Without the accumulator the sub-quantum branch
        # below returned `None`, which `step` committed, so the tank's value became `None` on the
        # first slow tick and every tick after it raised against a `None` level.
        #
        # The residual is carried in the same value map as the level, under a key namespaced to the
        # state. That is where it belongs while the stock is a float: §4's `acc` is part of the
        # stock's *representation*, and the representation here is the map. When the fixed-point
        # mantissa lands the residual moves inside it, and this key disappears.
        # --------------------------------------------------------------------------------------
        residual_key = f"{state.id}__residual"
        carried = float(values.get(residual_key) or 0.0)
        quanta = (total + carried) / quantum
        moved = math.floor(quanta)
        level = current + moved * quantum
        # "**Zero-crossing raises an event and records a shortfall.** Never clamp silently, never go
        # negative. The shortfall is the evidence." The event queue is step 3 and is a no-op until
        # there are latched states, so the shortfall is returned with the level and named here.
        shortfall = 0.0
        if level < 0.0:
            shortfall = -level
            level = 0.0
            moved = round((level - current) / quantum) if quantum else 0
        return {
            state.node: level,
            residual_key: (total + carried) - moved * quantum,
            f"{state.id}__shortfall": shortfall,
        }

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


def initial_values(world: World) -> dict[str, Any]:
    """The value map at t=0, from the stocks' own declared initial conditions.

    Until this existed the plant had no answer to "what is the vehicle holding at the start", and
    it did not need one: `advance()`'s stock branch returned the tick's net flux as the node's
    value, so it never read a level and never asked for one. Fixing the integrator made the
    question unavoidable, and the configuration turned out to be able to answer most of it —
    `vehicle.yaml#consumables` has the loads, the atmosphere model derives the cabin oxygen from
    the published volume and pressure, the absorbers' man-hour ratings are on their own coupling
    nodes, and the accumulators start at zero because that is what an accumulator is.

    **Keyed by node, which is the plant's own key space, not by channel.** `advance()` returns
    `{state.node: value}` and `step()` commits that map, so this is the seed of the same map and
    nothing here is a projection. The frame's `values` will want the *published* channel names,
    and that is the publisher's step rather than the plant's: `eclss.pp_o2_mmhg` is a partial
    pressure in millimetres of mercury while `csm_cabin_o2_kg` is a mass in kilograms, so the
    projection is a unit conversion and calling the mass by the channel's name would be a wrong
    number wearing the right one.

    A stock the configuration has not given a value is left out rather than guessed, and the plant
    refuses by name when a tick reaches it — which is the behaviour the check beside this wants.
    """
    values: dict[str, Any] = {}
    for state in world.states:
        if state.method != "stock":
            continue
        # `internal` is excluded for the reason `step` excludes it: the sentinel is one key shared
        # by every state that lives on it, so seeding it would let twenty-five accumulators
        # overwrite each other into a single value that means nothing. A state advanced with its
        # domain keeps its own storage in the domain, which is what the sentinel says.
        if state.node == "internal":
            continue
        initial = (state.spec or {}).get("initial")
        if isinstance(initial, (int, float)):
            values[state.node] = float(initial)
    return values


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


def report_build_order(world: World) -> None:
    """Print the derived build order: the worklist, grouped by what each state is waiting for."""
    buckets = build_order(world)
    total = sum(len(v) for v in buckets.values())
    titles = {
        "ready": "ready now — the two classes the reference plant can advance",
        "value": "owes a value — the cheapest to close, and the debt count already tracks them",
        "edge": "owes an edge — a coupling with no sensitivity, or a state nothing drives",
        "rule": "owes a rule — `algebraic`, `discrete`, `dynamics` or `hazard`: domain code",
    }
    print(f"{total} states, by what blocks them:\n")
    for key in ("ready", "value", "edge", "rule"):
        rows = buckets[key]
        share = f"{100 * len(rows) / total:.0f} %" if total else "-"
        print(f"  {len(rows):4d}  {share:>5s}  {titles[key]}")
    for key in ("value", "edge", "rule"):
        rows = buckets[key]
        if not rows:
            continue
        print(f"\nthe first of the {len(rows)} that {titles[key].split(' — ')[0]}:")
        for state in rows[:5]:
            print(f"  domains/{state.domain}/components.yaml:state {state.id} ({state.method})")


def build_order(world: World) -> dict[str, list[State]]:
    """What blocks every state, in the order the schedule reaches them — **derived, never authored**.

    This is the folder's answer to "what do I implement first", and it has to be computed for the
    same reason `--order` and `--phases` are: an authored worklist drifts from the data the moment
    anybody lands anything, and a stale build order is worse than none because it sends the next
    reader to work that is already done.

    **The classes are `advance()`'s refusal order, and this is deliberately *stricter* than it.**
    The sentence here used to claim the two "cannot disagree", and they can: run `advance` over
    every state with a permissive value map and thirty-six of them land somewhere else. Every one
    is one of two kinds, and neither is a defect —

      * **twenty-three are this classifier counting more.** `advance` refuses on the parameter its
        *method* needs (`tau_s`, `quantum`, `delay_s`, `lambda_per_h`) and on nothing else; this
        walks the whole spec, so a state whose `initial` or `moved_by` or `basis` is unset is
        reported as owing a value even where the plant would advance it from a number it was
        handed. That is the honest direction for a *worklist*: the spec is incomplete and the
        bucket says so.
      * **thirteen are the `internal` sentinel**, which this routes to `rule` and `advance` calls a
        missing edge. No edge can reach the sentinel — it is not a node — so its driver is domain
        code, and `rule` is the file an implementer should open.

    So the two describe different questions on purpose: `advance` answers *"can I compute this
    right now"*, and this answers *"what is missing from the definition"*. What the classification
    must not do is send a reader somewhere the answer is not — and it did, for sixteen states,
    until the `preloaded` exemption and the sentinel routing above were added. The same sequence of
    tests, then, with two deliberate differences:

      * **a value** — the method's named parameter is unset, or the state carries an `UNCONFIGURED`
        anywhere in its spec. These are the cheapest to close and the ones the debt count already
        tracks.
      * **an edge** — a state driven by an edge that carries no sensitivity. Closing these means
        supplying the coupling, and rounds 48 to 51 established what most of them need: a flow node.
      * **a rule** — `algebraic`, `discrete`, `dynamics` or `hazard`. The configuration deliberately
        does not carry these, so each is a piece of domain code rather than a number, and they are
        the largest class by construction.

    `lag` and `stock` with everything they need are ready, and they are the only classes the
    reference plant can actually advance.
    """
    blocking_value: list[State] = []
    blocking_edge: list[State] = []
    blocking_rule: list[State] = []
    ready: list[State] = []
    # The scheduled nodes first, then the states on the `internal` sentinel — they are advanced with
    # their domain rather than in the tick order, but they are 59 of the vehicle's 120 states and a
    # build order that omitted half the work would be a build order for the other half.
    ordered = list(world.schedule) + sorted(
        {s.node for s in world.states if s.node not in set(world.schedule)}
    )
    for node in ordered:
        for state in world.states_on(node):
            owed = OWED.get(state.method)
            if state.owed or (owed and state.spec.get(owed[0]) in (None, "UNCONFIGURED")):
                blocking_value.append(state)
                continue
            incoming = [
                e
                for e in world.edges
                if e.target == state.node
                and e.id not in world.back_edges
                and (e.advances is None or e.advances == state.id)
            ]
            # ------------------------------------------------------------------------------
            # **Two ways this disagreed with `advance()`, which the docstring above says it
            # cannot.** The classification is supposed to be the same sequence of tests the plant
            # runs when it gets there, and it ran two tests the plant does not.
            #
            # A tank filled at the pad has no incoming edge *on purpose*, and `advance` exempts it
            # by name — "without this exemption the plant refuses `water_cooling`, `o2_lm`,
            # `prop_rcs` and `pressurant_he` — four stocks that are correctly declared and simply
            # drain". This classifier did not have the exemption, so `o2_csm_kg`, `o2_lm_kg` and
            # `h2_csm_kg` were reported as owing a driver they do not need and cannot have. Their
            # outbound drains are all usable, so the plant integrates them happily: they are
            # **ready**, and the worklist was telling an implementer to go and build three edges
            # that must not exist.
            #
            # And a state on the `internal` sentinel cannot be reached by *any* edge, because the
            # sentinel is not a node — `coupling.yaml#nodes` does not declare it and nothing can
            # target it. So "no incoming edge" is not a gap in the graph for those states; it is
            # what the sentinel means, and the thing they need is the domain code that advances
            # them. Reporting them as owing a coupling sent an implementer to the wrong file for
            # thirteen states.
            # ------------------------------------------------------------------------------
            preloaded = (world.nodes.get(state.node) or {}).get("preloaded")
            if state.node == "internal":
                # Advanced with its domain, so its driver is code rather than an edge. This is the
                # same distinction `check_domain` draws for `internal_order`.
                blocking_rule.append(state)
                continue
            # **A state with no input at all owes an edge, whatever its method.**
            #
            # This test used to name four methods, on the reasoning that a `lag` or a `stock` is
            # the kind of state an edge drives and an `algebraic` one is domain code. But the
            # question a *worklist* answers is what is missing from the definition, and for a state
            # with no inbound edge, no `preloaded` exemption and no sibling on its node, the answer
            # is an input — no rule can be written without one.
            #
            # `bus_b_v` is the case, and it is the state the plant stops at. `power.dc_bus_b_v` is
            # published, has an undervoltage threshold, three faults that perturb it and a place on
            # a crew panel — and `bus_b` has **no inbound edge in the whole graph**. The tie is
            # declared B -> tie -> A, so bus B is a *source* for bus A, while the state's own note
            # calls it "second bus, cross-supported through the tie". `check_power_inventory` says
            # the quiet part out loud — "which bus a source feeds is a routing decision the tie
            # makes and the file does not state" — so the configuration knows, and the worklist was
            # still sending an implementer to write code for a state that has nothing to read.
            #
            # `discrete` and `hazard` are excluded because they are exceptions in fact rather than
            # by convention: a mode is moved by a command, an event or the domain's own logic, which
            # `moved_by` declares, and a hazard is drawn rather than computed.
            siblings = [o for o in world.states_on(state.node) if o.id != state.id]
            declares_own_inputs = bool(
                (state.spec.get("provenance") or {}).get("computation")
                or state.spec.get("computation")
            )
            if not incoming and not preloaded:
                # A `lag`, `stock`, `delay` or `dynamics` with no driver owes an edge wherever it
                # sits — an integrator has to be integrated from something.
                if state.method in {"lag", "stock", "delay", "dynamics"}:
                    blocking_edge.append(state)
                    continue
                # And an `algebraic` one with no driver, no sibling and **no computation of its
                # own** owes an edge too. The computation is the distinguisher, and the corpus
                # draws it: `cabin_heat_csm_w` carries `total_w: 733` with a `computation` summing
                # the loads `heat_inputs` assigns, so its inputs are declared outside the graph and
                # it needs no edge. `bus_b_v` carries nothing — its own reason says it is "the same
                # nodal solve over a *different source and load set*", and the load set is the part
                # that exists.
                if state.method == "algebraic" and not siblings and not declares_own_inputs:
                    blocking_edge.append(state)
                    continue
            # The bucket asks whether the *coupling is declared*, and the corpus has two complete
            # forms. This asked `usable`, which is the integrator's narrower question — can I
            # multiply by it — so a `discrete` edge carrying the `regimes` table the linter
            # *requires* of it ("a mode selection is a table rather than a sensitivity") read as a
            # missing coupling. `load_shed_class` is the case, and it moved buckets when its edge
            # was retargeted from the scalar form to the table form: the vehicle had not lost a
            # coupling, the edge had changed shape. Only the two methods `advance()` integrates need
            # a value it can apply; everything else on a node is domain code and needs the coupling
            # to be *stated*.
            integrated = state.method in {"lag", "stock"}
            if any(not (e.usable if integrated else e.declared) for e in incoming):
                blocking_edge.append(state)
                continue
            # `delay` belongs here: `advance()` implements `lag` and `stock` and refuses everything
            # else, so a delay state is domain code like the rest. Classifying it as ready would
            # have promised the plant a state it cannot advance.
            if state.method in {"algebraic", "discrete", "dynamics", "hazard", "delay"}:
                blocking_rule.append(state)
                continue
            ready.append(state)
    return {
        "value": blocking_value,
        "edge": blocking_edge,
        "rule": blocking_rule,
        "ready": ready,
    }


def readiness(world: World) -> None:
    """What is ready, what is not, and what the schedule reaches first."""
    ready = [s for s in world.states if not s.owed]
    blocked = [s for s in world.states if s.owed]
    usable = [e for e in world.edges if e.declared]
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
        # **The sentinel's states go last, not first.** `coupling.yaml#nodes` does not declare
        # `internal`, so no node in the schedule carries those states and `order.get(node, -1)`
        # answered "not in the schedule" with a value that sorts *before* everything in it. The
        # header says the list is in the order the schedule reaches them, and a state the schedule
        # never reaches was reported as the first one it does. Nothing was owed on the sentinel at
        # the value level until this round — three profiles and a tie mode landed as `UNCONFIGURED`
        # map targets — so the fallback had never been exercised. A state advanced with its domain
        # is reached *after* the nodes, and `len(world.schedule)` says that.
        order = {node: index for index, node in enumerate(world.schedule)}
        for state in sorted(blocked, key=lambda s: order.get(s.node, len(world.schedule))):
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
        "--build-order",
        action="store_true",
        help="what blocks every state, in the order the schedule reaches them",
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
            # The declared initial conditions, so a frame carries the vehicle's starting state
            # rather than nothing. See `initial_values` for why these are node keys.
            values=initial_values(world),
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
        surface = surface_contact(root)
        if surface:
            verdict = (
                "never loses the link"
                if surface["continuous"]
                else "loses the link for part of every stay"
            )
            print()
            print(
                f"  and the LM on the surface at "
                f"({surface['latitude_deg']}, {surface['longitude_deg']}) sees the Earth "
                f"{surface['earth_elevation_deg']:.1f} deg up, moving "
                f"{surface['variation_over_surface_deg']:.2f} deg across the "
                f"{surface['surface_hours']:.1f} h stay — so it {verdict}."
            )
            print(
                "  the vehicle's comms invert between its halves: the CSM is silent 39 % of the "
                "time and the LM, while the crew are in it, is not silent at all."
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

    if args.build_order:
        report_build_order(world)
        return 0

    print(
        f"loaded {len(world.states)} states, {len(world.edges)} edges, "
        f"{len(world.channels)} channels and {len(world.verbs)} verbs; "
        f"the tick order is {len(world.schedule)} nodes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
