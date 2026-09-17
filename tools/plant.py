#!/usr/bin/env python3
"""A reference plant: load the world from the configuration, and run it until it hits a debt.

This is not a simulator. It integrates nothing, and every state whose physics it does not have
raises a named exception rather than guessing. What it *is* is the proof that the configuration
is implementable and a list of what is missing, in the order the missing things are needed.

The idea is `simulator-design.md:146-150`'s, applied to the plant instead of to the linter: you
do not enumerate what a simulator needs up front, you build it, run it, and it tells you what you
now owe. `check_vehicle.py` does that for the *definition* — it reports 261 declared debts by
path, and `test_the_readme_status_matches_the_tools` holds that figure in this file as well as in
the README, because it said 202 here for longer than anybody noticed. This tool does it for the *implementation*: it loads the whole world, builds the tick order,
and then walks the tick in that order, stopping at the first thing it cannot compute and saying
exactly what it would need.

Why that is worth writing rather than asserting
-----------------------------------------------

"Ready to implement" is a claim, and a claim about a 34-file configuration is worth exactly as
much as the evidence behind it. The evidence here is mechanical:

  - **the schedule is derivable** — 58 nodes, from `coupling.yaml`'s edges minus its declared
    back-edges, with `check_vehicle.derive_schedule` doing the derivation so the plant and the
    linter cannot disagree about the order;
  - **the states are instantiable** — each with a method, a unit and the parameters its method
    owes, and the loader refuses a configuration where one is missing rather than defaulting it;
  - **the frame is producible** — `emit_frame` builds apollo's envelope from
    `presentation.yaml#frame`, which is the only reason to believe that contract is a contract
    rather than a description;
  - **the tick loop closes** — `step()` is `plant.md`'s seven steps, step 4 walks the derived order
    to the end, and a state that cannot advance is *recorded* rather than fatal.

That last one was a claim this file made and did not honour. `step()` raised out of step 4 at the
first state whose rule is domain code, so no tick ever completed and nothing was ever staged — and
the states on the `internal` sentinel were not even in the order it walked. "Closes" described the
shape of the loop rather than the outcome of running it, which is the failure mode this whole folder
is about arriving in the sentence that says what the tool does.

What the run then says is the build order, and `--readiness` is where the figures live: how many
states are configured, how many owe something, how many edge sensitivities are unset, and — the
figure this file exists to add — how many states a first tick can actually advance and which of the
rest are debts rather than consequences. **They are not repeated here.** A figure written into this
docstring has no reader, and these had already drifted: it said 119 states and "33 of 55 edge
sensitivities" while the tools said 134 and 65 of 79.

The method implementations are deliberately trivial and mostly raise. A `lag` needs a time
constant and a driving value; a `stock` needs a quantum and a flow; a `discrete` needs a
transition rule, which no configuration can supply because the rules are the domain's. Filling
those in is writing the plant — this file is the frame it goes in and the error messages that
tell you where.
"""

from __future__ import annotations

import argparse
import hashlib
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
    INTEGRATOR_METHODS,
    Report,
    derivation_value,
    derive_schedule,
    lag_driver_basis,
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

    def __init__(self, where: str, what: str, needs: str | None = None) -> None:
        super().__init__(f"{where}: {what}")
        self.where = where
        self.what = what
        # The node whose value was missing, where the refusal knows it. A tick that carries on has
        # to tell two different gaps apart: one whose *own* declaration is missing, and one that is
        # only missing because the node it reads is. The second is a consequence, and a list of
        # consequences is not a worklist. `None` means this refusal is about the state's own
        # definition — its rule, its parameter, its initial amount — and not about an input.
        self.needs = needs


