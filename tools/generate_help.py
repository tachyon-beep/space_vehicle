#!/usr/bin/env python3
"""Generate the vehicle's `HELP.md` from the command registries.

`docs/diode-contract.md:273-279` is the reason this exists:

    "They are told the shape above and nothing about the vocabulary. `HELP.md`, `state.json` and
     `README.md` are the vehicle's documentation of itself, written in whatever voice the vehicle's
     builder chooses, and **they are the only place a verb name can appear**."

So `HELP.md` is not a convenience — it is the *only* channel through which a fleet learns what it
may ask for, and it is therefore generated rather than written. A hand-written `HELP.md` and a
registry drift, and the drift is invisible from both sides: the registry is correct about a verb
nobody knows exists, and the help text is confident about a verb that was renamed.

What the file contains, and why each part is there
--------------------------------------------------

  - **One entry per verb, grouped by the domain that owns it.** The grouping is the vehicle's own
    decomposition and it is the only navigation aid a fleet gets over fifty-eight verbs.
  - **Every gate variable by name.** `diode-contract.md:136-138` requires it — "every verb's gate
    variable appears in `HELP.md` and `state.json`, so a verb that is closed is a door with a label
    rather than a dead end" — and §9's check 5 tests it. The variable's *value* is in `state.json`;
    its *name* is here.
  - **The interlocks by name.** A gate is something a fleet can move; an interlock is something it
    cannot (D-03). Naming both, and marking which is which, is what stops a fleet spending a command
    cycle trying to lower a service-owned guard.
  - **The authority, the irreversibility, the phases and the argument schema.** The argument schema
    is what a machine reads from the capability snapshot and what a person reads here; §4 says "the
    parameter shape is documentation… it will be read as carefully as the prose, and it should agree
    with the prose."
  - **The verbs this vehicle deliberately does not have, with the reason.** Every domain's
    `not_implemented`/`declined` list is documentation of an absence, and an absence a fleet does not
    know about costs it a command cycle to discover. A flight manual that lists what a vehicle
    cannot do is not giving anything away; it is the difference between a door with a label and a
    wall with no sign.
  - **The refusal vocabulary.** V-02's canonical chain, so a fleet knows what a refusal *means*
    rather than only that it happened.

Deliberately not in the file
----------------------------

The missions's objectives, the failure chains, the thresholds, the coupling graph, and anything
about which faults are seeded. `HELP.md` documents the *interface*; a document that also described
the vehicle's weaknesses would delete the experiment. The boundary is `docs/design.md` §8's: the
vehicle publishes what it has concluded and never what it suspects about itself.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path
from typing import Any

import yaml

AUTHORITY_MEANING = {
    "S0": "service-owned; never granted to an agent",
    "A0": "observe or acknowledge only",
    "A1": "routine, reversible control",
    "A2": "safety- or mission-significant control",
    "A3": "exceptional test or maintenance",
}

REFUSAL_VOCABULARY = [
    ("REJECTED", "the batch or the command did not survive validation: schema, authority or phase"),
    ("PHASE", "the verb is not available in the mission phase the vehicle is in"),
    ("GATE", "an agent-writable gate variable is closed; the variable is named in `state.json`"),
    ("INTERLOCK", "a service-owned condition blocks the verb; no console variable can move it"),
    ("CONFLICT_SUPERSEDED", "a later command replaced this one before it took effect"),
    ("STALE_STATE", "the evidence the command depends on was older than its maximum decision age"),
    ("EXPIRED", "the command's queue age exceeded its `maximum_queue_age_s` before effect"),
    ("INHIBITED", "the vehicle is in a state that inhibits effectful verbs"),
    ("INDETERMINATE", "the vehicle restarted with the effect unproven; the journal is the record"),
]


def load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text()) or {}


def describe_argument(name: str, spec: dict[str, Any]) -> str:
    """One argument, as a person reads it. The schema is documentation, so it has to read."""
    kind = spec.get("type", "?")
    unit = f" [{spec['unit']}]" if spec.get("unit") else ""
    if kind == "enum" and spec.get("values"):
        values = ", ".join(str(v) for v in spec["values"])
        body = f"one of: {values}"
    elif kind == "array":
        items = spec.get("items", "?")
        length = spec.get("length")
        body = f"{length} x {items}" if length else f"list of {items}"
        # An array whose component order is a convention says so here, from the field the linter
        # holds against `vehicle.yaml#conventions`. Before this, the order reached a fleet only as
        # the words "w-first" inside an argument's prose note, and this file is the only place a
        # fleet reads the command surface.
        if spec.get("quaternion_order"):
            body += f", order {spec['quaternion_order']}"
    elif kind == "number":
        low, high = spec.get("minimum"), spec.get("maximum")
        if low is not None and high is not None:
            body = f"number in [{low}, {high}]"
        elif low is not None:
            body = f"number >= {low}"
        elif high is not None:
            body = f"number <= {high}"
        else:
            body = "number"
    elif kind == "integer":
        body = "integer"
    elif kind == "boolean":
        body = "true or false"
    elif kind == "string":
        limit = spec.get("max_length")
        body = f"text{f' up to {limit} characters' if limit else ''}"
    else:
        body = str(kind)
    if spec.get("optional"):
        body += ", optional"
    if spec.get("note"):
        body += f" — {spec['note']}"
    return f"`{name}`: {body}{unit}"


def generate(root: Path) -> str:
    mission = load(root / "mission.yaml")
    # The phase's *window* travels with it. `mission.met_s` is published and the ladder is
    # declared, but the declaration is in the corpus and this file is where a fleet reads the
    # mission: without the window, "which phase is it and how far into it am I" needs a number the
    # fleet cannot see. The starts are the cumulative durations, which the linter re-derives.
    phases = [
        (
            p.get("id"),
            p.get("name"),
            p.get("starts_at_h"),
            p.get("duration_h"),
        )
        for p in mission.get("phases") or []
    ]

    lines: list[str] = []
    add = lines.append
    add("# Vehicle command reference")
    add("")
    add(
        "This file is generated from the vehicle's command registries and is the only place a verb "
        "name appears. Every verb below exists; every verb in the last section deliberately does "
        "not. Nothing here describes the mission, the vehicle's condition, or what may be wrong "
        "with it — that is what `state.json` and the telemetry ring are for."
    )
    add("")
    add("## How to read an entry")
    add("")
    add(
        "**Authority** is who may issue the verb: "
        + "; ".join(f"`{k}` {v}" for k, v in AUTHORITY_MEANING.items())
        + "."
    )
    add("")
    add(
        "**Gate** is an agent-writable preference. It appears as a variable you can set in "
        "`console.json`; closing it is how a fleet stops itself from issuing a verb, and the current "
        "value of every gate variable is published in `state.json`."
    )
    add("")
    add(
        "**Interlocks** are service-owned. They are conditions the vehicle evaluates itself, they "
        "cannot be moved from the console, and a verb that trips one is refused with the interlock's "
        "name rather than with a description."
    )
    add("")
    add(
        "**Phases** are the parts of the mission in which the verb is offered at all. Outside them "
        "it is refused on the phase, which is a different refusal from a gate or an interlock."
    )
    add("")

    verbs_by_domain: dict[str, list[dict[str, Any]]] = {}
    declined: list[tuple[str, dict[str, Any]]] = []
    domain_order: list[str] = []
    domains_dir = root / "domains"
    for path in sorted(p for p in domains_dir.iterdir() if p.is_dir()):
        commands = load(path / "commands.yaml")
        items = commands.get("commands") or []
        if items:
            verbs_by_domain[path.name] = items
            domain_order.append(path.name)
        for refused in commands.get("not_implemented") or commands.get("declined") or []:
            declined.append((path.name, refused))

    add(f"## Verbs ({sum(len(v) for v in verbs_by_domain.values())})")
    add("")
    add("### Contents")
    add("")
    for domain in domain_order:
        names = ", ".join(f"`{v.get('verb')}`" for v in verbs_by_domain[domain])
        add(f"- **{domain}** — {names}")
    add("")
    add("### The mission phases")
    add("")
    add(
        "The phase ids below are the vocabulary a refusal names and the vocabulary the entries "
        "use. The mission runs them in this order, and each carries its window in mission elapsed "
        "time — `mission.met_s` is published, so a fleet can tell how far into a phase it is and "
        "how long is left."
    )
    add("")
    for index, (pid, name, start, duration) in enumerate(phases, start=1):
        if isinstance(start, (int, float)) and isinstance(duration, (int, float)):
            window = f"MET {start:g}-{start + duration:g} h"
        else:
            window = "window undeclared"
        add(f"{index}. `{pid}` — {name} ({window})")
    add("")

    for domain in domain_order:
        add(f"### {domain}")
        add("")
        for verb in verbs_by_domain[domain]:
            name = verb.get("verb")
            authority = verb.get("authority", "?")
            flags = []
            if verb.get("irreversible"):
                flags.append("**irreversible**")
            if verb.get("execution_class") == "deferred":
                flags.append("deferred")
            if verb.get("idempotent"):
                flags.append("safe to repeat")
            if not verb.get("idempotent"):
                flags.append("not idempotent")
            add(f"#### `{name}`")
            add("")
            add(
                f"{authority} — {AUTHORITY_MEANING.get(authority, 'unknown authority')}"
                + (f" · {', '.join(flags)}" if flags else "")
            )
            add("")
            add(str(verb.get("help", "")).strip())
            add("")
            schema = verb.get("argument_schema") or {}
            if schema:
                add("Arguments:")
                add("")
                for arg, spec in schema.items():
                    add(f"- {describe_argument(arg, spec if isinstance(spec, dict) else {})}")
                add("")
            gate = verb.get("gate") or {}
            variable = gate.get("variable") if isinstance(gate, dict) else gate
            if variable:
                add(f"Gate variable: `{variable}`")
                add("")
            interlocks = verb.get("interlocks")
            if isinstance(interlocks, list) and interlocks:
                # A guard that applies to one value of an argument and not another is the
                # difference between a command being refused and being allowed, so it belongs
                # where the fleet reads it rather than only in the linter's model. `arm_engine`
                # is the case: three guards on `state: armed`, and none of them on `safe`.
                condition = verb.get("interlocks_when")
                if isinstance(condition, dict) and condition.get("values"):
                    when = ", ".join(f"`{v}`" for v in condition["values"])
                    add(
                        f"Interlocks: {', '.join(f'`{i}`' for i in interlocks)} — only when "
                        f"`{condition.get('argument')}` is {when}"
                    )
                else:
                    add("Interlocks: " + ", ".join(f"`{i}`" for i in interlocks))
                add("")
            allowed = verb.get("allowed_phases") or []
            if allowed:
                # The *ids*, not the names: `allowed_phases` is the token a refusal names and the
                # token a fleet reasons with, and the prose names are long enough that a
                # comma-joined list of them reads as a dozen phases rather than six. The legend
                # below the contents carries the prose for anyone who wants it.
                add("Phases: " + ", ".join(f"`{p}`" for p in allowed))
                add("")
            age = verb.get("maximum_queue_age_s")
            if age is not None:
                add(f"Queue age: {age} s")
                add("")
            if verb.get("requires_time_sync"):
                add("Requires a synchronised clock before it will execute.")
                add("")

    # A domain declining a verb means "this is not mine" and not "this vehicle has none". The two
    # readings coincide for most entries and come apart for exactly one on this vehicle —
    # `set_telemetry_profile`, which `consumables` and `power` each decline *because* it is a
    # publisher-level verb that `comms` owns. A single list headed "verbs this vehicle does not
    # have" would have told a fleet the verb is absent while a domain implements it, which is the
    # kind of falsehood the generator exists to catch: it is invisible in either file alone.
    implemented = {verb.get("verb") for verbs in verbs_by_domain.values() for verb in verbs}
    absent = [(d, r) for d, r in declined if r.get("verb") not in implemented]
    elsewhere = [(d, r) for d, r in declined if r.get("verb") in implemented]

    add(f"## Verbs this vehicle does not have ({len(absent)})")
    add("")
    add(
        "Each of these was considered and refused, with the reason. They are listed so that no "
        "command cycle is spent discovering an absence, and because an absence with a reason is "
        "documentation of the vehicle rather than a hole in it. Asking for one is refused by name."
    )
    add("")
    for domain, refused in sorted(absent, key=lambda d: str(d[1].get("verb"))):
        add(f"- `{refused.get('verb')}` ({domain}) — {str(refused.get('why', '')).strip()}")
    add("")

    if elsewhere:
        add(f"## Verbs another domain owns ({len(elsewhere)})")
        add("")
        add(
            "These are listed under a separate heading on purpose. A domain declining a verb means "
            "*this is not mine* rather than *this vehicle has none*, and for these the two readings "
            "differ — the verb exists and another domain owns it. Reading them as absent would cost "
            "a fleet the capability entirely."
        )
        add("")
        owners: dict[str, list[str]] = {}
        for verbs in verbs_by_domain.values():
            for verb in verbs:
                owners.setdefault(str(verb.get("verb")), []).append(str(verb.get("_domain", "")))
        for domain, refused in sorted(elsewhere, key=lambda d: str(d[1].get("verb"))):
            name = refused.get("verb")
            where = ", ".join(
                d for d in domain_order if any(v.get("verb") == name for v in verbs_by_domain[d])
            )
            add(
                f"- `{name}` — owned by {where}; declined by {domain}: {str(refused.get('why', '')).strip()}"
            )
        add("")

    add("## What a refusal means")
    add("")
    add(
        "Every command produces exactly one result, whether it succeeded or not, and a refusal names "
        "what refused it. These are the reasons, and they are not interchangeable:"
    )
    add("")
    for code, meaning in REFUSAL_VOCABULARY:
        add(f"- `{code}` — {meaning}")
    add("")
    add(
        "A verb that is refused is refused **at the moment of effect**, not at the moment of "
        "submission. A command accepted now and executed later is checked again when it is due, "
        "against the gates, interlocks, allowances and phase in force then. Nothing is captured at "
        "scheduling time."
    )
    add("")
    add("## What is not in this file")
    add("")
    add(
        "The vehicle's condition, its consumables, its trajectory, and anything it has concluded "
        "about itself. Those are in `state.json` and in the telemetry ring, at the rates described "
        "there. This file changes only when the vehicle's command surface does."
    )
    add("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate HELP.md from the command registries.")
    parser.add_argument("--dir", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--out", default="-", help="output path, or - for stdout")
    args = parser.parse_args(argv)

    root = Path(args.dir)
    if not (root / "domains").is_dir():
        sys.stderr.write(f"not a vehicle directory: {root}\n")
        return 3
    text = generate(root)
    if args.out == "-":
        # `| head` is a normal way to read a long generated document, and a generator that dies
        # on it is a generator someone will stop piping.
        with contextlib.suppress(BrokenPipeError):
            sys.stdout.write(text)
    else:
        Path(args.out).write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
