#!/usr/bin/env python3
"""The vehicle linter: the thing that says whether the vehicle definition composes.

Why this exists
---------------
A vehicle definition assembled from twelve documents that disagree is not a
configuration file, it is a negotiation. `simulator-design.md:141-150` specifies what
the linter must refuse a build for; `thermal_diode.md:1013` specifies it for one domain
and `thermal_diode.md:29` states the rule that generalises: *a value that is needed and
unset fails the build, loudly, naming what wants it.* The point is not to validate a
schema. The point is that you do not enumerate what the simulator needs up front — you
add a domain, run the linter, and it tells you what you now owe it. The config is a
conversation with the linter, not a form.

What it refuses
---------------
  * a value that is needed and unset            (reported as DEBT, fatal under --strict)
  * a name that is not in the canonical vocabulary   (V-01 .. V-08)
  * a channel referenced but not registered
  * a channel registered twice, or under another domain's prefix
  * an edge, cycle or chain that refers to something that does not exist
  * a cycle that does not say which edge is the back-edge, or that carries a delay
    without declaring one
  * a conservation edge whose two ends are in different dimensions
  * a `chosen` value with no reason, or a `derived` value with no inputs
  * a `conserve`-class edge that is not one of the kinds the scheduler knows how to run

Exit codes
----------
  0  composes, or has debts and was not run --strict
  1  refused: something is wrong, not merely missing
  2  --strict and there are unfilled debts
  3  the vehicle directory or a file could not be read

Usage
-----
    python3 tools/check_vehicle.py                 # report
    python3 tools/check_vehicle.py --strict        # fail on debt too (CI, and before a run)
    python3 tools/check_vehicle.py --dir ../vehicle

The reasoning lives in comments rather than in a separate document, matching the
convention of the operator-side services this project already has.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - exercised only where PyYAML is absent
    sys.stderr.write(
        "check_vehicle.py needs PyYAML to read the vehicle definition.\n"
        "It is deliberately not a dependency of the operator-side services.\n"
    )
    raise SystemExit(3) from None

# --------------------------------------------------------------------------------------
# The canonical vocabulary. These are the only values a domain may use, and they are
# duplicated here from reconciliation/02-canonical-vocabulary.md on purpose: the linter
# has to be able to refuse a build without a document parser, and a vocabulary that lives
# in one place is a vocabulary that gets forked.
# --------------------------------------------------------------------------------------
DOMAINS = {
    "power",
    "eclss",
    "thermal",
    "gnc",
    "propulsion",
    "rcs",
    "comms",
    "consumables",
    "avionics",
    "structure",
    "crew",
    "mission",
}
# A domain's name and its channel prefix are different namespaces on purpose: apollo's
# twelve operational domains are the names (apollo_diode.md:20), and the channel prefixes
# are the abbreviations its catalogue actually uses. Declaring the mapping here is what
# stops `propulsion` and `prop` from becoming two domains, which is how the corpus ended up
# with eight names for one uplink.
DOMAIN_PREFIX = {
    "power": "power",
    "eclss": "eclss",
    "thermal": "thermal",
    "gnc": "gnc",
    "propulsion": "prop",
    "rcs": "rcs",
    "comms": "comm",
    "consumables": "res",
    "avionics": "avionics",
    "structure": "structure",
    "crew": "cw",
    "mission": "mission",
}
# The crew domain spans three id prefixes because apollo's own table names them that way
# (apollo_diode.md:224-227): crew-facing controls and crew state are not `cw.*` channels.
SECTION_EXTRA_PREFIXES = {"crew": ("controls.", "crew.")}
# The inverse of DOMAIN_PREFIX, so a reference into another domain can be qualified by either
# name. `thermal.pump_dry_run` in the vocabulary's own example is a directory name; a channel
# prefix is what an agent reading the telemetry would have in front of it.
PREFIX_DOMAIN = {prefix: domain for domain, prefix in DOMAIN_PREFIX.items()}
# `executive` and `window` are not operational domains, but they are real nodes: the
# command executive is the only edge class originating outside the plant, and the window
# is a sink that is never read back.
NON_DOMAIN_NODE_DOMAINS = {"executive", "window"}
NODE_KINDS = {"stock", "flow", "state", "discrete", "service", "sink"}
# The integrator classes whose state is moved by an edge's *value*, and therefore the ones an
# `advances` declaration can name. `discrete` and `service` are excluded on purpose: they are
# advanced by transitions and by authority, so an executive command to `engine_main` advances
# whichever engine the command names rather than one state rather than another. See
# `states_by_node`.
DRIVEN_BY_A_VALUE = {"stock", "lag", "delay", "dynamics", "algebraic"}
EDGE_KINDS = {"conserve", "rate", "algebraic", "lag", "accumulate", "discrete", "delay", "limit"}

# `02-canonical-vocabulary.md` §9b's union, and §10 has promised it since it was written: "**a code
# not in the union** — a quality, kind, severity, lifecycle state or priority that is not one of
# the above". There was no union for a fault's `kind`, so the field was free text and drifted into
# eleven values across the 118 entries. `latent` merged into `latent_then_acute` because PWR-05 and
# PWR-06 are the same welded contactor in two directions and PWR-06's own mechanism text says
# "unavailable when it is needed"; `continuous` renamed to `sustained` because the six entries
# behind it do not drift — a hot amplifier is a persistent condition, not a trend — and because
# `continuous` is already `plant.md` §3's name for an integrator class.
FAULT_KINDS = {
    "discrete",
    "instrument",
    "continuous_degradation",
    "latent_then_acute",
    "sustained",
    "emergent",
    "transient",
    "accounting",
    "procedural",
    "environmental",
}
# Who acts, and it is the field that decides whether a fault is a decision or a protection. The
# split is `electrical_diode.md:845`'s: "a local LCL does not ask whether a dead short should be
# disconnected". A `service` response acts without asking and therefore owes a note saying what it
# did; an `advisory` one asks, and the note is where the asking is explained when it needs to be.
FAULT_RESPONSES = {"service", "advisory"}

# Keys that carry prose wherever they appear. Needed where a *string* has to be told from a
# malformed mapping: `vehicle.yaml#electrical.batteries` holds three groups and one `note`, and the
# note is documentation rather than a group somebody flattened.
# The sections of `vehicle.yaml` the linter or a tool reads by name, each with its reader. The
# list exists because the file's thermal block spent its life with its header empty and its three
# subsections as top-level keys — see `check_vehicle_sections`, which is the check this list
# enables: a key that is not here and not in `PROSE_SECTIONS` is refused.
VEHICLE_SECTIONS = {
    "configurations",  # the mass closure, and the configuration names mission.yaml refers to
    "propulsion",  # `check_propulsion` and the delta-v budget
    "consumables",  # the loads the mission starts with, and the leak and metabolic rates
    "thermal",  # `check_thermal_bindings`, against domains/thermal/components.yaml
    "comms",  # `check_blackout` and the link budget
    "electrical",  # `check_electrical_bindings`, against domains/power/components.yaml
    "open_debts",  # counted and printed, like every other open_debts in the folder
}
# Declarations addressed to a reader rather than to a tool. Naming them is the point: an unread
# section is either a decision or an oversight, and this set is where the decision is recorded.
PROSE_SECTIONS = {
    "schema_version",
    "source",
    "vocabulary",
    "conventions",
    "frames",
    "environment",
    "landing_site",
}

DOC_KEYS = {"note", "notes", "provenance", "why", "source", "reason", "ref", "relation"}

# `02-canonical-vocabulary.md` §6's ladder, which V-04 fixes: four annunciated levels and one
# non-annunciated record class. `INFO` is in the union because a record exists; it is not a level a
# panel lights at, and the distinction is the reason the ladder has five members and four rungs.
SEVERITIES = {"EMERGENCY", "WARNING", "CAUTION", "ADVISORY", "INFO"}
# A fault has to say how it can occur at all, and there are exactly three forms in the corpus: a
# hazard rate over mission time, a probability conditional on a trigger, and a coupling to another
# condition. The block also carries the fault's *magnitude* — 26 distinct keys across the 118 — and
# that half is deliberately not a union: a bias walk and a leak rate are different quantities and
# forcing them into one schema would be a schema about nothing.
SEEDING_FORMS = ("hazard", "on_demand_p", "coupled_to")

# A domain lands as five files (simulator-design.md:128-134). `components.yaml` also carries
# the declaration the scheduler reads, so the factoring stays at five rather than growing a
# sixth file that only the tooling looks at.
DOMAIN_FILES = (
    "components.yaml",
    "points.yaml",
    "profiles.yaml",
    "commands.yaml",
    "fault_policy.yaml",
)
# The integrator classes of plant.md §3, chosen by model form rather than by rate. The set
# grew from six to seven when the first delay element landed: a transport delay is not a
# lag, and the linter refused the state by name rather than letting it default.
METHODS = {"algebraic", "lag", "stock", "delay", "dynamics", "discrete", "hazard"}
# S0 is the service-owned safety kernel; A0..A3 are the agent ladder (vocabulary V-08).
AUTHORITIES = {"S0", "A0", "A1", "A2", "A3"}
# The eleven domains the corpus has specifications for, so the linter can name what is still
# owed rather than silently passing a vehicle with one domain in it.
EXPECTED_DOMAINS = {
    "power",
    "eclss",
    "thermal",
    "gnc",
    "propulsion",
    "rcs",
    "comms",
    "consumables",
    "avionics",
    "structure",
    "crew",
}
# Four classes, and a fifth that the design did not anticipate. `historical` exists because
# the corpus contains no mass, no thrust and no Isp (corpus-review.md §3), so the figures
# that make this a vehicle at all come from published sources about the real one. Marking
# them `chosen` would hide that they are checkable; marking them `apollo` would be a lie.
BASES = {"apollo", "historical", "derived", "chosen", "UNCONFIGURED"}
LAYERS = {"measurement", "estimate", "service"}
PRIORITIES = {"P0", "P1", "P2", "P3", "P4", "P5"}
QUALITY_CODES = {
    "GOOD",
    "SUSPECT",
    "STALE",
    "SATURATED",
    "OUT_OF_RANGE",
    "INVALID",
    "UNKNOWN",
    "SUBSTITUTED",
}
KINDS = {"MEASUREMENT", "ESTIMATE", "COMMAND_ECHO", "SERVICE_STATE", "SIMULATED"}

# Names the corpus uses that this vehicle does not. A domain ported from a spec will
# arrive speaking one of these; the linter's job is to say so by name rather than let a
# second dialect into the dictionary. `SUPERSEDED` is absent on purpose: the canonical
# refusal is CONFLICT_SUPERSEDED, which says who lost and why.
REJECTED = {
    "REVALIDATED": "REVALIDATING (transient) — electrical_diode.md:424 makes the same word terminal",
    "COMPLETED": "SUCCEEDED — gnc_diode.md:1526",
    "AUTHORIZED": "VALIDATED — electrical_diode.md:424",
    "AUTHENTICATED": "VALIDATED — rcs_diode.md:406",
    "ESTIMATED": "not a quality; use kind: ESTIMATE — eclss_diode.md:289 against :64",
    "DEGRADED": "not a quality; it is a subsystem mode",
    "FAULT": "not a quality; it is a conclusion (design.md:211-214)",
    "TEST": "kind: SIMULATED, or injected: true — crew_diode.md / rcs_diode.md:207",
    "TEST_INJECTED": "kind: SIMULATED, or injected: true — rcs_diode.md:207",
    "SIMULATED": "not a quality; use kind: SIMULATED — communcations_diode.md:329",
    "CRITICAL": "EMERGENCY or WARNING — events_diode.md:198, avionics_diode.md:516",
    "ATT_HOLD": "a target, not a mode — rcs_diode.md:365",
    "TRACK": "a target, not a mode — rcs_diode.md:365",
}

# Verbs this vehicle must not have, as patterns rather than as a list of names. `rcs_dode.md:369-380`
# enumerates the forms that would put an external agent inside the stability or
# propulsion-safety loop, and the enumeration is not the point — what matters is that no domain can
# *acquire* one later by writing a plausible-looking verb. The same reasoning as FORBIDDEN_CHANNEL
# (D-04): declining a capability while permitting its name is a decline in name only. Each pattern
# carries its reason, because a future author who trips this is about to rediscover why the
# boundary is where it is.
FORBIDDEN_VERB = {
    r"^(fire|pulse|open|close)_(thruster|valve|jet|engine)": (
        "valve- and thruster-level actuation: `rcs_dode.md:369` and the architectural claim at "
        "`:9`. An agent that names a thruster and a duration is inside the control loop"
    ),
    r"^set_minimum_(pulse|firing|impulse)": (
        "the qualified minimum firing time is a hardware constant (`rcs_dode.md:375`); a verb that "
        "set it would let a fleet choose how finely it may command illegal pulses"
    ),
    r"^(set|write|force)_(sensor|quality|body_rate|prop_mass|nav_state|attitude_state)": (
        "measurement integrity: an agent-writable estimate or quality code is a channel through "
        "which the thing the code exists to withhold can be asserted (`rcs_dode.md:371-373`, "
        "`simulator-design.md:496-508`)"
    ),
    r"^(disable|bypass|override)_(fdir|watchdog|limit|interlock|monitor|plume|keep_out)": (
        "every FDIR function and hard limit on this vehicle is S0 and none is agent-writable "
        "(`rcs_dode.md:370`, `:376`, `:380`). A verb that could switch a protection off makes the "
        "protection's silence ambiguous"
    ),
    r"^(write|set)_(controller_)?gain": (
        "gains are profile data selected by signed ID, never numbers an agent supplies "
        "(`rcs_dode.md:379`, D-05)"
    ),
    r"^clear_fault": (
        "a latched conclusion is cleared through a recovery-checked verb and never by assertion "
        "(`rcs_dode.md:374`, `:379`)"
    ),
    r"^(request_)?momentum_(dump|unload)": (
        "no reaction wheels, no CMGs and no magnetorquers on this vehicle (C-15, "
        "`apollo_diode.md:52`, `:147-153`) — the verb would control nothing"
    ),
}

# A partial dimensional map. It covers the units this vehicle actually uses. The check it
# supports is deliberately conservative: a conserve edge whose ends are in two *known* and
# *different* dimensions is refused, and anything compound or unknown is skipped with a
# note, because a linter that cries wolf is a linter that gets bypassed.
DIMENSION = {
    "kg": "mass",
    "g": "mass",
    "kg_CO2": "mass",
    "kg/s": "mass_flow",
    "g/s": "mass_flow",
    "L/min": "volume_flow",
    "ft3/min": "volume_flow",
    "V": "voltage",
    "A": "current",
    "W": "power",
    "J": "energy",
    "Wh": "energy",
    "N": "force",
    "N*s": "impulse",
    "K": "temperature",
    "degC": "temperature",
    "Pa": "pressure",
    "psi": "pressure",
    "psia": "pressure",
    "mmHg": "pressure",
    "m": "length",
    "km": "length",
    "m/s": "speed",
    "deg": "angle",
    "deg/s": "angular_rate",
    "ms": "time",
    "s": "time",
    "bit/s": "data_rate",
    "count/s": "rate",
    "count": "count",
    "dB": "ratio",
    "%": "ratio",
    "dimensionless": "ratio",
    "enum": "discrete",
    "bool": "discrete",
    "code": "discrete",
}

# Names the vehicle must never publish. D-04 declines the claims lifecycle because a published
# reservation is a deconfliction primitive, and `design.md` §8 refuses to supply one: ten agents
# flying one vehicle have to invent deconfliction, and a vehicle that hands them a reservation
# table has answered the mission's first question for them. The corpus's own schema makes
# `reserved`, `allocated`, `committed` and `available_to_new` *required* fields, so declining
# the feature without forbidding the fields would be a decline in name only. This is the check
# that keeps it a decline in fact.
FORBIDDEN_CHANNEL = re.compile(
    r"\.(reserved|allocated|committed|available_to_new|claim|claims|reservation)(\b|_)"
)

TEMPLATE = re.compile(r"\[[^\]]*\]")


class ChannelIndex:
    """Resolves a channel name against the registry, templates included.

    `power.lcl_[n]_state` in the registry has to answer for `power.lcl_1_state` in a domain,
    and `res.recon_[resource]_kg` for `res.recon_o2_kg`. Comparing normalised strings does not
    do that — it maps the template to `res.recon_[]_kg` and leaves the concrete name alone — so
    the registry is compiled to patterns instead: every `[...]` becomes a wildcard that must
    match at least one character. A registry entry with no brackets compiles to itself.
    """

    def __init__(self, registry: Iterable[str] | dict[str, dict[str, Any]]) -> None:
        self.rows = registry if isinstance(registry, dict) else {name: {} for name in registry}
        self.patterns = [self._compile(name) for name in self.rows]
        # **A known weakness, recorded rather than depended on.** `_compile` turns `[...]` into
        # `.+?`, which is unbounded, so a template matches names that merely *end* with its literal
        # tail: `thermal.zone_1_true_t_c` resolves against the registry's `thermal.zone_[id]_t_c`,
        # with the wildcard absorbing `1_true`. The same over-permissiveness means
        # `power.lcl_1_old_state` resolves to `power.lcl_[n]_state`'s row, so "is this a registered
        # channel" is a weaker question than it looks. It was found by a `not_published` entry that
        # was correctly withheld and wrongly reported as registered. Bounding the wildcard is not
        # the fix and cannot be: `[resource]` has to match `water_cooling`, so a placeholder may
        # contain underscores, and then `[id]` matching `1_true` is not distinguishable from
        # `[id]` matching `csm_cabin`. The real fix is to declare each template's instantiations in
        # the registry rather than inferring them, which is a decision about the registry's shape
        # and is recorded where that decision belongs — `channels.yaml`'s own debt list.

    @staticmethod
    def _compile(channel: str) -> re.Pattern[str]:
        parts = TEMPLATE.split(channel)
        return re.compile(".+?".join(re.escape(part) for part in parts))

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and any(p.fullmatch(name) for p in self.patterns)

    def row(self, name: str) -> dict[str, Any] | None:
        """The registry entry a concrete channel name resolves to, template included.

        `power.lcl_1_current_a` has to answer with `power.lcl_[n]_current_a`'s row, because a
        domain is allowed to name the concrete point while the registry holds the template.
        """
        for row, pattern in zip(self.rows.values(), self.patterns, strict=False):
            if pattern.fullmatch(name):
                return row
        return None

    def normalised(self) -> set[str]:
        """The template forms, for messages that want to say what the registry does hold."""
        return {TEMPLATE.sub("[]", p.pattern.replace("\\", "")) for p in self.patterns}


class Report:
    """Collects refusals, debts and notes so the whole build is reported in one pass."""

    def __init__(self) -> None:
        self.refusals: list[str] = []
        self.debts: list[str] = []
        self.notes: list[str] = []

    def refuse(self, where: str, why: str) -> None:
        self.refusals.append(f"{where}: {why}")

    def debt(self, where: str, why: str) -> None:
        self.debts.append(f"{where}: {why}")

    def note(self, where: str, why: str) -> None:
        self.notes.append(f"{where}: {why}")

    def print(self) -> None:
        for title, rows in (
            ("REFUSED", self.refusals),
            ("OWED (a value that is needed and unset)", self.debts),
            ("notes", self.notes),
        ):
            if not rows:
                continue
            print(f"\n{title} — {len(rows)}")
            for row in rows:
                print(f"  - {row}")


def duplicate_keys(text: str) -> list[tuple[int, str]]:
    """Every mapping key written twice in one mapping, with the line of the second.

    PyYAML accepts a duplicate key and lets the last one win, which makes this the quietest
    structural fault in the format. It is also the *signature* of the fault this repository has
    now made three times: a block scalar's content is indented to the same depth as a list item
    that follows it, so the item is absorbed into the prose, its keys become keys of the entry
    above it, and where those collide the earlier value is silently replaced. The YAML still
    parses, the linter still composes, and what was lost is a cycle's back-edge, a point entry, or
    a threshold's hysteresis.

    Nothing is ever written twice on purpose in these files, so a duplicate is always a fault and
    the check needs no exceptions. It found 49 of them across five files.
    """
    try:
        document = yaml.compose(text)
    except yaml.YAMLError:
        return []  # the parse refusal is `load`'s, and it names the line better
    found: list[tuple[int, str]] = []

    def walk(node: Any, trail: str = "") -> None:
        if isinstance(node, yaml.MappingNode):
            seen: set[str] = set()
            for key, value in node.value:
                name = str(key.value)
                if name in seen:
                    found.append((key.start_mark.line + 1, f"{trail}.{name}".lstrip(".")))
                seen.add(name)
                walk(value, f"{trail}.{name}")
        elif isinstance(node, yaml.SequenceNode):
            for index, value in enumerate(node.value):
                walk(value, f"{trail}[{index}]")

    walk(document)
    return found


def rederive(where: str, declared: Any, computation: Any, report: Report) -> None:
    """A value that states its own arithmetic is re-derived, on every run.

    The idiom is opt-in and it exists because of one edge found by hand: `E-RAD-WATER` declared
    3.8e-7 while the relation beside it computed `1/2.45e6 = 4.082e-7`, and the two had disagreed by
    7 % since the edge was written. A relation is prose and prose cannot be evaluated — but a
    `computation` can, so a value that states one in a form the linter can evaluate is a value that
    cannot drift from its own derivation.

    It is written as a function rather than inline in the coupling check because three more
    declarations arrived with the same shape and one of them was the same defect: `mission.yaml`'s
    `total_ticks_provenance` gave "192.0 h x 3600 s/h x 50 Hz = 34,560,000 ticks" in a *sentence*,
    so the vehicle's tick count — the figure the whole review has been pricing — existed only as
    prose. The expression is arithmetic over literals and nothing else: no names, no calls, no
    attribute access, so the configuration cannot become executable.
    """
    if computation is None:
        return
    if not isinstance(declared, (int, float)):
        report.refuse(where, "declares a `computation` and no numeric value to check it against")
        return
    try:
        if not re.fullmatch(r"[0-9eE+\-*/(). \t]+", str(computation)):
            raise ValueError("not a numeric expression")
        computed = eval(str(computation), {"__builtins__": {}}, {})  # noqa: S307
    except Exception as exc:  # noqa: BLE001 - any failure is a refusal
        report.refuse(where, f"has a `computation` the linter cannot evaluate: {exc}")
        return
    if abs(computed - float(declared)) > abs(float(declared)) * 0.01 + 1e-12:
        report.refuse(
            where,
            f"declares {declared!r} and its computation {computation!r} gives {computed:.6g}: a "
            "derived value that does not re-derive is a value nobody has checked",
        )


def check_met_clock(doc: dict[str, Any], report: Report) -> None:
    """The mission's clock is derived from the phase ladder three times, and none was checked.

    `mission.yaml` states the same total in three places and each says the linter holds it:
    `phase_total_check`'s note reads "the linter re-derives this and refuses a mismatch",
    `total_duration_provenance` reads "sum of the phase durations below; the linter refuses a build
    where they disagree", and `total_ticks_provenance` gives the tick count as a sentence. The
    linter does re-derive the ladder — the trajectory checks trip the moment a phase duration moves
    — but **none of the three declarations was read**: `sums_to_h` set to 999.0 passes silently.

    That is worse than an unchecked number, because the note tells the next reader not to check it
    by hand. So all three are held to the ladder now, and each states its arithmetic in a form the
    linter evaluates rather than in a sentence.
    """
    phases = doc.get("phases") or []
    if not phases:
        return
    durations = [p.get("duration_h") for p in phases]
    unset = [
        p.get("id")
        for p, d in zip(phases, durations, strict=True)
        if not isinstance(d, (int, float))
    ]
    if unset:
        report.debt("mission.yaml:phases", f"has phases with no duration: {unset}")
        return
    total = float(sum(durations))

    block = doc.get("phase_total_check")
    if not isinstance(block, dict):
        report.refuse(
            "mission.yaml",
            "has no `phase_total_check`, so nothing states what the phase ladder is supposed to "
            "sum to",
        )
    else:
        where = "mission.yaml:phase_total_check"
        if block.get("sums_to_h") != total:
            report.refuse(
                where,
                f"declares {block.get('sums_to_h')!r} h and the phases sum to {total:g} h",
            )
        rederive(where, block.get("sums_to_h"), block.get("computation"), report)

    provenance = doc.get("met_epoch_provenance") or {}
    declared = provenance.get("total_duration_h")
    if declared != total:
        report.refuse(
            "mission.yaml:met_epoch_provenance",
            f"declares total_duration_h {declared!r} and the phases sum to {total:g} h. MET is an "
            "integer tick count from the epoch, so a duration that disagrees with the ladder is a "
            "mission whose clock runs out somewhere other than where the phases end",
        )
    ticks = provenance.get("total_ticks")
    tick_hz = provenance.get("tick_hz")
    if not isinstance(ticks, (int, float)) or not isinstance(tick_hz, (int, float)):
        report.refuse(
            "mission.yaml:met_epoch_provenance",
            "does not state `total_ticks` and `tick_hz` as numbers, so the vehicle's tick count — "
            "the figure the whole review prices — exists only as a sentence in a relation",
        )
    else:
        expected = total * 3600.0 * float(tick_hz)
        if abs(float(ticks) - expected) > 1.0:
            report.refuse(
                "mission.yaml:met_epoch_provenance",
                f"declares {ticks!r} ticks and {total:g} h at {tick_hz} Hz is {expected:,.0f}",
            )
        rederive(
            "mission.yaml:met_epoch_provenance.total_ticks_provenance",
            ticks,
            (provenance.get("total_ticks_provenance") or {}).get("computation"),
            report,
        )


def absorbed_keys(text: str) -> list[tuple[int, str, int]]:
    """Keys that were written where a block scalar's prose lives, and are therefore text.

    `duplicate_keys` above describes this accident and catches the half of it that *collides*: a
    block scalar's content indented to the same depth as the list item or mapping that follows, so
    the item is absorbed and its keys overwrite the entry above. That check needs a clash to fire.

    **The other half is silent**, and it is the half that survived: when the absorbed keys are new,
    nothing collides, nothing is replaced, and the declaration simply does not exist. Two were
    found this way.

    `coupling.yaml`'s `C-WATER-BUDGET` had

        note: >-
          ...a temperature rise rather than a valve closing.
          stability:
            hysteresis_required: false

    — so the cycle's `stability` was prose, and the linter's own relay-oscillation rule read it as
    absent. It passed only because no member of that cycle happens to be latched, which is a
    coincidence rather than a declaration: the author wrote the field, and the file lost it.

    `domains/consumables/components.yaml`'s `reconciliation` lost **two** the same way — its
    `on_mismatch` rule ("publish a RECONCILIATION_MISMATCH event and leave both numbers alone") and
    its whole `provenance` block, so the vehicle's rule about never rewriting the ledger was a
    sentence in a note that no reader of the parsed document could reach.

    The shape is unambiguous and needs no tolerance: a bare `key:` line at exactly the indentation
    a block scalar's content sits at, followed by a line indented deeper. Prose does not do that;
    a `key: value` in a sentence stays on one line. Three instances existed in the corpus when this
    check was written and all three were accidents, so it carries no exemption list — an intentional
    YAML example inside a note has to be indented out of the scalar's own content column, and that
    is the right price.
    """
    found: list[tuple[int, str, int]] = []
    lines = text.split("\n")
    for index, line in enumerate(lines[:-1]):
        match = re.match(r"^(\s+)([a-z_][a-z0-9_]*):\s*$", line)
        if not match:
            continue
        indent, key = len(match.group(1)), match.group(2)
        following = lines[index + 1]
        if not following.strip():
            continue
        if len(following) - len(following.lstrip()) <= indent:
            continue
        # Walk back for the block scalar this line sits inside, stopping at anything shallower.
        for above in range(index - 1, max(-1, index - 80), -1):
            previous = lines[above]
            if not previous.strip():
                continue
            opener = re.match(r"^(\s*)[a-z_][a-z0-9_]*:\s*[>|][-+]?\s*$", previous)
            if opener:
                if len(opener.group(1)) < indent:
                    found.append((index + 1, key, above + 1))
                break
            shallower = re.match(r"^(\s*)\S", previous)
            if shallower and len(shallower.group(1)) < indent:
                break
    return found


def load(path: Path, report: Report) -> dict[str, Any] | None:
    if not path.exists():
        report.refuse(path.name, "not present")
        return None
    text = path.read_text()
    for line, trail in duplicate_keys(text):
        report.refuse(
            f"{path.name}:{line}",
            f"writes {trail!r} a second time in the same mapping. The last one wins and the first "
            "is silently replaced, which is how an absorbed list item destroys the entry above it",
        )
    for line, key, opener in absorbed_keys(text):
        report.refuse(
            f"{path.name}:{line}",
            f"writes {key!r} at the indentation of the block scalar opened on line {opener}, so it "
            "is prose rather than a key and the declaration does not exist in the parsed document. "
            "Dedent it to the level of its siblings",
        )
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:  # a config that does not parse is not a config
        report.refuse(path.name, f"does not parse: {exc}")
        return None
    if not isinstance(data, dict):
        report.refuse(path.name, "is not a mapping")
        return None
    # Semantic rather than structural, and here rather than in the per-file checks because every
    # document passes through this one call site. See `check_answered_debts`.
    check_answered_debts(path.name, data, report)
    return data


# The edge kinds a stock can have *flowing into it*. A stock is a conserved quantity, so the only
# thing an inbound edge can be is a flux — or a transfer of the same quantity, which is what
# `conserve` is. `lag`, `algebraic` and `delay` relate a stock's quantity to a *different* quantity,
# and `discrete` and `service` carry authority rather than matter.
STOCK_INFLOW_KINDS = {"rate", "conserve", "accumulate"}


# `limit` is the vocabulary's word for a **clamp**: an edge that lands on a stock node because the
# thing it constrains hangs off that node, rather than because matter crosses it. `E-RAD-WATER` at
# `J per kg` is what a kilogram of water *buys* — the rejection it permits — and it is the last of
# its class, because the other three relations of that shape are already declared by their
# `advances` naming an algebraic state rather than a stock. This one drives `water_cooling_kg`
# itself, so there was nothing for `advances` to point at and the rule had no way to say *a relation
# into this node's channel rather than into the stock*. Four attempts to express it as a flux failed
# because it is not one; the vocabulary was what was missing.
#
# A `limit` edge must say what it clamps and why it is not a flux, because an exemption without a
# reason is how a mislabelled conversion gets through — which is exactly what `E-ATM-ABSORB` and
# `E-PRESS-PROP` did while `conserve` was doing this job badly.
CLAMP_KIND = "limit"


def stock_flux_basis(edge: dict[str, Any], nodes: dict[str, Any]) -> tuple[str | None, str]:
    """How an edge's flux into a stock is established, or why it cannot be.

    Returns `("per_second" | "per_hour", "")` when the plant can integrate the edge, and
    `(None, reason)` when it cannot. **The linter and the plant call this same function**, in the
    pattern `derive_schedule` already set, so a rule about what a stock edge means cannot come
    apart from the rule that computes it.

    It exists because the plant's stock integrator had never run — the schedule stops at the first
    `algebraic` state, long before it reaches a stock — and when it was finally exercised it summed
    `sensitivity * dt` over every incoming edge and never read a driver. Handed `lm_cabin_o2_kg` it
    returned the same mass whether one crew member was aboard or three, having added
    `116.86 Pa per K` (a lag relation), `1.0 kg O2 per kg O2` (a ratio whose flux is the tank's
    outflow) and `0.03792 kg/h per crew`. Adding pascals-per-kelvin to kilograms-per-hour and
    calling the result a mass is a dimension error wearing a number.

    The three bases are the three things an inbound edge can legitimately be, and the refusals name
    what a correct model would have to declare rather than defaulting to something plausible:

      * **a rate with a time denominator** — `kg/s per W`, `kg/h per crew`. The driver multiplies
        it; the time basis converts it. This is the case a stock integrator is for.
      * **a same-dimension ratio** — `kg O2 per kg O2`. The driver is a *level* and the sensitivity
        is a transfer fraction, so the flux is the source's own outflow times that ratio, and the
        outflow belongs to whichever state produces it. A level cannot be converted into a rate.
      * **a relation between two different quantities** — `Pa per K`, `J per K`. These land on a
        stock node because the *channel* hangs off the node, not because anything flows into it.
    """
    sensitivity = edge.get("sensitivity") or {}
    unit = str(sensitivity.get("unit") or "")
    tokens = unit.replace("^", "").split()
    where = f"{edge.get('id')}"

    if sensitivity.get("value") in (None, "UNCONFIGURED"):
        return None, f"{where} carries no sensitivity value, so nothing establishes its flux"

    # The sensitivity's numerator has to be the stock's own unit, in whichever direction the edge
    # runs. Without this the time-basis test alone passes `E-PROP-ENG` at `3084.2 N per kg/s`, whose
    # `/s` reads as a rate while the numerator is a *force*: the number is thrust per unit of flow,
    # stated backwards, so a plant multiplying it by a thrust would get N^2 per (kg/s) and call the
    # result kilograms of propellant. The vehicle's stock units are `kg`, `J` and `man_hours`, and
    # a flux into or out of one of them is denominated in that unit or it is not a flux.
    # Which end is the stock decides whose unit the numerator has to be. An edge *into* a stock is
    # denominated in the target's unit; an edge *out of* one, in the source's. Getting this from
    # "whichever endpoint is a stock" rather than from the direction is how `E-PROP-ENG` slipped
    # through: its target `thrust_main` is a flow in newtons, so a numerator of newtons looked like
    # agreement.
    source_node = (nodes or {}).get(str(edge.get("from"))) or {}
    target_node = (nodes or {}).get(str(edge.get("to"))) or {}
    stock_unit = ""
    stock_endpoint = str(edge.get("from"))
    if target_node.get("kind") == "stock":
        stock_unit = str(target_node.get("unit") or "")
        stock_endpoint = str(edge.get("to"))
    elif source_node.get("kind") == "stock":
        stock_unit = str(source_node.get("unit") or "")

    # A node may be denominated in more than one quantity — `cabin_atm` is `kg + Pa`, because it
    # carries four gas masses *and* the pressure that reads them — so the numerator has to match one
    # of the node's units rather than the whole string. Comparing against the string is how moving
    # the pressure onto the cabin node silently broke the crew's own metabolic edge.
    # Normalised, because the same unit is spelled two ways across the two files: the coupling node
    # says `man_hours` and the sensitivity that spends it says `man-hours`. A comparator that reads
    # those as different units reports a dimensional error where there is only a hyphen.
    def _norm(text: str) -> str:
        return text.replace("-", "").replace("_", "").replace(" ", "").lower()

    node_units = {_norm(part) for part in re.split(r"[+,]", stock_unit) if part.strip()}
    # The `/h` or `/s` is the time denominator, not part of the unit symbol: `kg/h per crew` is a
    # mass rate, and reducing it to `kg` is what lets the same comparator judge it beside `kg/s per W`.
    numerator = unit.split(" per ")[0].split()[0].split("/")[0] if unit else ""
    if node_units and numerator and _norm(numerator) not in node_units:
        return None, (
            f"{where} declares {unit!r}, whose unit is {numerator!r}, against a stock denominated "
            f"in {stock_unit!r}. The number is a conversion between two quantities rather than a "
            "flux of the stock, and the flow it converts is not declared"
        )

    if edge.get("kind") not in STOCK_INFLOW_KINDS:
        return None, (
            f"{where} is a {edge.get('kind')!r} edge on a stock. A stock is a conserved quantity, so "
            "the only thing an edge on it can be is a flux: this one relates the stock's quantity to "
            "a different physical quantity, and it lands on the stock node because the *channel* "
            "hangs off the node rather than because anything flows"
        )

    per_hour = any(token.endswith("/h") for token in tokens)
    per_second = any(token.endswith("/s") for token in tokens) or "s" in tokens[1:]
    if per_hour and per_second:
        return None, (
            f"{where} declares the unit {unit!r}, which names a per-second and a per-hour basis at "
            "once, so nothing can tell which one the sensitivity is in"
        )

    if not per_hour and not per_second:
        # No time basis in the unit, so the *partner* has to supply one. Two cases reach here and
        # both are the same test: a same-dimension ratio (`kg O2 per kg O2`) and a conversion
        # (`man-hours per kg CO2`). Either is a flux when the other endpoint is a flow denominated
        # in the ratio's denominator — `man-hours per kg CO2` applied to `kg CO2/s` is man-hours per
        # second, which is exactly what the absorber counter integrates.
        denominator = " per ".join(unit.split(" per ")[1:]).strip()
        partner = str(edge.get("to") if edge.get("to") != stock_endpoint else edge.get("from"))
        partner_node = (nodes or {}).get(partner) or {}
        partner_unit = str(partner_node.get("unit") or "")
        partner_basis = partner_unit.split("/")[0]
        if partner_node.get("kind") == "flow" and _norm(partner_basis) == _norm(denominator):
            return "per_second", ""
        sides = unit.split(" per ")
        shared = (
            len(sides) >= 2
            and sides[0].split()
            and sides[1].split()
            and sides[0].split()[0] == sides[1].split()[0]
        )
        if shared:
            # A ratio *is* computable when the other endpoint is a flow denominated in the ratio's
            # own denominator: `1.0 kg water per kg reactants` applied to a node carrying
            # `kg reactants per second` gives water per second, which is a flux. So the test is not
            # "is this a ratio" but "does the graph carry the flow the ratio is against".
            #
            # Nothing in the vehicle does, and that is the finding rather than the rule's failure:
            # the denominators name `kg O2`, `kg CO2`, `kg reactants` and `kg prop`, and the six
            # flow nodes are denominated in `W`, `N`, `dB`, `kg/s` and nothing else. The refusal
            # therefore names the node that would fix the edge, which turns thirteen vague debts
            # into one build order.
            return None, (
                f"{where} carries the dimensionless ratio {unit!r} against a stock, so its flux is "
                f"the flow of {denominator} multiplied by that ratio — and no node in the graph "
                f"carries that flow. `{partner}` is {partner_unit or 'not a node'}, so the ratio has "
                "nothing to apply to. A level cannot be converted into a rate: the fix is a "
                f"`kind: flow` node denominated in {denominator} per second, which is what the "
                "stock's consumer produces"
            )
        return None, (
            f"{where} declares {unit!r}, which does not establish a flux: it relates the stock's "
            "quantity to a different quantity, and the flow between them is not declared"
        )

    return ("per_hour" if per_hour else "per_second"), ""


def check_basis(where: str, basis: str | None, extra: dict[str, Any], report: Report) -> None:
    """Every value that matters records where it came from, and a claim needs support.

    This is `simulator-design.md:151-156`'s provenance rule made mechanical: a `chosen`
    value with no reason is indistinguishable from a guess, a `derived` value with no
    inputs cannot be re-derived by the linter (so the linter cannot disagree with it), and
    an `apollo` value with no reference cannot be checked against the corpus.
    """
    if basis not in BASES:
        report.refuse(where, f"provenance basis {basis!r} is not one of {sorted(BASES)}")
        return
    if basis == "chosen" and not extra.get("reason"):
        report.refuse(where, "basis is `chosen` but no reason is given")
    if basis == "derived" and not (extra.get("inputs") or extra.get("relation")):
        report.refuse(where, "basis is `derived` but neither inputs nor a relation is given")
    if basis == "apollo" and not extra.get("ref"):
        report.refuse(where, "basis is `apollo` but no reference into the corpus is given")
    if basis == "historical" and not extra.get("source"):
        report.refuse(where, "basis is `historical` but no external source is given")


PLANT_DOMAINS = {
    "power",
    "eclss",
    "thermal",
    "gnc",
    "propulsion",
    "rcs",
    "comms",
    "consumables",
    "avionics",
    "structure",
    "crew",
}


def derive_schedule(doc: dict[str, Any], report: Report) -> list[str]:
    """Derive the total order `plant.md` promises, at the level the physics is written at.

    `plant.md:71-73` promises a deliverable: "Topological order is only partial, so **the linter
    emits a total order** and the scheduler obeys it: topological sort with a frozen lexicographic
    tiebreak on domain name." Nothing emitted one, and writing it found that the promise as
    worded **cannot be kept**.

    The reason is that a domain order is a coarsening of the node graph, and coarsening *creates*
    cycles that do not exist. `comms -> power -> consumables -> propulsion -> gnc -> comms` is a
    cycle in the domain projection and there is no node-level path from any of those nodes back to
    itself: it exists only because `E-AMP-LOAD` leaves `link` and `E-FC-DRAW-O2` leaves
    `fuel_cell`, and the projection cannot tell that those are different nodes. So a domain-level
    topological sort is over-constrained — it can report a loop the physics does not have and
    refuse a schedule that exists.

    The order is therefore derived over **nodes**, with the frozen lexicographic tiebreak on node
    id, and a domain with states on several nodes appears at several points in it. That is what
    Gauss-Seidel actually does: the domain is an authoring unit, not a scheduling one.

    Two refusals, and they are different kinds of broken:

      - **a residual node-level cycle**: the declared back-edges do not break every loop, so no
        total order exists at all. This is what found `C-COMM-BUS` and the hydrogen half of the
        reactant cycle.
      - **a declared back-edge that does not close its cycle**: the declaration reads as a decision
        and changes nothing.
    """
    nodes = {str(k): v for k, v in (doc.get("nodes") or {}).items()}
    edges = {str(e.get("id")): e for e in doc.get("edges") or []}
    back = {
        str(cy.get("back_edge"))
        for cy in doc.get("cycles") or []
        if cy.get("back_edge") is not None
    }
    where = "coupling.yaml"

    for eid, edge in sorted(edges.items()):
        for end in ("from", "to"):
            if str(edge.get(end)) not in nodes:
                report.refuse(
                    f"{where}:edge {eid}", f"names {end} {edge.get(end)!r}, which is no node"
                )

    # `command_executive` is the only edge class originating outside the plant and
    # `published_evidence` is its sink; neither is scheduled.
    unscheduled = {"command_executive", "published_evidence"}
    scheduled = [n for n in nodes if n not in unscheduled]
    # `successors` drives the cycle search; `predecessors` drives the emission, and they are kept
    # separate because the two loops want opposite directions and one map serving both is how the
    # order came out **reversed for the whole life of this function**. Kahn's algorithm as written
    # took nodes with no *successors* first, which is the last element of a topological order, so
    # every one of the thirty-nine ordering constraints was violated by the order the plant was
    # ticking in: fuel cells computed after the buses they feed, tanks after the engines that
    # drain them. Nothing refused it, because the only thing anyone checked was that a cycle was
    # absent — and a reversed topological order has no cycle either. It is fixed by taking nodes
    # with no *predecessors* first, and it is held fixed by a test that walks every non-back edge
    # and asserts the producer comes first.
    successors: dict[str, set[str]] = {n: set() for n in scheduled}
    predecessors: dict[str, set[str]] = {n: set() for n in scheduled}
    for eid, edge in sorted(edges.items()):
        if eid in back:
            continue
        a, b = str(edge.get("from")), str(edge.get("to"))
        if a in successors and b in successors and a != b:
            successors[a].add(b)
            predecessors[b].add(a)

    WHITE, GREY, BLACK = 0, 1, 2
    colour = dict.fromkeys(scheduled, WHITE)
    path: list[str] = []

    def walk(u: str) -> list[str] | None:
        colour[u] = GREY
        path.append(u)
        for v in sorted(successors.get(u, ())):
            if colour[v] == GREY:
                return path[path.index(v) :] + [v]
            if colour[v] == WHITE:
                found = walk(v)
                if found:
                    return found
        path.pop()
        colour[u] = BLACK
        return None

    for node in sorted(scheduled):
        if colour[node] == WHITE:
            loop = walk(node)
            if loop:
                report.refuse(
                    f"{where}:cycles",
                    "the declared back-edges do not break every loop, so the schedule "
                    "`plant.md:71` promises does not exist: "
                    + " -> ".join(loop)
                    + ". Declare the missing cycle with a back-edge, as `C-COMM-BUS` was",
                )
                return []

    # A declared back-edge has to close the loop it is on: its `from` must be reachable from its
    # `to` through the cycle's other members. A cycle that does not close is a declaration that
    # reads as a decision and changes nothing.
    for cycle in doc.get("cycles") or []:
        cid = str(cycle.get("id"))
        edge_id = cycle.get("back_edge")
        if edge_id is None or edge_id not in edges:
            continue
        members = [str(m) for m in cycle.get("members") or []]
        if edge_id not in members:
            continue  # refused in check_coupling
        start = str(edges[edge_id].get("to"))
        target = str(edges[edge_id].get("from"))
        reachable = {start}
        changed = True
        while changed:
            changed = False
            for member in members:
                if member == edge_id or member not in edges:
                    continue
                source, sink = str(edges[member].get("from")), str(edges[member].get("to"))
                if source in reachable and sink not in reachable:
                    reachable.add(sink)
                    changed = True
        if target not in reachable:
            report.refuse(
                f"{where}:cycle {cid}",
                f"names {edge_id!r} as its back-edge, but its other members do not form a path "
                f"from {start!r} back to {target!r}: the cycle is declared and does not close",
            )

    # Kahn's algorithm, emitting the *sources* first: a node is ready when every node that must
    # precede it has been emitted. The tiebreak is the frozen lexicographic one `plant.md:73`
    # names, applied to the ready set, so the order is a function of the graph alone.
    order: list[str] = []
    remaining = {n: set(predecessors[n]) for n in scheduled}
    while remaining:
        ready = sorted(n for n, before in remaining.items() if not before)
        if not ready:
            break  # unreachable: the cycle check above catches it
        for node in ready:
            order.append(node)
            del remaining[node]
        for before in remaining.values():
            before.difference_update(ready)
    return order


def check_channels(doc: dict[str, Any], report: Report) -> dict[str, dict[str, Any]]:
    """Build the channel registry, and refuse the vocabulary faults it can see."""
    registry: dict[str, dict[str, Any]] = {}
    for section, rows in doc.items():
        if section in {"schema_version", "source", "vocabulary"}:
            continue
        if section == "defaults":
            allowed = set(rows.get("quality_allowed") or [])
            for code in allowed - QUALITY_CODES:
                report.refuse("channels.yaml:defaults", f"quality code {code!r} is not canonical")
            continue
        if section == "crew_positions":
            continue
        if section == "open_debts":
            for row in rows or []:
                report.debt("channels.yaml:open_debts", str(row))
            continue
        if section not in DOMAINS:
            report.refuse("channels.yaml", f"section {section!r} is not a canonical domain")
            continue
        section_prefix = DOMAIN_PREFIX[section] + "."
        for row in rows or []:
            cid = row.get("id")
            if not cid:
                report.refuse(f"channels.yaml:{section}", "a row has no id")
                continue
            if cid in registry:
                report.refuse(cid, "registered twice")
                continue
            allowed_prefixes = (section_prefix,) + SECTION_EXTRA_PREFIXES.get(section, ())
            if FORBIDDEN_CHANNEL.search(cid):
                report.refuse(
                    cid,
                    "is a claim quantity. A published reservation, allocation or commitment is "
                    "a coordination mechanism the world deliberately withholds (D-04, "
                    "design.md §8); the vehicle may use claims internally and must not publish "
                    "them",
                )
            if not cid.startswith(allowed_prefixes):
                report.refuse(
                    cid,
                    "does not carry one of its section's prefixes "
                    + " or ".join(repr(p) for p in allowed_prefixes),
                )
            registry[cid] = row
            where = f"channels.yaml:{cid}"
            for field in ("unit", "layer", "precision", "priority", "rate_hz"):
                if field not in row:
                    report.refuse(where, f"missing {field!r}")
            if row.get("layer") not in LAYERS:
                report.refuse(where, f"layer {row.get('layer')!r} is not one of {sorted(LAYERS)}")
            if row.get("priority") not in PRIORITIES:
                report.refuse(where, f"priority {row.get('priority')!r} is not canonical")
            prov = row.get("provenance") or {}
            check_basis(where, prov.get("basis"), prov, report)
            if prov.get("basis") == "derived" and not row.get("inputs"):
                report.refuse(where, "provenance says derived but the row lists no inputs")
            # A rejected name appearing in a *value* position is a dialect leaking in. The
            # scan is deliberately narrow: a provenance note that quotes a rejected name in
            # order to reject it is documentation, not a fault, and a linter that flags its
            # own footnotes is a linter that gets bypassed.
            values = " ".join(
                str(row.get(field, "")) for field in ("id", "unit", "layer", "priority")
            )
            # An enum's *members* are the domain's own vocabulary; the rejected list is about
            # scale names that the whole vehicle shares — a lifecycle state, a quality code, an
            # alert severity. `res.status_[resource]` legitimately carries CRITICAL meaning
            # "the resource is critically low", which is a different scale from an alert
            # severity, and a rule that cannot tell them apart is a rule that gets bypassed.
            # A bare `severity: CRITICAL` is still refused, because it is not inside an enum.
            scanned = re.sub(r"enum\[[^\]]*\]", "", values)
            for bad, fix in REJECTED.items():
                if re.search(rf"\b{re.escape(bad)}\b", scanned):
                    report.refuse(where, f"uses {bad!r} in a value position; canonical is {fix}")
    return registry


def conserved_dimension(node: dict[str, Any]) -> str | None:
    """The dimension this node can receive a conservation of, or None if it does not say.

    Most nodes answer this from their unit. Two of them cannot: a cabin holds four gas masses
    *and* the total pressure they make together, so its unit is the composite `kg + Pa`, and a
    lookup that returns None for it used to make the conservation check pass by default — which
    is how `E-ATM-ABSORB` claimed cabin carbon dioxide is conserved into absorbent man-hours.
    A node that holds several quantities at once therefore names the ones it can receive a
    conservation of, and a node that names none has not answered the question.
    """
    unit = node.get("unit")
    if unit in DIMENSION:
        return DIMENSION[unit]
    declares = node.get("conserves")
    if isinstance(declares, str) and declares in set(DIMENSION.values()):
        return declares
    return None


def withheld_channels(root: Path) -> set[str]:
    """Every channel the domains declare they withhold, by name.

    `points.yaml#not_published` is the vehicle's statement of what it refuses to publish, and the
    failure chains are the vehicle's statement of what a fleet has to work out. Those two are the
    same subject from opposite sides, and until now nothing joined them: a chain whose first
    published clue is a withheld channel is a chain nobody can start on, and the linter would have
    reported the clue as *registered* and been satisfied. Nothing is wrong with the corpus today —
    41 withheld channels and not one of them a clue — which is exactly why the join is worth having.
    """
    names: set[str] = set()
    for path in sorted(p for p in (root / "domains").iterdir() if p.is_dir()):
        points = load(path / "points.yaml", Report()) or {}
        for entry in points.get("not_published") or []:
            listed = entry.get("channel") if isinstance(entry, dict) else entry
            for name in listed if isinstance(listed, list) else [listed]:
                if name is not None and "." in str(name):
                    names.add(str(name))
    return names


def states_by_node(root: Path) -> dict[str, list[str]]:
    """Every state each coupling node carries, in declaration order, `internal` excluded.

    This is the set an edge's `advances` may name, and the reason the field exists is that the set
    is not always a singleton: an edge says what it drives, and on a node holding four gas masses and
    a pressure it has five things to choose between.

    Two versions of this helper were wrong before this one, in opposite directions, and both are
    worth keeping.

    The first returned only the states whose *method* reads incoming edges — `stock`, `lag`, `delay`,
    `dynamics` — on the theory that only those are ambiguous for the plant. That refused the one true
    answer: `E-ZONE-ATM` carries the cabin's dP/dT, so the state it drives is
    `csm_cabin_pressure_pa`, which is `algebraic` and was excluded. **A rule that refuses the correct
    declaration is more expensive than no rule, because the next author bends the data to satisfy
    it.**

    The second returned *every* state, and demanded that an executive command to `engine_main` name
    which of `sps_state`, `dps_state` or `aps_state` it advances. It advances whichever the command
    says; a command is not a flux and does not drive one state rather than another.

    So the set is the methods an edge's *value* can drive — `stock`, `lag`, `delay`, `dynamics`,
    `algebraic` — which excludes `discrete` and `service` because those are advanced by transitions
    and by authority rather than by a sensitivity. The distinction is whether the edge's number is
    what moves the state.
    """
    by_node: dict[str, list[str]] = {}
    if not (root / "domains").is_dir():
        return by_node
    for path in sorted(p for p in (root / "domains").iterdir() if p.is_dir()):
        components = load(path / "components.yaml", Report()) or {}
        for state in components.get("state") or []:
            node = str(state.get("node") or "")
            if state.get("method") in DRIVEN_BY_A_VALUE and state.get("id") and node:
                by_node.setdefault(node, []).append(str(state["id"]))
    return by_node


def state_methods(root: Path) -> dict[str, str]:
    """Every state id and its integrator class, across the vehicle."""
    out: dict[str, str] = {}
    if not (root / "domains").is_dir():
        return out
    for path in sorted(p for p in (root / "domains").iterdir() if p.is_dir()):
        components = load(path / "components.yaml", Report()) or {}
        for state in components.get("state") or []:
            if state.get("id"):
                out[str(state["id"])] = str(state.get("method") or "")
    return out


def enumerated_values_by_node(root: Path) -> dict[str, set[str]]:
    """Which enumerated values each coupling node's own states can take.

    A regime keyed on a threshold steps through a *band*; a regime keyed on an enum steps through
    a *mode*, and the two are different questions. `E-STRUCT-PLATE` is the second kind: the
    coldplate temperature a vehicle configuration produces is a lookup over
    `enum[docked, undocked, separated, abandoned]`, and there is no threshold anywhere that could
    stand in for "the vehicle is undocked". Reading the vocabulary off the driver node's own
    states is what keeps the table honest — a regime naming a configuration the vehicle cannot be
    in is exactly the fault the vocabulary checks exist for.
    """
    values: dict[str, set[str]] = {}
    domains_dir = root / "domains"
    if not domains_dir.is_dir():
        return values
    for path in sorted(p for p in domains_dir.iterdir() if p.is_dir()):
        components = load(path / "components.yaml", Report()) or {}
        for state in components.get("state") or []:
            if not isinstance(state, dict):
                continue
            node, unit = state.get("node"), str(state.get("unit") or "")
            match = re.match(r"enum\[([^\]]*)\]", unit)
            if not node or not match:
                continue
            members = {v.strip() for v in match.group(1).split(",") if v.strip()}
            values.setdefault(str(node), set()).update(members)
    return values


def produced_channels_by_node(
    root: Path, registry: dict[str, dict[str, Any]]
) -> dict[str, set[str]]:
    """Which channels each coupling node can actually produce, via its states.

    A node names physical equipment; a *state* on that node names a number it holds; a
    `points.yaml` entry turns that state into a published channel. Composing the three is what
    lets the linter ask the one question a regime table has to answer: *is this coupling keyed on
    the driver's own quantity, or on something else wearing its name?* `E-BUS-GNC` says avionics
    power follows bus volts, so the thresholds it steps through must watch a channel that
    `bus_a`'s own states produce — and `power.dc_bus_a_v` is exactly that, which is why the
    voltage ladder already written in `domains/power/profiles.yaml` is the right ladder to reuse
    rather than a second one invented alongside it.
    """
    index = ChannelIndex(registry)
    node_of_state: dict[str, str] = {}
    domains_dir = root / "domains"
    if not domains_dir.is_dir():
        return {}
    for path in sorted(p for p in domains_dir.iterdir() if p.is_dir()):
        components = load(path / "components.yaml", Report()) or {}
        for state in components.get("state") or []:
            if isinstance(state, dict) and state.get("id") and state.get("node"):
                node_of_state[str(state["id"])] = str(state["node"])
    produced: dict[str, set[str]] = {}
    for path in sorted(p for p in domains_dir.iterdir() if p.is_dir()):
        points = load(path / "points.yaml", Report()) or {}
        for point in points.get("points") or []:
            if not isinstance(point, dict):
                continue
            channel, source = point.get("channel"), point.get("from")
            if not channel or not source:
                continue
            row = index.row(str(channel))
            if row is None:
                continue
            node = node_of_state.get(str(source))
            if node is None:
                continue
            for registered, candidate in registry.items():
                if candidate is row:
                    produced.setdefault(node, set()).add(registered)
    return produced


def check_coupling(
    doc: dict[str, Any],
    registry: dict[str, dict[str, Any]],
    report: Report,
    produced: dict[str, set[str]] | None = None,
    threshold_point: dict[str, str] | None = None,
    enum_values: dict[str, set[str]] | None = None,
    withheld: set[str] | None = None,
    states_by_node_map: dict[str, list[str]] | None = None,
    state_methods: dict[str, str] | None = None,
) -> None:
    nodes = doc.get("nodes") or {}
    edges = doc.get("edges") or []
    node_of = dict(nodes)
    produced = produced or {}
    threshold_point = threshold_point or {}
    enum_values = enum_values or {}
    withheld = withheld or set()

    for name, node in node_of.items():
        where = f"coupling.yaml:node {name}"
        if node.get("domain") not in DOMAINS | NON_DOMAIN_NODE_DOMAINS:
            report.refuse(where, f"domain {node.get('domain')!r} is not canonical")
        if node.get("kind") not in NODE_KINDS:
            report.refuse(where, f"kind {node.get('kind')!r} is not one of {sorted(NODE_KINDS)}")
        # `accumulates:` says a stock counts what has been *spent* rather than what is held, and a
        # count with no rating can be spent forever. That is not hypothetical: it is what
        # `absorber_capacity` did for the life of the file, in the one place where the consequence
        # is a crew who cannot run out of LiOH, and the ratings that would have exposed it sat in
        # two other files read by nothing. Until round 42 nothing checked that the field was there.
        if node.get("accumulates") and not isinstance(node.get("exhausted_at"), (int, float)):
            report.refuse(
                where,
                "declares `accumulates` and no numeric `exhausted_at`. A counter with no rating is "
                "a quantity that can be spent without limit, so whatever threshold watches it can "
                "never be reached",
            )
        if isinstance(node.get("exhausted_at"), (int, float)) and not node.get("accumulates"):
            report.refuse(
                where,
                "declares `exhausted_at` without `accumulates`, so the rating belongs to a stock "
                "that is held rather than counted and the reader cannot tell which",
            )

    # Every edge that lands on a stock, classified by the one function the plant also calls.
    #
    # This is a *debt* rather than a refusal because the vehicle genuinely is not there yet: eleven
    # of its fourteen stock edges cannot be integrated as written, and eight of those are structural
    # rather than unset. Refusing would refuse the corpus. But leaving them unreported was worse
    # than either, because **the plant's refusals were unreachable**: the schedule stops at the
    # first `algebraic` state, so no tool ever reached a stock and the eight were invisible to the
    # linter, to `--strict`, and to the debt count. A defect that only a code path nobody reaches
    # can see is a defect nobody has.
    #
    # Unset values are skipped here: they are already debts, reported by the edge walk with the
    # recipe the edge owes, and saying it twice would double-count the same obligation.
    stock_nodes = {name for name, node in node_of.items() if node.get("kind") == "stock"}
    stock_states = {sid for sid, method in (state_methods or {}).items() if method == "stock"}

    # A stock discharges through its *outbound* edges — that is the rule the README states and the
    # plant never implemented, so every tank in the vehicle only filled. An outbound edge from a node
    # carrying more than one stock state has to say which one it drains, for the same reason an
    # inbound edge has to say which one it advances.
    stocks_per_node: dict[str, list[str]] = {
        node_name: [state for state in states if state in stock_states]
        for node_name, states in (states_by_node_map or {}).items()
    }
    for edge in edges:
        source = str(edge.get("from"))
        candidates = stocks_per_node.get(source) or []
        if len(candidates) < 2:
            continue
        where = f"coupling.yaml:edge {edge.get('id')}"
        drains = edge.get("drains")
        if drains is None:
            report.refuse(
                where,
                f"leaves {source}, which carries {len(candidates)} stock states "
                f"({sorted(candidates)}), and declares no `drains`. The stock discharges through its "
                "outbound edges, so without this every one of them loses this edge's flow",
            )
        elif str(drains) not in candidates:
            report.refuse(
                where,
                f"declares `drains: {drains!r}`, which is not one of {source}'s stock states "
                f"({sorted(candidates)})",
            )

    for edge in edges:
        if edge.get("to") not in stock_nodes and edge.get("from") not in stock_nodes:
            continue
        if edge.get("kind") == CLAMP_KIND:
            # A declared clamp, exempt by its kind — but only with a reason attached, because the
            # exemption is the one place a mislabelled conversion could hide.
            if not (edge.get("sensitivity") or {}).get("note"):
                report.refuse(
                    f"coupling.yaml:edge {edge.get('id')}",
                    "declares `kind: limit` and gives no `sensitivity.note`. A clamp is exempt from "
                    "the flux rule because it is a relation rather than a flow, and the reason is "
                    "what separates that from a conversion wearing the label",
                )
            continue
        if (edge.get("sensitivity") or {}).get("value") in (None, "UNCONFIGURED"):
            continue
        # Only an edge that actually drives a *stock* has to be a flux. `advances` is what says so,
        # and it is required on every multi-state node; on a singleton node the node's one state is
        # the answer. `E-ZONE-ATM` is the case this distinction exists for: it lands on the cabin
        # node, which is a stock, but it drives `csm_cabin_pressure_pa` — an algebraic state — and a
        # pressure is not a conserved quantity that something flows into.
        # The `advances` test skips an edge that lands on a stock node's *channel* while driving
        # something else — but only on the inbound side. An edge that *leaves* a stock drains it
        # whatever it drives, and `E-O2-FC` and `E-H2-FC` are exactly that: they drive the fuel
        # cell's power state and empty the oxygen and hydrogen tanks, so testing `advances` alone
        # let both through while the tanks they drain stayed unfillable.
        driven = edge.get("advances")
        if edge.get("to") in stock_nodes and driven is not None and str(driven) not in stock_states:
            continue
        basis, reason = stock_flux_basis(edge, node_of)
        if basis is None:
            report.debt(f"coupling.yaml:edge {edge.get('id')}", reason)

    # An edge that drives a state has to say *which* state, whenever the node carries more than one
    # it could drive.
    #
    # Ten of the vehicle's 41 nodes hold more than one state, and the plant resolves a state's
    # drivers by node — so on `cabin_atm`, which holds four gas masses, `csm_cabin_o2_kg`,
    # `csm_cabin_n2_kg`, `csm_cabin_co2_kg` and `csm_cabin_h2o_kg` were all handed the same three
    # edges: the oxygen supply, the crew's CO2 production and a pressure/temperature relation. Every
    # gas integrated every other gas's flux. The discrete and dynamics nodes are harmless — those
    # methods do not consume `incoming` — so the rule counts only the methods that do, which makes
    # it four nodes and nine edges rather than ten and a guess.
    #
    # The field is required rather than inferred because the inference is exactly what was wrong:
    # "the only state on this node" is true today and stops being true the moment a second state
    # lands, silently and in the direction of the plant integrating the wrong thing.
    states_by_node = states_by_node_map or {}
    for edge in edges:
        # A `discrete` edge carries a command or a mode rather than a slope, so "which state does this
        # value drive" does not apply to it — the same reason `discrete` is excluded from the states
        # the field may name. `E-CMD-LINK` is an executive command to the link, and it advances
        # whichever state the command names.
        if edge.get("kind") == "discrete":
            continue
        target = str(edge.get("to"))
        candidates = states_by_node.get(target) or []
        if len(candidates) < 2:
            continue
        where = f"coupling.yaml:edge {edge.get('id')}"
        advances = edge.get("advances")
        if advances is None:
            report.refuse(
                where,
                f"drives {target}, which carries {len(candidates)} states an edge can advance "
                f"({sorted(candidates)}), and declares no `advances`. The plant resolves a state's "
                "drivers by node, so without this every one of them integrates this edge",
            )
        elif str(advances) not in candidates:
            report.refuse(
                where,
                f"declares `advances: {advances!r}`, which is not one of {target}'s edge-consuming "
                f"states ({sorted(candidates)})",
            )

    edge_ids: dict[str, dict[str, Any]] = {}
    for edge in edges:
        eid = edge.get("id")
        if not eid:
            report.refuse("coupling.yaml:edges", "an edge has no id")
            continue
        where = f"coupling.yaml:edge {eid}"
        if eid in edge_ids:
            report.refuse(where, "declared twice")
        edge_ids[eid] = edge
        for endpoint in ("from", "to"):
            if edge.get(endpoint) not in node_of:
                report.refuse(where, f"{endpoint} {edge.get(endpoint)!r} is not a declared node")
        if edge.get("kind") not in EDGE_KINDS:
            report.refuse(where, f"kind {edge.get('kind')!r} is not one of {sorted(EDGE_KINDS)}")
        sens = edge.get("sensitivity") or {}
        if not sens:
            report.refuse(where, "has no sensitivity; topology alone excludes nothing (#8)")
            continue
        # A *lookup* is not a slope, and eight edges in this file spent their lives asking for a
        # derivative where the physics is a regime. A consumer's draw does not scale with bus
        # volts — it is constant inside the 24-32 V envelope and then it stops; that envelope is
        # already the undervoltage ladder in `domains/power/profiles.yaml`. A `1 per V` load would
        # grow without bound as the bus sagged, which is the opposite of what a load does. So a
        # sensitivity is either a scalar or a `regimes:` table, and the two are exclusive.
        regimes = sens.get("regimes")
        for field in ("unit", "basis", "at"):
            if field not in sens:
                report.refuse(where, f"sensitivity is missing {field!r}")
        if regimes is not None and "value" in sens:
            report.refuse(
                where,
                "declares both a `value` and a `regimes` table; a lookup is not a slope, so it has "
                "no derivative to state and the table is the whole answer",
            )
        if regimes is None and "value" not in sens:
            report.refuse(where, "sensitivity is missing 'value'")
        if regimes is not None:
            if not isinstance(regimes, list) or not regimes:
                report.refuse(where, "declares `regimes` that is not a non-empty list")
            else:
                driver = edge.get("from")
                driver_channels = produced.get(str(driver), set())
                for regime in regimes:
                    where_regime = f"{where} (regime {regime.get('when')!r})"
                    if not isinstance(regime, dict) or not regime.get("when"):
                        report.refuse(where, "has a regime with no `when`")
                        continue
                    if not regime.get("response"):
                        report.refuse(where_regime, "states no `response`")
                    # A table whose *responses* are unset is still a table with holes in it, and
                    # the holes have to be reported at the granularity they exist at. One opaque
                    # "the sensitivity is missing" is how `K per enum` hid the fact that there
                    # were four separate temperatures owed, one per configuration.
                    elif regime.get("response") == "UNCONFIGURED":
                        report.debt(where_regime, str(regime.get("note", "")).strip())
                    when = str(regime["when"])
                    if when in ("nominal", "otherwise"):
                        continue
                    # An enum key is a *mode* the driver's own states can take; a threshold id is
                    # a *band* on a quantity the driver produces. Nothing else is a regime.
                    if when in enum_values.get(str(driver), set()):
                        continue
                    # Every other `when` is a threshold id, and the alignment this buys is the
                    # point: the regimes must step through a ladder that already exists rather
                    # than a private set of bands invented beside it.
                    if when not in threshold_point:
                        report.refuse(
                            where_regime,
                            f"names no declared threshold and no value {driver!r}'s own states can "
                            f"take; a regime is `nominal`, `otherwise`, an id from "
                            f"domains/*/profiles.yaml, or a member of an enum on the driver node",
                        )
                        continue
                    point = threshold_point[when]
                    row = ChannelIndex(registry).row(str(point))
                    watched = {
                        registered for registered, candidate in registry.items() if candidate is row
                    }
                    if produced and watched and not (watched & driver_channels):
                        report.refuse(
                            where_regime,
                            f"steps through {when!r}, which watches {point!r} — a channel "
                            f"{driver!r} does not produce. A coupling keyed on a quantity its own "
                            "driver does not carry is a coupling to something else",
                        )
        check_basis(where, sens.get("basis"), sens, report)
        # A `discrete` coupling that leaves physical equipment is a mode selection, and a mode
        # selection has no derivative. The command paths are the exception and they are a real
        # one: `E-CMD-*` leave `command_executive`, a `service` node that holds authority rather
        # than a quantity, and a command arriving one for one is genuinely a unity gain. What is
        # left is `bus_a -> rcs_valves` and `nav_state -> engine_main` and their like, which are
        # tables and were declaring units like `enum per m` to say so.
        from_kind = (node_of.get(str(edge.get("from"))) or {}).get("kind")
        if edge.get("kind") == "discrete" and from_kind != "service" and regimes is None:
            report.refuse(
                where,
                "is a discrete coupling out of physical equipment and declares no `regimes`; a "
                "mode selection is a table rather than a sensitivity, and a scalar here would be "
                "a proportional law the vehicle does not have",
            )
        if sens.get("value") == "UNCONFIGURED":
            report.debt(
                where, f"{edge.get('from')} -> {edge.get('to')}: {sens.get('note', '')}".strip()
            )
        crisis = sens.get("crisis") or {}
        if crisis and crisis.get("value") == "UNCONFIGURED":
            report.debt(where, f"crisis point: {crisis.get('note', '')}".strip())
        # A `derived` sensitivity whose arithmetic the linter cannot evaluate is a value the
        # linter must take on trust, and the one time it was checked by hand the prose and the
        # field disagreed by 7 %: `E-RAD-WATER` said 3.8e-7 while its own relation computed
        # 1/2.45e6 = 4.082e-7. A relation is prose and prose cannot be evaluated — but a
        # `computation` can, so an edge that states one is re-derived on every run.
        #
        # This is the mechanism the rest of the file already uses for the rocket equation and the
        # transfer ellipse, arriving at the coupling graph. It is opt-in because most relations
        # are not arithmetic (a DC motor's flow follows its voltage; the coupling is the signal
        # itself) and pretending otherwise would be worse than the prose.
        sensitivity = edge.get("sensitivity") or {}
        computation = sensitivity.get("computation")
        value = sensitivity.get("value")
        rederive(where, value, computation, report)

        # Conservation is the one invariant a linter can check without a plant.
        #
        # It has three ways to pass and each of them used to be a way to pass *vacuously*. The
        # dimension lookup returned None for six of the node units in this file — `kg + Pa`,
        # `man_hours`, `m, m/s`, `quat` and the `-` that means "no unit stated" — and the guard
        # `if a and b` then skipped the check entirely, which is how `E-ATM-ABSORB` spent its
        # life claiming that cabin carbon dioxide is *conserved* into absorbent man-hours. A
        # check that cannot run is not a check that passed, so an undecidable conservation edge
        # is now a refusal and the fix is to stop calling it conservation.
        if edge.get("kind") == "conserve":
            from_node = node_of.get(edge.get("from")) or {}
            to_node = node_of.get(edge.get("to")) or {}
            from_unit = from_node.get("unit")
            to_unit = to_node.get("unit")
            a = conserved_dimension(from_node)
            b = conserved_dimension(to_node)
            if a is None or b is None:
                undecided = [
                    f"{side} {unit!r}"
                    for side, unit in (("from", from_unit), ("to", to_unit))
                    if conserved_dimension(node_of.get(edge.get(side)) or {}) is None
                ]
                report.refuse(
                    where,
                    "is a conservation edge with no dimension for "
                    + " and ".join(undecided)
                    + "; conservation names one quantity travelling, so a composite or unstated "
                    "unit means this edge is a conversion wearing a conservation label. A node "
                    "that holds several quantities at once declares which of them it can receive "
                    "a conservation of, in `conserves:`",
                )
            elif a != b:
                report.refuse(where, f"is a conservation edge between {a} and {b}")
            # A conserved quantity crosses one for one. A `conserve` edge carrying any other
            # number is a conversion, and the number is the give-away: `E-PRESS-PROP` declared
            # "kg prop per kg He", which is a displacement law, not a conservation, and a
            # bladder pressurant never leaves its tank to be conserved anywhere.
            elif sens.get("value") == "UNCONFIGURED":
                report.refuse(
                    where,
                    "is a conservation edge with an unconfigured sensitivity; a conserved "
                    "quantity crosses one for one, so there is nothing to configure — an unset "
                    "value here means the edge is not a conservation",
                )
            elif sens.get("value") != 1.0:
                report.refuse(
                    where,
                    f"is a conservation edge carrying {sens.get('value')!r}; conservation is one "
                    "for one, and any other ratio is a conversion that should declare `rate`",
                )

    for cycle in doc.get("cycles") or []:
        where = f"coupling.yaml:cycle {cycle.get('id')}"
        members = cycle.get("members") or []
        for member in members:
            if member not in edge_ids:
                report.refuse(where, f"member {member!r} is not a declared edge")
        back = cycle.get("back_edge")
        if back is not None and back not in members:
            report.refuse(where, f"back_edge {back!r} is not one of its members")
        delay = cycle.get("delay_ticks")
        if delay is None:
            report.refuse(where, "does not declare delay_ticks (0 for an algebraic loop)")
        if back is None and delay:
            report.refuse(where, "declares a delay but names no back_edge")
        if back is not None and not delay and not cycle.get("algebraic"):
            report.refuse(where, f"names back_edge {back!r} but declares delay_ticks 0")
        # A latched state on a delayed cycle without hysteresis is the relay oscillation
        # review-findings.md #7 measured: ~1.7 Hz, which an agent reading 2 Hz telemetry
        # will read as physics.
        latched = [m for m in members if (edge_ids.get(m) or {}).get("kind") == "discrete"]
        if (
            latched
            and back is not None
            and not (cycle.get("stability") or {}).get("hysteresis_required")
        ):
            report.refuse(
                where,
                f"carries latched edges {latched} on a delayed cycle without declaring "
                "stability.hysteresis_required",
            )

    index = ChannelIndex(registry)

    # --------------------------------------------------------------------------------------
    # Nodes that participate in nothing, and stocks that only ever drain.
    #
    # Both are the shape of the bug that a *reversed* tick order hid for a round: `battery_energy`
    # sat in the graph with no forward inbound edge at all and the schedule could not tell, because
    # a reversed order still contains every node. Every check that asked "is X in the schedule"
    # passed. These ask the two questions a membership test cannot:
    #
    #   **a node with no incident edge is not in the graph**, it is only in the node list. The bus
    #   tie was in exactly that state — `bus_tie_closed` existed, the edge `E-BUSB-BUSA` existed,
    #   and that edge's own note said "gated by bus_tie" while `bus_tie` gated nothing, because
    #   the coupling ran straight from `bus_b` to `bus_a` and bypassed it.
    #
    #   **a stock that nothing fills only drains**, and the definition has to say so deliberately
    #   rather than by omission. Three tanks are legitimately pre-loaded — filled before launch and
    #   never again — and saying that is different from forgetting it.
    # --------------------------------------------------------------------------------------
    back_edge_ids = {
        str(cy.get("back_edge"))
        for cy in doc.get("cycles") or []
        if cy.get("back_edge") is not None
    }
    incident: dict[str, list[str]] = {name: [] for name in node_of}
    forward_into: dict[str, list[str]] = {name: [] for name in node_of}
    for edge in edges:
        eid = str(edge.get("id"))
        for endpoint in ("from", "to"):
            node = str(edge.get(endpoint))
            if node in incident and eid not in incident[node]:
                incident[node].append(eid)
        target = str(edge.get("to"))
        if target in forward_into and eid not in back_edge_ids:
            forward_into[target].append(eid)

    #   **a `lag` or `stock` with no *inbound* edge can never be advanced**, and that is a different
    #   property from having no edge at all — which is why this went unnoticed. `cabin_zone_t` has
    #   an edge: `E-ZONE-ATM` points *out* of it, into `cabin_atm`, so the isolation check above is
    #   satisfied and the node looks connected. But nothing drives it, `zone_csm_cabin_t` is a `lag`,
    #   and a lag with no driver has nothing to relax toward. The cabin's temperature has no heat
    #   input anywhere in the graph — the crew, the equipment and the loop all warm it in the prose
    #   and none of them is an edge.
    #
    #   It is reported rather than refused because the fix is a design step and not a repair: the
    #   heat inputs the thermal domain declares are `power/components.yaml#loads`, and **no load says
    #   which compartment it heats**. `load_budget` states the relationship and its own note says
    #   the numbers are not duplicated here; what is missing is the assignment, 25 loads against 6
    #   zones, and until it exists there is nothing to draw the edges from.
    driven_methods = {"lag", "stock", "delay", "dynamics"}
    for name in sorted(node_of):
        if not incident[name] or forward_into[name]:
            continue
        # A tank filled at the pad and never again has no inbound edge *on purpose*, and it says so
        # in `preloaded:`. Three do — `o2_lm`, `prop_rcs`, `pressurant_he` — and reporting them would
        # have made this check three parts noise to one part signal. The declaration is what separates
        # a stock that nothing fills from one that is filled once, which is the distinction the
        # README already draws for the same reason.
        if node_of[name].get("preloaded"):
            continue
        undriven = [
            sid
            for sid in (states_by_node_map or {}).get(name, [])
            if (state_methods or {}).get(sid) in driven_methods
        ]
        if undriven:
            report.debt(
                f"coupling.yaml:node {name}",
                f"has edges but none into it, and {undriven} cannot be advanced without a driver. "
                "A lag has nothing to relax toward and a stock has nothing to fill it",
            )

    for name, node in sorted(node_of.items()):
        if not incident[name]:
            report.refuse(
                f"coupling.yaml:node {name}",
                "has no edge in either direction, so it is in the node list and not in the graph. "
                "A node the schedule orders and nothing reads or writes is a declaration that "
                "looks like a connection",
            )
            continue
        # Outbound is a different question from inbound and the asymmetry is real: a back-edge
        # *out* of a stock still drains it, because the stock's own integrator subtracts the flow
        # the back-edge reads. Only the inbound side has to be forward, because a fill read from
        # last tick is not a fill. `h2_csm` and `water_cooling` both discharge entirely through
        # back-edges (`E-H2-FC`, `E-WATER-RAD`) and both are correct.
        if node.get("kind") == "stock" and not any(str(edge.get("from")) == name for edge in edges):
            if node.get("accumulates"):
                report.note(
                    f"coupling.yaml:node {name}",
                    f"accumulates and is never drawn: {node['accumulates']}",
                )
            else:
                report.refuse(
                    f"coupling.yaml:node {name}",
                    "is a stock with no outbound edge at all, so it only ever accumulates. A "
                    "resource produced and never drawn is the same defect as one drawn and never "
                    "produced, and it is the quieter of the two: the fleet watches the quantity "
                    "rise, and a rising number looks like a healthy number until the mission ends",
                )
            continue
        if node.get("kind") == "stock" and not forward_into[name]:
            if node.get("preloaded"):
                report.note(
                    f"coupling.yaml:node {name}",
                    f"is pre-loaded and never filled: {node['preloaded']}",
                )
            else:
                report.refuse(
                    f"coupling.yaml:node {name}",
                    "is a stock with no inbound edge that is not a back-edge, so it only ever "
                    "drains — which is what a reversed tick order did to the battery for the "
                    "whole life of the graph. If the tank is filled before launch and never "
                    "again, say so in `preloaded:`; a stock nobody fills is a missing producer "
                    "far more often than it is a design decision",
                )

    # The graph's own shopping list, and it is counted rather than merely displayed. `channels.yaml`
    # has had its `open_debts` counted since the section existed while this file's seven were read
    # by nobody — the same construct behaving two ways in two files, which is how a total stops
    # meaning anything. The seven here are the substantive ones (thermal time constants, loop
    # transit, the throttle law, the inertia tensor, the crisis gains, the source resistance, the
    # missing pack-voltage state), so leaving them out of the count understated the debt by the
    # part that matters most. A debt is a value that is needed and unset, wherever it is written.
    for row in doc.get("open_debts") or []:
        report.debt("coupling.yaml:open_debts", str(row))

    for chain in doc.get("failure_chains") or []:
        cid = chain.get("id", "?")
        where = f"coupling.yaml:chain {cid}"
        for field in ("primary", "secondary", "third_order", "first_published_clue"):
            if not chain.get(field):
                report.refuse(where, f"has no {field}")
        clue = chain.get("first_published_clue")
        if clue and clue not in index:
            report.refuse(where, f"first clue {clue!r} is not a registered channel")
        for observed in chain.get("observable_clues") or []:
            if observed not in index:
                report.refuse(where, f"clue {observed!r} is not a registered channel")
        # And the join with §7: a clue has to be something the vehicle is *willing to publish*.
        # Being registered is not the same claim — `avionics.sensor_[id]_true_value` is registered
        # nowhere and withheld by name, and the two answers come from two different sections that
        # nothing had ever compared.
        for observed in [clue, *(chain.get("observable_clues") or [])]:
            if observed in withheld:
                report.refuse(
                    where,
                    f"leans on {observed!r} as an observable clue, and that channel is declared "
                    "`not_published`. A chain whose first clue is hidden truth is a chain no fleet "
                    "can start on: it would be diagnosable only by the vehicle that already knows",
                )


def check_crew(
    channels: dict[str, Any], registry: dict[str, dict[str, Any]], report: Report
) -> None:
    """The perception bound must name real channels, or it bounds nothing (#4, D-06)."""
    index = ChannelIndex(registry)
    positions = channels.get("crew_positions") or []
    if not positions:
        report.debt(
            "channels.yaml:crew_positions", "no crew position is defined, so ask_crew is unbounded"
        )
    for pos in positions:
        where = f"channels.yaml:crew_position {pos.get('id')}"
        if not pos.get("perceivable"):
            report.refuse(where, "declares nothing perceivable")
        for cid in (pos.get("perceivable") or []) + (pos.get("not_perceivable") or []):
            if cid not in index:
                report.refuse(where, f"{cid!r} is not a registered channel")

    # The location channel's vocabulary and the position list are one list written twice, and
    # nothing else compares them. A crew member at a station `crew_positions` does not describe
    # has no bound; a station nobody can occupy is a display contract for an empty seat. Both
    # defects are silent — the first makes `ask_crew` answer from nowhere, the second makes a
    # panel that is never read — so the check runs in both directions.
    declared = [str(pos.get("id")) for pos in positions]
    if not declared:
        return
    row = registry.get("crew.location_[id]")
    if row is None:
        report.refuse(
            "channels.yaml:crew_positions",
            "there are crew positions but no `crew.location_[id]` channel, so nothing says "
            "which position a report came from and the perception bound cannot be applied",
        )
        return
    enum = re.findall(r"enum\[([^\]]*)\]", str(row.get("unit") or ""))
    listed = [member.strip() for member in enum[0].split(",")] if enum else []
    for missing in sorted(set(declared) - set(listed)):
        report.refuse(
            "channels.yaml:crew.location_[id]",
            f"omits position {missing!r}: a crew member standing there could not be located, so "
            "their report could not be bounded",
        )
    for extra in sorted(set(listed) - set(declared)):
        report.refuse(
            "channels.yaml:crew.location_[id]",
            f"offers position {extra!r}, which channels.yaml:crew_positions does not describe",
        )


def check_answered_debts(name: str, doc: Any, report: Report) -> None:
    """A debt that has been paid and is still on the books is worse than no debt.

    `mission.yaml` carried `initial_state.landing_site: UNCONFIGURED` for as long as it carried the
    real thing. When the site was chosen it was declared as a **top-level** `landing_site` block —
    with the derivation, the sub-Earth geometry and a `check_landing_site` that re-derives it — and
    the placeholder twenty lines above went on reporting the site as undecided, in the same file,
    while counting as one of the vehicle's declared debts.

    The reason that matters more than the count is the one this folder keeps rediscovering: a note
    that has outlived its answer tells the next reader to stop looking. A reader who opens
    `initial_state` first concludes the landing site is owed, and the site is what decides whether
    the LM can be heard from the surface at all.

    The rule is narrow on purpose — the *leaf* name of an unset value, against the **top-level**
    names of the same document — because that is the shape of an answered debt rather than a
    coincidence: a top-level declaration is the file saying "this is decided", and a nested
    `UNCONFIGURED` of the same name is the file still saying it is not.
    """
    if not isinstance(doc, dict):
        return
    decided = {
        str(key)
        for key, value in doc.items()
        if value is not None and value != "UNCONFIGURED" and not isinstance(value, str)
    }
    for trail in walk_unset(doc):
        if "." not in trail:
            continue
        leaf = trail.split(".")[-1].split("[")[0]
        if leaf in decided:
            report.refuse(
                f"{name}:{trail}",
                f"is UNCONFIGURED while the top-level `{leaf}` in this same file is declared. A "
                "debt that has been answered and left standing tells the next reader to stop "
                "looking, which is worse than never having recorded it",
            )


def walk_unset(node: Any, trail: str = "") -> list[str]:
    """Every `UNCONFIGURED` scalar in a loaded document, with the path that reaches it.

    A debt is only actionable if it names where it lives, so the trail is built as the walk
    descends: `fault_policy.yaml:seeding.rate_kg_per_h` rather than "something is unset".
    """
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            found.extend(walk_unset(value, f"{trail}.{key}" if trail else str(key)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(walk_unset(value, f"{trail}[{index}]"))
    elif node == "UNCONFIGURED":
        found.append(trail)
    return found


def check_fault_components(
    path: Path,
    docs: dict[str, dict[str, Any]],
    node_ids: set[str],
    components_elsewhere: set[str],
    report: Report,
) -> None:
    """A fault's `component` must name something this vehicle actually declares.

    Every fault says what it happens *to*, and until round 42 nothing read the field. That is the
    usual cost: renaming a component in `components.yaml` leaves every fault that pointed at it
    dangling, and the fault goes on looking plausible because the channels in `perturbs` are still
    real. The linter refused none of it — which is how the ECLSS absorber components kept the LM's
    ratings under CSM-neutral ids while `ECL-04`, the *CSM's* blower failure, named one of them.

    Three kinds of name are accepted, and writing the check is what established that the field
    already meant all three. A fault happens to a *component* (`csm_lioh_element`), to a *state*
    when the thing that fails is a quantity rather than an article (`csm_cabin_n2_kg` is the
    nitrogen inventory, and `ECL-10` has no supply article to name instead), or to a *coupling
    node* (`o2_csm`, `cabin_regulator`, `crew_state`). Twelve of the vehicle's 128 bindings name a
    node, and they are naming the tank or the crew rather than the variable the domain keeps about
    them, which is the more natural way to say what failed. Narrowing the field to components alone
    would have meant rewriting twelve correct bindings to satisfy a check written after them, and
    would have lost the distinction between an article and its accounting.

    Neither components nor nodes are matched within the domain, because a fault crossing domains is
    the normal case rather than an error: `CNS-07` is a consumables fault that happens to the eclss
    regulator, and `CNS-08` breaks the eclss element. `components_elsewhere` is every other domain's
    components and states, collected before this domain is checked for the same reason
    `thresholds_by_domain` is.
    """
    policy = docs.get("fault_policy.yaml") or {}
    components = docs.get("components.yaml") or {}
    known = (
        {
            str(entry["id"])
            for key in ("components", "state")
            for entry in (components.get(key) or [])
            if isinstance(entry, dict) and entry.get("id")
        }
        | node_ids
        | components_elsewhere
    )
    for fault in policy.get("faults") or []:
        if not isinstance(fault, dict):
            continue
        component = fault.get("component")
        where = f"domains/{path.name}/fault_policy.yaml:{fault.get('id')}"
        if not component:
            report.refuse(where, "names no component, so there is nothing for it to happen to")
        elif str(component) not in known:
            near = sorted(n for n in known if str(component).split("_")[0] in n)
            report.refuse(
                where,
                f"happens to {component!r}, which is not a component, a state or a node of this "
                f"vehicle" + (f". Did you mean {near}?" if near else ""),
            )


def check_fault_coverage(path: Path, docs: dict[str, dict[str, Any]], report: Report) -> None:
    """A domain's coverage claim must be true, and every domain makes one.

    Each `fault_policy.yaml` opens its `coverage` block with the same sentence — "Every channel this
    domain publishes is perturbed by at least one fault above, except ..." — and then names the
    exceptions in `unperturbed`. It is the most useful claim in the file: it says which of a
    domain's channels a fault can never move, which is exactly what a fleet should know before it
    spends an afternoon diagnosing one.

    **Eight of the eleven claims were false**, and nothing read the block at all. `power` said its
    two battery channels were "perturbed only *indirectly* through PWR-07" while PWR-07 lists both
    in its `perturbs` — and `perturbs` is a *direct* list, which is the only thing it can be. The
    drift is structural rather than careless: `perturbs` is edited when a fault is added and
    `unperturbed` is edited when somebody remembers, so the two drift apart in the direction of the
    claim being more optimistic than the policy.

    The comparison is exact — the declared exceptions must *be* the channels no fault perturbs —
    because a claim with a slack clause is a claim that cannot be checked.
    """
    points = docs.get("points.yaml") or {}
    policy = docs.get("fault_policy.yaml") or {}
    coverage = policy.get("coverage")
    if not coverage:
        return
    published = {
        re.sub(r"\[[^\]]*\]", "[]", str(p.get("channel")))
        for p in points.get("points") or []
        if isinstance(p, dict) and p.get("channel")
    }
    perturbed = {
        re.sub(r"\[[^\]]*\]", "[]", str(c))
        for fault in policy.get("faults") or []
        for c in (fault.get("perturbs") or [])
    }
    actual = published - perturbed
    declared = {re.sub(r"\[[^\]]*\]", "[]", str(x)) for x in coverage.get("unperturbed") or []}
    where = f"domains/{path.name}/fault_policy.yaml:coverage"
    if actual - declared:
        report.refuse(
            where,
            f"claims every channel is perturbed except {sorted(declared)}, and no fault perturbs "
            f"{sorted(actual - declared)} either. A coverage claim that is more optimistic than the "
            "policy is the one direction this can drift without anybody noticing",
        )
    if declared - actual:
        report.refuse(
            where,
            f"names {sorted(declared - actual)} as exceptions, and a fault perturbs them. A stale "
            "exception is a channel a fleet is told to ignore and should not",
        )
    if actual and not coverage.get("unperturbed_reason"):
        report.refuse(
            where,
            f"declares {len(actual)} unperturbed channel(s) and no reason. The reason is the whole "
            "value of the declaration: a channel no fault can move is either a coverage gap or a "
            "channel nothing should be diagnosing, and the reader cannot tell which",
        )

    # ------------------------------------------------------------------------------------
    # The four siblings. `unperturbed` was the first key in this block to be checked, and
    # checking it made the block *look* read: four other keys sitting beside it made claims of
    # exactly the same kind — counts and directions — and nothing had ever compared one of them
    # to the policy. Five of their first eight numeric claims were wrong, in both directions:
    # `avionics` said six of its faults crossed a domain boundary and four did, `comms` said four
    # and five did, `gnc` said five and one did, and `gnc` separately said seven of eleven faults
    # moved `gnc.nav_integrity` when nine do. `avionics` also claimed the vehicle's *highest*
    # cross-domain reach while `eclss` has three times as many.
    #
    # Prose cannot be checked, so each block now declares its claim as a field. The notes stay —
    # they are the reasoning, and the reasoning was mostly right — but the number is data.
    # ------------------------------------------------------------------------------------
    prefix = DOMAIN_PREFIX.get(path.name, path.name)

    def crosses(channel: Any) -> bool:
        return not re.match(rf"^{re.escape(prefix)}[._]", str(channel))

    crossing = {
        str(fault.get("id"))
        for fault in policy.get("faults") or []
        if any(crosses(c) for c in fault.get("perturbs") or [])
    }
    cross_domain = coverage.get("cross_domain")
    if isinstance(cross_domain, dict):
        claim = cross_domain.get("faults_outside")
        if not isinstance(claim, int):
            report.refuse(
                f"{where}.cross_domain",
                "states no `faults_outside`, so its count is prose. A claim of the form 'six of the "
                "eleven faults perturb a channel outside this domain' is exactly checkable, and "
                "leaving it in a sentence is how it drifts while the policy it describes moves",
            )
        elif claim != len(crossing):
            report.refuse(
                f"{where}.cross_domain",
                f"claims {claim} fault(s) perturb a channel outside `{prefix}.*`, and "
                f"{len(crossing)} do: {sorted(crossing)}",
            )

    ladder = coverage.get("ladder_coupling")
    if isinstance(ladder, dict):
        channel = str(ladder.get("channel") or "")
        claim = ladder.get("faults_perturbing")
        moves = {
            str(fault.get("id"))
            for fault in policy.get("faults") or []
            if channel in (fault.get("perturbs") or [])
        }
        if not channel:
            report.refuse(f"{where}.ladder_coupling", "names no channel")
        elif not isinstance(claim, int):
            report.refuse(
                f"{where}.ladder_coupling",
                f"states no `faults_perturbing`, so the claim that {claim!r} faults move {channel} "
                "is prose",
            )
        elif claim != len(moves):
            report.refuse(
                f"{where}.ladder_coupling",
                f"claims {claim} fault(s) move `{channel}`, and {len(moves)} do: {sorted(moves)}",
            )

    gated = coverage.get("gated_alarms")
    if isinstance(gated, dict):
        profiles = docs.get("profiles.yaml") or {}
        by_id = {str(t.get("id")): t for t in profiles.get("thresholds") or []}
        for tid in gated.get("thresholds") or []:
            entry = by_id.get(str(tid))
            if entry is None:
                report.refuse(
                    f"{where}.gated_alarms",
                    f"names {tid!r}, which is not a threshold in this domain's profiles.yaml",
                )
            elif not entry.get("gated_by"):
                report.refuse(
                    f"{where}.gated_alarms",
                    f"names {tid!r} as a gated alarm, and that threshold declares no `gated_by`. "
                    "A suppression a fleet is told about and the vehicle does not apply is a "
                    "silence nobody can explain",
                )

    shared = coverage.get("shared_rules")
    if isinstance(shared, dict):
        components = docs.get("components.yaml") or {}
        diagnostics = {
            str(d.get("id")): d
            for d in components.get("diagnostics") or []
            if isinstance(d, dict) and d.get("id")
        }
        for did in shared.get("implemented_elsewhere") or []:
            entry = diagnostics.get(str(did))
            if entry is None:
                report.refuse(
                    f"{where}.shared_rules",
                    f"names {did!r}, which is not a diagnostic function in this domain's "
                    "components.yaml",
                )
            elif not entry.get("implemented_by"):
                report.refuse(
                    f"{where}.shared_rules",
                    f"names {did!r} as implemented elsewhere, and that diagnostic declares no "
                    "`implemented_by` domain. Two implementations of one rule is how two domains "
                    "come to disagree, and one implementation with no pointer is how a reader "
                    "finds neither",
                )


def check_profiles(path: Path, docs: dict[str, dict[str, Any]], report: Report) -> None:
    """D-05's two rules, neither of which was enforced: narrow only, and never edit.

    `thermal_diode.md:823-826` states the first as a design constraint rather than a preference —
    "the model that reasons about the spacecraft must not also be able to rewrite the limits by
    which its reasoning is constrained" — and each domain's `profile_selection` states the second in
    its own words: "An agent may select a profile revision and may never edit one."

    **The narrowing rule is the one with a defect behind it.** A profile's `factors` scale its
    thresholds, and the direction a factor has to move depends on the comparator: tightening a
    *ceiling* means lowering it, and tightening a *floor* means raising it. One number cannot do
    both, and three of the four domains had chosen whichever direction suited the thresholds they
    happened to have — so `power`'s `tight` profile, selected by a fleet that wanted warning
    *earlier*, dropped the bus undervoltage ladder from 26.5 V to 23.85 V and widened the envelope
    it was supposed to narrow. A profile whose name promises margin and whose arithmetic delivers
    less of it is worse than no profile: it is a decision an agent can make in good faith and lose
    by.
    """
    profiles_doc = docs.get("profiles.yaml") or {}
    alternatives = profiles_doc.get("alternatives") or []
    selection = profiles_doc.get("profile_selection") or {}
    if not alternatives and not selection:
        return
    where = f"domains/{path.name}/profiles.yaml"
    if selection and selection.get("selectable_by") not in AUTHORITIES:
        report.refuse(
            f"{where}:profile_selection",
            f"declares selectable_by {selection.get('selectable_by')!r}, which is not one of "
            f"{sorted(AUTHORITIES)}. D-05 makes selection an agent authority; a profile nobody may "
            "select is a profile that exists to be edited instead",
        )
    comparators = {
        str(th.get("comparator"))
        for th in profiles_doc.get("thresholds") or []
        if isinstance(th.get("assert"), (int, float))
    }
    for alt in alternatives:
        awhere = f"{where}:alternative {alt.get('id')}"
        factors = alt.get("factors")
        if not isinstance(factors, dict) or not factors:
            report.refuse(
                awhere,
                "declares no `factors`. One number cannot tighten both a ceiling and a floor — the "
                "direction depends on the comparator — so a profile that will be applied to both "
                "has to say which way each one moves",
            )
            continue
        if not alt.get("revision"):
            report.refuse(awhere, "declares no `revision`")
        if not (alt.get("provenance") or {}).get("reason"):
            report.refuse(
                awhere, "declares no reason; a selectable envelope is a reviewed decision"
            )
        for comparator in sorted(comparators):
            if comparator not in factors:
                report.refuse(
                    awhere,
                    f"declares no factor for its {comparator}-comparator thresholds, so selecting "
                    "it leaves them untouched while appearing to tighten everything",
                )
                continue
            factor = factors[comparator]
            if not isinstance(factor, (int, float)) or factor <= 0:
                report.refuse(awhere, f"declares {comparator} factor {factor!r}")
            elif comparator == "below" and factor < 1:
                report.refuse(
                    awhere,
                    f"declares a below factor of {factor}, which *lowers* a floor and so warns "
                    "later. Tightening a floor means raising it: the factor must be at least 1, or "
                    "a fleet that selects this profile for margin gets less of it",
                )
            elif comparator == "above" and factor > 1:
                report.refuse(
                    awhere,
                    f"declares an above factor of {factor}, which *raises* a ceiling and so warns "
                    "later. Tightening a ceiling means lowering it: the factor must be at most 1",
                )


def check_profile_immutability(
    docs: dict[str, dict[str, Any]], threshold_ids: set[str], report: Report
) -> None:
    """No verb may edit a threshold or a profile, which is D-05 stated as a refusal.

    This is vacuous today — every verb mentions a threshold only in its `interlocks`, which is the
    legitimate direction, and none declares a write at all — and that is the point: the property is
    one line of a future domain away from being false, and the thing it protects is the experiment.
    An agent that can widen its own envelope has not been tested on the envelope.
    """
    for doc in docs.values():
        for verb in doc.get("commands") or []:
            writes = verb.get("writes")
            if not isinstance(writes, list):
                continue
            for target in writes:
                if str(target) in threshold_ids:
                    report.refuse(
                        f"commands.yaml:{verb.get('verb')}",
                        f"declares that it writes {target!r}, which is a threshold. D-05: an agent "
                        "may select a profile revision and may never edit one, because the model "
                        "that reasons about the spacecraft must not also be able to rewrite the "
                        "limits by which its reasoning is constrained",
                    )


def check_range_kinds(registry: dict[str, dict[str, Any]], report: Report) -> None:
    """A channel's alarms have to be able to fire, and `range` alone cannot say whether they can.

    The field carries **two meanings** and the numbers do not distinguish them. On a physical
    channel it is an acceptable operating *band* and every alarm fires outside it — cabin pressure
    ranges 4.8-5.2 psia with events at <4.5 and <3.5. On a reserve channel it is the quantity's
    full *scale* and the alarms fire inside it — `rcs.propellant_remaining_pct` ranges 0-100 with a
    reserve at <25, which is inside a full span and correct. A rule of the form "an alarm must lie
    outside its channel's range" would therefore refuse 34 legitimate thresholds, which is how the
    ambiguity was found: the check was written, it refused things that were right, and the missing
    piece turned out to be the field rather than the thresholds.

    `range_kind` is that field, and what it buys is this check. For a `band`, an alarm strictly
    inside it fires during normal operation — a cabin that alarms "low" at a pressure its own
    registry calls normal. For a `scale` there is nothing to check, and saying so is the point: the
    reserve ladders in this vehicle are correct *because* their channels are scales, which was an
    implicit argument until it was a declared field.
    """
    for cid, row in sorted(registry.items()):
        rng = row.get("range")
        if not isinstance(rng, list) or len(rng) != 2:
            continue
        if all(v is None for v in rng):
            continue
        if all(isinstance(v, bool) for v in rng):
            continue  # `[true, true]` is the boolean convention: true is the normal value
        if any(v is None for v in rng):
            # A band needs both ends, so a half-bounded range is a scale by construction — and the
            # declaration has to say so, because `[0, null]` is exactly the shape a reader would
            # otherwise take for a band whose ceiling nobody wrote down.
            if row.get("range_kind") != "scale":
                report.refuse(
                    f"channels.yaml:{cid}",
                    f"declares the half-bounded range {rng} and range_kind "
                    f"{row.get('range_kind')!r}. A range with one end is a scale rather than a "
                    "band, and it has to say so: `[0, null]` is otherwise indistinguishable from a "
                    "band whose ceiling nobody wrote down",
                )
            continue
        if not all(isinstance(v, (int, float)) for v in rng):
            report.refuse(f"channels.yaml:{cid}", f"declares the range {rng}, which is not a pair")
            continue
        kind = row.get("range_kind")
        if kind not in {"band", "scale"}:
            report.refuse(
                f"channels.yaml:{cid}",
                f"declares the numeric range {rng} and range_kind {kind!r}. Whether a range is an "
                "acceptable band or a full scale cannot be read from the numbers, and every alarm "
                "on the channel is judged against the answer",
            )
        elif kind == "band" and rng[0] >= rng[1]:
            report.refuse(f"channels.yaml:{cid}", f"declares a band {rng} with no width")


def check_domain(
    path: Path,
    node_ids: set[str],
    index: ChannelIndex,
    positions: dict[str, list[str]],
    report: Report,
    thresholds_by_domain: dict[str, set[str]] | None = None,
    components_elsewhere: set[str] | None = None,
) -> None:
    """One `domains/<name>/`, checked against the vocabulary, the graph and the plant contract.

    A domain is where a specification stops being prose. The checks here are the ones that
    stop a ported spec from arriving in its own dialect, and the ones that catch a domain that
    looks complete but cannot be integrated: a lag with no time constant, a stock with no
    quantum, a latched comparator with no hysteresis, a command whose authority nobody
    declared.
    """
    name = path.name
    docs: dict[str, dict[str, Any]] = {}
    for filename in DOMAIN_FILES:
        file = path / filename
        if not file.exists():
            report.refuse(f"domains/{name}", f"is missing {filename}")
            continue
        loaded = load(file, report)
        if loaded is not None:
            docs[filename] = loaded

    check_fault_components(path, docs, node_ids, components_elsewhere or set(), report)
    check_fault_coverage(path, docs, report)
    check_profiles(path, docs, report)

    components = docs.get("components.yaml") or {}
    where = f"domains/{name}/components.yaml"
    if components.get("domain") != name:
        report.refuse(where, f"declares domain {components.get('domain')!r}, not {name!r}")

    # A domain's own `open_debts` are counted, and this is the third place the same asymmetry has
    # been found: `channels.yaml`'s four have been counted since the section existed, this file's
    # eight were counted when the graph's shopping list turned out to be read by nobody, and the
    # **24 across the eleven domains** were read by nothing at all — not counted, not displayed,
    # not parsed. The crew domain's own note about EVA was sitting in that set while a fleet
    # question about EVA went looking for it. A debt is a value that is needed and unset wherever
    # it is written, and the whole point of the count is that it is fatal under `--strict`; a
    # paragraph in a file that no tool opens is not a debt, it is a comment with better manners.
    for row in components.get("open_debts") or []:
        report.debt(f"domains/{name}/components.yaml:open_debts", str(row))
    for filename in ("profiles.yaml", "fault_policy.yaml", "points.yaml", "commands.yaml"):
        for row in (docs.get(filename) or {}).get("open_debts") or []:
            report.debt(f"domains/{name}/{filename}:open_debts", str(row))
    if not isinstance(components.get("natural_rate_hz"), (int, float)):
        report.debt(
            where,
            "declares no natural_rate_hz; the scheduler ignores it today and the interface "
            "has to be right before the scheduler catches up (plant.md §10)",
        )
    for direction in ("reads", "writes"):
        for node in components.get(direction) or []:
            if node not in node_ids:
                report.refuse(where, f"{direction} {node!r}, which coupling.yaml does not declare")

    # State: the method decides what parameters are owed, and an owed parameter is a debt with
    # a name rather than an invented default (thermal_diode.md:29).
    claimed: dict[str, int] = {}
    for state in components.get("state") or []:
        sid = state.get("id", "?")
        swhere = f"{where}:state {sid}"
        method = state.get("method")
        if method not in METHODS:
            report.refuse(swhere, f"method {method!r} is not one of the six in plant.md §3")
            continue
        prov = state.get("provenance") or {}
        check_basis(swhere, prov.get("basis"), prov, report)
        # Every state says which coupling node it advances, or says `internal` out loud. A
        # state that silently backs nothing is a state the scheduler cannot order, and a node
        # a domain claims to write but never advances is a node that never changes.
        node = state.get("node")
        if node is None:
            report.refuse(swhere, "declares no node; write `node: <id>` or `node: internal`")
        elif node != "internal":
            if node not in (components.get("writes") or []):
                report.refuse(
                    swhere, f"advances {node!r}, which the domain does not declare it writes"
                )
            claimed[node] = claimed.get(node, 0) + 1
        owed = {
            "lag": ("tau_s", "has no time constant, so it cannot be advanced"),
            "stock": ("quantum", "has no quantum, so its conservation cannot be exact"),
            "delay": ("delay_s", "has no delay, so there is nothing to store"),
            "hazard": ("lambda_per_h", "has no hazard rate"),
        }.get(method)
        # An owed parameter is owed whether it is absent or explicitly declared unset; the
        # difference is only whether somebody has thought about it yet.
        if owed and state.get(owed[0]) in (None, "UNCONFIGURED"):
            report.debt(swhere, owed[1])
        if method == "stock" and isinstance(state.get("quantum"), (int, float)):
            quantum = float(state["quantum"])
            flow = state.get("min_flow_per_s")
            dt = 1.0 / float(components.get("natural_rate_hz") or 50)
            if isinstance(flow, (int, float)) and abs(float(flow)) * dt < quantum:
                report.refuse(
                    swhere,
                    f"its smallest flow ({flow}/s over {dt:g} s) is below its quantum "
                    f"({quantum}), so the stock has a dead zone. plant.md §4: the nominal "
                    "cabin leak is below the instrument's own precision and must still happen",
                )
        if method == "discrete" and not state.get("non_latching"):
            # A discrete state is protected from chatter in one of two ways, and they are not
            # interchangeable. A *comparator-driven* latch (a bus undervoltage, a zone limit)
            # needs hysteresis: a recovery band wider than the change that caused the trip,
            # because dwell alone leaves the relay oscillation review-findings.md #7 measured
            # at ~1.7 Hz. A *commanded* state machine (an engine, a valve, a mode) has no
            # comparator to band — what it needs is minimum on and off times, because a
            # machine that can be re-commanded every tick is a machine that chatters on
            # command instead of on noise.
            if state.get("hysteresis"):
                hyst = state["hysteresis"]
                for field in ("assert", "clear", "dwell_ms"):
                    if hyst.get(field) is None:
                        report.debt(swhere, f"its hysteresis has no {field!r}")
            elif state.get("dwell"):
                dwell = state["dwell"]
                for field in ("min_on_s", "min_off_s"):
                    if dwell.get(field) is None:
                        report.debt(swhere, f"its dwell has no {field!r}")
            elif state.get("one_way"):
                # A third protection mechanism, and it is not interchangeable with the other
                # two. A one-way state cannot chatter because it cannot be re-entered, so it
                # needs neither a hysteresis band nor a minimum dwell — what it needs is a
                # guard against being entered *by accident*, which is the arm/commit pattern
                # `apollo_diode.md:476-509` describes and which nothing else on this vehicle
                # uses (conflict C-16). A one-way state with no arming requirement is a state
                # a single misread line can fire.
                if not state.get("requires_arm"):
                    report.refuse(
                        swhere,
                        "is one-way but declares no arming requirement; an irreversible "
                        "transition must be arm-protected or a single misread line fires it "
                        "(apollo_diode.md:167-170)",
                    )
            else:
                report.debt(
                    swhere,
                    "is a discrete state with neither hysteresis nor dwell, so nothing stops "
                    "it chattering; declare one, or `non_latching: true` if it genuinely "
                    "cannot latch",
                )

    for node in components.get("writes") or []:
        if not claimed.get(node):
            report.refuse(
                where,
                f"declares that it writes {node!r} but no state advances it, so the node never "
                "changes",
            )

    # Every unset value anywhere in the domain is a debt with a path attached, not only the
    # ones a state's method happens to require. plant.md §11 generalises
    # `thermal_diode.md:29` — a value that is needed and unset fails the build, naming what
    # wants it — and it applies to a radiator's absorbed load as much as to a time constant.
    # A threshold declares `assert` and `clear` together, and a comparator with no limit has neither:
    # **twenty-six thresholds declared both `UNCONFIGURED`**, which is one missing limit reported as
    # two debts and took the vehicle's headline count from 223 to 249 without a single new unknown.
    # The walk reports the pair once, at the threshold, because one number closes both.
    unset_trails = walk_unset(docs)
    unset_set = set(unset_trails)
    for trail in unset_trails:
        base, _, field = trail.rpartition(".")
        sibling = f"{base}.{'clear' if field == 'assert' else 'assert'}"
        if field == "clear" and sibling in unset_set:
            continue
        if field == "assert" and sibling in unset_set:
            report.debt(
                f"domains/{name}/{trail}", "and its `clear` are UNCONFIGURED — one missing limit"
            )
            continue
        report.debt(f"domains/{name}/{trail}", "is UNCONFIGURED")

    # A `nominal_kg_s` that states its arithmetic is re-derived, on the same rule as every other
    # derived value in the folder. The two cabin supply rates are the first states whose *nominal*
    # operating point is derived from other files' published figures — the leak from `vehicle.yaml`
    # and the metabolic rate from the corpus — so a change to either lands here.
    for state in (docs.get("components.yaml") or {}).get("state") or []:
        if not isinstance(state, dict):
            continue
        # A state that declares the operating point it is sized at, in whatever unit its subject
        # takes: `nominal_kg_s` for the ECLSS flows, `total_w` for the thermal ones. Both are
        # re-derived against their own `computation`, and a new one is added here rather than given
        # a re-derivation of its own so that "a derived value states its arithmetic" stays one rule.
        for field in ("nominal_kg_s", "total_w"):
            if state.get(field) is None:
                continue
            rederive(
                f"domains/{name}/components.yaml:state {state.get('id')}",
                state.get(field),
                (state.get("provenance") or {}).get("computation"),
                report,
            )

    for component in components.get("components") or []:
        cid = component.get("id", "?")
        cwhere = f"{where}:component {cid}"
        if not component.get("kind"):
            report.refuse(cwhere, "has no kind")
        prov = component.get("provenance") or {}
        check_basis(cwhere, prov.get("basis"), prov, report)

    # The quality-assignment function, which is `simulator-design.md:496-508` made checkable.
    #
    # The clause is that quality is assigned only by a function that cannot see the simulator's
    # fault state, and nothing in the corpus implemented it — `corpus-review.md` §6 lists it
    # among what nobody wrote. `avionics_diode.md:371-388` is a function that satisfies it, and
    # the two ways to break the property are both mechanical: read a fault-state parameter, or
    # emit a code that is not a quality. The second is the subtle one — the document's own
    # function returns FAULT from the same place it returns SUSPECT, and FAULT is refused as a
    # quality by design.md:211-214.
    quality = components.get("quality_assignment")
    if isinstance(quality, dict):
        qwhere = f"{where}:quality_assignment"
        params = {str(p) for p in quality.get("parameters") or []}
        forbidden = {str(p) for p in quality.get("forbidden_parameters") or []}
        if not params:
            report.refuse(qwhere, "declares no parameters, so the signature is not a signature")
        for bad in sorted(params & forbidden):
            report.refuse(
                qwhere,
                f"takes {bad!r} as a parameter: a quality function that can see that cannot "
                "satisfy simulator-design.md:496-508, and a silently biased sensor would stop "
                "being GOOD",
            )
        if not quality.get("signature"):
            report.refuse(qwhere, "declares no signature")
        for rule in quality.get("rules") or []:
            code = rule.get("quality")
            if not rule.get("condition"):
                report.refuse(qwhere, "a rule has no condition")
            if code not in QUALITY_CODES:
                report.refuse(
                    qwhere,
                    f"a rule emits {code!r}, which is not a canonical quality code. A conclusion "
                    "belongs in `conclusions`, not in a quality: design.md:211-214 makes that "
                    "split the difference between withholding a diagnosis and publishing one",
                )
        if not quality.get("rules"):
            report.refuse(qwhere, "declares no rules")
        if not quality.get("conclusions"):
            report.refuse(
                qwhere,
                "declares no conclusions, so the trip-counter verdict has nowhere to go and a "
                "quality code would have to carry it",
            )
        # `not_a_quality` is documentation and is checked in the opposite direction: every code
        # listed there must genuinely *not* be a quality, or the list is wrong about the
        # vocabulary it exists to protect.
        for entry in quality.get("not_a_quality", {}).get("codes") or []:
            code = entry.get("code")
            if not entry.get("why"):
                report.refuse(qwhere, f"rejects {code!r} without saying why")
            if code in QUALITY_CODES:
                report.refuse(
                    qwhere, f"lists {code!r} as not-a-quality, but it is a canonical quality code"
                )

    # The diagnostic inventory: a function with no output channel is a function whose result
    # nobody can see, and a declared output that is not registered is a result that goes nowhere.
    for diagnostic in components.get("diagnostics") or []:
        dwhere = f"{where}:diagnostic {diagnostic.get('id')}"
        for field in ("id", "rule", "watches", "outputs"):
            if not diagnostic.get(field):
                report.refuse(dwhere, f"declares no {field}")
        for output in diagnostic.get("outputs") or []:
            if isinstance(output, str) and "." in output and output not in index:
                report.refuse(dwhere, f"produces {output!r}, which is not a registered channel")

    # Points may not invent channels: the registry in channels.yaml is the vocabulary, and a
    # domain that needs a new one registers it there rather than forking it here.
    points = docs.get("points.yaml") or {}
    all_state_ids = {str(s.get("id")) for s in components.get("state") or []}
    other_state_ids = {
        str(s.get("id"))
        for sibling in sorted(p for p in path.parent.iterdir() if p.is_dir() and p != path)
        for s in (load(sibling / "components.yaml", Report()) or {}).get("state") or []
    }
    for point in points.get("points") or []:
        cid = point.get("channel") if isinstance(point, dict) else point
        pwhere = f"domains/{name}/points.yaml:{cid}"
        if not cid:
            report.refuse(f"domains/{name}/points.yaml", "a point declares no channel")
        elif cid not in index:
            report.refuse(pwhere, "is not a registered channel in channels.yaml")
        # `from` names what the point reads, and it has to be something that exists: a state in
        # this domain, or a coupling node the domain reads. It is not decoration — the enum
        # binding above compares a channel's vocabulary with its source's, and a `from` that
        # resolves to nothing silently skips that comparison. Two of the three failures this
        # found were exactly that: `power.battery_temp_c` named a state that exists nowhere, and
        # the crew's switch and breaker channels named *the hatch*, a state in another domain
        # that has nothing to do with them.
        source = point.get("from") if isinstance(point, dict) else None
        if not source or not isinstance(source, str):
            continue
        if source in all_state_ids or source in node_ids:
            continue
        if source in other_state_ids:
            report.refuse(
                pwhere,
                f"reads {source!r}, which is a state in another domain. A point reads a state it "
                "owns or a coupling node; reading another domain's state directly bypasses the "
                "edge that is supposed to carry the value",
            )
        else:
            report.refuse(
                pwhere,
                f"reads {source!r}, which is neither a state in this domain nor a coupling node — "
                "so nothing produces it and the enum binding silently skips this point",
            )

    # A channel that publishes a discrete state's value must offer the same value set the state
    # can take, or the vehicle publishes a vocabulary it does not use. This is the same binding
    # as the phase, posture, crew-station and antenna checks, generalised to every domain: the
    # point says `from: <state>` and the two `enum[...]` lists have to be one list. It cost
    # nothing to add because all eleven domains already agreed — which is the point of adding it
    # now rather than after the first domain that does not.
    states_by_id = {str(s.get("id")): s for s in components.get("state") or []}
    for point in points.get("points") or []:
        if not isinstance(point, dict):
            continue
        cid, source = point.get("channel"), point.get("from")
        state = states_by_id.get(str(source))
        row = index.row(str(cid)) if cid else None
        if state is None or row is None:
            continue
        chan_enums = re.findall(r"enum\[([^\]]*)\]", str(row.get("unit") or ""))
        state_enums = re.findall(r"enum\[([^\]]*)\]", str(state.get("unit") or ""))
        if not chan_enums or not state_enums:
            # A `bool` state is a two-valued vocabulary like any other, and leaving it out of this
            # check is the third time in this file that an unhandled *type* made a check pass by
            # default rather than fail — after the conservation guard's `if a and b` and the six
            # node units with no dimension. `bus_tie_closed` was `unit: bool` while publishing
            # `power.bus_tie_state` as `enum[open,closed,tripped]`, so the tie could not represent
            # `tripped` — the state its own note says it latches into on a ground fault. The
            # vehicle could be in a condition it was structurally unable to report, which is the
            # same fault as the regulator position and the stage configuration, arriving through a
            # hole rather than through a disagreement.
            if str(state.get("unit") or "") == "bool" and chan_enums:
                offered = {m.strip() for m in chan_enums[0].split(",")}
                if len(offered) > 2:
                    report.refuse(
                        f"domains/{name}/points.yaml:{cid}",
                        f"publishes {sorted(offered)} from `{source}`, which is a `bool`: a "
                        "two-valued state cannot hold a value the channel offers, so the vehicle "
                        "can be in a condition it cannot report",
                    )
            continue
        chan_members = {m.strip() for m in chan_enums[0].split(",")}
        state_members = {m.strip() for m in state_enums[0].split(",")}
        if chan_members == state_members:
            continue
        if not chan_members & state_members:
            # Disjoint vocabularies are a projection rather than a fork: `cw.active_lights`
            # reports which system lamps are lit, which is derived from the alert lifecycle and
            # is not the lifecycle's own value set. Worth a note because a *sibling* channel that
            # looks like a projection may not be one, and no rule can tell without reading it.
            report.note(
                f"domains/{name}/points.yaml:{cid}",
                f"publishes {sorted(chan_members)}, a vocabulary disjoint from `{source}`'s "
                f"{sorted(state_members)} — treated as a projection of that state rather than a "
                "republication of it",
            )
        else:
            report.refuse(
                f"domains/{name}/points.yaml:{cid}",
                f"publishes {sorted(chan_members)} but `{source}` can take "
                f"{sorted(state_members)}: the vehicle would report a value its own state cannot "
                "hold, or hold one it never reports",
            )

    commands = docs.get("commands.yaml") or {}
    same_thresholds = (thresholds_by_domain or {}).get(name, set())
    other_thresholds = thresholds_by_domain or {}
    for verb in commands.get("commands") or []:
        vid = verb.get("verb", "?")
        vwhere = f"domains/{name}/commands.yaml:{vid}"
        if vid != vid.lower() or not re.fullmatch(r"[a-z][a-z0-9_]*", str(vid)):
            report.refuse(vwhere, "is not lowercase_snake_case (vocabulary V-06, apollo's style)")
        # The forbidden forms are refused by *name*, in the registry, so that declining a
        # capability and forgetting to remove its verb cannot look the same. The declined lists
        # below are allowed to name them: a refusal with a reason is documentation.
        for pattern, why in FORBIDDEN_VERB.items():
            if re.match(pattern, str(vid)):
                report.refuse(vwhere, f"is a forbidden form: {why}")
        if verb.get("authority") not in AUTHORITIES:
            report.refuse(
                vwhere, f"authority {verb.get('authority')!r} is not one of {sorted(AUTHORITIES)}"
            )
        # D-03: a gate is an agent-writable preference, an interlock is service-owned, and the
        # two must be separately declared or the probe cannot tell them apart.
        if "gate" not in verb:
            report.refuse(vwhere, "declares no gate (D-03)")
        if "interlocks" not in verb:
            report.refuse(
                vwhere,
                "declares no interlocks; write `interlocks: none` deliberately if there are none",
            )
        # The argument schema is what `capability.snapshot` publishes so a machine can build a
        # call (vocabulary §1, `communcations_diode.md:436-452`). Four domains wrote the schema
        # keys at the verb's own indent under an empty `argument_schema:`, which parses, passes
        # every other check, and advertises an argument-less verb — a fleet would discover the
        # bug by calling it wrong.
        if not isinstance(verb.get("argument_schema"), dict):
            report.refuse(
                vwhere,
                f"argument_schema is {verb.get('argument_schema')!r}, not a mapping; an empty "
                "schema advertises a verb with no arguments",
            )
        # A gate *template* is a name a fleet never sees. `presentation.yaml#mirror` says so and
        # gives the reason §9's check 5 depends on it: "a refusal that named the template rather
        # than the instantiation — `reserve_floor_<resource>_enable` instead of
        # `reserve_floor_water_cooling_enable` — would be a name a fleet cannot act on." So every
        # placeholder has to be expandable, and the thing that makes it expandable is that it names
        # an enum argument of its own verb. Ten verbs were not: eight wrote a bare `<id>` where the
        # argument was `antenna`, `source`, `load`, `breaker`, `battery`, `engine`, `pump`, `hatch`
        # or `loop`, and two declared their values as a *sentence* — `[any id in
        # components.yaml#loads]` — which instantiates to nothing at all. Both are refusals now,
        # because a gate the mirror cannot publish is a closed gate a fleet cannot name.
        gate = verb.get("gate")
        if isinstance(gate, dict) and gate.get("variable"):
            variable = str(gate["variable"])
            schema = verb.get("argument_schema") or {}
            for placeholder in re.findall(r"<([^>]+)>", variable):
                spec = schema.get(placeholder)
                if not isinstance(spec, dict) or spec.get("type") != "enum":
                    report.refuse(
                        vwhere,
                        f"declares gate {variable!r}, whose placeholder <{placeholder}> names no "
                        f"enum argument of this verb (it has {sorted(schema)}). The mirror expands "
                        "the template by name, so a placeholder that names nothing is a gate with "
                        "no instantiation",
                    )
                    continue
                values = spec.get("values") or []
                if not values:
                    report.refuse(
                        vwhere,
                        f"declares argument {placeholder!r} as an empty enum, so {variable!r} cannot expand",
                    )
                elif len(values) == 1 and isinstance(values[0], str) and " " in values[0]:
                    report.refuse(
                        vwhere,
                        f"declares argument {placeholder!r} by describing its values ({values[0]!r}) "
                        f"rather than listing them, so {variable!r} has no instantiation and the "
                        "mirror would publish a gate variable with a sentence in it",
                    )
        # `avionics_diode.md:496-508` is the only place in the corpus with an ordering argument
        # for an authorization chain, and the two clauses it puts last are the two that apply
        # only to irreversible commands: a valid prepare token, and a *synchronised clock* —
        # `require(vehicle.time_quality == SYNC, "TIME_UNTRUSTED")`. The reason is specific
        # rather than ceremonial: an irreversible action with a time-tagged deadline has that
        # deadline measured against the vehicle's clock, so a drifted clock is a one-shot with an
        # unknown arming window. `mission_diode.md`'s eight-term predicate has no such term.
        if verb.get("irreversible") and "requires_time_sync" not in verb:
            report.refuse(
                vwhere,
                "is irreversible but does not say whether it requires a synchronised clock "
                "(avionics_diode.md:496-508); a time-tagged irreversible action on a drifted "
                "clock has an unknown deadline",
            )
        # An interlock that names nothing is an interlock that is never evaluated, and the
        # failure is silent in the worst way: the verb declares a guard, the executive looks it
        # up, finds nothing, and either passes or crashes. Three domains wrote a flat
        # `other_domain_thing` that resolves nowhere; the vocabulary's own example is dotted
        # (`thermal.pump_dry_run`), so a bare name means this domain and a qualified name means
        # that one.
        interlocks = verb.get("interlocks")
        if isinstance(interlocks, list):
            for ref in interlocks:
                if not isinstance(ref, str):
                    report.refuse(vwhere, f"interlock {ref!r} is not a name")
                    continue
                owner, dot, ref_id = ref.partition(".")
                # A qualifier is a domain, and a domain answers to both of its names: the
                # directory it lives in and the channel prefix it publishes under. Requiring
                # one of them would make `consumables.cooling_water_reserve` and
                # `res.cooling_water_reserve` a spelling test instead of a lookup.
                if dot:
                    owner = PREFIX_DOMAIN.get(owner, owner)
                if not dot:
                    if ref not in same_thresholds:
                        report.refuse(
                            vwhere,
                            f"interlock {ref!r} resolves to no threshold in this domain, and it "
                            "is not qualified with another domain's name",
                        )
                elif owner not in DOMAINS:
                    report.refuse(vwhere, f"interlock {ref!r} names no canonical domain")
                elif ref_id not in other_thresholds.get(owner, set()):
                    report.refuse(
                        vwhere,
                        f"interlock {ref!r} resolves to no threshold in domains/{owner}/",
                    )

    # A refusal has to be readable, or declining a verb and forgetting to decline it look the
    # same. The shape matters because of how this list fails: an entry indented one level too
    # deep is absorbed into the previous entry's block scalar, so the YAML parses, the verb
    # disappears, and the file still reads as though it is there. That is how
    # `set_engine_valve`, `set_thermal_limit`, `set_telemetry_profile` and four consumables
    # verbs went missing at once — six absent entries in a file that looked complete.
    # `declined` and `not_implemented` are both in use and both checked; the canonical name is
    # the vocabulary's, and a second name is a dialect until it is reconciled.
    registered = {str(v.get("verb")) for v in commands.get("commands") or []}
    for key in ("not_implemented", "declined"):
        for refused in commands.get(key) or []:
            rwhere = f"domains/{name}/commands.yaml:{key}"
            if not isinstance(refused, dict) or not refused.get("verb"):
                report.refuse(rwhere, f"an entry names no verb: {refused!r}")
                continue
            if not refused.get("why"):
                report.refuse(f"{rwhere} {refused['verb']}", "gives no reason for the refusal")
            # A verb cannot be both registered and refused *by the same domain*: the registry
            # would offer it and the help text would deny it, and which one a fleet believed
            # would depend on which file it read. Declining a verb another domain owns is
            # legitimate and is a different statement — "not mine" rather than "not this
            # vehicle's" — which is why the check is scoped to this domain and not to the vehicle.
            if str(refused["verb"]) in registered:
                report.refuse(
                    f"{rwhere} {refused['verb']}",
                    "is both registered and declined in this domain, so one file offers the verb "
                    "and the other denies it",
                )

    profiles = docs.get("profiles.yaml") or {}
    for threshold in profiles.get("thresholds") or []:
        tid = threshold.get("id", "?")
        twhere = f"domains/{name}/profiles.yaml:{tid}"
        point = threshold.get("point")
        if point and point not in index:
            report.refuse(twhere, f"watches {point!r}, which is not a registered channel")
        elif point:
            # Invariant D, made mechanical: a guard whose evidence has no maximum age cannot be
            # evaluated for staleness, so a hazardous effect could proceed on a reading from an
            # hour ago and nothing would object. This is the check that would have caught the
            # registry's own `decision_age_ms` table — nineteen of the fifty channels a
            # threshold watches publish more slowly than their priority default demanded, so
            # every one of those guards was unsatisfiable in principle.
            row = index.row(point) or {}
            age = decision_age_ms(row)
            if age is None:
                report.refuse(
                    twhere,
                    f"watches {point!r}, which resolves to no maximum decision age: it "
                    "publishes only on events and declares no `max_decision_age_ms`, so the "
                    "guard cannot tell a fresh sample from one that will never arrive "
                    "(mission_diode.md:1292, invariant D)",
                )
            else:
                rate = row.get("rate_hz")
                period = 1000.0 / float(rate) if isinstance(rate, (int, float)) and rate else None
                if period is not None and age < period:
                    report.refuse(
                        twhere,
                        f"watches {point!r}, whose maximum decision age ({age:g} ms) is shorter "
                        f"than one publish period ({period:g} ms): every sample would be stale "
                        "on arrival, so the guard can never pass",
                    )
        # §10 has promised this since it was written — "**a code not in the union** — a quality,
        # kind, severity, lifecycle state or priority that is not one of the above" — and `severity`
        # is the field that decides what a crew actually *sees*. It is declared 138 times and was
        # read by nothing: the linter's only mentions of the word were in comments. The four
        # annunciated levels are `02-canonical-vocabulary.md` §6, and `INFO` is the non-annunciated
        # record class, which "exists only as a **non-annunciated** record class — it never lights a
        # panel". The data is correct today; nothing was keeping it correct.
        severity = threshold.get("severity")
        if severity not in SEVERITIES:
            report.refuse(
                twhere,
                f"declares severity {severity!r}, which is not in the V-04 ladder "
                f"{sorted(SEVERITIES)} (§6). A severity outside it is an alert that lights at no "
                "level a crew is trained to read",
            )
        # A band is an acceptable operating region, so an alarm strictly inside it fires during
        # normal operation — a cabin that alarms "low" at a pressure its own registry calls normal.
        # The comparison needs `range_kind` to exist at all, which is why this check could not be
        # written until that field did: `range` alone carries two meanings and the numbers do not
        # distinguish them.
        #
        # Two exemptions, and both are the threshold *saying* it measures something else. A
        # `point_units` that differs from the channel's own unit covers the rate thresholds — six
        # of them watch a level channel with a per-minute limit — and `gated_by` covers a threshold
        # the schema forced onto a channel it is not really about. Without one of the two, an alarm
        # inside its own band has nothing to explain it.
        point_row = index.row(str(point)) if point else None
        if isinstance(point_row, dict) and point_row.get("range_kind") == "band":
            band = point_row.get("range")
            assert_v = threshold.get("assert")
            comparator_here = threshold.get("comparator")
            numeric_band = (
                isinstance(band, list)
                and len(band) == 2
                and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in band)
            )
            explained = bool(threshold.get("point_units")) or bool(threshold.get("gated_by"))
            if (
                numeric_band
                and isinstance(assert_v, (int, float))
                and not isinstance(assert_v, bool)
                and not explained
            ):
                low, high = band
                inside = (comparator_here == "above" and assert_v < high) or (
                    comparator_here == "below" and assert_v > low
                )
                if inside:
                    report.refuse(
                        twhere,
                        f"asserts {comparator_here} {assert_v} on {point!r}, whose declared band is "
                        f"{band} and whose `range_kind` is `band` — so this alarm fires while the "
                        "channel is inside the region its own registry calls acceptable. Either the "
                        "band is wrong or the threshold measures something other than the channel's "
                        "own quantity, in which case `point_units` says so",
                    )
        comparator = threshold.get("comparator")
        if comparator not in {"above", "below"}:
            report.refuse(twhere, f"comparator {comparator!r} is neither 'above' nor 'below'")
            continue
        if threshold.get("latched") or threshold.get("hysteresis"):
            assert_v, clear_v = threshold.get("assert"), threshold.get("clear")
            if not isinstance(assert_v, (int, float)) or not isinstance(clear_v, (int, float)):
                report.debt(twhere, "is latched but its assert and clear values are not both set")
            elif comparator == "above" and clear_v >= assert_v:
                report.refuse(
                    twhere,
                    f"hysteresis is inverted: above-comparator with clear {clear_v} >= assert {assert_v}",
                )
            elif comparator == "below" and clear_v <= assert_v:
                report.refuse(
                    twhere,
                    f"hysteresis is inverted: below-comparator with clear {clear_v} <= assert {assert_v}",
                )
            for field in ("dwell_assert_ms", "dwell_clear_s"):
                if threshold.get(field) is None:
                    report.debt(twhere, f"is latched but declares no {field}")

    # The display contract and the channel registry's perception bound must agree.
    #
    # They say different things on purpose: `channels.yaml#crew_positions` says which published
    # channels are perceptible from a position, and the domain's `display_contract` says what an
    # instrument in front of a person actually displays, at what precision. A readout listed in
    # one and absent from the other is a leak in the perception bound — the crew would be
    # reporting something they cannot see, which is the exact failure `review-findings.md` #4
    # says cannot be cleaned up retroactively.
    contract = components.get("display_contract") or {}
    for position in contract.get("positions") or []:
        pid = position.get("id")
        pwhere = f"{where}:display_contract position {pid}"
        if pid not in positions:
            report.refuse(pwhere, "is not a declared crew position in channels.yaml")
            continue
        allowed = set(positions[pid])
        for panel in position.get("panels") or []:
            for readout in panel.get("shows") or []:
                cid = readout.get("channel") if isinstance(readout, dict) else readout
                if cid not in index:
                    report.refuse(
                        f"{pwhere}/{panel.get('id')}",
                        f"shows {cid!r}, which is not a registered channel",
                    )
                elif cid not in allowed:
                    report.refuse(
                        f"{pwhere}/{panel.get('id')}",
                        f"shows {cid!r}, which this position cannot perceive; the display "
                        "contract and the perception bound disagree, and the crew would be "
                        "reporting something they cannot see",
                    )

    # The station vocabulary is written in three places and must be one list: the bound's
    # `crew_positions`, the `crew.location_[id]` channel that indexes a report to a station, and
    # the state that says where a person actually is. The third decides which bound gets applied
    # at runtime, so a name that exists in only two of them is a station a fleet can be told
    # about and can never be answered from. The state is found by *shape* — a per-crew-member
    # enum that overlaps the station list — rather than by id, because a check that goes silent
    # when somebody renames the state is a check that has already failed once.
    if name == "crew" and positions:
        for st in components.get("state") or []:
            unit = str(st.get("unit") or "")
            if "crew_id" not in unit:
                continue
            enum = re.findall(r"enum\[([^\]]*)\]", unit)
            listed = [member.strip() for member in enum[0].split(",")] if enum else []
            if not set(listed) & set(positions):
                continue  # per-crew-member, but not a station vocabulary
            for missing in sorted(set(positions) - set(listed)):
                report.refuse(
                    f"{where}:state {st.get('id')}",
                    f"omits station {missing!r}, which the perception bound describes: a crew "
                    "member standing there would be answered from a bound that does not exist",
                )
            for extra in sorted(set(listed) - set(positions)):
                report.refuse(
                    f"{where}:state {st.get('id')}",
                    f"offers station {extra!r}, which channels.yaml:crew_positions does not "
                    "describe",
                )

    # A one-way event's `observable` list is the same kind of claim as a fault's `perturbs`:
    # it says which published channels would reveal that the vehicle became a different
    # vehicle. An event that names an unregistered channel names an observation nobody can
    # make, which is how an irreversible action becomes invisible.
    for event in components.get("one_way_events") or []:
        ewhere = f"{where}:one_way_event {event.get('id')}"
        if not event.get("verb"):
            report.refuse(ewhere, "names no verb that fires it")
        if event.get("arm_required") is None:
            report.refuse(ewhere, "does not say whether it needs arming")
        for cid in event.get("observable") or []:
            if cid not in index:
                report.refuse(ewhere, f"observable {cid!r} is not a registered channel")

    # --------------------------------------------------------------------------------------
    # The truth boundary, which is the one invariant the whole design rests on.
    #
    # `plant.md` §7: "The plant owns hidden truth. **The instruments turn `T` into `A`; the
    # publisher turns `A` into files.**" Every domain's `points.yaml#not_published` is where the
    # vehicle says by name which truths it is withholding — 44 declarations, and this file did not
    # read one of them. `presentation.yaml` calls it "where the vehicle says which truths it is
    # withholding" and the epistemic mapping's `T` rule points at it; nothing checked it. A
    # channel that is declared hidden *and* registered is truth on the wire, and the declaration
    # that would have said so was the one nobody read. Nothing leaks today, which is the point:
    # the corpus happens to be right and there is nothing keeping it right.
    # --------------------------------------------------------------------------------------
    withheld = 0
    descriptive = 0
    for entry in points.get("not_published") or []:
        names = entry.get("channel") if isinstance(entry, dict) else entry
        reason = entry.get("why", "") if isinstance(entry, dict) else ""
        for withheld_name in names if isinstance(names, list) else [names]:
            text = str(withheld_name)
            if "." not in text:
                # An entry that is a *description* rather than a name — "hidden truth", "a
                # conclusion", "the battery's true capacity after a derate". It states a category
                # rather than a channel, so no rule can check it and the count is reported instead
                # of pretending otherwise.
                descriptive += 1
                continue
            withheld += 1
            where_np = f"domains/{name}/points.yaml:not_published {text!r}"
            if not reason:
                report.refuse(
                    where_np, "is withheld with no `why`, so nobody can review the choice"
                )
            # **Exact keys, not `ChannelIndex`.** The index compiles `[id]` to `.+?`, which is
            # unbounded, so `thermal.zone_[id]_true_t_c` resolves against the registry's
            # `thermal.zone_[id]_t_c` — the wildcard absorbs `1_true` and the literal `_t_c` then
            # matches. That is a false *positive* here and a false *negative* everywhere the index
            # answers "is this a registered channel": `power.lcl_1_old_state` resolves to
            # `power.lcl_[n]_state`'s row. A withheld truth has to be named exactly, so this uses
            # the registry's own keys and the weakness is recorded rather than depended on.
            if text in index.rows:
                report.refuse(
                    where_np,
                    "is declared withheld and is a registered channel. A hidden truth that is "
                    "registered is truth on the wire: this is the §7 boundary, and the declaration "
                    "that says so is the one nothing was reading",
                )
    if withheld or descriptive:
        report.note(
            f"domains/{name}/points.yaml:not_published",
            f"withholds {withheld} named channel(s) and {descriptive} described categor"
            f"{'y' if descriptive == 1 else 'ies'} from the fleet",
        )

    fault_policy = docs.get("fault_policy.yaml") or {}
    for fault in fault_policy.get("faults") or []:
        fid = fault.get("id", "?")
        fwhere = f"domains/{name}/fault_policy.yaml:{fid}"
        for field in ("component", "mechanism"):
            if not fault.get(field):
                report.refuse(fwhere, f"has no {field}")
        # review-findings.md #11: an aggregate model is legitimate only if it can produce every
        # fault signature in its own policy, which requires each fault to name the channels it
        # perturbs. A fault that perturbs nothing observable is a fault nobody can diagnose.
        perturbs = fault.get("perturbs") or []
        if not perturbs:
            report.refuse(
                fwhere, "names no published channel it perturbs, so it cannot be diagnosed"
            )
        for cid in perturbs:
            if cid not in index:
                report.refuse(fwhere, f"perturbs {cid!r}, which is not a registered channel")

        # Everything below was read by nothing until this round, and that is why two schema
        # dialects and four broken entries lived in the corpus unnoticed. `component`, `mechanism`
        # and `perturbs` were checked; `kind`, `seeding`, `detection` and `response` — the whole
        # diagnostic half of all 118 faults — were not. A field no tool reads is a field that
        # drifts, and these had drifted into eleven kinds, two placements for `response`, and four
        # faults whose `detection:` was an empty key with its three children left at fault level.
        kind = fault.get("kind")
        if kind not in FAULT_KINDS:
            report.refuse(
                fwhere,
                f"declares kind {kind!r}, which is not in the fault-kind union "
                f"(02-canonical-vocabulary.md §9b): {sorted(FAULT_KINDS)}",
            )
        seeding = fault.get("seeding")
        if not isinstance(seeding, dict) or not any(form in seeding for form in SEEDING_FORMS):
            report.refuse(
                fwhere,
                f"declares seeding {seeding!r}, which names none of {list(SEEDING_FORMS)}; a fault "
                "that does not say how it can occur cannot be scheduled",
            )
        elif "hazard" in seeding and not seeding.get("unit"):
            report.refuse(
                fwhere, "seeds on a hazard rate with no `unit`; a rate without a unit is not a rate"
            )
        detection = fault.get("detection")
        if not isinstance(detection, dict):
            report.refuse(
                fwhere,
                f"declares detection {detection!r}, not a mapping. This is the shape an absorbed "
                "list item leaves behind: an empty `detection:` key with its children at fault "
                "level, which four faults had",
            )
        else:
            for field in ("evidence", "latency", "false_positive_risk"):
                if not detection.get(field):
                    report.refuse(fwhere, f"declares no detection.{field}")
            # The fork that was repaired this round: 41 faults nested `response` inside
            # `detection` and 77 put it at fault level, so half the corpus's responses were in a
            # place the other half did not use. A response is what the vehicle does about the
            # fault, not part of detecting it.
            if "response" in detection:
                report.refuse(
                    fwhere,
                    "nests `response` inside `detection`; the response is what the vehicle does "
                    "about the fault, and 41 faults had it here while 77 had it at fault level",
                )
        response = fault.get("response")
        if not isinstance(response, dict) or response.get("kind") not in FAULT_RESPONSES:
            report.refuse(
                fwhere,
                f"declares response {response!r}; every fault must say whether the service acts "
                f"without asking or an agent decides, one of {sorted(FAULT_RESPONSES)}",
            )
        elif response.get("kind") == "service" and not response.get("note"):
            report.refuse(
                fwhere,
                "has a `service` response and no note. A service response acts without asking "
                "(electrical_diode.md:845), so the note is the only place its action is stated",
            )


def check_outbound_extremes(
    root: Path, report: Report, policies: dict[str, dict[str, Any]]
) -> None:
    """A domain that claims the vehicle's largest or smallest cross-domain reach is checked.

    Every other coverage claim is a statement about one domain, which is why this one is separate:
    `outbound_extreme` is a claim about **all eleven**, and it is the kind of claim that rots
    without anyone touching it — the domain that made it stays still while another domain's faults
    grow past it. That is exactly what happened. `avionics` said it had "the highest cross-domain
    reach of any domain on the vehicle", and by round 44 `eclss` had three times as many, because
    a life-support failure reaches every domain that plans around a consumable.

    `gnc` makes the opposite claim and it is the domain's thesis rather than a boast: one fault of
    eleven crosses a boundary, against `eclss`'s twelve, because navigation is a consumer of the
    vehicle rather than a component of it. That reading is worth holding to a count, since the
    whole `ladder_coupling` argument rests on the failures being visible through degraded
    knowledge rather than through spilled channels.
    """
    counts: dict[str, int] = {}
    for name, policy in policies.items():
        prefix = DOMAIN_PREFIX.get(name, name)
        counts[name] = len(
            {
                str(fault.get("id"))
                for fault in policy.get("faults") or []
                if any(
                    not re.match(rf"^{re.escape(prefix)}[._]", str(c))
                    for c in fault.get("perturbs") or []
                )
            }
        )
    for name, policy in sorted(policies.items()):
        coverage = policy.get("coverage") or {}
        block = coverage.get("cross_domain")
        if not isinstance(block, dict):
            continue
        claim = block.get("outbound_extreme")
        if claim not in ("lowest", "highest"):
            continue
        best = (min if claim == "lowest" else max)(counts, key=lambda k: counts[k])
        if best != name:
            report.refuse(
                f"domains/{name}/fault_policy.yaml:coverage.cross_domain",
                f"claims the vehicle's {claim} cross-domain reach, and `{best}` has it "
                f"({counts[best]} against this domain's {counts[name]}). A superlative is a claim "
                "about all eleven domains, so it is the one that rots while nobody touches it",
            )


def check_domains(
    root: Path,
    registry: dict[str, dict[str, Any]],
    coupling: dict[str, Any] | None,
    channels_doc: dict[str, Any] | None,
    report: Report,
) -> None:
    """Every `domains/<name>/` composes, and the linter names the ones still owed."""
    node_ids = set((coupling or {}).get("nodes") or {})
    index = ChannelIndex(registry)
    positions = {
        p.get("id"): [str(c) for c in (p.get("perceivable") or [])]
        for p in (channels_doc or {}).get("crew_positions") or []
    }
    domains_dir = root / "domains"
    present: set[str] = set()
    # A verb's interlocks may name its own thresholds or another domain's, so the whole set is
    # collected before any domain is checked: `set_vent_valve` refusing to vent while a hatch is
    # open is a claim about `domains/structure/`, and a check that only saw its own directory
    # could not tell a cross-domain interlock from a typo.
    thresholds_by_domain: dict[str, set[str]] = {}
    # The same reasoning applies to a fault's `component`, and the same surprise arrived with it:
    # the field may name another domain's article. `CNS-08` is a consumables fault that breaks the
    # *ECLSS* lithium-hydroxide element, which is the normal shape of a cross-domain fault rather
    # than an error, so the vehicle's components and states are collected before the loop too.
    components_elsewhere: set[str] = set()
    policies: dict[str, dict[str, Any]] = {}
    if domains_dir.is_dir():
        for path in sorted(p for p in domains_dir.iterdir() if p.is_dir()):
            profiles = load(path / "profiles.yaml", report) or {}
            thresholds_by_domain[path.name] = {
                str(t.get("id")) for t in profiles.get("thresholds") or [] if t.get("id")
            }
            parts = load(path / "components.yaml", report) or {}
            components_elsewhere |= {
                str(entry["id"])
                for key in ("components", "state")
                for entry in (parts.get(key) or [])
                if isinstance(entry, dict) and entry.get("id")
            }
            policies[path.name] = load(path / "fault_policy.yaml", report) or {}
    if domains_dir.is_dir():
        for path in sorted(p for p in domains_dir.iterdir() if p.is_dir()):
            present.add(path.name)
            check_domain(
                path,
                node_ids,
                index,
                positions,
                report,
                thresholds_by_domain,
                components_elsewhere,
            )
    check_outbound_extremes(root, report, policies)
    # ----------------------------------------------------------------------------------
    # Intra-node ordering. A node is advanced by one or more states, and when it is more
    # than one, *which advances first is a modelling decision that nothing declared*.
    #
    # This is not a formality, because the tiebreak actively gets it wrong: the frozen
    # lexicographic rule sorts `link_snr` before `tx_power`, and transmit power is a term in
    # the link budget — so the derived order would compute the signal-to-noise ratio from
    # last tick's power and call it this tick's. Ten nodes are in this position, and silence
    # about them means the alphabet decides.
    #
    # So each node with more than one producing state declares either an order or that it has
    # none. `independent` is a permitted answer and it is a *claim*: four conserved gas masses
    # in one compartment genuinely do not care which advances first, three engine state
    # machines do not either, and saying so is better than an arbitrary list that reads as a
    # finding.
    # ----------------------------------------------------------------------------------
    by_node: dict[str, list[tuple[str, str]]] = {}
    if domains_dir.is_dir():
        for path in sorted(p for p in domains_dir.iterdir() if p.is_dir()):
            components = load(path / "components.yaml", Report()) or {}
            for state in components.get("state") or []:
                node = str(state.get("node"))
                if node and node != "internal":
                    by_node.setdefault(node, []).append((path.name, str(state.get("id"))))
    node_docs = (coupling or {}).get("nodes") or {}
    for node, producers in sorted(by_node.items()):
        if len(producers) < 2:
            continue
        where = f"coupling.yaml:node {node}"
        declared = node_docs.get(node) or {}
        order = declared.get("state_order")
        names = sorted(state_id for _, state_id in producers)
        if order is None:
            report.refuse(
                where,
                f"is advanced by {len(producers)} states ({', '.join(names)}) and declares no "
                "`state_order`, so the frozen lexicographic tiebreak decides which advances "
                "first — and for this node that may be the wrong one",
            )
            continue
        if order == "independent":
            if not declared.get("state_order_note"):
                report.refuse(
                    where,
                    "declares its state order independent without a reason. Independence is a "
                    "claim about the physics and it needs to be on the record",
                )
            else:
                report.note(
                    f"coupling.yaml:node {node}", "advances " + ", ".join(names) + " in any order"
                )
            continue
        if not isinstance(order, list):
            report.refuse(
                where, f"declares state_order {order!r}, which is neither a list nor 'independent'"
            )
            continue
        if sorted(str(s) for s in order) != names:
            report.refuse(
                where,
                f"declares state_order {sorted(str(s) for s in order)} but the node is advanced "
                f"by {names}: the list has to be the group, in the order it advances",
            )
            continue
        # The declared order is the group's; report it so the derived schedule can be read.
        report.note(
            f"coupling.yaml:node {node}",
            "advances " + " then ".join(str(s) for s in order),
        )

    for missing in sorted(EXPECTED_DOMAINS - present):
        report.debt(
            f"domains/{missing}",
            "has no specification yet; the corpus has one and it has not been landed",
        )


def check_vehicle_sections(doc: dict[str, Any], report: Report) -> None:
    """Every top-level section of `vehicle.yaml` is one something reads, and none is empty.

    This check exists because of how the thermal block failed, and the failure is worth stating in
    full because it is silent in two directions at once. `vehicle.yaml` had

        thermal:

        loops:
          - id: primary
          ...

    — the section header with nothing under it, and its three subsections one indent level out, as
    top-level keys. YAML reads that as `thermal: null` plus three orphan keys, so every reader that
    asked for `vehicle["thermal"]["loops"]` got `None` and the section the file declared was in fact
    empty. Nothing had ever asked, so nothing had ever failed: the loops, the radiators and the
    zones were complete, plausible and **read by no tool at all**, and the audit that went looking
    for unread sections found the symptom without the cause.

    So the two rules are the two halves of that shape. A key the linter has never heard of is either
    a new section nobody has wired up or a block that lost its parent; and a section declared and
    left empty is the second half of the same accident, which is why it is refused rather than
    skipped. `PROSE_SECTIONS` is the honest part: `conventions`, `frames`, `vocabulary` and `source`
    are declarations for a human reader, naming them makes being unread a stated decision rather
    than an oversight, and refusing an unknown key is what stops that list from quietly growing.
    """
    where = "vehicle.yaml"
    for key in doc:
        if key in VEHICLE_SECTIONS or key in PROSE_SECTIONS:
            continue
        report.refuse(
            where,
            f"declares a top-level section {key!r} that nothing reads. Either it is a new section "
            "and the reader is missing, or a block has lost the section it belongs to — a header "
            "with nothing under it and its children one level out reads as an empty section plus "
            "orphan keys, and every reader of the section then silently gets `None`",
        )
    for key in sorted(VEHICLE_SECTIONS):
        if key not in doc:
            report.refuse(where, f"has no {key!r} section")
        elif doc[key] is None:
            report.refuse(
                where,
                f"declares {key!r} and leaves it empty. A section header with nothing under it is "
                "how its children end up as top-level keys, where nothing will look for them",
            )


def check_vehicle(doc: dict[str, Any], report: Report) -> None:
    """Mass closure, and the configuration names the mission refers to."""
    check_vehicle_sections(doc, report)
    for cfg in doc.get("configurations") or []:
        where = f"vehicle.yaml:configuration {cfg.get('id')}"
        if "mass_kg" not in cfg:
            report.debt(where, "has no mass")
            continue
        parts = cfg.get("mass_breakdown") or {}
        stated = cfg.get("mass_breakdown_total_kg")
        if parts and stated is not None:
            numeric = {k: v for k, v in parts.items() if isinstance(v, (int, float))}
            unset = sorted(k for k, v in parts.items() if not isinstance(v, (int, float)))
            if unset:
                report.debt(
                    where, f"mass breakdown has unset parts {unset}, so it cannot be summed"
                )
                continue
            total = sum(numeric.values())
            if abs(total - stated) > 1.0:
                report.refuse(
                    where,
                    f"mass breakdown sums to {total:g} kg but the total is stated as {stated:g} kg",
                )


def check_propulsion(doc: dict[str, Any], vehicle: dict[str, Any], report: Report) -> None:
    """Fly the mission's Δv budget through each tank, in order, and see whether it closes.

    This is the check that stops a trajectory and a tank size drifting apart — the failure
    mode where a mission looks flyable and is not, and the one that is invisible in either
    file read alone.

    It has to be sequential rather than a single ratio, because two of this vehicle's burns
    leave from very different masses and the difference is not a detail: LOI is flown by the
    44-tonne docked stack and TEI by a CSM that has since lost the LM, so an engine's
    requirement computed from one mass is wrong by fifteen tonnes. Each burn therefore names
    the configuration it is flown in, and the linter walks the mission in phase order,
    subtracting what that engine has already spent.

    What it does not model, stated rather than hidden: non-ideal Isp during thrust build-up
    and tailoff, and residual propellant that cannot be drawn from a tank. Both make a real
    vehicle need *more* than this check computes, so a vehicle that passes still has to
    survive the plant's own accounting — which is the right direction for a pre-flight check
    to be wrong in.
    """
    phases = [p.get("id") for p in (doc.get("phases") or [])]
    order = {pid: index for index, pid in enumerate(phases)}
    configs = {c.get("id"): c for c in (vehicle.get("configurations") or [])}

    burns: dict[str, list[dict[str, Any]]] = {}
    for burn in doc.get("delta_v_budget") or []:
        where = f"mission.yaml:burn {burn.get('id')}"
        engine = burn.get("engine")
        if not engine:
            report.refuse(where, "names no engine")
            continue
        dv = burn.get("dv_m_s")
        if not isinstance(dv, (int, float)):
            report.debt(where, "has no dv_m_s")
            continue
        prov = burn.get("provenance") or {}
        check_basis(where, prov.get("basis"), prov, report)
        # A burn in no phase is a burn before the mission starts; it is allowed only if the
        # provenance says so, because otherwise it is a burn nobody can ever fly.
        phase = burn.get("phase")
        if phase is not None and phase not in order:
            report.refuse(
                where, f"is flown in phase {phase!r}, which mission.yaml does not declare"
            )
        if engine == "external_sivb":
            continue
        cfg = burn.get("configuration")
        if cfg not in configs:
            report.refuse(
                where, f"names configuration {cfg!r}, which vehicle.yaml does not declare"
            )
            continue
        burns.setdefault(engine, []).append(
            {"id": burn.get("id"), "dv": float(dv), "config": cfg, "sort": order.get(phase, -1)}
        )

    for name, engine in (vehicle.get("propulsion") or {}).items():
        where = f"vehicle.yaml:propulsion {name}"
        engine_burns = sorted(burns.get(name, []), key=lambda b: b["sort"])
        if not engine_burns:
            report.note(where, "has no mission burn, so its load is unchecked")
            continue
        isp = engine.get("isp_s")
        tank = engine.get("mass_kg")
        if not all(isinstance(v, (int, float)) for v in (isp, tank)):
            report.debt(where, "cannot be checked: isp_s and mass_kg are not both set")
            continue
        ve = float(isp) * 9.80665
        spent = 0.0
        for burn in engine_burns:
            wet = float(configs[burn["config"]]["mass_kg"]) - spent
            if wet <= 0:
                report.refuse(
                    where, f"{burn['id']} starts from a negative mass; the budget is impossible"
                )
                break
            spent += wet * (1.0 - pow(2.718281828459045, -burn["dv"] / ve))
        else:
            reserve = 100.0 * (float(tank) / spent - 1.0) if spent else float("inf")
            if float(tank) < spent * 0.98:
                report.refuse(
                    where,
                    f"cannot fly the budget: the mission needs {spent:,.0f} kg through this "
                    f"tank at Isp {isp:g} s and it holds {tank:,g} kg",
                )
            else:
                report.note(
                    where,
                    f"closes: the mission spends {spent:,.0f} kg of {tank:,g} kg "
                    f"({reserve:.1f} % reserve)",
                )


def decision_age_ms(row: dict[str, Any]) -> float | None:
    """The maximum age at which a channel may still be the basis of a decision.

    `mission_diode.md:1227-1262` supplies the calculation and the per-guard manifest; the
    rule the vehicle adopted is the one in `channels.yaml`'s header — **one publish period**,
    because a value older than one period is a value the publisher was obliged to refresh and
    did not. A channel may declare its own value, which is how an event-driven channel (no
    period at all) becomes usable as a decision input, and how a channel whose *service*
    evaluates it faster than it publishes can say so.

    Returning `None` means the executive cannot tell a fresh sample from one that will never
    arrive, which for a threshold's point is a refusal rather than a debt: `mission_diode.md`'s
    invariant D makes a hazardous effect on stale evidence a safety failure, and a guard with
    no age cannot be evaluated for staleness at all.
    """
    declared = row.get("max_decision_age_ms")
    if isinstance(declared, (int, float)):
        return float(declared)
    rate = row.get("rate_hz")
    if isinstance(rate, (int, float)) and rate > 0:
        return 1000.0 / float(rate)
    return None


def check_event_classes(
    root: Path,
    registry: dict[str, dict[str, Any]],
    report: Report,
    all_threshold_ids: set[str] | None = None,
) -> None:
    """Every declared event says what kind it is, and the kind is checkable.

    `channels.yaml`'s `events` field is **apollo's prose** — "stuck-on/off signature", "<8
    degraded", "any non-null" — and the thresholds are the machine-readable form of the same
    promises. Nothing joined them, and the audit that did found **53 of 118 channels declaring
    events that no threshold watched**. A dictionary that promises an alarm the vehicle does not
    raise is worse than one that promises nothing, because a fleet reads the promise and waits.

    Not all 53 were holes. Some events are publications rather than alarms — a phase change, a
    mode change, an "unexpected" condition the vehicle cannot recognise because only the fleet
    knows what it expected. So each channel with events declares `event_class`, and the class is
    a claim the linter can hold:

      - **alarm** — the vehicle raises a C&W alert, so *something must watch it*. The threshold
        is on this channel, because an alarm on a quantity is a comparison against that quantity.
      - **notification** — published when it changes and not alerted. Requires `on_event: true`,
        since a notification with no cadence is a notification nobody gets.
      - **frame** — carried in the envelope's own fields rather than as a value in `values`.
    """
    index = ChannelIndex(registry)
    all_threshold_ids = all_threshold_ids or set()
    watched: set[str] = set()
    domains_dir = root / "domains"
    if domains_dir.is_dir():
        for path in sorted(p for p in domains_dir.iterdir() if p.is_dir()):
            profiles = load(path / "profiles.yaml", Report()) or {}
            for threshold in profiles.get("thresholds") or []:
                all_threshold_ids.add(str(threshold.get("id")))
                point = threshold.get("point")
                if not point:
                    continue
                row = index.row(str(point))
                if row is None:
                    continue
                for cid, candidate in registry.items():
                    if candidate is row:
                        watched.add(cid)
    frame_fields = (
        {
            str((f or {}).get("name"))
            for f in (load(root / "presentation.yaml", Report()) or {})
            .get("frame", {})
            .get("fields", [])
        }
        if (root / "presentation.yaml").exists()
        else set()
    )

    for cid, row in sorted(registry.items()):
        events = row.get("events")
        kind = row.get("event_class")
        if not events:
            if kind:
                report.refuse(
                    f"channels.yaml:{cid}",
                    f"declares event_class {kind!r} and no events. A class with nothing to classify "
                    "is a field somebody filled in because it was there",
                )
            continue
        if not kind:
            report.refuse(
                f"channels.yaml:{cid}",
                f"declares {len(events)} event(s) and no `event_class`. Whether an event is an "
                "alarm, a publication or an envelope field is what decides whether anything has "
                "to implement it, and leaving it unsaid is how 53 promises went unkept",
            )
            continue
        if kind not in {"alarm", "notification", "frame", "realised_by"}:
            report.refuse(f"channels.yaml:{cid}", f"declares event_class {kind!r}")
            continue
        # `realised_by` is the commonest class and the one worth having: an event on one channel
        # is often implemented by a threshold on *another* channel that measures the same
        # quantity from a different domain's side. `eclss.cabin_temp_c` and
        # `thermal.zone_[id]_t_c` are one cabin temperature seen twice, and the thermal domain's
        # `csm_cabin_low`/`csm_cabin_high` are what keep the ECLSS channel's promise. Naming the
        # threshold makes the claim checkable rather than plausible.
        if kind == "realised_by":
            named = row.get("event_thresholds") or []
            if not named:
                report.refuse(
                    f"channels.yaml:{cid}",
                    "is classed `realised_by` and names no `event_thresholds`, so the claim that "
                    "something implements it is unverifiable",
                )
            for tid in named:
                if str(tid) not in all_threshold_ids:
                    report.refuse(
                        f"channels.yaml:{cid}",
                        f"names {tid!r} as the threshold that realises it, and no domain declares "
                        "a threshold by that id",
                    )
        if kind == "alarm" and cid not in watched:
            report.refuse(
                f"channels.yaml:{cid}",
                f"declares event_class 'alarm' and no threshold watches it: the dictionary "
                f"promises {events!r} and the vehicle raises nothing. Either add the threshold or "
                "classify the event as a notification",
            )
        if kind == "notification" and not row.get("on_event"):
            report.refuse(
                f"channels.yaml:{cid}",
                "is a notification and declares no `on_event`, so it is published on a cadence "
                "rather than when it changes — which is not what a notification is",
            )
        if kind == "frame" and cid.split(".", 1)[-1] not in frame_fields:
            report.refuse(
                f"channels.yaml:{cid}",
                f"is classed as a frame field and {cid.split('.', 1)[-1]!r} is not one of "
                "presentation.yaml#frame's fields",
            )


def check_producers(
    root: Path,
    registry: dict[str, dict[str, Any]],
    presentation: dict[str, Any],
    report: Report,
) -> None:
    """Every registered channel must have a declared source, or the vehicle publishes nothing.

    This is the check whose absence let 27 channels sit in the registry with no producer at all.
    A channel that no domain publishes and no frame field carries is not a missing feature: it is
    a threshold watching a value nobody computes, a crew position told it can read a gauge that
    does not exist, and a failure chain whose first clue is a number that is never emitted. None
    of those shows up anywhere else, because every other check runs from the name *to* the
    registry and this is the one that runs back.

    Three kinds of source are legitimate, and they are different kinds:

      - **a domain point** — `domains/<name>/points.yaml#points[].channel`, resolved through the
        registry's own templates so `res.recon_o2_kg` answers for `res.recon_[resource]_kg`.
      - **a frame field** — `presentation.yaml#frame.fields`, which is how apollo's `phase` and
        `met_s` reach a fleet without being anybody's domain point.
      - **the plant's own envelope** — `presentation.yaml#plant_published`, for the mission
        channels the plant computes and no domain owns.
    """
    index = ChannelIndex(registry)
    covered: set[str] = set()
    domains_dir = root / "domains"
    if domains_dir.is_dir():
        for path in sorted(p for p in domains_dir.iterdir() if p.is_dir()):
            points = load(path / "points.yaml", Report()) or {}
            for point in points.get("points") or []:
                cid = point.get("channel") if isinstance(point, dict) else point
                if not cid:
                    continue
                row = index.row(str(cid))
                if row is not None:
                    for registered, candidate in registry.items():
                        if candidate is row:
                            covered.add(registered)
    for field in (presentation.get("frame") or {}).get("fields") or []:
        name = str((field or {}).get("name"))
        for registered in registry:
            if registered == f"mission.{name}" or registered.endswith(f".{name}"):
                covered.add(registered)
    for entry in presentation.get("plant_published") or []:
        cid = entry.get("channel") if isinstance(entry, dict) else entry
        if cid:
            row = index.row(str(cid))
            if row is None:
                report.refuse(
                    "presentation.yaml:plant_published",
                    f"names {cid!r}, which is not a registered channel",
                )
            else:
                for registered, candidate in registry.items():
                    if candidate is row:
                        covered.add(registered)
    for cid in sorted(set(registry) - covered):
        report.refuse(
            f"channels.yaml:{cid}",
            "is registered and nothing publishes it: no domain point, no frame field, and no "
            "entry in presentation.yaml#plant_published. A channel with no producer is a "
            "threshold watching a number nobody computes",
        )


def check_zone_nodes(root: Path, vehicle: dict[str, Any], report: Report) -> None:
    """A zone's temperature lives on a node, or the zone is declared as one that has none.

    Six zones are declared for this vehicle and all six carry a temperature state; five carry a
    *driver*. The gap was invisible because of where the states lived: **two sat on the `internal`
    sentinel**, which is not a node, so no edge could terminate on them — `internal` has no inbound
    edge at all — and they were undrivable by construction. The two crewed cabins were in exactly
    that position until round 55, and the radiator was in it until round 70 moved it onto
    `radiator_reject`, where a node already existed and was already driven.

    A zone may legitimately have no node — `radiator_loop` is an observable rather than a
    compartment — so the rule is a declaration: the zone goes in `zones_not_on_nodes` with its
    reason. What is refused is a zone whose absence from the graph is neither.
    """
    thermal = load(root / "domains" / "thermal" / "components.yaml", report) or {}
    # `vehicle` is `None` when `vehicle.yaml` will not parse, and this check takes both files —
    # exactly the shape that reintroduced the round-48 crash. The caller guards the vehicle's own
    # checks; a cross-file one has to guard itself.
    if not isinstance(vehicle, dict) or not thermal:
        return
    zones = {str(z.get("id")) for z in (vehicle.get("thermal") or {}).get("zones") or []}
    declared = thermal.get("zones_not_on_nodes") or {}
    states = [s for s in thermal.get("state") or [] if isinstance(s, dict)]
    # Which zone each temperature state is about. The ids are not mechanical (`zone_csm_service_t`
    # against a zone called `csm_service_bay`), so the match is by the zone's own words.
    for zone in sorted(zones):
        stem = zone.replace("_bay", "").replace("_loop", "").replace("csm_", "").replace("lm_", "")
        mine = [
            s for s in states if stem.split("_")[0] in str(s.get("id")) and "_t" in str(s.get("id"))
        ]
        on_node = [s for s in mine if str(s.get("node")) != "internal"]
        if on_node or zone in declared:
            continue
        report.refuse(
            "domains/thermal/components.yaml",
            f"gives {zone!r} a temperature state on the `internal` sentinel, which is not a node, so "
            "no edge can drive it. Either put it on a node or declare it in `zones_not_on_nodes` "
            "with the reason it has none",
        )
    for zone, why in sorted(declared.items()):
        if zone not in zones:
            report.refuse(
                f"domains/thermal/components.yaml:zones_not_on_nodes.{zone}",
                "is not a declared zone",
            )
            continue
        if not str(why or "").strip():
            report.refuse(
                f"domains/thermal/components.yaml:zones_not_on_nodes.{zone}", "gives no reason"
            )
            continue
        # The other direction, and it was missing: a zone on the list that has since been put on a
        # node is a **stale exemption** — a reader told to expect a gap that has been closed. Round
        # 68's `check_cabin_pairing` checks both directions and this one did not, which is how the
        # two bays' exemptions survived the round that closed them.
        stem = zone.replace("_bay", "").replace("_loop", "").replace("csm_", "").replace("lm_", "")
        on_node = [
            s
            for s in states
            if stem.split("_")[0] in str(s.get("id"))
            and "_t" in str(s.get("id"))
            and str(s.get("node")) != "internal"
        ]
        if on_node:
            report.refuse(
                f"domains/thermal/components.yaml:zones_not_on_nodes.{zone}",
                f"is declared as having no node, and {on_node[0].get('id')!r} sits on "
                f"{on_node[0].get('node')!r}. A stale exemption is a reader told to expect a gap "
                "that has been closed",
            )


def check_atmosphere_symmetry(root: Path, report: Report) -> None:
    """Every gas the atmosphere model declares is held in *every* cabin, or the gap is explained.

    Round 68 asked this question of the channels and found the CO2 exposure average published for one
    compartment. This is the same question one file over, and it found the same shape: the atmosphere
    model declares four gases (`o2`, `n2`, `co2`, `h2o`), `cabin_atm` held four stock states, and
    **`lm_cabin_atm` held three — there was no `lm_cabin_h2o_kg`** — while `lm_water_separator` sits
    in the same file to remove the vapour that had no state to be in. A component whose subject the
    model does not hold is the round-42 absorber finding arriving in the atmosphere.

    A cabin may legitimately lack a gas, so the rule is a declaration rather than a refusal: the pair
    goes in `atmosphere_model.absent` with its reason. Nothing is absent today, and the list is what
    keeps a future one from being silent.
    """
    eclss = load(root / "domains" / "eclss" / "components.yaml", report) or {}
    model = eclss.get("atmosphere_model") or {}
    gases = [str(g.get("id")) for g in model.get("gases") or [] if isinstance(g, dict)]
    if not gases:
        return
    states = [s for s in eclss.get("state") or [] if isinstance(s, dict)]
    absent = {
        (str(pair[0]), str(pair[1]))
        for pair in model.get("absent") or []
        if isinstance(pair, (list, tuple)) and len(pair) == 2
    }
    for cabin in ("cabin_atm", "lm_cabin_atm"):
        held = {
            str(s.get("id"))
            for s in states
            if str(s.get("node")) == cabin and s.get("method") == "stock"
        }
        for gas in gases:
            if any(gas in name for name in held) or (cabin, gas) in absent:
                continue
            report.refuse(
                "domains/eclss/components.yaml:atmosphere_model",
                f"declares the gas {gas!r} and {cabin} holds no stock state for it. A model that "
                "names a species and no state to hold it is a component acting on nothing — either "
                "add the state or declare the pair in `absent` with its reason",
            )


def check_cabin_pairing(
    registry: dict[str, Any], presentation: dict[str, Any], report: Report
) -> None:
    """Every channel about a cabin is either paired with its twin, or says why it is not.

    The vehicle has two crewed compartments, so a channel about a cabin's air is about *one* cabin:
    `eclss.cabin_temp_c` and `eclss.lm_cabin_temp_c` are one quantity in two rooms with no shared air
    between them, and the registry's own ids are the evidence. Most of the ECLSS channels are paired
    that way. **A channel that is not paired is therefore a claim, and until round 68 it was an
    implicit one** — the CO2 exposure average was published for the cabin the crew leave and none for
    the one they live in, and nothing could tell that from a channel that is legitimately single
    because its subject is.

    So each unpaired ECLSS channel is named in `presentation.yaml#single_cabin` with its reason, and
    the comparison is exact in both directions: a new unpaired channel is refused until it is paired
    or explained, and a stale explanation is refused once the channel gains a twin.
    """
    declared = presentation.get("single_cabin")
    if not isinstance(declared, dict):
        report.debt(
            "presentation.yaml#single_cabin",
            "names no single-cabin channels, so an ECLSS channel about one compartment and an ECLSS "
            "channel about both are indistinguishable to a reader",
        )
        return
    # `check_channels` returns the registry keyed by channel id, not in its file's sections.
    published = {str(cid) for cid in (registry or {}) if str(cid).startswith("eclss.")}
    # The twin is the CSM id with the compartment inserted, not the LM id with it removed: stripping
    # `eclss.lm_` from a CSM id is a no-op, so the first version called every channel paired and the
    # check reported all four declarations as stale.
    paired = {
        cid
        for cid in published
        if ".lm_" not in cid and cid.replace("eclss.", "eclss.lm_", 1) in published
    }
    single = {cid for cid in published if ".lm_" not in cid and cid not in paired}
    for cid in sorted(single - set(declared)):
        report.refuse(
            "presentation.yaml#single_cabin",
            f"does not explain {cid!r}, which is published for one cabin and has no `lm_` twin. "
            "Either pair it or say why its subject is one compartment",
        )
    for cid in sorted(set(declared) - single):
        report.refuse(
            "presentation.yaml#single_cabin",
            f"explains {cid!r}, which is either paired or not an ECLSS channel. A stale explanation "
            "is a reader told to expect a gap that has been closed",
        )
    for cid, why in sorted(declared.items()):
        if not str(why or "").strip():
            report.refuse(f"presentation.yaml#single_cabin.{cid}", "gives no reason")


def check_presentation(
    doc: dict[str, Any],
    registry: dict[str, dict[str, Any]],
    verbs: dict[str, dict[str, Any]],
    report: Report,
) -> None:
    """The vehicle's side of the frozen window: what the fleet is told, and in which file.

    `presentation.yaml` exists because nothing did. The registries define what the vehicle knows
    and this defines what a fleet can see, and the gap between them is where a vehicle satisfying
    nothing of `docs/diode-contract.md` would hide: 145 channels with no statement of which appear
    in `state.json` and which in the ring, and 58 verbs whose gate variables §3 requires to be
    published and which were collected nowhere.

    What the linter can hold is the part that is a *count* or a *set*: the mirror's bound, the
    cadence classes' membership against the registry's own rates, the frame's field list. The parts
    that are prose — why a service state is layer A — are argued in the file and cannot be checked,
    which is the honest division.
    """
    where = "presentation.yaml"
    if not doc:
        report.debt(
            where,
            "is absent, so nothing says which of the registry's channels reach the fleet or in "
            "which file — the vehicle's side of the frozen window is undeclared",
        )
        return

    # 1. The contract this file claims to satisfy has to be the frozen one.
    if doc.get("contract_status") != "frozen":
        report.refuse(
            f"{where}:contract_status",
            f"declares {doc.get('contract_status')!r}; `docs/diode-contract.md` is frozen and a "
            "vehicle claiming otherwise would be free to drift from the window it must meet",
        )

    # 2. Every kind in the registry's `layer` field must map to a contract layer. An unmapped kind
    #    is a channel whose epistemic status the publisher cannot state.
    mapping = (doc.get("epistemic_layers") or {}).get("mapping") or {}
    for kind in sorted(LAYERS):
        if kind not in mapping:
            report.refuse(
                f"{where}:epistemic_layers.mapping",
                f"does not map the registry's {kind!r} kind onto a contract layer, so a channel of "
                "that kind cannot be published with an epistemic status",
            )
    for kind, layer in sorted(mapping.items()):
        if layer not in {"A", "I", "T"}:
            report.refuse(
                f"{where}:epistemic_layers.mapping",
                f"maps {kind!r} to {layer!r}, which is not one of A, I or T",
            )
        if layer == "T":
            report.refuse(
                f"{where}:epistemic_layers.mapping",
                f"maps {kind!r} to T: `docs/diode-contract.md:202` publishes truth never, and every "
                "domain's `not_published` section is where the vehicle says so by name",
            )

    # 3. The mirror's bound. `state.json` is rewritten every cycle, so its membership is a cost
    #    paid at the ring's cadence and the bound is what makes growth a decision.
    mirror = doc.get("mirror") or {}
    members = [
        cid
        for cid, row in registry.items()
        if row.get("layer") == "service" and row.get("priority") in {"P0", "P1"}
    ]
    declared_count = mirror.get("member_count")
    if declared_count != len(members):
        report.refuse(
            f"{where}:mirror.member_count",
            f"declares {declared_count!r} but the registry has {len(members)} service channels at "
            "P0/P1, which is the set the mirror's own rule derives",
        )
    bound = mirror.get("bound")
    if not isinstance(bound, int):
        report.refuse(f"{where}:mirror.bound", "declares no bound")
    elif len(members) > bound:
        report.refuse(
            f"{where}:mirror.bound",
            f"the mirror would carry {len(members)} members against a bound of {bound}: "
            "`state.json` is rewritten every cycle, so every member is a cost at the ring's cadence",
        )

    # 4. Every verb must declare exactly one gate *variable*, because §9's check 5 is "a closed
    #    gate is refused by name, and its variable is published". A gate written as a string has no
    #    name to publish.
    gate_vars = set()
    for vid, verb in sorted(verbs.items()):
        gate = verb.get("gate")
        if isinstance(gate, dict) and gate.get("variable"):
            gate_vars.add(str(gate["variable"]))
        elif isinstance(gate, str):
            report.refuse(
                f"domains/*/commands.yaml:{vid}",
                f"declares a gate as a string ({gate!r}); the contract publishes gate *variables* "
                "and a string has no name to publish",
            )
    if not gate_vars:
        report.debt(f"{where}:mirror.vehicle_keys", "no gate variable is published anywhere")

    # 5. The frame's fields.
    frame_fields = {
        f.get("name") for f in (doc.get("frame") or {}).get("fields") or [] if isinstance(f, dict)
    }
    for required in (
        "schema",
        "seq",
        "boot_id",
        "met_s",
        "sensor_time_s",
        "publish_time_s",
        "state_revision",
        "values",
        "quality",
        "phase",
        "vehicle",
    ):
        if required not in frame_fields:
            report.refuse(
                f"{where}:frame.fields",
                f"has no {required!r} field. A ring that cannot be sequenced, aged or attributed is "
                "a ring a fleet has to guess about",
            )

    # 6. The cadence classes must account for every channel and match the rates the registry
    #    actually declares. A summary that disagrees with its source is worse than no summary.
    classes = (doc.get("ring") or {}).get("cadence_classes") or []
    counted = {c.get("class"): c.get("members") for c in classes if isinstance(c, dict)}
    actual: dict[str, int] = dict.fromkeys(counted, 0)
    for row in registry.values():
        rate = row.get("rate_hz") or 0
        if rate == 0:
            bucket = "event"
        elif rate >= 5:
            bucket = "fast"
        elif rate >= 1:
            bucket = "control"
        elif rate >= 0.5:
            bucket = "slow"
        else:
            bucket = "resource"
        if bucket in actual:
            actual[bucket] += 1
    for name in sorted(counted):
        if counted[name] != actual.get(name):
            report.refuse(
                f"{where}:ring.cadence_classes.{name}",
                f"claims {counted[name]} members but the registry has {actual.get(name)} channels "
                "in that cadence class",
            )
    total = (doc.get("ring") or {}).get("members_total")
    if total != len(registry):
        report.refuse(
            f"{where}:ring.members_total",
            f"claims {total} channels against a registry of {len(registry)}",
        )
    report.note(
        where,
        f"the fleet's view is declared: {len(members)} mirrored, {len(registry)} in the ring over "
        f"{len(classes)} cadence classes, {len(frame_fields)} frame fields, {len(gate_vars)} gate "
        "variables published",
    )


def check_trajectory(doc: dict[str, Any], report: Report) -> None:
    """Re-derive the patched-conic elements, because a state vector is checkable arithmetic.

    `mission.yaml#initial_state` was the vehicle's largest single debt for several rounds: the
    published geometry does not close as an Earth-centred conic, so the state vector was declared
    UNCONFIGURED with the constraint set recorded beside it. Solving that constraint is arithmetic
    rather than design — Kepler's equation and vis-viva, the same two relations the propulsion
    check uses for the rocket equation — so the linter can hold the answer to it. What it cannot
    check is the part that needs a datum nobody has, and the debt says which part that is.

    The check also carries conflict C-24's disposition: the published post-injection speed is
    *slower than a Hohmann transfer*, so it cannot arrive at the Moon at any transit time, and the
    linter refuses an initial state whose speed cannot reach the arrival radius. That is the check
    that would have caught the published figure in the first place.
    """
    state = doc.get("initial_state") or {}
    orbit = state.get("earth_parking_orbit") or {}
    elements = state.get("osculating_elements")
    if not elements:
        report.debt(
            "mission.yaml:initial_state",
            "declares no osculating elements, so nothing says where the vehicle starts",
        )
        return
    where = "mission.yaml:initial_state.osculating_elements"
    mu = 398600.4418
    radius_earth = 6378.137
    perigee = orbit.get("perigee_altitude_km")
    apogee = orbit.get("apogee_altitude_km")
    if not all(isinstance(v, (int, float)) for v in (perigee, apogee)):
        report.debt(where, "cannot be re-derived: the parking orbit's altitudes are unset")
        return
    r_p = radius_earth + (float(perigee) + float(apogee)) / 2
    a = elements.get("semi_major_axis_km")
    e = elements.get("eccentricity")
    speed = elements.get("speed_at_cutoff_m_s")
    arrival_h = elements.get("arrival_at_moon_h")
    if not all(isinstance(v, (int, float)) for v in (a, e, speed, arrival_h)):
        report.debt(
            where, "cannot be re-derived: a, e, the cutoff speed or the arrival time is unset"
        )
        return
    a, e, speed, arrival_h = float(a), float(e), float(speed), float(arrival_h)

    # 1. e = 1 - r_p/a. One determination, so the two published numbers must agree with it.
    e_derived = 1.0 - r_p / a
    if abs(e_derived - e) > 1e-4:
        report.refuse(
            where,
            f"declares e = {e:g} but 1 - r_p/a = {e_derived:.6f}; the parking-orbit radius and "
            "the semi-major axis determine the eccentricity and there is no third degree of "
            "freedom to spend on a disagreement",
        )
    # 2. vis-viva at cutoff.
    v_derived = math.sqrt(mu * (2.0 / r_p - 1.0 / a)) * 1000.0  # km/s -> m/s
    if abs(v_derived - speed) > 1.0:
        report.refuse(
            where,
            f"declares a cutoff speed of {speed:g} m/s but vis-viva gives {v_derived:.1f} m/s",
        )
    # 3. the speed must actually reach the arrival radius — conflict C-24 made mechanical.
    r_arrival = 384400.0
    apogee_r = a * (1.0 + e)
    if apogee_r < r_arrival:
        report.refuse(
            where,
            f"its apogee is {apogee_r:,.0f} km and the Moon is at {r_arrival:,.0f} km: this "
            "trajectory cannot arrive at any transit time, however long. The published "
            "post-injection speed is 93.8 m/s slower than a minimum-energy Hohmann transfer "
            "(conflict C-24)",
        )
    # 4. and the arrival time must be the one the phase ladder prices.
    phases = {str(p.get("id")): p for p in doc.get("phases") or []}
    coast = phases.get("translunar_coast") or {}
    if isinstance(coast.get("duration_h"), (int, float)):
        ladder_h = float(coast["duration_h"])
        if abs(ladder_h - arrival_h) > 0.05:
            report.refuse(
                where,
                f"arrives at {arrival_h:g} h but the translunar coast phase is {ladder_h:g} h: "
                "the element was solved from the ladder, so a disagreement means one of them moved",
            )
        # 5. Kepler's equation, solved the same way the element was.
        lo, hi = r_p + 1.0, 1.0e7
        target = 2.0 * math.pi * math.sqrt(a**3 / mu)  # full period, for the bisection bound
        if target <= 0:
            report.refuse(where, "has a non-positive transfer period")
        else:
            lo, hi = 1.0, 1.0e8
            for _ in range(200):
                mid = (lo + hi) / 2
                ecc = 1.0 - r_p / mid
                ratio = (1.0 - r_arrival / mid) / ecc
                ecc_anom = math.acos(max(-1.0, min(1.0, ratio)))
                t = (ecc_anom - ecc * math.sin(ecc_anom)) / math.sqrt(mu / mid**3)
                if t > ladder_h * 3600.0:
                    lo = mid
                else:
                    hi = mid
            a_solved = (lo + hi) / 2
            if abs(a_solved - a) / a > 2e-3:
                report.refuse(
                    where,
                    f"declares a = {a:,.0f} km but Kepler's equation puts the Moon's mean "
                    f"distance at {ladder_h:g} h from a = {a_solved:,.0f} km",
                )
    report.note(
        where,
        f"re-derived: a = {a:,.0f} km, e = {e:.6f}, apogee {apogee_r:,.0f} km, cutoff "
        f"{speed:g} m/s, arriving at {arrival_h:g} h",
    )


def check_power_inventory(root: Path, report: Report) -> None:
    """The power domain's quantities, related to each other rather than merely declared.

    `demand_w`, `inrush_w`, `bus`, `rated_w`, `ah` and `v_nominal` were read by nothing — 17
    components and 25 loads — so the whole quantitative inventory was a set of numbers with no
    arithmetic between them. The linter has enforced the *mass* closure since the beginning; this is
    the same check one domain over, and the load budget happens to close (CSM 1723 W, LM 1007 W)
    with nothing keeping it closed.

    Four relationships, and the fourth is the one with teeth: `power.battery_soc_pct` is a
    percentage whose denominator was declared nowhere, and `battery_charge_j` is a stock with no
    capacity, so what the vehicle carries in joules existed only as a product nobody computed.
    """
    power = load(root / "domains" / "power" / "components.yaml", report) or {}
    loads = power.get("loads") or []
    components = power.get("components") or []
    budget = power.get("load_budget") or {}
    if not loads or not components or not budget:
        report.debt(
            "domains/power/components.yaml",
            "has no loads, components or load_budget, so the power inventory relates to nothing",
        )
        return

    # 1. The demand closes, per vehicle, against the total the file states.
    by_vehicle: dict[str, int] = {}
    for row in loads:
        by_vehicle[str(row.get("vehicle"))] = by_vehicle.get(str(row.get("vehicle")), 0) + int(
            row.get("demand_w") or 0
        )
    for vehicle, total in sorted(by_vehicle.items()):
        declared = budget.get(f"{vehicle}_total_demand_w")
        if not isinstance(declared, (int, float)):
            report.refuse(
                "domains/power/components.yaml:load_budget",
                f"states no total for {vehicle!r}, whose loads sum to {total} W",
            )
        elif int(declared) != total:
            report.refuse(
                "domains/power/components.yaml:load_budget",
                f"declares {vehicle}_total_demand_w as {declared} and its loads sum to {total} W. "
                "This is the mass closure's check one domain over: a stated total that does not "
                "equal its parts is a vehicle whose demand nobody has added up",
            )

    # 2. A load sits on a bus, and the bus belongs to the same vehicle as the load.
    buses = {str(c.get("id")): str(c.get("vehicle")) for c in components if c.get("class") == "bus"}
    for row in loads:
        bus = str(row.get("bus"))
        if bus not in buses:
            report.refuse(
                f"domains/power/components.yaml:load {row.get('id')}",
                f"sits on bus {bus!r}, which no component of class `bus` declares",
            )
        elif buses[bus] != str(row.get("vehicle")):
            report.refuse(
                f"domains/power/components.yaml:load {row.get('id')}",
                f"is a {row.get('vehicle')} load on {bus!r}, which is a {buses[bus]} bus",
            )

    # 3. The sources cover the connected load, per vehicle. Not per bus: which bus a source feeds
    #    is a routing decision the tie makes and the file does not state, so the honest bound is
    #    the vehicle's own total against the capacity presenting to it.
    capacity: dict[str, int] = {}
    for cell in components:
        if cell.get("class") == "source":
            # A source feeds the vehicle whose bus band its output sits in.
            for bus_id, vehicle in buses.items():
                band = next(
                    (c.get("v_band") for c in components if str(c.get("id")) == bus_id), None
                )
                out = cell.get("bus_v")
                if (
                    isinstance(band, list)
                    and isinstance(out, list)
                    and out[0] <= band[1]
                    and out[1] >= band[0]
                ):
                    capacity[vehicle] = capacity.get(vehicle, 0) + int(cell.get("rated_w") or 0)
    for vehicle, total in sorted(by_vehicle.items()):
        if vehicle in capacity and capacity[vehicle] < total:
            report.refuse(
                "domains/power/components.yaml:components",
                f"presents {capacity[vehicle]} W of source capacity to {vehicle!r} against {total} W "
                "of connected load",
            )

    # 4. The energy the batteries carry, which is the denominator `battery_soc_pct` never had.
    energy: dict[str, int] = {}
    for cell in components:
        if cell.get("class") == "storage":
            ah, volts = cell.get("ah"), cell.get("v_nominal")
            if not isinstance(ah, (int, float)) or not isinstance(volts, (int, float)):
                report.refuse(
                    f"domains/power/components.yaml:component {cell.get('id')}",
                    f"is a battery with ah={ah!r} and v_nominal={volts!r}, so its energy cannot be "
                    "computed and the state of charge has no denominator",
                )
                continue
            group = str(cell.get("id", "")).rsplit("_", 1)[0]
            energy[group] = energy.get(group, 0) + int(ah * volts)
    # Grouped by vehicle, and by *substring* rather than suffix: the groups are named
    # `battery_csm`, `battery_lm_ascent` and `battery_lm_descent`, and only the first of those ends
    # with its vehicle's name. The first version of this used `endswith` and summed the LM's cells
    # to zero, which is the kind of error the check itself exists to catch one level up.
    per_vehicle: dict[str, int] = {}
    for group, total in energy.items():
        vehicle = "csm" if "csm" in group else "lm"
        per_vehicle[vehicle] = per_vehicle.get(vehicle, 0) + total
    for vehicle, total in sorted(per_vehicle.items()):
        declared = budget.get(f"{vehicle}_battery_energy_wh")
        if not isinstance(declared, (int, float)):
            report.refuse(
                "domains/power/components.yaml:load_budget",
                f"states no battery energy for {vehicle!r}, whose cells carry {total} Wh",
            )
        elif int(declared) != total:
            report.refuse(
                "domains/power/components.yaml:load_budget",
                f"declares {vehicle}_battery_energy_wh as {declared} and its cells carry {total} Wh "
                "(ah x v_nominal)",
            )
    # The staged split is a claim about which stage carries which cells, and it has to add up.
    asc = budget.get("lm_battery_energy_ascent_stage_wh")
    desc = budget.get("lm_battery_energy_descent_stage_wh")
    lm = budget.get("lm_battery_energy_wh")
    if all(isinstance(v, (int, float)) for v in (asc, desc, lm)):
        if asc + desc != lm:
            report.refuse(
                "domains/power/components.yaml:load_budget",
                f"declares the LM's ascent stage at {asc} Wh and its descent stage at {desc} Wh, "
                f"which is {asc + desc} against a stated total of {lm}. The descent batteries are "
                "jettisoned with the descent stage, so the split is the number that decides whether "
                "the ascent can be flown",
            )
        if asc != energy.get("battery_lm_ascent", asc):
            report.refuse(
                "domains/power/components.yaml:load_budget",
                f"declares the ascent stage at {asc} Wh and the ascent cells carry "
                f"{energy.get('battery_lm_ascent')} Wh",
            )


def check_thermal_bindings(root: Path, vehicle: dict[str, Any], report: Report) -> None:
    """The thermal machine is declared twice, and the two declarations disagreed.

    `vehicle.yaml#thermal` is the vehicle-level view — loops, radiators, zones, which is what its
    own header says it carries — and `domains/thermal/components.yaml` is the domain's: the same
    hardware with the states that integrate it. Both had been written, both were complete, and
    **nothing joined them**, so `vehicle.yaml:thermal` was read by no tool at all.

    Writing the join found the disagreement immediately, and it is the kind that survives review
    because both halves look right on their own. `vehicle.yaml` called its second loop `secondary`
    and gave it the **LM's** fluid ("65 % water / 35 % inhibited ethylene glycol"), the LM's flow
    band (1.5-1.9 L/min) and the LM's temperatures — while the domain has three loops, one of them
    `loop_lm` carrying exactly that fluid and exactly that band, and a `loop_secondary` that is the
    CSM's second loop with different, `chosen` figures. So the vehicle-level file named the LM's
    coolant loop "secondary" and did not mention the CSM's second loop at all. A reader taking
    `vehicle.yaml` for the loop list would size the wrong vehicle.

    The join is by `id`, because unlike the electrical inventory the two files already agree on
    their names — which is the evidence that they were always meant to be one declaration.
    """
    thermal = (vehicle or {}).get("thermal") or {}
    domain = load(root / "domains" / "thermal" / "components.yaml", report) or {}
    if not thermal or not domain:
        report.debt(
            "vehicle.yaml#thermal",
            "either the vehicle's thermal section or the thermal domain is absent, so the two "
            "views of the cooling machine cannot be compared",
        )
        return

    declared = {str(loop.get("id")): loop for loop in thermal.get("loops") or []}
    built = {
        str(c.get("id")): c
        for c in domain.get("components") or []
        if isinstance(c, dict) and c.get("class") == "loop"
    }
    where = "vehicle.yaml#thermal.loops"
    for missing in sorted(set(built) - set(declared)):
        report.refuse(
            where,
            f"omits {missing!r}, which domains/thermal/components.yaml declares as a loop. The "
            "vehicle-level file is the loop list a reader reaches for first",
        )
    for extra in sorted(set(declared) - set(built)):
        report.refuse(
            where,
            f"declares {extra!r}, which no component of class `loop` in the thermal domain has",
        )
    for loop_id in sorted(set(declared) & set(built)):
        one, two = declared[loop_id], built[loop_id]
        for field in ("fluid", "flow_l_min", "vehicle"):
            if field in one and field in two and one[field] != two[field]:
                report.refuse(
                    f"{where}.{loop_id}",
                    f"gives {field} as {one[field]!r} and the thermal domain gives {two[field]!r}. "
                    "One machine, two files: a loop re-rated in one and not the other is a vehicle "
                    "whose cooling was sized against a loop nobody flies",
                )
        volume = one.get("loop_volume_l", one.get("volume_l"))
        if volume is not None and two.get("volume_l") is not None and volume != two["volume_l"]:
            report.refuse(
                f"{where}.{loop_id}",
                f"gives a volume of {volume!r} L and the thermal domain gives {two['volume_l']!r}",
            )

    # The radiators. `vehicle.yaml` gives a per-panel rejection and a panel count; the domain
    # derives an effective area and a total from them. The product is the one quantity both state,
    # so it is the one worth holding.
    radiators = {str(r.get("id")): r for r in thermal.get("radiators") or []}
    model = (domain.get("radiator_model") or {}).get("csm") or {}
    panel = radiators.get("csm_radiator") or {}
    if panel and model:
        per_panel = panel.get("rejection_w_per_panel")
        panels = panel.get("panels")
        total = model.get("rejection_w")
        if (
            isinstance(per_panel, (int, float))
            and isinstance(panels, int)
            and total is not None
            and per_panel * panels != total
        ):
            report.refuse(
                "vehicle.yaml#thermal.radiators.csm_radiator",
                f"gives {panels} x {per_panel} W of rejection and the thermal domain's "
                f"radiator model derives {total} W from it",
            )
        area = panel.get("area_m2")
        geometric = model.get("area_geometric_m2")
        if area is not None and geometric is not None and area != geometric:
            report.refuse(
                "vehicle.yaml#thermal.radiators.csm_radiator",
                f"gives {area} m2 of panel area and the thermal domain gives {geometric} m2 "
                "geometric",
            )

    # The zones. Both files list the same six, and the domain's `vehicle` is what binds a zone to
    # the compartment whose atmosphere it is; a zone named in one file and not the other is a
    # compartment that is either unregulated or unwatched, and the two are hard to tell apart.
    zones_one = {str(z.get("id")) for z in thermal.get("zones") or []}
    zones_two = {str(z.get("id")) for z in domain.get("zones") or []}
    if zones_one != zones_two:
        report.refuse(
            "vehicle.yaml#thermal.zones",
            f"lists {sorted(zones_one)} and the thermal domain lists {sorted(zones_two)}",
        )


def check_thermal_heat_inputs(root: Path, report: Report) -> None:
    """Which loads heat which zone, held as a partition rather than a list.

    `load_budget` says the thermal domain's heat inputs are the power domain's loads and that the
    numbers are not duplicated. What it did not say — anywhere, in either file — is **which
    compartment each load's watts warm**, and that is why `zone_csm_cabin_t` spent its life as a
    `lag` with no driver: the thermal domain declares the heat sources, the power domain declares
    the loads, and nothing joined them.

    Three properties, and the third is the one with teeth:

      * every zone in `vehicle.yaml#thermal.zones` is either given inputs or listed in `unheated`
        with a reason — so "nothing heats it" is a decision rather than an oversight;
      * every load named exists in `power/components.yaml#loads`, and every load in that inventory
        is assigned somewhere. A load nobody assigned is a watt that heats nothing, and its zone
        would run cold for a reason no reader could find;
      * the distinct loads assigned to each vehicle sum to that vehicle's declared demand. This is
        the mass closure's shape a fourth time, and it is what makes the assignment a *partition*
        rather than a wish: 1,723 W of CSM load and 1,007 W of LM load have to arrive somewhere.

    A load may appear in more than one zone — `csm_heaters` is one 300 W bank serving the cabin and
    the avionics bay, which `heater_bank_csm` already declares as a shared source — so the closure
    counts *distinct* loads per vehicle while the zones may overlap.
    """
    thermal = load(root / "domains" / "thermal" / "components.yaml", report) or {}
    power = load(root / "domains" / "power" / "components.yaml", report) or {}
    if not thermal or not power:
        return
    heat = thermal.get("heat_inputs")
    if not heat:
        report.debt(
            "domains/thermal/components.yaml",
            "declares no `heat_inputs`, so the zones that a `lag` advances have no declared driver "
            "and the power domain's loads heat nowhere",
        )
        return

    vehicle_doc = load(root / "vehicle.yaml", report) or {}
    zones = {str(z.get("id")) for z in (vehicle_doc.get("thermal") or {}).get("zones") or []}
    unheated = {str(k) for k in (thermal.get("unheated") or {})}
    where = "domains/thermal/components.yaml:heat_inputs"
    for zone in sorted(zones - set(heat) - unheated):
        report.refuse(
            where,
            f"gives {zone!r} no heat inputs and does not list it in `unheated`, so whether anything "
            "warms it is undeclared",
        )
    for zone in sorted(set(heat) - zones):
        report.refuse(where, f"names {zone!r}, which is not a zone in vehicle.yaml#thermal")
    for zone in sorted(unheated & set(heat)):
        report.refuse(where, f"lists {zone!r} as unheated and also gives it heat inputs")

    loads = {str(row.get("id")): row for row in power.get("loads") or []}
    by_vehicle: dict[str, int] = {}
    for zone, block in sorted(heat.items()):
        for load_id in (block or {}).get("loads") or []:
            if str(load_id) not in loads:
                report.refuse(
                    f"{where}.{zone}",
                    f"names {load_id!r}, which is not a load in domains/power/components.yaml",
                )
                continue
    # Distinct loads per vehicle, so a shared bank is not counted twice.
    by_vehicle = {}
    seen: dict[str, set[str]] = {}
    for block in heat.values():
        for load_id in (block or {}).get("loads") or []:
            row = loads.get(str(load_id))
            if row:
                seen.setdefault(str(row.get("vehicle")), set()).add(str(load_id))
    for vehicle, ids in sorted(seen.items()):
        total = sum(int(loads[i].get("demand_w") or 0) for i in ids)
        by_vehicle[vehicle] = total
    # And the heat-rate states carry the totals, re-derived against the loads they sum. Without this
    # the two declarations could part company in the quiet direction: a load re-rated in
    # `domains/power/` would change what the cabin's equipment actually draws while the thermal
    # state went on relaxing toward the old figure — a cabin modelled as cooler than it is.
    states = {
        str(s.get("id")): s
        for s in thermal.get("state") or []
        if isinstance(s, dict) and s.get("id")
    }
    for zone, block in sorted(heat.items()):
        state = next(
            (s for s in states.values() if str(s.get("node")) == f"cabin_heat_{block.get('rate')}"),
            None,
        )
        if state is None:
            continue
        declared = sum(
            int(loads[str(load_id)].get("demand_w") or 0)
            for load_id in block.get("loads") or []
            if str(load_id) in loads
        )
        if state.get("total_w") != declared:
            report.refuse(
                f"domains/thermal/components.yaml:state {state['id']}",
                f"declares {state.get('total_w')!r} W and the loads `heat_inputs.{zone}` assigns "
                f"sum to {declared} W. The cabin would relax toward a heat rate its equipment does "
                "not produce",
            )
        rederive(
            f"domains/thermal/components.yaml:state {state['id']}",
            state.get("total_w"),
            (state.get("provenance") or {}).get("computation"),
            report,
        )

    for vehicle, total in sorted(by_vehicle.items()):
        expected = sum(
            int(row.get("demand_w") or 0)
            for row in loads.values()
            if str(row.get("vehicle")) == vehicle
        )
        if total != expected:
            missing = sorted(
                str(row.get("id"))
                for row in loads.values()
                if str(row.get("vehicle")) == vehicle and str(row.get("id")) not in seen[vehicle]
            )
            report.refuse(
                f"{where}.{vehicle}",
                f"accounts for {total} W of {vehicle} load against a declared {expected} W. A load "
                f"assigned to no zone is a watt that heats nothing: {missing}",
            )


def check_metabolic_rules(
    root: Path, vehicle: dict[str, Any], mission: dict[str, Any], report: Report
) -> None:
    """What a cabin's equipment must remove is what the crew in it produce.

    Three declarations in three files have to agree, and until round 66 nothing joined them: the
    crew count (`mission.yaml#crew`), the metabolic production rate
    (`vehicle.yaml#consumables.metabolic`), and the removal rate the ECLSS domain declares for each
    compartment. The arithmetic is one line per cabin — **rate = crew x kg_per_crew_day / 86400** —
    and it is the same three-way join as the cabin equilibrium's, applied to a different quantity.

    The two figures are not the same figure, which is the point of the round-63 split: the CSM
    element removes what `size` crew produce and the LM cartridge what `surface_party` produce, so
    the LM's rate is two thirds of the CSM's on the same metabolic constant. A single shared state
    could not have expressed either.
    """
    eclss = load(root / "domains" / "eclss" / "components.yaml", report) or {}
    states = {str(s.get("id")): s for s in eclss.get("state") or [] if isinstance(s, dict)}
    crew = (mission or {}).get("crew") or {}
    metabolic = ((vehicle or {}).get("consumables") or {}).get("metabolic") or {}
    per_day = metabolic.get("co2_kg_per_crew_day")
    if not crew or not per_day:
        report.debt(
            "domains/eclss/components.yaml",
            "declares removal rates and either mission.yaml#crew or the metabolic rate is absent, "
            "so what the crew produce cannot be compared with what the equipment removes",
        )
        return
    # The oxygen side joins the same two declarations plus the leak: the regulator replaces what the
    # cabin loses, which is the leak plus the crew's metabolic consumption. Both terms are published,
    # so the supply rates are as checkable as the removal rates are.
    leak = ((vehicle or {}).get("consumables") or {}).get("leak") or {}
    o2_per_day = metabolic.get("o2_kg_per_crew_day")
    for state_id, crew_key, leak_per_h in (
        ("cabin_o2_supply_csm_kg_s", "size", leak.get("csm_kg_per_h")),
        ("cabin_o2_supply_lm_kg_s", "surface_party", leak.get("lm_kg_per_h")),
    ):
        state = states.get(state_id)
        headcount = crew.get(crew_key)
        if state is None or not isinstance(leak_per_h, (int, float)):
            continue
        if not isinstance(headcount, (int, float)) or not isinstance(o2_per_day, (int, float)):
            continue
        expected = (float(leak_per_h) + headcount * float(o2_per_day) / 24.0) / 3600.0
        declared = state.get("nominal_kg_s")
        if not isinstance(declared, (int, float)) or abs(declared - expected) > expected * 0.01:
            report.refuse(
                f"domains/eclss/components.yaml:state {state_id}",
                f"declares {declared!r} kg/s and {headcount} crew at {o2_per_day} kg per crew-day "
                f"plus a {leak_per_h} kg/h leak is {expected:.6g} kg/s. The regulator replaces what "
                "the cabin loses, and a supply that does not is a cabin whose pressure drifts",
            )

    for state_id, key in (
        ("co2_removal_csm_kg_s", "size"),
        ("co2_removal_lm_kg_s", "surface_party"),
    ):
        state = states.get(state_id)
        if state is None:
            continue
        headcount = crew.get(key)
        if not isinstance(headcount, (int, float)):
            report.refuse(
                "mission.yaml#crew",
                f"states no {key!r}, which is the crew count {state_id} is sized against",
            )
            continue
        expected = headcount * float(per_day) / 86400.0
        declared = state.get("nominal_kg_s")
        if not isinstance(declared, (int, float)) or abs(declared - expected) > expected * 0.01:
            report.refuse(
                f"domains/eclss/components.yaml:state {state_id}",
                f"declares {declared!r} kg/s and {headcount} crew at {per_day} kg per crew-day is "
                f"{expected:.6g} kg/s. A cabin's equipment that removes a different amount from what "
                "its crew produce is a cabin whose CO2 either climbs or falls with nobody in it",
            )


def check_cabin_equilibrium(root: Path, vehicle: dict[str, Any], report: Report) -> None:
    """The cabin's equilibrium temperature has to lie inside the cabin's own limit band.

    Four declarations have to agree for that to be true, and they live in three files: the heat the
    compartment's equipment puts into it (`domains/thermal/components.yaml#heat_inputs`, summed from
    the *power* domain's load inventory), the cabin's lumped conductance (the same file's
    `conductance_w_per_k`), the coolant supply temperature (`vehicle.yaml#thermal.loops`), and the
    zone's own `limit_c` (`vehicle.yaml#thermal.zones`). Nothing joined them, and the arithmetic is
    one line: **T = supply + Q/G**.

    Writing it found a swapped pair of fields. `loop_primary` declared `supply_c: [2.8, 7.2]` and
    `evaporator_outlet_c: 5.3`, while its own source reads *"mixed supply 45 F = 7.2 C, evaporator
    outlet 41.5 F = 5.3 C over a 37-45 F range"* — so the supply carried the evaporator's span and
    the evaporator carried the midpoint of the span it should have been. Taken at face value the
    lower end of that band is a 2.8 C supply, and at 2.8 C:

      * `csm_cabin`: 2.8 + 733/125 = **8.66 C**, against a 10 C floor
      * `lm_cabin`:  2.8 + 827/125 = **9.42 C**, against a 10 C floor

    Both below. The vehicle would have tripped its own cabin-low alarm on every cold pass of a
    nominal mission, and the cause was a field name rather than a physical impossibility: the cabin
    supply is the *mixed* supply at 7.2 C, which puts the two cabins at 13.06 C and 13.82 C, inside
    their bands with margin. The check is what makes the difference visible, because either
    assignment is plausible in isolation.
    """
    thermal = load(root / "domains" / "thermal" / "components.yaml", report) or {}
    if not thermal or not vehicle:
        return
    zones = {str(z.get("id")): z for z in (vehicle.get("thermal") or {}).get("zones") or []}
    loops = {
        str(loop.get("id")): loop for loop in (vehicle.get("thermal") or {}).get("loops") or []
    }
    # Which loop serves which compartment. Declared here rather than inferred because the two loops
    # are the CSM's and the LM's and nothing in either file says so.
    served = {"csm_cabin": "loop_primary", "lm_cabin": "loop_lm"}
    states = {str(s.get("id")): s for s in thermal.get("state") or [] if isinstance(s, dict)}
    for zone, loop_id in sorted(served.items()):
        zone_doc = zones.get(zone)
        loop = loops.get(loop_id)
        if not zone_doc or not loop:
            continue
        bands = zone_doc.get("limit_c") or [None, None]
        supply = loop.get("supply_c")
        if not isinstance(supply, (int, float)):
            report.refuse(
                f"vehicle.yaml#thermal.loops.{loop_id}",
                f"serves {zone} and states no single `supply_c` figure. A band here is the "
                "evaporator outlet's range rather than the mixed supply the cabin sees, and the "
                "difference is several kelvin of cabin temperature",
            )
            continue
        heat = states.get(f"cabin_heat_{zone.split('_')[0]}_w")
        cabin = states.get(
            next(
                (
                    sid
                    for sid, s in states.items()
                    if str(s.get("node"))
                    == ("cabin_zone_t" if zone == "csm_cabin" else "lm_cabin_zone_t")
                ),
                "",
            )
        )
        if not heat or not cabin or not cabin.get("conductance_w_per_k"):
            continue
        equilibrium_c = float(supply) + float(heat.get("total_w") or 0) / float(
            cabin["conductance_w_per_k"]
        )
        equilibrium = equilibrium_c
        # The state that carries this figure is re-derived too, in kelvin. Two declarations of one
        # quantity is the shape that drifts, and here it would drift in the quiet direction: the
        # cabin would relax toward a stale equilibrium while the loads and the supply moved on.
        eq_state = states.get(f"cabin_eq_{zone.split('_')[0]}_k")
        if eq_state is not None:
            declared_k = eq_state.get("total_k")
            computed_k = equilibrium_c + 273.15
            if not isinstance(declared_k, (int, float)) or abs(declared_k - computed_k) > 0.02:
                report.refuse(
                    f"domains/thermal/components.yaml:state {eq_state.get('id')}",
                    f"declares {declared_k!r} K and the supply plus Q/G is {computed_k:.2f} K. The "
                    "cabin would relax toward an equilibrium its own declarations do not produce",
                )
            rederive(
                f"domains/thermal/components.yaml:state {eq_state.get('id')}",
                declared_k,
                (eq_state.get("provenance") or {}).get("computation"),
                report,
            )
        low, high = (bands + [None, None])[:2]
        if isinstance(low, (int, float)) and equilibrium < low:
            report.refuse(
                f"vehicle.yaml#thermal.zones.{zone}",
                f"has a {low} C floor and its equipment's {heat.get('total_w')} W over a "
                f"{cabin['conductance_w_per_k']} W/K conductance puts it at {equilibrium:.2f} C on a "
                f"{supply} C supply. The vehicle would trip its own cabin-low alarm in a nominal "
                "mission, which means one of the four declarations is wrong",
            )
        if isinstance(high, (int, float)) and equilibrium > high:
            report.refuse(
                f"vehicle.yaml#thermal.zones.{zone}",
                f"has a {high} C ceiling and its equilibrium at {equilibrium:.2f} C is above it",
            )


def check_electrical_bindings(root: Path, vehicle: dict[str, Any], report: Report) -> None:
    """One machine, two files, and no sentence saying so.

    `vehicle.yaml#electrical` is the vehicle-level view: what it carries, with the mass each item
    contributes to the mass closure. `domains/power/components.yaml` is the domain's view: the same
    hardware, one entry per unit, with the bus each load sits on and the protection between them.
    Both are right. They agreed on every comparable quantity when this check was written — and
    **nothing was keeping them agreeing**, which is the whole of the problem: a battery re-rated in
    one file and not the other is two machines wearing one name, and the mass closure would go on
    summing the mass of the one nobody flies.

    `domain_group` is the link, and it is declared rather than inferred because the two files name
    the same batteries differently — `csm_entry` against `battery_csm` — so a rule that guessed from
    the string would have to know that `entry` and `csm` are the same vehicle.
    """
    electrical = (vehicle or {}).get("electrical") or {}
    power = load(root / "domains" / "power" / "components.yaml", report) or {}
    components = power.get("components") or []
    if not electrical or not components:
        report.debt(
            "vehicle.yaml#electrical",
            "either the vehicle's electrical section or the power domain's component list is "
            "missing, so the two views of the inventory cannot be compared",
        )
        return

    cells = electrical.get("fuel_cells") or {}
    where = "vehicle.yaml:electrical.fuel_cells"
    group = cells.get("domain_group")
    sources = [c for c in components if str(c.get("id", "")).startswith(str(group or "\0"))]
    if not group:
        report.refuse(where, "declares no `domain_group`, so it is not linked to the domain's list")
    elif not sources:
        report.refuse(where, f"names domain_group {group!r}, which matches no component")
    else:
        if cells.get("modules") != len(sources):
            report.refuse(
                where,
                f"declares {cells.get('modules')} module(s) and the power domain lists "
                f"{len(sources)} ({[c.get('id') for c in sources]})",
            )
        for cell in sources:
            if cell.get("rated_w") != cells.get("power_w_each"):
                report.refuse(
                    where,
                    f"declares {cells.get('power_w_each')} W per module and {cell.get('id')!r} is "
                    f"rated {cell.get('rated_w')} W in the power domain",
                )
            if cell.get("bus_v") != cells.get("bus_v"):
                report.refuse(
                    where,
                    f"declares bus_v {cells.get('bus_v')} and {cell.get('id')!r} presents "
                    f"{cell.get('bus_v')}",
                )

    for name, battery in sorted((electrical.get("batteries") or {}).items()):
        bwhere = f"vehicle.yaml:electrical.batteries.{name}"
        if isinstance(battery, str):
            # `batteries.note` is documentation and belongs here. The distinction is the *key*: a
            # known prose key is prose, and a string under any other name is a group someone
            # flattened — which nothing can compare, and which still reads as documentation.
            if name in DOC_KEYS:
                continue
            report.refuse(
                bwhere,
                "is a bare string where a battery group belongs. A group carries a `count`, an "
                "`ah` and a `domain_group`; prose under a prose key is fine, prose under a group's "
                "name is a group nobody can compare",
            )
            continue
        if not isinstance(battery, dict):
            report.refuse(bwhere, f"is a {type(battery).__name__}, not a mapping")
            continue
        group = battery.get("domain_group")
        if not group:
            report.refuse(bwhere, "declares no `domain_group`, so it is not linked to the domain")
            continue
        units = [c for c in components if str(c.get("id", "")).startswith(str(group))]
        if not units:
            report.refuse(bwhere, f"names domain_group {group!r}, which matches no component")
            continue
        if battery.get("count") != len(units):
            report.refuse(
                bwhere,
                f"declares {battery.get('count')} unit(s) and the power domain lists {len(units)} "
                f"({[c.get('id') for c in units]})",
            )
        for unit in units:
            if unit.get("ah") != battery.get("ah"):
                report.refuse(
                    bwhere,
                    f"declares {battery.get('ah')} Ah and {unit.get('id')!r} is {unit.get('ah')} Ah",
                )
            # The two files express the same cell's voltage in the shapes their readers need: a
            # group carries a *range* (open-circuit down to loaded) and a unit carries the nominal
            # it is modelled at. The nominal has to lie inside the range, which is the claim the
            # two shapes make about each other.
            nominal = unit.get("v_nominal")
            low, high = battery.get("min_loaded_v"), battery.get("open_circuit_v")
            if isinstance(low, (int, float)) and isinstance(high, (int, float)):
                if not isinstance(nominal, (int, float)) or not low <= nominal <= high:
                    report.refuse(
                        bwhere,
                        f"declares a loaded range {low}-{high} V and {unit.get('id')!r} is modelled "
                        f"at {nominal!r} V, which is not inside it",
                    )
            elif isinstance(battery.get("v"), (int, float)) and nominal != battery.get("v"):
                report.refuse(
                    bwhere,
                    f"declares {battery.get('v')} V and {unit.get('id')!r} is modelled at "
                    f"{nominal!r} V",
                )


def check_objectives(
    doc: dict[str, Any], registry: dict[str, dict[str, Any]], report: Report
) -> None:
    """What counts as success has to be evaluable, or the challenge has no score.

    The `term` field is prose and should be — it is what a person reads. What it could not do is say
    *who* settles the objective or *from what*, so nothing could score a run: four of the nine terms
    name a channel inside a sentence, `thermal_margin` ("worst zone margin, all phases") named none
    at all, and the three outcome objectives are sentences no machine can read. A challenge whose
    definition of success is prose is a strange thing for a challenge to have.

    Three checks, and the second is the one with teeth. Every objective declares `evaluated_by`, and
    a `vehicle` objective names at least one channel — because a judgement the vehicle is supposed
    to settle from its own record, naming no channel, is a judgement nobody can make. And **every
    channel an objective names must be one the vehicle is willing to publish**: an objective scored
    on a `not_published` truth is one the record cannot settle at all, which is the §7 boundary
    arriving in the scoring rather than in the telemetry.
    """
    objectives = doc.get("objectives") or []
    if not objectives:
        report.refuse("mission.yaml:objectives", "declares no objectives, so nothing is scored")
        return
    index = ChannelIndex(registry)
    withheld = withheld_channels(Path(__file__).resolve().parent.parent)
    for objective in objectives:
        where = f"mission.yaml:objectives.{objective.get('id')}"
        if not objective.get("term"):
            report.refuse(where, "states no term, so nobody can read what it wants")
        evaluator = objective.get("evaluated_by")
        if evaluator not in {"vehicle", "far_side", "external"}:
            report.refuse(
                where,
                f"declares evaluated_by {evaluator!r}. A term is a sentence; who settles it is a "
                "different question and the one that makes it scoreable — `vehicle` when the record "
                "settles it, `far_side` when the operator does, `external` when neither can",
            )
            continue
        channels = objective.get("channels") or []
        if evaluator == "vehicle" and not channels:
            report.refuse(
                where,
                "is settled by the vehicle and names no channel, so its verdict comes from nowhere "
                "the record contains",
            )
        for cid in channels:
            if cid not in index:
                report.refuse(where, f"names {cid!r}, which is not a registered channel")
            elif str(cid) in withheld:
                report.refuse(
                    where,
                    f"is scored on {cid!r}, which the domain declares `not_published`. An objective "
                    "the vehicle withholds the evidence for is one the record cannot settle — the "
                    "§7 boundary arriving in the scoring rather than in the telemetry",
                )
        if objective.get("kind") == "margin" and not objective.get("sense"):
            report.refuse(
                where,
                "is a margin and declares no `sense`. A margin's direction is not implied by its "
                "name: a propellant reserve wants to be high and a zone margin wants to be far from "
                "its limit, and the two are judged differently",
            )


def check_landing_site(doc: dict[str, Any], report: Report) -> None:
    """Re-derive where the Earth is in the LM's sky, because that is what the site decides.

    The site's coordinates are `chosen` — `apollo_diode.md:1206` asks that the scenario not match a
    historical site closely enough for a fleet to take the shortcut, and the corpus carries no
    coordinates for any site, flown or otherwise, so there is nothing to check the choice against.
    What *is* checkable is the geometry the choice produces, and it is the only thing the site
    decides that nothing else does: whether the LM can be heard at all while it is on the surface.

    The Earth sits near the sub-Earth point, so its elevation from a site at (lat, lon) is 90
    degrees minus the angular distance between them. Above the horizon the link is there for the
    whole stay and below it there is no link at any time, and there is no interesting in-between at
    this timescale — which is the second thing this checks, because a variation large enough to set
    and rise inside one surface phase would mean the premise was wrong.
    """
    site = doc.get("landing_site")
    if not isinstance(site, dict):
        report.refuse("mission.yaml:landing_site", "is missing or is not a mapping")
        return
    where = "mission.yaml:landing_site"
    for name in ("latitude_deg", "longitude_deg"):
        value = site.get(name)
        if not isinstance(value, (int, float)):
            report.refuse(where, f"declares {name} as {value!r}")
            return
    lat, lon = float(site["latitude_deg"]), float(site["longitude_deg"])
    if abs(lat) > 90 or abs(lon) > 180:
        report.refuse(where, f"declares ({lat}, {lon}), which is not a point on a sphere")
    sub = site.get("sub_earth") or {}
    libration = site.get("libration") or {}
    for name, block in (("sub_earth", sub), ("libration", libration)):
        if not isinstance(block, dict):
            report.refuse(where, f"declares {name} as {block!r}, not a mapping")
            return
    sub_lat = float(sub.get("latitude_deg", 0.0))
    sub_lon = float(sub.get("longitude_deg", 0.0))
    amplitude = libration.get("amplitude_deg")
    period_days = libration.get("period_days")
    if not isinstance(amplitude, (int, float)) or not isinstance(period_days, (int, float)):
        report.refuse(where, "declares no libration amplitude and period")
        return

    def elevation(site_lat: float, site_lon: float, e_lat: float, e_lon: float) -> float:
        cos_d = math.sin(math.radians(site_lat)) * math.sin(math.radians(e_lat)) + math.cos(
            math.radians(site_lat)
        ) * math.cos(math.radians(e_lat)) * math.cos(math.radians(site_lon - e_lon))
        return 90.0 - math.degrees(math.acos(max(-1.0, min(1.0, cos_d))))

    declared = site.get("earth_elevation_deg")
    if not isinstance(declared, (int, float)):
        report.refuse(where, "declares no earth_elevation_deg")
        return
    computed = elevation(lat, lon, sub_lat, sub_lon)
    if abs(computed - float(declared)) > abs(float(declared)) * 0.01 + 1e-9:
        report.refuse(
            where,
            f"declares earth_elevation_deg as {declared:g} and the geometry gives {computed:.4g} "
            f"from ({lat}, {lon}) to the sub-Earth point ({sub_lat}, {sub_lon})",
        )
    if computed <= 0.0:
        report.refuse(
            where,
            f"puts the Earth below the horizon ({computed:.1f} deg of elevation). A far-side site "
            "has no link at any time, so the surface phase would be flown in silence by design "
            "rather than by fault",
        )
    # The fastest the sub-Earth point moves, in degrees per hour, from an amplitude and a period.
    rate = 2.0 * math.pi * float(amplitude) / (float(period_days) * 24.0)
    surface = next(
        (p for p in doc.get("phases") or [] if p.get("id") == "surface"),
        None,
    )
    hours = float((surface or {}).get("duration_h") or 0.0)
    variation = rate * hours
    declared_variation = site.get("elevation_variation_over_surface_deg")
    if isinstance(declared_variation, (int, float)) and variation > max(
        float(declared_variation) * 1.01, float(declared_variation) + 1e-9
    ):
        report.refuse(
            where,
            f"declares the elevation to move {declared_variation:g} deg over the surface phase, and "
            f"libration at {rate:.4f} deg/h over {hours:g} h moves it {variation:.2f}. If it moved "
            "enough to set and rise inside one phase the LM's contact would be intermittent, which "
            "is a different mission",
        )
    # And the sub-Earth point must not wander far enough to change the answer.
    worst = min(
        elevation(lat, lon, sub_lat + d_lat, sub_lon + d_lon)
        for d_lat in (-float(amplitude), 0.0, float(amplitude))
        for d_lon in (-float(amplitude), 0.0, float(amplitude))
    )
    if worst <= 0.0:
        report.refuse(
            where,
            f"has the Earth {computed:.1f} deg up at the mean sub-Earth point and {worst:.1f} deg "
            f"at the edge of a {amplitude:g} deg libration envelope. A site whose link appears and "
            "disappears with libration is a surface mission whose comms plan depends on the month",
        )


def check_blackout(doc: dict[str, Any], report: Report) -> None:
    """Re-derive the lunar comms blackout from its own inputs, the way the trajectory is re-derived.

    `mission.yaml#comms_blackout` is the vehicle's one derived figure that can be checked against an
    operational one, and its own comment says so: "Apollo's loss-of-signal was about 45 minutes per
    revolution. The two figures differ by the orbit's eccentricity and by the Earth's own
    1.8-degree disc, both of which shorten the blackout slightly — so the derivation is right to
    within the effects it deliberately omits, and that is a stronger statement than a citation
    would be."

    That claim is only stronger than a citation if somebody re-does the arithmetic. Nothing did: no
    tool read the block at all, so the four numbers were a paragraph's worth of working with no
    referee — which is exactly the arrangement that let `E-RAD-WATER` say 3.8e-7 while its own
    relation computed 4.082e-7, a 7 % disagreement nobody could see. The tolerance is 1 %, matching
    the `computation` check, because the declared values are rounded for a reader: 71.0 against
    70.95, 0.395 against 0.3942, 46.5 against 46.43, 117.8 against 117.78.
    """
    block = doc.get("comms_blackout")
    if not isinstance(block, dict):
        report.refuse("mission.yaml:comms_blackout", "is missing or is not a mapping")
        return
    where = "mission.yaml:comms_blackout"
    orbit = block.get("orbit") or {}
    radius = block.get("moon_radius_km")
    mu = block.get("mu_moon_km3_s2")
    altitude = orbit.get("altitude_km")
    for name, value in (
        ("orbit.altitude_km", altitude),
        ("moon_radius_km", radius),
        ("mu_moon_km3_s2", mu),
        ("orbit.period_min", orbit.get("period_min")),
        ("half_angle_deg", block.get("half_angle_deg")),
        ("fraction_of_revolution", block.get("fraction_of_revolution")),
        ("duration_min", block.get("duration_min")),
    ):
        if not isinstance(value, (int, float)) or value <= 0:
            report.refuse(where, f"declares {name} as {value!r}")
            return
    if abs(mu - 4902.8) > 1e-6:
        # The constant is the derivation's root and it is not a design choice: a different mu is a
        # different Moon. Named so that a change is a decision rather than a typo.
        report.refuse(
            where,
            f"declares mu_moon {mu!r}; the lunar gravitational parameter is 4902.8 km^3/s^2, and a "
            "different value is a different Moon rather than a rounding",
        )
    a = float(radius) + float(altitude)
    period_min = 2.0 * math.pi * math.sqrt(a**3 / float(mu)) / 60.0
    half_angle = math.degrees(math.asin(float(radius) / a))
    fraction = 2.0 * half_angle / 360.0
    duration = fraction * period_min
    for name, computed, declared in (
        ("orbit.period_min", period_min, float(orbit["period_min"])),
        ("half_angle_deg", half_angle, float(block["half_angle_deg"])),
        ("fraction_of_revolution", fraction, float(block["fraction_of_revolution"])),
        ("duration_min", duration, float(block["duration_min"])),
    ):
        if abs(computed - declared) > abs(declared) * 0.01 + 1e-9:
            report.refuse(
                where,
                f"declares {name} as {declared:g} and its own derivation gives {computed:.4g}. "
                "The block states its arithmetic in full and nothing was re-doing it",
            )


def check_scenario_postures(doc: dict[str, Any], report: Report) -> None:
    """The difficulty scaling has to be *scale-invariant in the class*, or it cannot be applied.

    `apollo_diode.md:370-374` gives three postures and `mission.yaml` declares them. Each row has a
    critical hazard, a noncritical hazard and a demand-failure probability, and the corpus's 118
    fault policies declare a bare `hazard` with no class — 39 sitting exactly on one of the two
    baselines and **19 sitting between them** (`COM-09` at 1e-4 is 5x critical and 0.5x
    noncritical). A posture whose two hazard columns moved by different factors would therefore make
    a fault's new rate depend on a class the fault does not declare, and a third of the corpus would
    be unscalable.

    It works today only because both columns move x5 at `degraded` and x10 at `crisis` — which is
    a property of these numbers and not a rule anybody wrote down, so it is a rule now. This is the
    fourth time in this folder that something was true by coincidence and the fix was to say it.
    """
    postures = doc.get("scenario_postures") or []
    if not postures:
        report.refuse("mission.yaml:scenario_postures", "declares no postures, so nothing scales")
        return
    base = next((p for p in postures if p.get("id") == "nominal"), None)
    if base is None:
        report.refuse(
            "mission.yaml:scenario_postures",
            "declares no `nominal` posture, and every other row is a scaling *of* the baseline "
            "apollo gives — without it there is no divisor",
        )
        return
    for field in ("critical_hazard_per_h", "noncritical_hazard_per_h", "failure_on_demand_p"):
        value = base.get(field)
        if not isinstance(value, (int, float)) or value <= 0:
            report.refuse(
                "mission.yaml:scenario_postures.nominal", f"declares {field} as {value!r}"
            )
    for posture in postures:
        where = f"mission.yaml:scenario_postures.{posture.get('id')}"
        if not posture.get("seeded_faults"):
            report.refuse(where, "does not say which faults it seeds")
        if not posture.get("gm_disposition"):
            report.refuse(where, "declares no `gm_disposition`")
        for field in ("critical_hazard_per_h", "noncritical_hazard_per_h", "failure_on_demand_p"):
            value = posture.get(field)
            if not isinstance(value, (int, float)) or value <= 0:
                report.refuse(where, f"declares {field} as {value!r}")
        if posture is base:
            continue
        critical = posture["critical_hazard_per_h"] / base["critical_hazard_per_h"]
        noncritical = posture["noncritical_hazard_per_h"] / base["noncritical_hazard_per_h"]
        if abs(critical - noncritical) > 1e-9:
            report.refuse(
                where,
                f"scales its critical hazard by x{critical:g} and its noncritical hazard by "
                f"x{noncritical:g}. The fault policies declare a bare `hazard` and no class, and "
                "19 of the 58 sit between the two baselines, so a class-dependent factor leaves a "
                "third of the corpus unscalable. Move both columns together or give the faults a "
                "class",
            )


def check_mission(doc: dict[str, Any], vehicle: dict[str, Any], report: Report) -> None:
    """Phase durations must close, and every phase must name a real configuration."""
    phases = doc.get("phases") or []
    if not phases:
        report.refuse("mission.yaml", "declares no phases")
        return
    known = {c.get("id") for c in (vehicle.get("configurations") or [])}
    durations = 0.0
    seen: set[str] = set()
    for phase in phases:
        pid = phase.get("id", "?")
        where = f"mission.yaml:phase {pid}"
        if pid in seen:
            report.refuse(where, "declared twice")
        seen.add(pid)
        hours = phase.get("duration_h")
        if hours is None:
            report.debt(where, "has no duration, so MET cannot be computed")
        else:
            durations += float(hours)
        for cfg in phase.get("configurations") or []:
            if cfg not in known:
                report.refuse(
                    where, f"names configuration {cfg!r} which vehicle.yaml does not declare"
                )
        # `also_present` is the other half of the configuration list and it says something the list
        # cannot: which vehicles exist throughout the phase without being its subject. During
        # `descent` and `surface` the CSM is alone in lunar orbit, and naming it here is the only
        # way the vehicle can say where the CM pilot is — `plant.py --crew` reported her UNPLACED
        # for both phases until the field existed.
        sequence = list(phase.get("configurations") or [])
        for cfg in phase.get("also_present") or []:
            if cfg not in known:
                report.refuse(
                    where,
                    f"declares also_present {cfg!r}, which vehicle.yaml does not declare",
                )
            elif cfg in sequence:
                report.refuse(
                    where,
                    f"lists {cfg!r} in both `configurations` and `also_present`. The first is the "
                    "sequence the phase's subject passes through and the second is what else is "
                    "there, so one configuration cannot be both",
                )
        # A phase used to carry `allowed_verbs` as the inverse of the verbs' `allowed_phases`.
        # By the time eight domains had landed the two lists disagreed in 197 places, and the
        # phase side was the weaker claim: it cannot know whether a verb's guards are
        # satisfiable, and `state.json`'s capability snapshot publishes the authoritative answer
        # at runtime anyway. Deleted rather than synchronised, so a return is a regression.
        if phase.get("allowed_verbs") is not None:
            report.refuse(
                where,
                "declares `allowed_verbs`, which is a second source of truth for a relation the "
                "verb registry already owns; the derived view is "
                "`tools/check_vehicle.py --phases`",
            )
        for field in ("entry", "exit"):
            if not phase.get(field):
                report.note(where, f"has no {field} condition")
    total = doc.get("total_duration_h")
    if total is not None and abs(durations - float(total)) > 0.51:
        report.refuse(
            "mission.yaml",
            f"phase durations sum to {durations:g} h but total_duration_h is {total:g} h",
        )
    if doc.get("met_epoch_utc") is None:
        report.debt(
            "mission.yaml", "met_epoch_utc is unset, so every timestamp is relative to nothing"
        )
    if not (doc.get("crew") or {}).get("size"):
        report.debt("mission.yaml", "crew size is unset, so metabolic loads cannot be computed")
    if doc.get("postures") is None:
        report.debt(
            "mission.yaml",
            "declares no execution postures, so the mission has no way to say it is held, "
            "aborting or over — the phase ladder alone cannot distinguish an abandoned mission "
            "from a successful one (mission_diode.md:300-347, invariant J)",
        )


def check_crew_bindings(
    mission: dict[str, Any],
    vehicle: dict[str, Any],
    registry: dict[str, dict[str, Any]],
    report: Report,
    station_ids: set[str] | None = None,
) -> None:
    """The crew are described twice, in two files, and nothing had ever compared the two.

    `mission.yaml#crew` is a personnel model — `size`, `surface_party`, and one entry per person
    with `location_phase_default` and `goes_to_surface`. `vehicle.yaml#configurations` is a hardware
    model — `crew_aboard` and `crew_in` per configuration. Both are right, they overlap on every
    question a fleet can ask, and **all five fields were read by nothing**: not by a tool, not by
    the other file, not by a sentence in any document. Four checks, each of which is a way the two
    could disagree while both looking complete.
    """
    crew = mission.get("crew") or {}
    positions = crew.get("positions") or []
    configurations = vehicle.get("configurations") or []
    if not positions or not configurations:
        report.debt(
            "mission.yaml#crew",
            "no crew positions or no vehicle configurations, so the two crew models cannot be "
            "compared at all",
        )
        return

    size = crew.get("size")
    if not isinstance(size, int):
        report.refuse("mission.yaml#crew", f"declares size {size!r}, not an integer")
    surface_party = crew.get("surface_party")
    to_surface = [p for p in positions if p.get("goes_to_surface")]
    if not isinstance(surface_party, int):
        report.refuse("mission.yaml#crew", f"declares surface_party {surface_party!r}")
    elif surface_party != len(to_surface):
        report.refuse(
            "mission.yaml#crew",
            f"declares surface_party {surface_party} and {len(to_surface)} position(s) carry "
            f"`goes_to_surface: true` ({[p.get('id') for p in to_surface]}). The party that lands "
            "is the party the LM carries, so the two numbers are one number",
        )
    aboard = [c.get("crew_aboard") for c in configurations if isinstance(c.get("crew_aboard"), int)]
    if isinstance(size, int) and aboard and max(aboard) != size:
        report.refuse(
            "mission.yaml#crew",
            f"declares size {size} and the largest `crew_aboard` across vehicle.yaml's "
            f"configurations is {max(aboard)}. A crew size no configuration can hold is a number "
            "nobody is standing in",
        )
    # Where a crew member is when nothing else says. **The field names a vehicle, not a station**,
    # and the distinction is the point of checking it: `location_phase_default: csm` is answered
    # against `crew_in`'s vocabulary (`csm`, `lm`), while `channels.yaml#crew_positions` names
    # *stations* (`csm_commander`, `csm_navigator`, `csm_lower_equipment_bay`, `lm_commander`,
    # `lm_pilot`, `tunnel`, `surface_eva`). A vehicle can hold three people at three stations, so
    # "which vehicle" is a question this field can answer and "which station" is one it cannot —
    # and `ask_crew`'s perception bound is per *station*. The mapping from a person to their
    # station is therefore undeclared, which is recorded in `open_debts` rather than invented here.
    vehicles = {str(c.get("crew_in")) for c in configurations if isinstance(c.get("crew_in"), str)}
    for person in positions:
        where = f"mission.yaml#crew.position {person.get('id')}"
        default = person.get("location_phase_default")
        if default is not None and default not in vehicles:
            report.refuse(
                where,
                f"declares location_phase_default {default!r}, which is not a vehicle any "
                f"configuration carries crew in ({sorted(vehicles)})",
            )
        if person.get("goes_to_surface") is None:
            report.refuse(where, "does not say whether this crew member goes to the surface")
    # And a configuration has to say *which vehicle* the crew are in, in a term the positions use.
    # `crew_in` has to be a term the *station* vocabulary already uses as a prefix, or the two
    # namings of the same place never meet. Without this the check is circular: `crew_in` is
    # validated against `location_phase_default` and that against `crew_in`, so a configuration
    # saying the crew are in a `cockpit` would agree with a mission saying the same and neither
    # would be a station anybody can be asked a question from.
    prefixes = {str(s).split("_")[0] for s in (station_ids or set())}
    for config in configurations:
        aboard_in = config.get("crew_in")
        if not aboard_in:
            report.refuse(
                f"vehicle.yaml:configuration {config.get('id')}",
                "names no `crew_in`, so a crew member aboard it cannot be placed at a station",
            )
        elif prefixes and str(aboard_in) not in prefixes:
            report.refuse(
                f"vehicle.yaml:configuration {config.get('id')}",
                f"carries crew in {aboard_in!r}, which is not the prefix of any station in "
                f"`channels.yaml#crew_positions` ({sorted(prefixes)})",
            )

    # --------------------------------------------------------------------------------------
    # Where each person *is*, which is a different question from which vehicle they are in.
    #
    # `location_phase_default` answers "csm" and a station is `csm_commander` or `csm_navigator`
    # or `csm_lower_equipment_bay` — three different panel sets and three different `cannot_see`
    # lists, which is what `ask_crew`'s perception bound is computed from. Until this map existed
    # the corpus named three crew and three CSM stations and never said who sat where, so a
    # question asked from "csm" was a question asked from nowhere. The map is `chosen`, with the
    # reasoning in the file, because the corpus gives the geometry and not the assignment.
    # --------------------------------------------------------------------------------------
    stations = station_ids or set()
    for person in positions:
        where = f"mission.yaml#crew.position {person.get('id')}"
        declared = person.get("stations")
        if not isinstance(declared, dict) or not declared:
            report.refuse(
                where,
                "declares no `stations` map, so this crew member cannot be placed at a station "
                "and `ask_crew`'s perception bound has nothing to bound against",
            )
            continue
        for vehicle, station in declared.items():
            if vehicle not in vehicles:
                report.refuse(
                    where,
                    f"declares a station in {vehicle!r}, which is not a vehicle any configuration "
                    f"carries crew in ({sorted(vehicles)})",
                )
            if stations and station not in stations:
                report.refuse(
                    where,
                    f"declares station {station!r}, which is not in "
                    "`channels.yaml#crew_positions` — so it names a place with no panels and no "
                    "perception bound",
                )
        # A crew member who does not go to the surface has no station in the LM, and one who does
        # has both. Either way the map covers exactly the vehicles they can be in.
        if person.get("goes_to_surface") and "lm" in vehicles and "lm" not in declared:
            report.refuse(
                where,
                "goes to the surface and declares no LM station, so the phase that matters most "
                "for this person is the one they cannot be placed in",
            )
        if not person.get("goes_to_surface") and "lm" in declared:
            report.refuse(
                where,
                f"does not go to the surface and declares an LM station ({declared['lm']!r}); a "
                "station in a vehicle this crew member never boards is a place they can be asked "
                "a question from but never are",
            )
    # Two people cannot occupy one seat, and a map that put them there would give two crew the
    # same perception bound while the corpus insists the stations differ.
    for vehicle in sorted(vehicles):
        seats: dict[str, list[str]] = {}
        for person in positions:
            station = (person.get("stations") or {}).get(vehicle)
            if station:
                seats.setdefault(str(station), []).append(str(person.get("id")))
        for station, occupants in sorted(seats.items()):
            if len(occupants) > 1:
                report.refuse(
                    "mission.yaml#crew",
                    f"seats {occupants} in {vehicle!r} station {station!r}. `crew_positions` gives "
                    "each station its own panels and its own `cannot_see` list, so two crew at one "
                    "station would be two people with one perception bound",
                )

    # The finding this check was written for, reported rather than refused because the fix is a
    # decision rather than a repair. `descent` and `surface` name only LM configurations, so no
    # configuration in those phases can hold the CSM pilot — while her own note in this same file
    # says she is "alone in the CSM for the surface phase". The two readings of `configurations`
    # (the configurations a phase passes *through* versus the vehicles *present* in it) give
    # different answers here and the corpus never says which it means.
    by_id = {c.get("id"): c for c in configurations}
    for phase in mission.get("phases") or []:
        vehicles = {
            str(by_id[name].get("crew_in"))
            for name in phase.get("configurations") or []
            if name in by_id and by_id[name].get("crew_in")
        }
        missing = sorted(
            {
                str(person.get("location_phase_default"))
                for person in positions
                if person.get("location_phase_default") not in vehicles
            }
        )
        if missing:
            report.debt(
                f"mission.yaml:phase {phase.get('id')}",
                f"names no configuration holding {missing}, so a crew member whose default station "
                "is there cannot be placed in this phase",
            )


def check_mission_bindings(
    channels: dict[str, Any],
    mission: dict[str, Any],
    registry: dict[str, dict[str, Any]],
    report: Report,
    vehicle: dict[str, Any] | None = None,
) -> None:
    """The mission file and the channel registry describe one machine, so they must agree.

    Each binding here is a place where two files could drift apart with nothing to object.
    `mission.phase` is the value set every verb's `allowed_phases` is written against, so an
    enum that does not match `mission.yaml`'s phases means the phase gate compares a command
    against a vocabulary the mission never uses — and it would compare equal to nothing, which
    reads as "wrong phase" for every command in every phase. The same for the posture. And the
    freshness manifest is a set of claims about channels and ages, which is exactly the kind of
    claim that goes silently wrong when a publish rate changes.
    """
    index = ChannelIndex(registry)
    audit = channels.get("mission") or []
    rows = {row.get("id"): row for row in audit if isinstance(row, dict)}
    # A channel that enumerates hardware has to enumerate the hardware the vehicle declares.
    # `comm.antenna`'s four members are `vehicle.yaml#comms.antennas`; a member that no entry
    # declares is an antenna a fleet can select and the vehicle does not have, which is the same
    # defect the phase and posture vocabularies are checked for and is worth one more table.
    for section, key, source in (
        (channels.get("comms") or [], "comm.antenna", (vehicle or {}).get("comms") or {}),
    ):
        declared = [str(a.get("id")) for a in (source.get("antennas") or [])]
        if not declared:
            continue
        for row in section:
            if not isinstance(row, dict) or row.get("id") != key:
                continue
            enum = re.findall(r"enum\[([^\]]*)\]", str(row.get("unit") or ""))
            listed = [member.strip() for member in enum[0].split(",")] if enum else []
            for missing in sorted(set(declared) - set(listed)):
                report.refuse(
                    f"channels.yaml:{key}",
                    f"cannot select {missing!r}, which vehicle.yaml declares as an antenna",
                )
            for extra in sorted(set(listed) - set(declared)):
                report.refuse(
                    f"channels.yaml:{key}",
                    f"offers {extra!r}, which vehicle.yaml does not declare as an antenna",
                )

    for cid, expected in (
        ("mission.phase", [str(p.get("id")) for p in mission.get("phases") or []]),
        ("mission.posture", [str(p.get("id")) for p in mission.get("postures") or []]),
    ):
        row = rows.get(cid)
        if row is None:
            report.debt(f"channels.yaml:{cid}", "is not registered")
            continue
        enum = re.findall(r"enum\[([^\]]*)\]", str(row.get("unit") or ""))
        listed = [member.strip() for member in enum[0].split(",")] if enum else []
        for missing in sorted(set(expected) - set(listed)):
            report.refuse(
                f"channels.yaml:{cid}",
                f"omits {missing!r}, which mission.yaml declares: the gate would compare "
                "against a value the mission never takes",
            )
        for extra in sorted(set(listed) - set(expected)):
            report.refuse(
                f"channels.yaml:{cid}", f"offers {extra!r}, which mission.yaml does not declare"
            )

    # The freshness manifest. `mission_diode.md:1241-1262` requires it to be machine-readable
    # and to generate both runtime checks and verification tests; what the linter adds is the
    # part that matters — refusing a requirement the vehicle cannot meet, because a guard
    # demanding evidence fresher than the publisher publishes never passes, and a transition
    # that can never fire is a mission that can never run.
    for entry in mission.get("transition_evidence") or []:
        where = f"mission.yaml:transition {entry.get('transition')!r}"
        if not entry.get("guard"):
            report.refuse(where, "names no guard")
        for cid, requirement in (entry.get("requires") or {}).items():
            if cid not in index:
                report.refuse(where, f"requires {cid!r}, which is not a registered channel")
                continue
            row = index.row(cid) or {}
            if not (requirement or {}).get("why"):
                report.refuse(f"{where}/{cid}", "gives no reason for the freshness requirement")
            age = (requirement or {}).get("max_age_ms")
            if not isinstance(age, (int, float)):
                report.refuse(f"{where}/{cid}", "declares no max_age_ms")
                continue
            resolved = decision_age_ms(row)
            if resolved is None:
                report.refuse(
                    f"{where}/{cid}",
                    "names a channel with no resolvable decision age, so the requirement cannot "
                    "be met by any publisher",
                )
            elif age < resolved:
                report.refuse(
                    f"{where}/{cid}",
                    f"requires evidence no older than {age:g} ms, but the channel's own maximum "
                    f"decision age is {resolved:g} ms: every sample would already be too old, so "
                    "the transition could never fire",
                )


def order_report(root: Path, schedule: list[str], coupling: dict[str, Any], report: Report) -> None:
    """Print the derived tick order, node by node, with the states that advance each one.

    This is the artifact `plant.md:71` promises and the one an implementer needs: the total order
    over nodes, and within each node the order the states advance in. Both are derived — the node
    order from `coupling.yaml`'s edges minus the declared back-edges, the intra-node order from
    each node's `state_order` — so neither can drift from the configuration that produces it.
    """
    by_node: dict[str, list[tuple[str, str]]] = {}
    domains_dir = root / "domains"
    if domains_dir.is_dir():
        for path in sorted(p for p in domains_dir.iterdir() if p.is_dir()):
            components = load(path / "components.yaml", Report()) or {}
            for state in components.get("state") or []:
                node = str(state.get("node"))
                if node and node != "internal":
                    by_node.setdefault(node, []).append((path.name, str(state.get("id"))))
    nodes = coupling.get("nodes") or {}
    print(f"--- tick order: {len(schedule)} nodes (dependencies first) ---")
    for index, node in enumerate(schedule, start=1):
        declared = (nodes.get(node) or {}).get("state_order")
        producers = by_node.get(node) or []
        if isinstance(declared, list):
            ordered = [str(s) for s in declared]
        elif declared == "independent":
            ordered = [sid for _, sid in sorted(producers)]
        else:
            ordered = [sid for _, sid in sorted(producers)]
        domain = (nodes.get(node) or {}).get("domain", "?")
        # A node with one producing state has nothing to order; only a group needs a declaration.
        suffix = (
            "  [order undeclared]"
            if len(producers) > 1
            and declared not in ("independent",)
            and not isinstance(declared, list)
            else ""
        )
        label = "  [independent]" if declared == "independent" else suffix
        print(f"{index:3d}. {node:20s} ({domain}){label}")
        for state_id in ordered:
            print(f"       {state_id}")
    internal = sum(
        1
        for path in sorted((root / "domains").iterdir())
        if path.is_dir()
        for state in (load(path / "components.yaml", Report()) or {}).get("state") or []
        if state.get("node") == "internal"
    )
    print(f"\n{internal} further states are internal to a domain and are advanced with it.")


def debts_report(report: Report) -> None:
    """Group the owed list by the *scalar* each debt wants rather than by the path that reaches it.

    Round 74 found that twenty-six thresholds reported `assert` and `clear` separately — one missing
    limit counted as two — and this view is how that class of inflation is looked for now. It splits
    the list into the literal `UNCONFIGURED` scalars and the prose `open_debts`, groups the first by
    the field they want, and reports the second by file.

    **Run against the corpus the round after, the prose half is clean**: the nineteen per-edge debts
    in `coupling.yaml` and the seventeen edge ids named inside its eight `open_debts` sentences are
    disjoint, and the one subject named from two files — the inertia tensor, in `coupling.yaml` and
    `domains/rcs/` — is one missing datum with two genuinely different consequences, each recorded
    where it bites. That is the folder's style rather than a double-count, and it is worth being able
    to say so: the count is the headline claim, and a reader is entitled to know which half of it has
    been examined.
    """
    import collections

    owed = [row.split(": ", 1)[1] for row in report.debts]
    paths = [row.split(": ", 1)[0] for row in report.debts]
    literal = [(p_, w) for p_, w in zip(paths, owed, strict=True) if w == "is UNCONFIGURED"]
    prose = [(p_, w) for p_, w in zip(paths, owed, strict=True) if w != "is UNCONFIGURED"]
    print(f"\n{len(report.debts)} owed, grouped by what each wants:\n")
    print(f"  {len(literal):4d}  a literal `UNCONFIGURED` scalar")
    print(f"  {len(prose):4d}  a prose obligation in an `open_debts` list")
    fields = collections.Counter(p.rsplit(".", 1)[-1].split("[")[0] for p, _ in literal)
    if fields:
        print("\n  the literal half, by the field it wants:")
        for field, count in fields.most_common(8):
            print(f"    {count:4d}  {field}")
    files = collections.Counter(p.split(":")[0] for p, _ in prose)
    if files:
        print("\n  the prose half, by the file that keeps it:")
        for name, count in files.most_common(6):
            print(f"    {count:4d}  {name}")


def phases_report(
    root: Path, mission: dict[str, Any], registry_by_verb: dict[str, dict[str, Any]]
) -> None:
    """Print, per phase, the verbs the registry allows — derived, never authored.

    `mission.yaml` used to carry the inverse of this relation as `allowed_verbs` on each phase,
    and by the time eight domains had landed the two lists disagreed in 197 places. A phase
    cannot be authoritative about what its verbs permit, because permission lives on the verb
    (`apollo_diode.md:439`), and a fleet is told the answer at runtime by the capability snapshot
    in `state.json` anyway (vocabulary §1). What this mode is for is the *author*: it is the view
    the deleted field was trying to provide, and it cannot drift because it is computed.
    """
    order = [str(p.get("id")) for p in mission.get("phases") or []]
    print("--- verbs by phase (derived from domains/*/commands.yaml) ---")
    for pid in order:
        rows = [
            (verb, row)
            for verb, row in sorted(registry_by_verb.items())
            if pid in (row.get("allowed_phases") or [])
        ]
        print(f"\n{pid}: {len(rows)} verb(s)")
        for verb, row in rows:
            flags = []
            if row.get("irreversible"):
                flags.append("irreversible")
            if row.get("execution_class") == "deferred":
                flags.append("deferred")
            suffix = f"  [{', '.join(flags)}]" if flags else ""
            print(f"    {row.get('authority', '?'):3s} {verb}{suffix}")
    undeclared = sorted(
        verb for verb, row in registry_by_verb.items() if not (row.get("allowed_phases") or [])
    )
    if undeclared:
        print(f"\nverbs allowed in no phase: {', '.join(undeclared)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check that the vehicle definition composes.")
    parser.add_argument("--dir", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--strict", action="store_true", help="treat unfilled debts as failures")
    parser.add_argument(
        "--phases",
        action="store_true",
        help="also print the verb-by-phase view derived from the command registries",
    )
    parser.add_argument(
        "--debts",
        action="store_true",
        help="also group the owed list by the scalar each debt wants, not by the path that reaches it",
    )
    parser.add_argument(
        "--order",
        action="store_true",
        help="also print the derived tick order, node by node, with the states at each",
    )
    args = parser.parse_args(argv)

    root = Path(args.dir)
    if not root.is_dir():
        sys.stderr.write(f"not a directory: {root}\n")
        return 3

    report = Report()
    presentation = load(root / "presentation.yaml", report)
    channels = load(root / "channels.yaml", report)
    coupling = load(root / "coupling.yaml", report)
    vehicle = load(root / "vehicle.yaml", report)
    mission = load(root / "mission.yaml", report)
    # The domain files are walked for unset values by `check_domain` and the coupling graph by
    # `check_coupling`'s edge walk, so between them every `UNCONFIGURED` scalar in the vehicle was
    # counted — except the ones in the two files nothing walks. **Eleven were being counted by
    # nothing at all**, and the folder's headline number is the debt count, so the omission was
    # invisible by construction: `mission.yaml`'s initial position, velocity and state-vector basis
    # and `vehicle.yaml`'s three minimum impulse bits, the LM sublimator's rejection and water
    # consumption, and its radiators' unset values. Two of those are named in a prose
    # `open_debts` entry somewhere else, which is how they stayed plausible.
    #
    # So the walk is over every top-level document. `coupling.yaml` is deliberately absent: all
    # thirty of its unset scalars are edge sensitivity fields, and the edge walk already reports
    # them against the edge they belong to, which is a more useful place to read them.
    for name, doc in (
        ("mission.yaml", mission),
        ("vehicle.yaml", vehicle),
        ("channels.yaml", channels),
        ("presentation.yaml", presentation),
    ):
        for trail in walk_unset(doc):
            report.debt(f"{name}.{trail}", "is UNCONFIGURED")

    registry: dict[str, dict[str, Any]] = {}
    if channels is not None:
        registry = check_channels(channels, report)
        check_crew(channels, registry, report)
    schedule: list[str] = []
    if coupling is not None:
        # A regime table steps through thresholds that already exist, so the linter has to know
        # which ones there are and what each watches before it can check a single regime.
        threshold_point: dict[str, str] = {}
        domains_dir = root / "domains"
        if domains_dir.is_dir():
            for path in sorted(p for p in domains_dir.iterdir() if p.is_dir()):
                profiles = load(path / "profiles.yaml", Report()) or {}
                for threshold in profiles.get("thresholds") or []:
                    if threshold.get("id") and threshold.get("point"):
                        threshold_point[str(threshold["id"])] = str(threshold["point"])
        check_coupling(
            coupling,
            registry,
            report,
            produced=produced_channels_by_node(root, registry),
            threshold_point=threshold_point,
            enum_values=enumerated_values_by_node(root),
            withheld=withheld_channels(root),
            states_by_node_map=states_by_node(root),
            state_methods=state_methods(root),
        )
        schedule = derive_schedule(coupling, report)
    check_range_kinds(registry, report)
    threshold_ids: set[str] = set()
    all_commands: dict[str, dict[str, Any]] = {}
    if (root / "domains").is_dir():
        for path in sorted(p for p in (root / "domains").iterdir() if p.is_dir()):
            profiles = load(path / "profiles.yaml", Report()) or {}
            for threshold in profiles.get("thresholds") or []:
                if threshold.get("id"):
                    threshold_ids.add(str(threshold["id"]))
            all_commands[path.name] = load(path / "commands.yaml", Report()) or {}
    check_profile_immutability(all_commands, threshold_ids, report)
    check_domains(root, registry, coupling, channels, report)
    # `vehicle.yaml` is the file every cross-file check joins against, so when it cannot be
    # loaded the answer is not to run them with a hole in the middle of the argument list — it is
    # to say once that the joins are unavailable and carry on with the checks that do not need it.
    # The alternative was measured: the file was made unparseable and the linter died with
    # `AttributeError: 'NoneType' object has no attribute 'get'` **from inside a check**, losing
    # the report that named the parse error. A linter that crashes on the input it exists to
    # diagnose is worse than one that misses a fault, because the operator sees a traceback and
    # reasonably concludes the tool is broken rather than the definition.
    if vehicle is not None:
        check_vehicle(vehicle, report)
        check_electrical_bindings(root, vehicle, report)
        check_thermal_bindings(root, vehicle, report)
    else:
        report.refuse(
            "vehicle.yaml",
            "could not be loaded, so every check that joins another file against it — the "
            "electrical and thermal inventories, the phase-to-configuration names, the crew "
            "placements and the propulsion budgets — is unavailable for this run",
        )
    check_power_inventory(root, report)
    check_thermal_heat_inputs(root, report)
    check_cabin_equilibrium(root, vehicle, report)
    check_metabolic_rules(root, vehicle, mission, report)
    if mission is not None:
        if vehicle is not None:
            check_mission(mission, vehicle, report)
            check_propulsion(mission, vehicle, report)
            if channels is not None:
                check_mission_bindings(channels, mission, registry, report, vehicle)
                check_crew_bindings(
                    mission,
                    vehicle,
                    registry,
                    report,
                    {str(p.get("id")) for p in (channels or {}).get("crew_positions") or []},
                )
        check_scenario_postures(mission, report)
        check_blackout(mission, report)
        check_landing_site(mission, report)
        if channels is not None:
            check_objectives(mission, registry, report)
        check_trajectory(mission, report)
        check_met_clock(mission, report)
    check_cabin_pairing(registry, presentation, report)
    check_atmosphere_symmetry(root, report)
    check_zone_nodes(root, vehicle, report)
    # The fleets' view: collected from every registry, because a gate variable is declared on a
    # verb and published in `state.json`, and nothing else in the tool joins the two.
    all_verbs: dict[str, dict[str, Any]] = {}
    domains_dir = root / "domains"
    if domains_dir.is_dir():
        for path in sorted(p for p in domains_dir.iterdir() if p.is_dir()):
            commands = load(path / "commands.yaml", Report()) or {}
            for verb in commands.get("commands") or []:
                all_verbs[str(verb.get("verb"))] = verb
    check_presentation(presentation or {}, registry, all_verbs, report)
    if channels is not None:
        check_producers(root, registry, presentation or {}, report)
        check_event_classes(root, registry, report)

    if schedule:
        # Reported before the print so it appears in the report, and it is a note rather than a
        # refusal because the schedule's *existence* is what is checked, not its content: the
        # order is derived, so there is nothing for a human to agree with.
        report.note(
            "coupling.yaml:schedule",
            f"the node schedule is {len(schedule)} nodes; the last eight are "
            + " -> ".join(schedule[-8:]),
        )
    report.print()
    if args.order and coupling is not None:
        order_report(root, schedule, coupling, report)
    if args.debts:
        debts_report(report)
    if args.phases and mission is not None:
        verbs: dict[str, dict[str, Any]] = {}
        domains_dir = root / "domains"
        if domains_dir.is_dir():
            for path in sorted(p for p in domains_dir.iterdir() if p.is_dir()):
                commands = load(path / "commands.yaml", Report()) or {}
                for verb in commands.get("commands") or []:
                    verbs[str(verb.get("verb"))] = verb
        phases_report(root, mission, verbs)
    print(
        f"\n{len(registry)} channels, "
        f"{len((coupling or {}).get('edges') or [])} edges, "
        f"{len((coupling or {}).get('failure_chains') or [])} failure chains, "
        f"{len((mission or {}).get('phases') or [])} mission phases."
    )
    if report.refusals:
        print(f"\nREFUSED: {len(report.refusals)} fault(s) must be fixed.")
        return 1
    if report.debts:
        print(f"\nCOMPOSES, with {len(report.debts)} declared debt(s).")
        return 2 if args.strict else 0
    print("\nCOMPOSES clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