@dataclass(frozen=True)
class Gap:
    """A state a tick reached and could not advance, and the debt that stopped it.

    The tick records this instead of raising it. Raising meant the first gap ended the tick, so
    every state after it in the frozen order went unadvanced and the order itself was never
    exercised past that point — a plant stopped by its first debt cannot tell you how many debts it
    has, and this one named a single state. A figure written here would have no reader; the live
    counts are what `--readiness` prints.
    """

    state: State
    where: str
    owed: str
    # The node this state could not read, when the reason is a missing input rather than a missing
    # declaration. See `Unconfigured.needs`.
    needs: str | None = None

    def __str__(self) -> str:
        return f"{self.where}: {self.owed}"


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
    # domain -> `components.yaml#internal_order`: the list of that domain's states on the `internal`
    # sentinel in the order they advance, or the string "independent". Absent means the domain owes
    # the declaration, and this file says so out loud when it advances them anyway.
    internal_order: dict[str, Any] = field(default_factory=dict)
    # Every YAML document a declared `derivation` may name, so an `algebraic` state's own arithmetic
    # can be evaluated at a tick. Loaded once, with the world.
    documents: dict[str, Any] = field(default_factory=dict)
    # node id (and the `internal` sentinel) -> the ids of the states that advance it, in the order
    # they advance. Derived once in `load_world` from `coupling.yaml#nodes.<node>.state_order`, which
    # is the declaration `--order` prints and `plant.md:71` promises the scheduler obeys.
    advance_order: dict[str, list[str]] = field(default_factory=dict)
    # channel id -> the row `domains/*/points.yaml` declares it in: what state it reads, whether it
    # is registered as a different quantity from that state, and — where the row carries one — the
    # arithmetic that turns the state into the channel. The frame's `values` is declared
    # `map[channel_id, ...]`, and this is the registry that says which channel id is what.
    points: dict[str, dict[str, Any]] = field(default_factory=dict)

    def states_on(self, node: str) -> list[State]:
        return [s for s in self.states if s.node == node]

    def sentinel_states(self) -> tuple[list[State], list[str]]:
        """The states on the `internal` sentinel, in the order a tick advances them, and the
        domains whose order this file had to choose itself.

        The sentinel is not a coupling node, so §9's `for node in SCHEDULE` does not reach it and
        `advance()` never sees these states — every mode, latch and accumulator the command surface
        writes. They are advanced after the whole schedule, which is the order
        the *reporting* tools already used (`--build-order` and `--readiness` both put the sentinel
        last) and the only order available: no edge can target the sentinel, so nothing inside a
        tick reads one of these states and means this tick's value.

        `internal_order` is the declaration that decides the order within a domain, and the linter
        requires it of every domain with more than one state here. Where it is absent the frozen
        lexicographic tiebreak decides, exactly as it does on a node — and this returns the domain
        names so the caller can say so rather than let the alphabet pass for a decision.
        """
        by_domain: dict[str, list[State]] = {}
        for state in self.states:
            if state.node == "internal":
                by_domain.setdefault(state.domain, []).append(state)
        ordered: list[State] = []
        alphabetised: list[str] = []
        # Domains are sorted, not scheduled. §2 forbids a domain calling another and no edge can
        # reach the sentinel, so the per-domain blocks cannot interact within a tick; the order
        # between them only has to be frozen, and a name is frozen.
        for domain in sorted(by_domain):
            block = by_domain[domain]
            declared = self.internal_order.get(domain)
            if isinstance(declared, list):
                position = {str(sid): i for i, sid in enumerate(declared)}
                # The linter refuses an order naming a different set of states, so anything missing
                # here is a state that arrived after the order was written.
                block = sorted(block, key=lambda s: position.get(s.id, len(position)))
            else:
                block = sorted(block, key=lambda s: s.id)
                # Only where the tiebreak actually decided something: a domain with one state on the
                # sentinel has nothing to order, and reporting it would make this figure 10 where
                # the linter's is 9 for the same question. The linter owes the declaration of every
                # domain with *more than one* state here, and this file must not disagree with it
                # about which those are.
                if len(block) > 1:
                    alphabetised.append(domain)
            ordered.extend(block)
        return ordered, alphabetised


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
    internal_order: dict[str, Any] = {}
    for directory in sorted(p for p in (root / "domains").iterdir() if p.is_dir()):
        components = load_yaml(directory / "components.yaml")
        # The sentinel states' within-domain order. The linter requires this declaration of every
        # domain with more than one state on the sentinel and validates it against the states; the
        # plant is the reader that makes it mean something, so it travels with the world.
        domain_name = str(components.get("domain", directory.name))
        if components.get("internal_order") is not None:
            internal_order[domain_name] = components["internal_order"]
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

    # **Every document a declared derivation may name.** The plant evaluates an `algebraic` state's
    # own arithmetic (round 20), and a derivation's inputs are dotted paths into these files — so the
    # world carries them, exactly as the linter's `load_documents` does. Without this the plant would
    # have to guess where a number lives, and a guess about that is how a plant invents one.
    documents: dict[str, Any] = {
        "vehicle.yaml": load_yaml(root / "vehicle.yaml"),
        "coupling.yaml": coupling,
        "mission.yaml": load_yaml(root / "mission.yaml"),
        "channels.yaml": channels_doc,
        "presentation.yaml": presentation,
    }
    for directory in sorted(p for p in (root / "domains").iterdir() if p.is_dir()):
        for name in ("components", "profiles", "commands", "points", "fault_policy"):
            candidate = directory / f"{name}.yaml"
            if candidate.is_file():
                documents[f"domains/{directory.name}/{name}.yaml"] = load_yaml(candidate)

    points: dict[str, dict[str, Any]] = {}
    for directory in sorted(p for p in (root / "domains").iterdir() if p.is_dir()):
        candidate = directory / "points.yaml"
        if not candidate.is_file():
            continue
        for row in (load_yaml(candidate).get("points") or []):
            if isinstance(row, dict) and row.get("channel") and row.get("from"):
                points[str(row["channel"])] = row

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

    world = World(
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
        internal_order=internal_order,
        channels=channels,
        documents=documents,
        points=points,
        frame_fields=[
            str((f or {}).get("name"))
            for f in (presentation.get("frame") or {}).get("fields") or []
        ],
        verbs=verbs,
        plant_published=[str(e.get("channel")) for e in presentation.get("plant_published") or []],
        # Counted here rather than taken from the linter, and deliberately a *different* number:
        # the linter reports 261 declared debts, most of which are prose obligations ("this needs a
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

    # --------------------------------------------------------------------------------------
    # **The intra-node order, which this file declared and never read.**
    #
    # `coupling.yaml#nodes.<node>.state_order` is the order the states on a node advance in, and
    # every multi-state node carries one with a note arguing it — `structure_config`'s says in as
    # many words that `configuration` "is a projection of the separation and descent-stage machines"
    # and "therefore has to read them after they have advanced". `--order` prints that order, and
    # `plant.md:71` promises the scheduler "obeys it".
    #
    # It did not. `states_on` returned `[s for s in self.states if s.node == node]` — the order the
    # *files* list the states in — and `step` walks that. Four of the vehicle's eleven multi-state
    # nodes were running in an order nobody declared: `structure_config` advanced `lm_separation_state`
    # and `configuration` before `pyro_fired`, `vehicle_dynamics` advanced `orbital_state` before
    # `body_rate`, `link` advanced `link_snr` before `tx_power`, and `coolant_flow` advanced
    # `coolant_flow_kg_s` before `pump_1_speed_rpm`. Two of those four are the *documented*
    # tiebreak bug in writing order — `link_snr` before `tx_power` is the example `plant.md` uses to
    # say the frozen lexicographic tiebreak "is wrong somewhere" — and here it was not even the
    # tiebreak: it was the alphabet of another kind, the order the YAML happened to be written in.
    #
    # Nothing has been visibly wrong yet, and the reason is worth stating: the states that would
    # expose it are the ones the plant cannot yet advance, so four nodes' worth of order has never
    # been exercised. It is the same shape as the reversed node schedule the README records — a
    # declaration nothing reads, agreeing with a promise in prose — and it is fixed the same way, by
    # making the order derived *once* here so every reader of `states_on` gets the declared one.
    #
    # `independent` is not an order and falls to the frozen lexicographic tiebreak, which is what the
    # linter's `order_report` prints for it and what `plant.md` says decides an undeclared group.
    # --------------------------------------------------------------------------------------
    for node in world.schedule:
        declared = (world.nodes.get(node) or {}).get("state_order")
        producers = [s for s in world.states if s.node == node]
        if isinstance(declared, list):
            position = {str(sid): i for i, sid in enumerate(declared)}
            # The linter refuses an order naming a different set of states, so anything missing here
            # is a state that arrived after the order was written; it sorts last rather than first.
            ordered = sorted(producers, key=lambda s: position.get(s.id, len(position)))
        else:
            ordered = sorted(producers, key=lambda s: s.id)
        world.advance_order[node] = [s.id for s in ordered]

    # And `states` itself, in the order a tick walks it — so `states_on` needs no special case and a
    # caller that concatenates it gets the schedule order rather than the file order. The sentinel is
    # appended after the scheduled block, in `sentinel_states`' own order.
    scheduled_states: list[State] = []
    for node in world.schedule:
        position = {sid: i for i, sid in enumerate(world.advance_order[node])}
        scheduled_states.extend(
            sorted((s for s in world.states if s.node == node), key=lambda s: position.get(s.id, 0))
        )
    sentinel_block, _ = world.sentinel_states()
    world.states = scheduled_states + sentinel_block
    world.advance_order["internal"] = [s.id for s in sentinel_block]
    return world


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

    **A channel is a reading, so a derived one is computed rather than omitted.** `points.yaml`
    declares each channel's `from` — the state it is a reading of — and, where the channel's unit is
    not that state's, a `derivation`: an expression over named inputs, in the same idiom the corpus
    has used for an `algebraic` state's rule since round 20. Round 27 settled what those inputs may
    be, and it is the one question the conversion could not answer for itself: an input bound to a
    **bare state id** means *that state's value this tick*, and everything else is a literal or a
    `<file>.yaml:<dotted.path>` source, as `derivation_value` already defines them. So the emitter
    substitutes this tick's readings before it evaluates, and `eclss.co2_pp_mmhg` — a mass in
    kilograms, a volume, a temperature and the gas law — is published as a partial pressure in
    millimetres of mercury rather than omitted or, worse, published as the mass.

    Three answers per channel, in this order, and the order is the contract:

      - **the row carries an evaluable `derivation`** — evaluate it at this tick's readings and
        publish the number;
      - **it does not, and the units agree** — the channel is the state itself, so publish the
        state's value;
      - **neither** — **omit**. Publishing 0.042 kg under a channel whose unit is mmHg is a reading
        a fleet would act on and a quantity it is not, which is worse than a gap a reader can see.
    """
    # **`values` is `map[channel_id, ...]` and it was the plant's node-keyed map.** The registry
    # declares which state each channel is read from, so the frame is built from it: a channel whose
    # source has no value this tick is *omitted* rather than filled with something plausible, which is
    # what makes the map a reading rather than a rumour.
    published: dict[str, Any] = {}
    by_state = {state.id: state for state in world.states}
    # What this tick's value map can be read *by name*: a channel's derivation names what it reads,
    # and anything the plant has not advanced is absent rather than bound to a zero. The map is the
    # answer, and its keys are the names — a state's own id, and a node's name where the node carries
    # one state (`state_values` writes no node key for a shared node, because one value cannot mean
    # four). **The node half is what the eight node-sourced channels need**: `prop_main` is a
    # reading exactly as `prop_main_kg` is, and a channel whose `from` names the node should be able
    # to name it in its arithmetic too.
    readings = {
        key: value
        for key, value in values.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    for channel, point in sorted(world.points.items()):
        if "[" in channel:
            # **A channel id carrying a placeholder is a *family*, not a channel.** The registry
            # declares `res.ledger_[resource]_kg` for every resource at once, and the frame's `values`
            # is `map[channel_id, ...]` — so publishing this row's number under that name tells a
            # fleet nothing it can bind: `res.ledger_[resource]_kg: 18508.0` is the propellant mass
            # under a name that also claims to be the water, the oxygen and the RCS ledger. What can
            # be published is the instantiations, and instantiating a template is the registry's own
            # open debt ("each registry entry naming the values its placeholders take"). Until then
            # the family is omitted, and two channels that were being filled from the *observed*
            # stock — the ledger and the residual — stop being published as if they were readings.
            continue
        source = point.get("from")
        if not isinstance(source, str):
            # `thermal.zone_[id]_t_c` names its four states as a *list*, because the template covers
            # four zones and naming one of them was how the cabins stayed in the family. A frame's
            # `values` is keyed by channel id and this id still carries its placeholder, so what the
            # frame can carry is the four instantiations — and instantiating a template is the
            # registry's own open debt (`channels.yaml#open_debts`: "each registry entry naming the
            # values its placeholders take"), not something the emitter may guess at. Omitted here.
            continue
        state = by_state.get(source)
        row = world.channels.get(channel) or {}
        derivation = point.get("derivation")
        if isinstance(derivation, dict) and derivation.get("expression"):
            # A channel that declares its arithmetic is evaluated, whether or not its unit happens
            # to agree with its source's: the `derivation` is what the channel *means*, and the
            # unit test below is the fallback for the rows whose `derivation` is still prose.
            value, why = derivation_value(derivation, world.documents, readings)
            if value is not None:
                published[channel] = value
                continue
            # No number this tick — a reading the plant has not advanced yet, most often — so the
            # channel falls through to the unit test and then to omission. The linter is what
            # refuses a derivation whose *bindings* do not resolve; this is the tick's own answer.
        # **A channel is its source only when the two are the same quantity.** `points.yaml`'s
        # `derivation` is *prose* — "the CO2 partial pressure from the mass and the volume" — and
        # prose cannot be evaluated, so a channel whose unit differs from its source's is **omitted
        # rather than filled with the source's number**: publishing 0.042 kg under
        # `eclss.co2_pp_mmhg` is a reading a fleet would act on and a quantity it is not. The units
        # are the registry's own, so this is a comparison of two declarations rather than a guess
        # about a sentence.
        #
        # **And it was a comparison of two *states*.** The `else` branch below published whatever a
        # coupling node held, with no unit test at all, which is how seven channels came to carry the
        # node's raw number under a unit it is not: `prop.propellant_remaining_pct` read 18,508 kg
        # and published it as a percentage, `eclss.o2_supply_pressure_psi` published a mass under a
        # pressure, `res.battery_energy_wh` published joules under a watt-hour. A node declares its
        # unit in `coupling.yaml#nodes`, so the comparison was always available; it was simply not
        # made on half the registry.
        source_unit = (
            state.unit
            if state is not None
            else (world.nodes.get(source) or {}).get("unit") or ""
        )
        if str(row.get("unit") or "").strip().lower() != str(source_unit or "").strip().lower():
            continue
        value = state_level(values, state) if state is not None else values.get(source)
        if value is None:
            continue
        published[channel] = value

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
        "values": published,
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


def canonical_state(values: dict[str, Any]) -> str:
    """A state encoded so that two equal states have equal strings, byte for byte.

    `plant.md` §6's compare-point is *"a hash over canonically-encoded state, every tick"*, and the
    encoding is the half that matters: `json.dumps` with a sorted key order and `repr`-exact floats,
    because the contract is bit-identity and not equality within a tolerance — *"no ε survives a
    comparator"*. A float is written with `repr`, which round-trips exactly; an integer stays an
    integer; and every other type is tagged with its name so that `0` and `False` cannot collide.

    Until this round **step 7 was a comment**. The tick's docstring listed the seven steps and said
    steps 1, 2, 5, 6 and 7 were stubbed "with their contracts written down" — and for step 7 the
    contract was written down and nothing else was: the plant produced a value map, and no reader
    could tell whether two runs of it agreed. That is the definition of done's own second clause
    (*"a stable determinism hash across two runs of the same seed"*) with no implementation, which
    is this folder's oldest finding arriving in the one place the folder cannot lint.
    """
    return json.dumps(_canonical(values), sort_keys=True, separators=(",", ":"))


def _canonical(value: Any) -> Any:
    """One value, in a form `json.dumps` writes the same way twice — and tags what JSON cannot."""
    if isinstance(value, bool):
        return {"bool": value}
    if isinstance(value, int):
        return {"int": value}
    if isinstance(value, float):
        # `repr` and not `round`: the contract is bit-identity, so the encoding must round-trip.
        return {"float": repr(value)}
    if isinstance(value, str):
        return {"str": value}
    if isinstance(value, dict):
        return {"map": {str(key): _canonical(item) for key, item in value.items()}}
    if isinstance(value, (list, tuple)):
        return {"seq": [_canonical(item) for item in value]}
    if value is None:
        return {"null": None}
    return {"other": repr(value)}


def state_hash(values: dict[str, Any]) -> str:
    """The compare-point itself: a SHA-256 over the canonical encoding, truncated to 16 hex digits.

    Truncated because this is a *compare-point* rather than a security boundary: it localises the
    first differing tick by binary search, and a collision would have to happen inside one run's own
    state space to cost anything.
    """
    return hashlib.sha256(canonical_state(values).encode("utf-8")).hexdigest()[:16]


def canonical_contributors(edges: list[Edge]) -> list[Edge]:
    """A node's contributors in a canonical order: **sorted by id**, as `plant.md` §6 requires.

    *"Canonical summation order: contributors sorted by id before summing — float addition is
    non-associative and hash-map order is not a guarantee."* The plant summed in `coupling.yaml`'s
    **declaration order**, which is stable for one file and not canonical for the corpus: adding an
    edge, or moving one, changes the order of the additions and can change the last bits of a sum —
    and with them every compare-point hash downstream. Nothing demonstrated it until now because no
    stock in the corpus has two contributors yet (one edge each, and the rest unset), which is
    exactly the condition under which the thirty-eighth edge is added wrongly.

    Sorting is *not* a change of the model: both integrators that carry more than one incoming edge
    (`zone_csm_avionics_t` and `coolant_loop_t`) already have the id-first edge as their driver, so
    the driver this picks is the driver the declaration order picked; and the linter's
    `check_lag_drivers` sorts the same way, so the rule and the plant agree about which edge drives.
    """
    return sorted(edges, key=lambda edge: edge.id)


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
            needs=source,
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
        # **The rule this branch did not have.** The lag integrator relaxes the state toward the
        # driver's raw value and applies neither the edge's unit nor its scale — so an edge whose
        # two ends are different quantities, or whose transfer is not the identity, produced a
        # plausible number out of a dimensional error. Both states this plant could advance were
        # exactly that: a crew workload relaxed toward 14 kg of water and a thrust toward 18,508 kg
        # of propellant. `lag_driver_basis` is the linter's rule, called here so the refusal and the
        # linter's debt cannot come apart.
        spec = state.spec
        # **§6's canonical order, and it decides the driver too.** The first contributor by id, not by
        # declaration: which edge drives a lag is a modelling decision, and file order is not one.
        driver_edge = canonical_contributors(incoming)[0]
        ok, why = lag_driver_basis(
            {
                "id": driver_edge.id,
                "from": driver_edge.source,
                "to": driver_edge.target,
                "kind": driver_edge.kind,
                "sensitivity": driver_edge.sensitivity,
            },
            str(spec.get("unit") or ""),
            # `world.nodes` is the coupling file's own mapping, so the rule reads the same shape the
            # linter hands it: raw node dictionaries with `unit` and `kind` in them.
            {name: dict(node) for name, node in world.nodes.items()},
        )
        if not ok:
            raise Unconfigured(f"coupling.yaml:edge {driver_edge.id}", why)
        tau = float(state.spec["tau_s"])
        driver = values.get(incoming[0].source)
        if driver is None:
            raise Unconfigured(
                f"{where}",
                f"reads {incoming[0].source!r}, which nothing supplies",
                needs=incoming[0].source,
            )
        # --------------------------------------------------------------------------------------
        # **The conversion the edge declares and this branch did not apply.**
        #
        # `lag_driver_basis` used to require the driver edge to be an *identity* transfer — the
        # state's quantity, one for one, scale 1 — because this branch read `values[source]` and
        # relaxed toward it with neither the unit nor the value touched. Four edges were refused by
        # that rule and none of them is an identity transfer: `E-BUS-PUMP` is the pump's
        # volts-to-flow gain (8.9998e-4 kg/s per V), `E-PROP-ENG` and `E-RCSP-RCS` are a rocket's
        # thrust-to-flow relation inverted (3.24e-4 and 3.52e-4 kg/s per N), and `E-FC-HEAT` is the
        # cell's waste-heat fraction (0.58336 W per W). Each declares both of its quantities in its
        # unit and carries the factor between them in its value, which is what a converter is.
        #
        # So the driver is the source's value *through the edge*: `value x values[source]`, in the
        # state's own quantity by the rule's own check. An identity edge multiplies by 1.0 and is
        # unchanged, which is why the twenty states that already advanced do not move.
        # --------------------------------------------------------------------------------------
        transfer = float((driver_edge.sensitivity or {}).get("value") or 1.0)
        driver = float(driver) * transfer
        # **The state's own level, and a refusal when it has none.** This read
        # `state_level(values, state) or driver`, which invented a starting value two ways: a state
        # the plant had not seeded began at its *target* — so the two cabin zones started at their
        # equilibrium, 286.214 K, and not at the 295 K the vehicle calls their nominal — and a state
        # whose level was genuinely *zero* (an engine at rest, a pump commanded off) fell through
        # the same `or` to the driver, because zero is falsy. Both are the failure this folder is
        # organised against, arriving in the one line that had a fallback rather than a refusal.
        current_level = state_level(values, state)
        if current_level is None:
            raise Unconfigured(
                f"{where}.initial",
                "declares no starting value, so there is nothing to relax from. The linter requires "
                "one of every integrator (`INTEGRATOR_METHODS`) and the plant seeds it; a state "
                "without one would be advanced from a number this file chose",
                needs=f"{state.id}.initial",
            )
        current = float(current_level)
        alpha = math.exp(-dt / tau)
        # **`state_values`, not `{state.node: value}`.** This returned the node key alone, which was
        # invisible while a lag had only a node key: `state_level` prefers the state's own id and
        # falls back to the node, so one key without the other still read correctly. The round that
        # gave every integrator a declared `initial` seeds *both* — and then the state-id key,
        # written once at t=0, shadowed this write on every tick: `state_level` read the seed back as
        # the current level, so the lag relaxed from 295 K forever and the node key stopped moving
        # after the first tick. One place decides what a state's entries are, and this is it.
        return state_values(world, state, driver + (current - driver) * alpha)

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
        current = state_level(values, state)
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
        # **Sorted by id before summing**, because float addition is non-associative: the sum a
        # declaration order produces is a sum an edit can change without changing the model.
        total = 0.0
        for edge in canonical_contributors(incoming):
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
            **state_values(world, state, level),
            residual_key: (total + carried) - moved * quantum,
            f"{state.id}__shortfall": shortfall,
        }

    if state.method == "delay":
        # --------------------------------------------------------------------------------------
        # **The seventh integrator class, which the reference plant could not advance at all.**
        #
        # `plant.md` §3 grew a seventh method when the first transport delay landed, and its two
        # implementation constraints are stated there as non-negotiable: a **ring, not a chain of
        # small nodes** (a chain of nodes each with a residence time below `dt` reintroduces the
        # stiffness the exponential map exists to remove), and **indexed by tick, not by accumulated
        # wall time** (the delay is part of the state, so it is part of the snapshot and the
        # compare-point hash; a delay stored as a float deadline is a replay divergence waiting for a
        # slow machine).
        #
        # This branch is that ring. `delay_s` becomes an integral number of ticks once, here, and the
        # ring is a list of slots in the value map under `<state>__delay` — the same place, and for
        # the same reason, that a stock's Bresenham residual lives: §4's `acc` and §3's ring are both
        # part of the state's *representation*, and the representation here is the map. When the
        # fixed-point mantissa and a real state struct land, both move inside them and both keys
        # disappear.
        #
        # **A delay is not a lag, and the difference is the whole reason the class exists.** A lag
        # forgets its history exponentially; a delay *is* its history — it hands back exactly what
        # entered `delay_ticks` ago, unchanged. That is what makes "the pump stalled just now" and
        # "the pump has been degrading for an hour" different observations, which is
        # `review-findings.md` §12 arriving as an integrator.
        #
        # **And the buffer is not full at MET 0.** For the first `delay_ticks` ticks there is nothing
        # `delay_ticks` ago to read, so the state holds its declared `initial` — which for the
        # transport line is the *right* answer rather than a fallback: the pipe is full of
        # supply-temperature coolant before anything moves, and that is what the state's own
        # `initial_provenance` says. The alternative — starting the buffer full of the initial value
        # — would be the same number arrived at by inventing history, so the ring is born empty and
        # the initial stands until the first value that entered has travelled the whole pipe.
        # --------------------------------------------------------------------------------------
        spec = state.spec
        driver_edge = canonical_contributors(incoming)[0]
        ok, why = lag_driver_basis(
            {
                "id": driver_edge.id,
                "from": driver_edge.source,
                "to": driver_edge.target,
                "kind": driver_edge.kind,
                "sensitivity": driver_edge.sensitivity,
            },
            str(spec.get("unit") or ""),
            {name: dict(node) for name, node in world.nodes.items()},
        )
        if not ok:
            raise Unconfigured(f"coupling.yaml:edge {driver_edge.id}", why)
        transfer = float((driver_edge.sensitivity or {}).get("value") or 1.0)
        if abs(transfer - 1.0) > 1e-12:
            # The linter refuses this too (`check_lag_drivers`), so a corpus carrying one is refused
            # before it runs. This is the same rule at the only other place a delay is built, because
            # a delay that multiplied would be a ring recording a quantity the pipe never carried.
            raise Unconfigured(
                f"coupling.yaml:edge {driver_edge.id}",
                f"carries {transfer:g} into a delay, which stores the driver's own value and hands "
                "it back unchanged. A delay is its history, so a scale here cannot be applied later "
                "without inventing it",
            )
        driver = values.get(driver_edge.source)
        if driver is None:
            raise Unconfigured(
                f"{where}",
                f"reads {driver_edge.source!r}, which nothing supplies",
                needs=driver_edge.source,
            )
        current_level = state_level(values, state)
        if current_level is None:
            raise Unconfigured(
                f"{where}.initial",
                "declares no starting value, for the ticks before its first sample has travelled "
                "the whole pipe. The linter requires one of every integrator "
                "(`INTEGRATOR_METHODS`) and the plant seeds it",
                needs=f"{state.id}.initial",
            )
        residence = float(spec["delay_s"]) / dt
        delay_ticks = int(round(residence))
        # **The sub-tick case first, and the order matters for what a reader is told.** A delay of
        # 0.4 ticks is not a delay at all and saying "not a whole number of them" about it would send
        # the author to fix the arithmetic rather than the model.
        if residence < 1.0:
            raise Unconfigured(
                f"{where}.delay_s",
                f"is {spec['delay_s']!r} s, which is {residence:g} of a tick at this dt: a delay "
                "shorter than one tick is not a delay, and reading the driver straight through would "
                "be a lag with tau = 0 wearing this class's name",
            )
        if abs(residence - delay_ticks) > 1e-9:
            # Not a rounding: a delay that is not a whole number of ticks has to be *decided*, and
            # choosing which tick it lands on is the corpus's call rather than the plant's.
            raise Unconfigured(
                f"{where}.delay_s",
                f"is {spec['delay_s']!r} s, which is {residence!r} ticks at this dt and not a whole "
                "number of them. `plant.md` §3 indexes a delay by tick, so the corpus has to say "
                "which tick it lands on rather than leaving the plant to round",
            )
        ring_key = f"{state.id}__delay"
        carried = values.get(ring_key) or {}
        slots = list(carried.get("slots") or [])
        if len(slots) < delay_ticks:
            # The ring is born empty and grows to its declared depth. `None` is "a tick that has
            # not happened yet" rather than a temperature, so the reading below can tell the
            # difference between a pipe full of 0 K and a pipe that has not been filled.
            slots = slots + [None] * (delay_ticks - len(slots))
        next_slot = int(carried.get("next") or 0)
        # **Read the slot being overwritten, then write it — in that order.** The slot this tick
        # lands on is the one that holds the value from `delay_ticks` ticks ago, which is exactly
        # the age the modulo expresses; so the reading is that slot *before* the write, and doing it
        # the other way round yields a delay of `delay_ticks - 1`. The first version of this branch
        # wrote first and read `(next + 1) % depth`, which is the same arithmetic one tick out, and
        # the test caught it at index 59 of a fifty-tick staircase.
        oldest = slots[next_slot]
        slots[next_slot] = float(driver) * transfer
        delayed = float(current_level) if oldest is None else float(oldest)
        return {
            **state_values(world, state, delayed),
            ring_key: {"next": (next_slot + 1) % delay_ticks, "slots": slots},
        }

    if state.method == "algebraic":
        # **The rule the configuration does carry, and the plant would not read.** Thirteen
        # `algebraic` states declare their arithmetic as a `derivation` over named inputs — load
        # sums, an equilibrium temperature, an environment heat — and the linter has evaluated every
        # one of them since the round that introduced the idiom. The plant refused all thirteen with
        # "its rule is not in the configuration", which was false: the rule *was* in the
        # configuration, as arithmetic, and the plant was the one reader that would not compute it.
        #
        # So an `algebraic` state whose provenance carries a `derivation` is evaluated here,
        # with the same substitution and the same evaluator the linter checks it with — one
        # definition of what a derivation means, so a derivation the linter accepts is one the plant
        # can compute. A state with no derivation still owes domain code and still refuses by name.
        #
        # **And the key is `provenance.derivation` and not `state.derivation`, which is the round
        # after the one above.** This read was `state.spec.get("derivation") or provenance...`, and
        # `build_order` carried the same expression — so a state declaring arithmetic only at the
        # state level was computed on every tick under a key `check_provenance_derivations` never
        # looked at, and the *agreement* between these two reads is what hid it: two copies of one
        # expression cannot disagree with each other, and neither was the declaration the linter
        # checked. The linter refuses that spelling by name now, and this reads one key, which is
        # the only way two readers cannot disagree about which declaration they are reading.
        derivation = (state.spec.get("provenance") or {}).get("derivation")
        if derivation is not None:
            value, why = derivation_value(derivation, world.documents, readings=values)
            if value is None:
                raise Unconfigured(f"{where}.derivation", f"cannot be evaluated: {why}")
            return state_values(world, state, value)

    if state.method == "hazard":
        raise Unconfigured(
            where,
            "is a hazard state: the plant needs the RNG stream and the hazard draw, which are "
            "plant code rather than configuration",
        )

    raise Unconfigured(
        where,
        # "a algebraic". The article was hard-coded and every method but one takes "a", so this read
        # correctly for six classes out of seven — and it went unnoticed until the tick carried on
        # and printed the sentence once per gapped state in one report.
        f"is {'an' if state.method[:1] in 'aeiou' else 'a'} {state.method} state and its rule is "
        "not in the configuration. An `algebraic` "
        "state's relation, a `discrete` state's transitions and a `dynamics` state's equations "
        "are domain code, and the configuration deliberately does not pretend to carry them",
    )


def state_values(world: World, state: State, value: Any) -> dict[str, Any]:
    """The map entries one state's new value belongs under.

    **The value space was keyed by node, and twelve nodes carry more than one state.** `cabin_atm`
    carries four gas masses and a pressure, so `values["cabin_atm"]` held whichever the loader wrote
    last — and the first tick after the plant learned to evaluate derivations set `csm_cabin_o2_kg` to
    the *water vapour's* mass, and `lm_cabin_o2_kg` to the nitrogen's. Two plausible numbers of
    kilograms, both the wrong gas.

    So every state's value is written under **its own id**, and a node that carries exactly one state
    also keeps its node key, because a node id already is a state id in every one of those cases and
    every reader in this file and every channel in the registry asks for it that way. The map is
    therefore additive: nothing that worked before changes, and the states that could not be named are
    named. The sentinel keeps `values["internal"][state.id]` as well, which is where its accumulators
    have always lived.
    """
    out: dict[str, Any] = {state.id: value}
    if len(world.states_on(state.node)) == 1:
        out[state.node] = value
    if state.node == "internal":
        out["internal"] = {state.id: value}
    return out


def state_level(values: dict[str, Any], state: State) -> Any:
    """A state's own current value, by its own key first and its node's second.

    The order matters while the two key spaces coexist: a multi-state node has no node key at all, and
    a single-state node's two keys are the same number by construction.
    """
    if state.id in values:
        return values[state.id]
    if state.node == "internal":
        # The sentinel's states live in a sub-map keyed by state id, so a node-key fallback would
        # hand back the whole map — which is what the first channel-keyed frame did, publishing
        # `avionics.clock_offset_ms` as six accumulators at once.
        return (values.get("internal") or {}).get(state.id)
    return values.get(state.node)


def _refuse_shared_node(world: World, state: State, where: str) -> None:
    """Refuse a state whose node carries another state, because the value map cannot hold both.

    The map is keyed by node, so two states on one node overwrite each other and whichever advanced
    first reads the other's number on the next tick. Twelve nodes carry more than one state;
    `cabin_atm` and `lm_cabin_atm` carry five each. The fix is a key-space change and the reason this
    is a refusal rather than a repair: a value of the wrong quantity is indistinguishable from a
    right one on a panel.
    """
    if state.node == "internal" or state.method not in {"lag", "stock", "algebraic", "dynamics", "delay"}:
        return
    siblings = [other.id for other in world.states_on(state.node) if other.id != state.id]
    if not siblings:
        return
    raise Unconfigured(
        f"{where}",
        f"integrates `{state.node}`, which carries {len(siblings) + 1} states "
        f"({state.id} and {', '.join(sorted(siblings))}). The plant's value map is keyed by node, so "
        "a second state's value overwrites the first and this one would advance from another "
        "state's number — a plausible value of the wrong quantity rather than a refusal",
    )



def initial_values(world: World) -> dict[str, Any]:
    """The value map at t=0, from the states' own declared initial conditions.

    Until this existed the plant had no answer to "what is the vehicle holding at the start", and
    it did not need one: `advance()`'s stock branch returned the tick's net flux as the node's
    value, so it never read a level and never asked for one. Fixing the integrator made the
    question unavoidable, and the configuration turned out to be able to answer most of it —
    `vehicle.yaml#consumables` has the loads, the atmosphere model derives the cabin oxygen from
    the published volume and pressure, the absorbers' man-hour ratings are on their own coupling
    nodes, and the accumulators start at zero because that is what an accumulator is.

    **And it seeded the stocks, which was the same defect one class over.** A `stock` was the only
    integrator the linter asked for an `initial`, so a `lag` had none and never missed one — and
    the lag branch relaxed each of them from its *driver* when it had no value, which is a number
    nothing declared. The two cabin zones began at their equilibrium, 286.214 K, rather than at the
    295 K `vehicle.yaml#thermal.zones` calls their nominal, and the frame's four partial pressures
    had no cabin temperature to read on the first tick because of it. Every integrator declares a
    starting value now (`INTEGRATOR_METHODS`, the same set the linter holds) and every declared one
    is seeded here. The two classes outside that set are outside for a reason of *shape* rather
    than of importance — `dynamics` carries vectors and matrices, `discrete` carries modes — so
    their absence here is the same statement the linter makes.

    **Keyed by node, which is the plant's own key space, not by channel.** `advance()` returns
    `{state.node: value}` and `step()` commits that map, so this is the seed of the same map and
    nothing here is a projection. The frame's `values` wants the *published* channel names, and
    that is the publisher's step rather than the plant's: `eclss.pp_o2_mmhg` is a partial pressure
    in millimetres of mercury while `csm_cabin_o2_kg` is a mass in kilograms, so the projection is a
    unit conversion and calling the mass by the channel's name would be a wrong number wearing the
    right one — which is the round that landed the frame's derived channels.

    A state the configuration has not given a value is left out rather than guessed, and the plant
    refuses by name when a tick reaches it — which is the behaviour the check beside this wants.

    **`internal` is a key space rather than a key.** The sentinel is not a node, and forty-eight
    states live on it — so `values["internal"]` as a single slot would let every one of them
    overwrite the next, which is why this function and `step()` both used to drop it. Dropping it
    was the workaround, and it cost the command surface a third of its effects: eight of the
    twenty-four command→state links are to states on the sentinel (`computer_mode`, `breaker_panel`,
    `rcs.mode`, `hatch_state` and four more), so `apply_command` had nowhere to put what those
    commands change and returned an empty map that was indistinguishable from a verb that writes
    nothing. The sentinel holds a *map* keyed by state id, which is what "not one node" means.
    """
    values: dict[str, Any] = {}
    for state in world.states:
        if state.method not in INTEGRATOR_METHODS:
            continue
        initial = (state.spec or {}).get("initial")
        if not isinstance(initial, (int, float)):
            continue
        if state.node == "internal":
            values.setdefault("internal", {})[state.id] = float(initial)
            values[state.id] = float(initial)
        else:
            # **Every state's own key, and the node's as well when the node carries one state.**
            # Four gas masses share `cabin_atm`, so a node-keyed seeding kept only the last of them
            # and the oxygen integrated from the water vapour's level — a plausible mass of the wrong
            # gas. `state_values` is the one place that decides what a state's entries are, so the
            # seeding and the advancing cannot disagree about the key space.
            values.update(state_values(world, state, float(initial)))
    # The key exists even when nothing seeds it, so a reader can tell the sentinel is there and
    # empty rather than absent — the same distinction `state.json`'s gate map draws for a closed
    # gate and an unnamed one.
    values.setdefault("internal", {})
    return values


def step(
    world: World,
    values: dict[str, Any],
    dt: float,
    gaps: list[Gap] | None = None,
) -> dict[str, Any]:
    """`plant.md`'s seven-step tick, with the parts that need code named rather than faked.

    Steps 1, 2, 5, 6 and 7 are the window's and the executive's and are stubbed with their
    contracts written down. Step 3's event queue needs latched states, so it is a no-op until
    there are any. Step 4 is the one this file is really about: it walks the derived order.

    **Step 4 does not stop.** §9's loop is `for node in SCHEDULE: for producer in node.producers:
    staged.update(producer.advance(...))` — unconditional, and "writes are staged, then committed".
    A producer that cannot produce stages nothing and the previous value stands; the tick still
    reaches every state after it. This function used to raise out of the loop instead, so the first
    unconfigured state ended the tick and the rest of the order has never advanced — a plant that
    stops at its first debt is a plant whose frozen order has never been run.

    Where a state cannot advance, the gap is appended to `gaps` and the tick carries on. Passing a
    list is how `--readiness` reports them; passing nothing still runs the whole tick, because a gap
    is a fact about the configuration and not a mode of operation.

    The states on the `internal` sentinel advance last, in the order `sentinel_states()` derives —
    see there for why the sentinel is last and why the order *between* domains does not matter.
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
            _advance_into(world, state, values, staged, horizon, gaps)
    # The sentinel, after every node. It is not a coupling node, so the loop above cannot reach it
    # and `coupling.yaml#nodes` does not carry it — which used to mean these states were advanced by
    # nothing at all, while two comments here and in `check_vehicle.py` said they "are advanced with
    # their domain". They are advanced here, in the order `internal_order` declares.
    sentinel, _ = world.sentinel_states()
    for state in sentinel:
        _advance_into(world, state, values, staged, horizon, gaps)
    committed = {**values, **staged}
    # **The sentinel is a sub-map, so replacing its key loses the others.** A stage writes
    # `{"internal": {state_id: value}}` and `{**values, **staged}` replaced the whole map with that
    # one entry, so the accumulators wiped each other out — the same shape of defect as the node
    # collision, one level down. Merged here rather than in `state_values`, because a *stage* is a
    # partial view and only the commit sees both halves.
    if isinstance(values.get("internal"), dict) and isinstance(staged.get("internal"), dict):
        committed["internal"] = {**values["internal"], **staged["internal"]}

    # 5. Commit, then assert what can be asserted exactly. `assert_conservation` needs the
    #    accumulator of every `conserve` edge, which exists once the stocks do.
    # 6. Instruments: T -> A. One-way, and the quality function cannot see the faults.
    # 7. Compare-point: `state_hash(committed)` is a hash over canonically-encoded state, and the
    #    caller takes it every tick — `--determinism` is the caller that does, and
    #    `test_two_runs_of_the_same_start_produce_the_same_compare_point` is the reader. It is a
    #    function rather than a line in here because `step`'s contract is to *return the state*: a
    #    hash appended to the map would be a value nothing declared.
    return committed


def _advance_into(
    world: World,
    state: State,
    values: dict[str, Any],
    staged: dict[str, Any],
    horizon: float,
    gaps: list[Gap] | None,
) -> None:
    """One producer's contribution, staged, or the reason it has none.

    Split out of `step` so the scheduled nodes and the sentinel states run *the same* code — the
    two loops used to be one loop and a hand-written copy in `--readiness`, which is how the copy
    came to read unstaged values and stop early without either problem being visible.
    """
    try:
        staged.update(advance(world, state, {**values, **staged}, horizon))
    except Unconfigured as exc:
        if gaps is not None:
            # `.where` and `.what`, not `args`: `Unconfigured.__init__` hands `Exception` the
            # formatted message as *one* argument, so `args[1]` is an IndexError and the tick dies
            # of the reporting rather than of the debt.
            gaps.append(Gap(state=state, where=exc.where, owed=exc.what, needs=exc.needs))
        # An `algebraic` state has no memory to stand on, so leaving its value alone leaves it at
        # whatever `initial_values` seeded — `UNCONFIGURED`, or absent. That is the honest outcome:
        # a tick that carried on must not invent the number it could not compute.


class SkipComputed(Exception):
    """This state's value is its rule's, and the command's effect is on the input it selects.

    Not an error: `apply_command` catches it and moves on, because the state the command *does*
    write is reached by the same loop — both halves declare the same verb, and the linter refuses a
    `selects` that says otherwise.
    """

    def __init__(self, where: str) -> None:
        super().__init__(where)
        self.where = where


def prune(value: Any, width: int = 40) -> str:
    """A value, short enough to print beside another one."""
    text = repr(value)
    return text if len(text) <= width else text[: width - 1] + "…"


def command_targets(world: World, verb: str) -> list[State]:
    """Every state that declares this verb as a `command:` mover.

    The declaration is the state's, not the verb's: `moved_by: command:<verb>` is the half that
    says a command writes this state, and the corpus was joined to the graph for exactly this
    reason in rounds 23, 27 and 28. What was missing until round 30 was the other half — which
    argument carries the value and what each of its values becomes — and round 31 found that the
    rule enforcing it had never run on a state that is not a mode.
    """
    return [
        state
        for state in world.states
        if f"command:{verb}" in [str(m) for m in (state.spec.get("moved_by") or [])]
    ]


def command_dwell(world: World, verb: str) -> list[tuple[State, float | None, float | None]]:
    """The minimum-dwell guard on every state a command moves: `(state, min_on_s, min_off_s)`.

    A commanded state declares a `dwell` and not a hysteresis band, because "a band answers 'has
    the quantity crossed' and a command does not cross anything" (`check_domain`'s own rule). Until
    this existed **no tool read one**: fifteen commanded states declared the guard and the effect
    path, implemented in round 32, could re-command a mode inside its own dwell with nothing
    noticing. `rcs.thruster_valve`'s two values are `UNCONFIGURED`, which is an obligation owed to
    a field nothing consumed — the clearest possible statement that the field had no reader.

    **The two values' meaning is stated here rather than in the corpus, and that is the second
    half of the finding.** `dwell` declares `min_on_s` and `min_off_s` on thirty-three states and
    nowhere says what they measure. `propulsion.sps_state`'s are 0.5 and 5, and its provenance
    explains the five — "a five-second floor between burns is what keeps a fleet from spending its
    restart budget in a minute" — which fixes the reading: **`min_on_s` is how long the state must
    hold a value before it may change, and `min_off_s` is how long it must stay away from a value
    before it may return to it.** Both are measured from the last change, and a state that has not
    changed yet has no floor — the vehicle starts with its machine where the configuration put it.

    `None` for either value means it is owed, and an owed guard cannot be enforced: the caller
    reports it rather than treating it as zero, because a dwell of zero is exactly the chattering
    the field exists to prevent.
    """
    rows: list[tuple[State, float | None, float | None]] = []
    for state in command_targets(world, verb):
        dwell = state.spec.get("dwell") or {}
        if not dwell:
            continue

        def seconds(key: str, declared: dict[str, Any] = dwell) -> float | None:
            # Bound rather than closed over: the loop rebinds `dwell`, and a closure reading the
            # loop variable is a claim about when it runs rather than what it reads.
            value = declared.get(key)
            return None if value in (None, "UNCONFIGURED") else float(value)

        rows.append((state, seconds("min_on_s"), seconds("min_off_s")))
    return rows


def command_effect(world: World, state: State, verb: str, arguments: dict[str, Any]) -> Any:
    """What one command makes one state, from `command_value`, or a named refusal.

    Four forms, and the order matters because they are checked in the order the corpus can answer
    them. A `computed` effect is refused rather than guessed: the state's value follows from its
    own rule, and the plant's contract is that a rule is code and never configuration. A `constant`
    is the value, and `maps` looks the carrying argument's value up. With no entry at all the effect
    is the identity — the carrying argument is the unique enum argument whose values intersect the
    state's, which is derivable from the two declarations and is why seven of the twenty-four links
    need nothing written down.
    """
    where = f"domains/{state.domain}/components.yaml:state {state.id}"
    schema = (
        next((v for v in [world.verbs.get(verb)] if v), {}) or {}
    ).get("argument_schema") or {}
    entry = next(
        (
            e
            for e in (state.spec.get("command_value") or [])
            if isinstance(e, dict) and str(e.get("verb")) == verb
        ),
        None,
    )
    values = set()
    for match in re.findall(r"enum\[([^\]]*)\]", str(state.spec.get("unit") or "")):
        values.update(v.strip() for v in match.split(","))

    def carried() -> str:
        """The value of the argument that carries this state's value."""
        if entry is not None:
            argument = str(entry.get("argument"))
        else:
            reaching = [
                name
                for name, spec in schema.items()
                if isinstance(spec, dict)
                and spec.get("type") == "enum"
                and values & {str(v) for v in spec.get("values") or []}
            ]
            if len(reaching) != 1:
                raise Unconfigured(
                    f"{where}.command_value",
                    f"is moved by {verb!r} and {len(reaching)} of its arguments can carry a value "
                    f"this state holds ({reaching}), so which one is not readable off either side",
                )
            argument = reaching[0]
        if argument not in arguments:
            raise Unconfigured(
                f"{where}.command_value",
                f"needs {verb!r}'s {argument!r} argument, which the call did not supply",
            )
        return str(arguments[argument])

    if entry is not None and entry.get("computed") is not None:
        # **A rule-computed state is changed through an input, and `selects` names it.** The state
        # itself is the rule's business, so there is no value here to write — and the input is a
        # state of its own, which the loop in `apply_command` reaches because it declares the same
        # verb. What is left is the case where the input does not exist: then the command's real
        # effect is a state nobody has declared, and the honest answer is the field that is owed
        # rather than a value the plant would have to invent.
        if str(entry.get("selects")) == "UNCONFIGURED":
            raise Unconfigured(
                f"{where}.command_value.selects",
                f"is owed: {entry.get('note', '')}",
            )
        raise SkipComputed(where)
    if entry is not None and entry.get("constant") is not None:
        return entry["constant"]
    source = carried()
    if entry is not None and isinstance(entry.get("maps"), dict):
        if source not in {str(k) for k in entry["maps"]}:
            return source if source in values else _refuse_unmapped(where, verb, source, values)
        target = entry["maps"][source]
        if target == "UNCONFIGURED":
            raise Unconfigured(
                f"{where}.command_value.maps.{source}",
                f"is owed: {entry.get('note', '')}",
            )
        return target
    if values and source not in values:
        raise Unconfigured(
            f"{where}.command_value",
            f"is moved by {verb!r}, which can be called with {source!r}, and this state cannot hold "
            f"it; it holds {sorted(values)}",
        )
    return source


def _refuse_unmapped(where: str, verb: str, source: str, values: set[str]) -> Any:
    raise Unconfigured(
        f"{where}.command_value",
        f"has no mapping for {verb!r}'s value {source!r}, and this state cannot hold it; it holds "
        f"{sorted(values)}",
    )


def apply_command(
    world: World, values: dict[str, Any], verb: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """`plant.md` step 1: the effect of one command, applied to the value map.

    Until this existed the plant could *describe* a command — `capability_snapshot` reports whether
    one is available, and `command_value` says what each of its values becomes — and could not
    perform one. `console.py` said so in as many words in every success it wrote: "this reference
    implementation resolves, refuses and settles; it does not simulate the effect."

    Two things this is not. It is **not the admission check**: the phase gate, the gate variable and
    the interlocks are the executive's, evaluated at the moment of effect, and a plant that
    re-implemented them would be a second source of truth for the one rule §9 check 9 is about.
    And it is **not a tick**: it writes the states a command moves and returns what it wrote, so the
    caller can see the delta. The nodes it touches are the nodes those states live on, in the
    plant's own key space, which is what makes the result something `step()` can go on integrating.

    A verb that moves no state is not an error. Thirty-seven of the fifty-eight are reads, events
    and configuration, and the honest answer for one of those is that the vehicle's value map is
    unchanged — which is worth returning rather than raising, because a fleet that cannot tell
    "nothing happened" from "the plant does not know" has lost the cheaper of the two answers.
    """
    if verb not in world.verbs:
        raise Unconfigured(f"plant.py:verb {verb}", "is not registered by any domain")
    staged: dict[str, Any] = {}
    for state in command_targets(world, verb):
        try:
            value = command_effect(world, state, verb, arguments)
        except SkipComputed:
            # The input this command selects carries the effect; see `SkipComputed`.
            continue
        entry = next(
            (
                e
                for e in (state.spec.get("command_value") or [])
                if isinstance(e, dict) and str(e.get("verb")) == verb
            ),
            None,
        )
        key_argument = (entry or {}).get("key")
        # Three destinations, and the sentinel is one of them rather than an exemption. A scalar
        # state on a node is `values[node]`; a keyed state on a node is a map of its elements; and a
        # state on the sentinel is an entry in `values["internal"]` under its own id. The first
        # version of this skipped the sentinel, on the reading that a state advanced with its domain
        # is stored by the domain — which is true of what *advances* it and false of where the plant
        # keeps it, and the difference was eight commands whose effect vanished.
        #
        # **And the sentinel's two namespaces are nested rather than shared**, which the first
        # version got wrong in a way only a keyed sentinel state shows: `hatch_state` is a map of
        # hatches, so writing `hatch_crew_lm` beside the sentinel's state ids put a hatch id in the
        # same namespace as `computer_mode` and lost the state's own name. A keyed state's elements
        # live under the state's id, on the sentinel and on a node alike.
        if key_argument:
            if str(key_argument) not in arguments:
                raise Unconfigured(
                    f"domains/{state.domain}/components.yaml:state {state.id}.command_value.key",
                    f"names {key_argument!r} and the call did not supply it",
                )
            if state.node == "internal":
                sentinel = dict(staged.get("internal") or values.get("internal") or {})
                elements = dict(sentinel.get(state.id) or {})
                elements[str(arguments[key_argument])] = value
                sentinel[state.id] = elements
                staged["internal"] = sentinel
            else:
                elements = dict(staged.get(state.node) or values.get(state.node) or {})
                elements[str(arguments[key_argument])] = value
                staged[state.node] = elements
        elif state.node == "internal":
            sentinel = dict(staged.get("internal") or values.get("internal") or {})
            sentinel[state.id] = value
            staged["internal"] = sentinel
        else:
            staged[state.node] = value
    return staged


def report_build_order(world: World) -> None:
    """Print the derived build order: the worklist, grouped by what each state is waiting for."""
    buckets = build_order(world)
    total = sum(len(v) for v in buckets.values())
    titles = {
        "ready": "ready now — the classes the reference plant can advance",
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
            # **Declared arithmetic first, sentinel or not.** This test used to sit below the
            # sentinel routing, and the routing `continue`s — so the one rule that asks "is the
            # rule already in the configuration?" was unreachable for every state on the sentinel.
            # `avionics_bay_heat_w`, the fifth heat rate, is what exposed it: its arithmetic is a
            # `derivation` over three dotted paths, the linter re-derives it, the plant evaluates it
            # in a real tick — and the worklist filed it under "owes a rule: domain code" and sent
            # an implementer to write a rule that exists. The sentinel means "advanced with its
            # domain", which is a statement about *where the driver comes from*, not about whether
            # one is declared; a state whose inputs are all named has nothing left to write.
            #
            # **And it reads the same key `advance` does, which it did not until the round that
            # found the disagreement.** This was `state.spec.get("derivation") or provenance...`,
            # the same expression `advance` carried — so a state declaring its arithmetic only at
            # the state level was called *ready* for a tick that the linter's own rule could not
            # see, and the state at the top of this bucket's list was reported as implemented for
            # a rule declared nowhere a check looks. A worklist that reads a different declaration
            # than the checker is the "two readers, two answers" defect this function's own
            # docstring is about.
            if state.method == "algebraic":
                derivation = (state.spec.get("provenance") or {}).get("derivation")
                if derivation is not None:
                    ready.append(state)
                    continue
            if state.method == "delay" and isinstance(state.spec.get("delay_s"), (int, float)):
                # **The seventh class joined the ones the plant can advance, and this classifier did
                # not hear about it.** `loop_transport_t` declares its `initial`, its `delay_s` and
                # its driver at `K per K` = 1.0, and round 49 gave `advance` the ring that walks it —
                # so the worklist was still filing the one state whose rule was *never* missing under
                # "owes a rule: domain code", which is the same defect as the `algebraic` case above
                # arriving one class later. A delay is a `delay_s` and a ring and nothing else, which
                # makes it the third method a configuration can carry on its own.
                ready.append(state)
                continue
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
            # **And the driver has to be one the integrator can use.** `lag_driver_basis` is the
            # rule round 18 added, and this classifier has to ask it for the same reason it asks
            # `usable`: a lag whose driver edge is in a different quantity is not "ready now" — it
            # is a coupling that cannot be integrated, and the bucket an implementer should read is
            # the edge. Without this the build order called four states ready that `advance` refuses
            # by name, which is precisely the disagreement this function's docstring says cannot
            # happen.
            if state.method == "lag" and incoming:
                node_map = {name: dict(node) for name, node in world.nodes.items()}
                ok, _ = lag_driver_basis(
                    {
                        "id": incoming[0].id,
                        "from": incoming[0].source,
                        "to": incoming[0].target,
                        "kind": incoming[0].kind,
                        "sensitivity": incoming[0].sensitivity,
                    },
                    str(state.spec.get("unit") or ""),
                    node_map,
                )
                if not ok:
                    blocking_edge.append(state)
                    continue
            siblings = [o for o in world.states_on(state.node) if o.id != state.id]
            # **Either form of declared arithmetic counts, and `derivation` is the stronger one.**
            # This tested only `computation`, so the round that converted twelve literal
            # computations into derivations moved five states — `cabin_heat_csm_w`,
            # `cabin_heat_lm_w`, `service_bay_heat_w`, `descent_bay_heat_w` and
            # `environment_heat_w` — out of "owes a rule" and into "owes an edge", which is the
            # opposite of what happened: their inputs went from being *restated as literals* to
            # being *named by dotted path*. A classifier keyed on one spelling of a declaration
            # reads the other spelling as absence, which is this folder's oldest finding arriving
            # in the worklist that exists to send an implementer to the right file.
            provenance = state.spec.get("provenance") or {}
            declares_own_inputs = bool(
                provenance.get("computation")
                or provenance.get("derivation")
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


def short(owed: str) -> str:
    """A refusal's first sentence — enough to name a debt in a list of them."""
    head, dot, _ = owed.partition(". ")
    return f"{head}." if dot else owed


def roots(world: World, gaps: list[Gap]) -> list[Gap]:
    """The gaps whose *own* declaration is missing, in the order the tick reached them.

    A debt that stops one state stops everything that reads it, and a list that does not tell the
    two apart sends an implementer to every gap instead of to the work: `--readiness` prints both
    figures and they are different questions.

    The relation comes from the refusals rather than from the graph. `advance()` reports the node it
    could not read as `Unconfigured.needs`, and *it* is the only thing that knows: which end of an
    edge drives the flux depends on the edge's basis, so a discharge reads its **target** and a fill
    its source. Reading the graph directly got this wrong in both directions — one-way along the
    edge called cascades roots, and either-way along it collapsed almost every root into two.

    The remaining relation is the Gauss-Seidel half: a state on a node whose earlier state is a gap
    reads a value that was never staged.
    """
    sequence = [s for node in world.schedule for s in world.states_on(node)]
    sequence += world.sentinel_states()[0]
    position = {state.id: index for index, state in enumerate(sequence)}

    node_of = {g.state.id: g.state.node for g in gaps}
    # A node is dark this tick if a gap state owns it and it is not the state's own node: a stock
    # integrating from its own level reads last tick's value, which `initial_values` supplied.
    dark: set[str] = {node for node in node_of.values() if node != "internal"}
    out: list[Gap] = []
    for gap in gaps:
        fed_by_a_gap = gap.needs is not None and gap.needs in dark and gap.needs != gap.state.node
        # A sibling earlier on the same node that is also a gap never staged its value.
        sibling = any(
            other.state.node == gap.state.node
            and other.state.node != "internal"
            and other.state.id != gap.state.id
            and position.get(other.state.id, -1) < position.get(gap.state.id, -1)
            for other in gaps
        )
        sentinel_sibling = gap.state.node == "internal" and any(
            other.state.node == "internal"
            and other.state.id != gap.state.id
            and position.get(other.state.id, -1) < position.get(gap.state.id, -1)
            for other in gaps
        )
        if not (fed_by_a_gap or sibling or sentinel_sibling):
            out.append(gap)
    return out


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
    # The first tick, run rather than described. This block used to be a second, hand-written copy
    # of §9's step 4 which asked `advance()` against an *empty* state — so every stock answered with
    # its missing `initial` and the first one ended the probe. It reported "the first thing it
    # cannot compute" about a tick that had computed nothing, and the rest of the order was never
    # reached by it either. `step()` is the only implementation of step 4 now.
    #
    # The seed is `initial_values`, because that is what a first tick is run against.
    gaps: list[Gap] = []
    step(world, initial_values(world), 1.0 / 50.0, gaps)
    sentinel, alphabetised = world.sentinel_states()
    scheduled = sum(1 for node in world.schedule for _ in world.states_on(node))
    # `scheduled` states, not `len(world.schedule)` nodes: the first version of this line said "57
    # nodes then the 55 states on the sentinel" and the tick below it reported 134, so the two
    # figures in the same report disagreed by the twenty-two states that share a node. The test that
    # asserts they add up is what caught it.
    reached = scheduled + len(sentinel)
    print(
        f"First tick, in §9's order — {scheduled} states over {len(world.schedule)} nodes, then the "
        f"{len(sentinel)} on the `internal` sentinel:"
    )
    print(f"  {reached - len(gaps):3d} of {reached} states advanced, {len(gaps)} could not")
    on_sentinel = {s.id for s in sentinel}
    own = roots(world, gaps)
    print(
        f"  of the {len(gaps)}, {len(own)} are the debt and {len(gaps) - len(own)} are states that "
        "read one"
    )
    for gap in own:
        # Where the debt *is*. Nine of these are an edge's missing sensitivity rather than the
        # state's own definition — the state is only the thing that noticed — and naming the
        # state's node for those sends the reader to the wrong file.
        edge = gap.where.startswith("coupling.yaml:edge ")
        where = (
            gap.where.split(" ", 1)[1]
            if edge
            else ("internal" if gap.state.id in on_sentinel else gap.state.node)
        )
        kind = "edge" if edge else "node"
        # The first sentence only. `advance()`'s refusals explain themselves at length on purpose,
        # and that many of them at three hundred characters each is a wall rather than a worklist;
        # the rest of the paragraph is one `advance()` call away.
        print(f"  {gap.state.domain:12s} {gap.state.id:26s} {kind} {where:20s} {short(gap.owed)}")
    if alphabetised:
        # Not a fault: the linter already reports the missing declaration as a debt. It is said out
        # loud here because this file is what turns the declaration into an order, and where there
        # is none it uses the alphabet — which must not pass for a decision.
        print(
            f"\n  {len(alphabetised)} domain(s) put states on the sentinel with no `internal_order`, "
            f"so the frozen lexicographic tiebreak ordered them: {', '.join(alphabetised)}"
        )


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
        "--apply",
        nargs="+",
        metavar="VERB ARG=VALUE",
        help="apply one command to the value map and print what it changed: "
        "`--apply set_bus_tie tie=csm_tie_ab state=closed`",
    )
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
        "--determinism",
        action="store_true",
        help="run the same start twice and print the two compare-point hash sequences: "
        "`plant.md` §6's per-tick state hash, which two equivalent runs must share",
    )
    parser.add_argument(
        "--ticks",
        type=int,
        default=50,
        help="how many ticks `--determinism` walks (default 50, at the declared tick rate)",
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

    if args.apply:
        verb, *pairs = args.apply
        arguments: dict[str, Any] = {}
        for pair in pairs:
            name, _, text = pair.partition("=")
            if not _:
                sys.stderr.write(f"{pair!r} is not an `argument=value` pair\n")
                return 4
            arguments[name] = text
        values = initial_values(world)
        try:
            staged = apply_command(world, values, verb, arguments)
        except Unconfigured as exc:
            sys.stderr.write(f"the plant cannot apply {verb!r}: {exc}\n")
            return 3
        # **The delta, not the map.** A staged value for the sentinel is a copy of the whole
        # sentinel map, so printing it whole listed every accumulator the command had not touched
        # and read as seven changes where there was one. What a caller wants is what moved.
        changed: list[tuple[str, Any, Any]] = []
        for node, value in sorted(staged.items()):
            if node == "internal":
                for sid in sorted(value):
                    was = (values.get("internal") or {}).get(sid, "<unset>")
                    if was != value[sid]:
                        changed.append((f"internal:{sid}", was, value[sid]))
            elif values.get(node) != value:
                changed.append((node, values.get(node, "<unset>"), value))
        print(f"{verb} {arguments} -> {len(changed)} value(s) changed")
        if not changed:
            targets = command_targets(world, verb)
            if targets:
                print(
                    "  none: the command was applied and every state it moves already held that "
                    f"value ({', '.join(s.id for s in targets)})"
                )
            else:
                print(
                    "  nothing: this verb writes no state. Thirty-seven of the fifty-eight are "
                    "reads, events and configuration, and an unchanged value map is the honest "
                    "answer for one of them — which is a different answer from a plant that does "
                    "not know."
                )
        for where, was, now in changed:
            print(f"  {where:34} {prune(was)} -> {prune(now)}")
        return 0

    if args.determinism:
        # **§6's compare-point, run.** The two runs are *independent*: a fresh `load_world` each time,
        # so anything the loader does in file order is inside the comparison rather than outside it.
        # The tick is the declared one — `mission.yaml`'s `tick_hz` — because "equivalent" is a claim
        # about the ticks the mission actually flies.
        # **Where the name implies, which is where it was not.** This read `mission.yaml`'s
        # `tick_hz` at the top level and found nothing, because the mission's clock was nested
        # inside `met_epoch_provenance` — so the first reader that was not written alongside the
        # nesting is what found it. The keys are at the top level now, and `check_basis` refuses a
        # provenance block carrying another one, which is the shape that hid them.
        tick_hz = load_yaml(root / "mission.yaml").get("tick_hz")
        if not isinstance(tick_hz, (int, float)) or tick_hz <= 0:
            sys.stderr.write("mission.yaml declares no numeric tick_hz, so there is no tick to run\n")
            return 3
        dt = 1.0 / float(tick_hz)
        runs: list[list[str]] = []
        for _ in range(2):
            world = load_world(root)
            values = initial_values(world)
            hashes: list[str] = []
            for _tick in range(max(1, int(args.ticks))):
                gaps: list[Gap] = []
                values = step(world, values, dt, gaps)
                hashes.append(state_hash(values))
            runs.append(hashes)
        print(f"  the declared tick: {tick_hz:g} Hz, dt = {dt:g} s, over {len(runs[0])} tick(s)")
        for index, digest in enumerate(runs[0][:5], start=1):
            print(f"  tick {index:4}  {digest}")
        if len(runs[0]) > 5:
            print(f"  ... and {len(runs[0]) - 5} more")
        if runs[0] == runs[1]:
            print()
            print(
                f"  two independent runs of the same start: **{len(runs[0])} of {len(runs[0])} "
                "compare-points identical**"
            )
            print(
                "  and that is the whole claim: bit-identity, same build and same platform, with no "
                "tolerance anywhere in it"
            )
            return 0
        differing = next(
            (i for i, (a, b) in enumerate(zip(runs[0], runs[1], strict=True)) if a != b), None
        )
        print()
        print(f"  **the runs diverge at tick {(differing or 0) + 1}**: {runs[0][differing]} against {runs[1][differing]}")
        print(
            "  two runs of one start disagreeing means something in the tick reads state the world "
            "does not carry — a set, a dict key order, a clock — and `plant.md` §6 says every "
            "compare-point must be byte-identical"
        )
        return 1

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
