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
# The time basis of a seeded hazard rate. One word, declared on all sixty-six rate-seeded faults,
# and the scheduler multiplies by it — so it is a vocabulary rather than a string.
SEEDING_RATE_UNITS = {"per_h"}

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
    "spacecraft",  # `check_spacecraft_vocabulary`: every `vehicle:` is held against this list
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


def duplicate_keys(text: str, document: Any = None) -> list[tuple[int, str]]:
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

    The composed document may be passed in: `load` has already parsed the file to check it, and
    parsing it a second time here was half of everything this linter spent its time on.
    """
    if document is None:
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


def significant_figures(value: float) -> int:
    """How many digits a declared number is written to, read off its own decimal representation.

    Trailing zeros **count**, and that is the whole of this function's judgement. The first version
    stripped them, so `coolant_mass_kg: 30` read as one significant figure — and one significant
    figure of 26.25 is 30, so a coolant mass of 30 kg agreed with a volume and a density that
    determine 26.25. The fixture written to catch exactly that did not fire, which is how the rule
    was found to be too permissive.

    A person who writes `30` means thirty rather than "thirty to one figure", and a person who
    writes `1.0` means two digits. Counting what is written is the same reading of precision the
    beamwidth tolerance uses — half a unit in the last place — and it errs toward refusing, which
    is the direction a check is allowed to be wrong in.
    """
    text = repr(float(value))
    mantissa = text.split("e")[0].split("E")[0]
    digits = mantissa.lstrip("-").replace(".", "").lstrip("0")
    return max(len(digits), 1)


def round_to_significant(value: float, digits: int) -> float:
    if value == 0:
        return 0.0
    return float(f"{value:.{max(digits - 1, 0)}e}")


def agrees_with_derivation(declared: float, derived: float) -> bool:
    """Whether a derived number *is* the declared number, at the precision it is declared to.

    A relative tolerance is the wrong instrument here, and the corpus proves it with two of its own
    values. `E-GEOM-LINK` declares -0.54 dB per degree for a derivative that is -0.54325: it is
    0.6 % away from its own derivation, because two significant figures is how it is written.
    `E-CREW-WATER` declares 0.094583 for 2.27/24 = 0.09458333, which is 0.0004 % away, because six
    is. One tolerance loose enough for the first is blind to a 0.9 % drift in the second — and
    0.9 % of a metabolic rate is a whole step in its second decimal, which is a change somebody
    would make on purpose. The first version of this check used a flat 1 % and let exactly that
    through.

    So the comparison is at the declared value's own precision: the derivation must round to the
    number in the file. That is the claim a declared value makes about its own arithmetic, and it
    is the same reading of precision the beamwidth tolerance uses — half a unit in the last place
    is the widest two figures can differ and still be the same declared number.
    """
    digits = significant_figures(declared)
    return round_to_significant(derived, digits) == round_to_significant(declared, digits)


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
        computed = evaluate_expression(str(computation))
    except Exception as exc:  # noqa: BLE001 - any failure is a refusal
        report.refuse(where, f"has a `computation` the linter cannot evaluate: {exc}")
        return
    if not agrees_with_derivation(float(declared), computed):
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


# The parse cache, and the reason the linter is fast enough to be a test fixture.
#
# `load` is called **586 times** for this corpus's thirty-odd files, because every check that joins
# two documents reads both of them again, and each call parsed its file twice: once with
# `yaml.compose` for the duplicate-key walk and once with `safe_load` for the document. Profiling a
# run puts **95 % of it** in those two parses — 1172 documents composed to produce thirty.
#
# So the *parse* is memoised, keyed by the path and its mtime and size rather than by the path
# alone: a caller that rewrites a file between two loads is a real pattern (a fixture that moves a
# value, a plant that reloads the world) and must not be served a stale document. Everything the
# call site does with the result still runs on every load — the duplicate-key walk, the absorbed-key
# walk and `check_answered_debts` all report per call, exactly as before — so what the linter says
# is unchanged and only the parsing is shared.
_PARSE_CACHE: dict[tuple[str, int, int], tuple[str, Any, dict[str, Any] | None, str | None]] = {}


def _parsed(path: Path) -> tuple[str, Any, dict[str, Any] | None, str | None]:
    """One file's text, its composed node, its loaded document, and its parse error if it had one."""
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        key = (str(path), 0, 0)
    hit = _PARSE_CACHE.get(key)
    if hit is not None:
        return hit
    text = path.read_text()
    try:
        composed: Any = yaml.compose(text)
    except yaml.YAMLError:
        composed = None
    try:
        data: dict[str, Any] | None = yaml.safe_load(text)
        error: str | None = None
    except yaml.YAMLError as exc:
        data, error = None, str(exc)
    result = (text, composed, data, error)
    _PARSE_CACHE[key] = result
    return result


def load(path: Path, report: Report) -> dict[str, Any] | None:
    if not path.exists():
        report.refuse(path.name, "not present")
        return None
    text, composed, data, error = _parsed(path)
    for line, trail in duplicate_keys(text, composed):
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
    if error is not None:  # a config that does not parse is not a config
        report.refuse(path.name, f"does not parse: {error}")
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


def check_placeholders(docs: dict[str, dict[str, Any]], report: Report) -> None:
    """A value the plant integrates with, inside an entry whose magnitudes are owed, says so.

    `basis: UNCONFIGURED` is a *decision*, not an undecided one: it is the class for a value no source
    supplies, and the twenty-eight entries carrying it are correctly declared. Round 76 set out to
    settle them and found there was nothing to settle — which is the useful answer, and it corrected
    the round-75 framing that called them open judgements.

    But two of them carry a number the plant **uses** while the entry says its magnitudes are owed:
    `pressurant_pressure_psi.tau_s = 5` (integrated as a lag, and the seed of PRP-03) and
    `pressurant_he_kg.quantum = 1.0e-05` (the fixed-point stock cannot exist without one). Read
    together with `basis: UNCONFIGURED` those look like contradictions until a reader finds the
    sentence that reconciles them, and a reader who does not find it cannot tell whether to use the
    number or ignore it.

    So an entry whose basis is unconfigured and which carries a numeric integrator parameter must
    declare *that* parameter a placeholder, with the reason. The rule names the four parameters the
    plant reads — `tau_s`, `quantum`, `delay_s`, `lambda_per_h` — because those are the ones whose
    absence stops a tick rather than merely leaving a quantity unset.
    """
    for state in (docs.get("components.yaml") or {}).get("state") or []:
        if not isinstance(state, dict):
            continue
        basis = (state.get("provenance") or {}).get("basis")
        if basis != "UNCONFIGURED":
            continue
        for field in ("tau_s", "quantum", "delay_s", "lambda_per_h"):
            value = state.get(field)
            if not isinstance(value, (int, float)):
                continue
            if not state.get(f"{field}_placeholder"):
                report.refuse(
                    f"components.yaml:state {state.get('id')}",
                    f"declares `{field}: {value}` inside an entry whose basis is UNCONFIGURED, and "
                    f"does not say it is a placeholder. The plant *uses* this number while the entry "
                    "says its magnitudes are owed, and the two read as a contradiction until the "
                    f"reason is stated in `{field}_placeholder`",
                )


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


PROSE_FIELDS = {"note", "notes", "reason", "relation", "why"}

# A note is allowed to point at another entry — "as above, for the LM" is a real argument
# given twice with the difference stated once — but the pointer has to leave a clause behind.
POINTER_PHRASE = re.compile(
    r"\A\s*(?:as\s+above\b|see\s+above\b|same\s+as\s+above\b|as\s+before\b|ditto\b|ibid\b\.?)"
    r"\s*[,;:.\u2014\u2013-]*\s*",
    re.IGNORECASE,
)


def prose_strings(node: Any, trail: str = "") -> list[tuple[str, str]]:
    """Every string under a prose key, whatever shape it is written in.

    `walk_unset`'s sibling for the opposite failure. `walk_unset` finds a value that is
    *absent*; this finds one that is *present and empty of content*, which no scalar-level
    walk can see because `note: "as above"` is a perfectly good string.
    """
    if isinstance(node, str):
        return [(trail, node)]
    if isinstance(node, dict):
        found: list[tuple[str, str]] = []
        for key, value in node.items():
            found.extend(prose_strings(value, f"{trail}.{key}" if trail else str(key)))
        return found
    if isinstance(node, list):
        found = []
        for index, value in enumerate(node):
            found.extend(prose_strings(value, f"{trail}[{index}]"))
        return found
    return []


def prose_fields(node: Any, trail: str = "") -> list[tuple[str, str]]:
    """Locate every prose field in a document and hand back its strings."""
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{trail}.{key}" if trail else str(key)
            if str(key) in PROSE_FIELDS:
                found.extend(prose_strings(value, here))
            else:
                found.extend(prose_fields(value, here))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(prose_fields(value, f"{trail}[{index}]"))
    return found


# The boundary values a quintic segment is written in terms of, and the only names its
# coefficient formulas may use. `gnc_diode.md:724-753` states the segment as
# `p(s) = a0 + a1 s + ... + a5 s^5` with `s = t/T`, so these seven symbols are the whole
# vocabulary of the derivation.
QUINTIC_SYMBOLS = ("p0", "pf", "v0", "vf", "alpha0", "alpha_f", "T")


def polynomial(coefficients: dict[str, float], s: float, derivative: int = 0) -> float:
    """`a0 + a1 s + ... + a5 s^5` and its first two derivatives, at one point.

    Written as a function rather than as three closures inside the loop, which is what ruff's B023
    objected to and rightly: a closure over a loop variable is correct only as long as nothing
    defers the call, and a check about arithmetic that silently changes meaning under refactoring
    is a poor guardian of arithmetic.
    """
    total = 0.0
    for i in range(derivative, 6):
        factor = 1
        for j in range(derivative):
            factor *= i - j
        total += factor * coefficients[f"a{i}"] * s ** (i - derivative)
    return total


def check_quintic_segment(where: str, coefficients: dict[str, Any], report: Report) -> None:
    """The declared coefficients must satisfy the boundary conditions they are declared for.

    This is `rederive`'s idiom applied to six *formulas* instead of one number, and it is possible
    only because the segment states its own boundary conditions — "position, velocity and
    acceleration at both ends: (p0, v0, alpha0) and (pf, vf, alpha_f)". Given those, the six
    coefficients are determined, and whether the declared expressions are the determined ones is a
    question arithmetic can answer without a reader.

    The expressions are evaluated with the same discipline `rederive` uses and for the same reason:
    **the configuration must not become executable.** Every identifier is checked against the seven
    symbols above before anything runs, `^` is translated to `**` because that is how the corpus
    writes a power, and the namespace holds seven floats and no builtins.

    The segment is evaluated at both ends and its first two derivatives compared against the
    declared boundary values, through the chain rule that `s = t/T` implies: `dp/dt = (1/T) dp/ds`
    and `d2p/dt2 = (1/T^2) d2p/ds2`. All six hold today, so this check refuses nothing — which is
    the point of writing it. The value it protects is a *derivation*, and a derivation with no
    reader is the one kind of declaration that cannot be spot-checked by eye: five of the six
    coefficients are sums of five terms each, and a sign flipped in one of twenty-five places
    would leave a trajectory that still starts and ends in the right place.
    """
    if set(map(str, coefficients)) != {f"a{i}" for i in range(6)}:
        report.refuse(
            f"{where}:trajectory_segment.coefficients",
            f"declares {sorted(map(str, coefficients))}, and a quintic has a0 through a5",
        )
        return
    for name, expression in coefficients.items():
        text = str(expression)
        if "__" in text:
            report.refuse(f"{where}:trajectory_segment.coefficients.{name}", "contains `__`")
            return
        unknown = [
            token
            for token in re.findall(r"[A-Za-z_][A-Za-z_0-9]*", text)
            if token not in QUINTIC_SYMBOLS
        ]
        if unknown:
            report.refuse(
                f"{where}:trajectory_segment.coefficients.{name}",
                f"uses {sorted(set(unknown))}, and the segment's vocabulary is "
                f"{list(QUINTIC_SYMBOLS)}. An expression that reaches outside its own derivation "
                "cannot be re-derived",
            )
            return
    # Deterministic values, so a refusal is reproducible rather than intermittent.
    trials = (
        (0.0, 1000.0, 0.0, 0.0, 0.0, 0.0, 100.0),
        (-500.0, 250.0, 12.0, -8.0, 0.05, -0.02, 37.5),
        (1e4, -1e4, -300.0, 300.0, -1.5, 2.25, 900.0),
        (3.25, 3.25, 0.0, 0.0, 0.0, 0.0, 1.0),
    )
    for p0, pf, v0, vf, alpha0, alpha_f, period in trials:
        namespace = {
            "p0": p0,
            "pf": pf,
            "v0": v0,
            "vf": vf,
            "alpha0": alpha0,
            "alpha_f": alpha_f,
            "T": period,
        }
        a: dict[str, float] = {}
        try:
            for name in (f"a{i}" for i in range(6)):
                expression = str(coefficients[name]).replace("^", "**")
                if not re.fullmatch(r"[0-9eE+\-*/(). \tA-Za-z_]+", expression):
                    raise ValueError(f"{name} is not an arithmetic expression")
                a[name] = float(eval(expression, {"__builtins__": {}}, namespace))  # noqa: S307
        except Exception as exc:  # noqa: BLE001 - any failure is a refusal
            report.refuse(
                f"{where}:trajectory_segment.coefficients",
                f"cannot be evaluated: {exc}",
            )
            return

        wanted = {
            "position at s=0": (polynomial(a, 0.0), p0),
            "velocity at s=0": (polynomial(a, 0.0, 1), period * v0),
            "acceleration at s=0": (polynomial(a, 0.0, 2), period**2 * alpha0),
            "position at s=1": (polynomial(a, 1.0), pf),
            "velocity at s=1": (polynomial(a, 1.0, 1), period * vf),
            "acceleration at s=1": (polynomial(a, 1.0, 2), period**2 * alpha_f),
        }
        scale = max(1.0, abs(pf), abs(period * vf), abs(period**2 * alpha_f))
        for label, (got, want) in wanted.items():
            if abs(got - want) > 1e-9 * scale:
                report.refuse(
                    f"{where}:trajectory_segment.coefficients",
                    f"do not satisfy their own boundary conditions: {label} is {got:g} and the "
                    f"boundary says {want:g} (p0={p0}, pf={pf}, T={period}). The segment declares "
                    "position, velocity and acceleration at both ends, and those six conditions "
                    "determine all six coefficients",
                )
                return


def check_pointer_notes(docs: Iterable[tuple[str, Any]], report: Report) -> None:
    """A note may point at another entry, but it has to say what *this* entry is.

    A bare `note: "as above"` is a declaration whose content lives somewhere else, and it
    fails in the specific way this folder keeps re-learning: it is a declaration no tool
    reads, so it drifts and nothing notices. The drift here is positional — "above" means
    whatever happens to precede it, so reordering a file silently repoints the note, and
    the entry goes on looking sourced while its justification has moved to a different
    claim. It also fails for a reader, which is the more immediate cost: `recon_mismatch_o2`
    said only "as above" while `consumables_diode.md:852` refuses a universal tolerance
    *per resource*, so the two thresholds are separate numbers even where the argument is
    shared, and the entry owed the reader that sentence rather than a direction to look in.

    Only a *leading* pointer phrase with nothing substantive behind it is refused. The
    four other pointer notes in the corpus each keep a clause — "for the LM", "at apollo's
    second propellant level", "2 K wider than the CSM's upper limit because ..." — and that
    clause is the whole reason the second entry exists rather than being merged into the
    first. The check therefore measures what makes those four good, not the pattern of
    their opening words.
    """
    for name, doc in docs:
        if doc is None:
            continue
        for trail, text in prose_fields(doc):
            match = POINTER_PHRASE.match(text)
            if not match:
                continue
            remainder = text[match.end() :].strip()
            if len(remainder) < 8:
                report.refuse(
                    f"{name}:{trail}",
                    f"is {text.strip()!r} — a pointer with nothing behind it. Say what this "
                    "entry's own argument is; a note that only points is a note that moves "
                    "when the file is reordered, and the reader cannot tell which claim it "
                    "was borrowing",
                )


def check_layer_coverage(
    channels: dict[str, Any] | None,
    faults_by_domain: dict[str, list[Any]],
    report: Report,
) -> None:
    """The registry's own coverage claim, compared to the eleven policies it summarises.

    `check_fault_coverage` holds each domain's `unperturbed` list to its own faults. Nothing held
    the *vehicle-wide* claim, which lived in `channels.yaml:open_debts` as a sentence and had two
    of its three numbers wrong: it said "Twelve of the 22" where the policy gives fifteen of
    twenty, and it said faults perturb 117 `layer: service` channels where they perturb 42.

    The second was never arithmetically possible, which is the part worth keeping. The `service`
    layer is 57 of the registry's 148 channels, so no split of it can produce 117 perturbed — and
    the sentence sat in the file through several rounds of channel additions because **a number in
    prose has no reader, and a number with no reader does not have to be plausible.** The two
    domain-level claims beside it were caught the moment they became fields (round 44, five of
    eight wrong). This one stayed prose for thirty more rounds and is the last of them.

    The comparison is exact and whole-registry, matching `check_fault_coverage`'s rule per domain:
    a fault that names a template perturbs the template, and a fault that names one instance of a
    template has named a channel this registry does not have.
    """
    coverage = (channels or {}).get("coverage")
    if not isinstance(coverage, dict):
        return

    def flatten(name: Any) -> str:
        return re.sub(r"\[[^\]]*\]", "[]", str(name))

    registry: set[str] = set()
    layers: dict[str, Any] = {}
    for section, rows in (channels or {}).items():
        if section in {"coverage", "crew_positions", "open_debts", "defaults"}:
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and row.get("id"):
                registry.add(flatten(row["id"]))
                layers[flatten(row["id"])] = row.get("layer")
    if not registry:
        return

    perturbed = {
        flatten(channel)
        for faults in faults_by_domain.values()
        for fault in faults
        if isinstance(fault, dict)
        for channel in (fault.get("perturbs") or [])
    }
    unperturbed = registry - perturbed
    derived = {
        "unperturbed": len(unperturbed),
        "unperturbed_service_layer": sum(1 for c in unperturbed if layers.get(c) == "service"),
        "perturbed_service_layer": sum(1 for c in perturbed if layers.get(c) == "service"),
    }
    where = "channels.yaml:coverage"
    for field, actual in derived.items():
        claim = coverage.get(field)
        if not isinstance(claim, int):
            report.refuse(
                f"{where}.{field}",
                f"states no integer count (got {claim!r}), so this part of the claim is prose. The "
                "whole point of the block is that the number is data the linter can disagree with",
            )
        elif claim != actual:
            report.refuse(
                f"{where}.{field}",
                f"claims {claim} and the policies give {actual}. A coverage claim that is more "
                "optimistic than the policy is the direction this drifts without anybody noticing",
            )


# The three classes a verb's execution falls into, and the corpus uses all three. `declarative`
# takes effect in the tick it is accepted, `deferred` may be held for a due time, and `armed` is the
# two-step the irreversible events use. **Only `deferred` is compared anywhere** — `plant.py`'s
# capability snapshot, `console.py`'s `settle` and `generate_help.py` each test for that one string —
# so `defered` on a deferrable verb would silently make it take effect immediately, and the other two
# values are compared against nothing at all.
EXECUTION_CLASSES = {"declarative", "deferred", "armed"}
# What a gate's `kind` may say. It is `preference` on all fifty-eight verbs and **no tool reads the
# field**: D-03's rule is that a gate is an agent-writable preference and an interlock is
# service-owned, and the gate's *variable* carries that. The kind is where a second class would be
# declared — a service-owned gate the console may not write — and naming the set is what stops a
# third from being spelled into existence one verb at a time.
GATE_KINDS = {"preference"}

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
    declared_inputs: list[tuple[str, str]] = []
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
        if section == "coverage":
            # Not channels: the registry's own coverage claim, as data. `check_layer_coverage`
            # reads it against the eleven fault policies, because the claim spans all of them.
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
            # And the inputs themselves were **counted and never resolved**. The field was
            # *required* of every `derived` row — so the corpus has 30-odd of them — and no tool
            # read a single value: rename `comm.tx_power_w` and the row goes on naming a channel
            # that does not exist, which is the same silence `derives_from` was given a resolution
            # for on the threshold side. A channel's inputs are the channels its value is a
            # function of, so a name that resolves to nothing is a claim about nothing.
            # And the inputs themselves were **counted and never resolved**. The field was
            # *required* of every `derived` row — so the corpus has thirty-odd of them — and no
            # tool read a single value: rename a channel a row is derived from and the row goes on
            # naming a channel that does not exist. The resolution happens after this loop rather
            # than inside it, because a row may name a channel declared *below* it: the first
            # version resolved here and refused eleven legitimate references, which is this
            # folder's own shape arriving in the check written to catch it — a value looked up
            # before the thing it points at exists.
            for source_id in row.get("inputs") or []:
                declared_inputs.append((where, str(source_id)))
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
    # The second pass, with the whole registry in hand and through the index rather than the raw
    # id set: the corpus names an *instantiated* template among a row's inputs —
    # `gnc.body_rate_xyz_deg_s` for `gnc.body_rate_[axis]_deg_s` — which is the resolution every
    # other channel reference in this file gets.
    source_index = ChannelIndex(registry)
    for where, source_id in declared_inputs:
        if source_id not in source_index:
            report.refuse(
                where,
                f"lists {source_id!r} among its inputs, and no row in this registry declares it. "
                "The field is required of every `derived` row and was read by nothing, so this is "
                "the first check that has ever looked at a value in it",
            )
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
    selection = profiles_doc.get("profile_selection") or {}
    # --------------------------------------------------------------------------------------
    # **The alternatives are declared in two different places, and this read one of them.**
    #
    # `profiles_doc.get("alternatives")` — top level — is where four domains put them
    # (consumables, power, propulsion, thermal). The other **seven** put the same list under
    # `profile_selection.alternatives`, which is where the section's own header reads naturally:
    # "what a profile changes" belongs with "who may select one". So this loop has never run on
    # avionics, comms, crew, eclss, gnc, rcs or structure — **thirteen alternatives, and the rule
    # it applies is the one whose docstring records the defect it was written for**: `power`'s
    # `tight` profile, selected by a fleet that wanted warning *earlier*, dropped the bus
    # undervoltage ladder from 26.5 V to 23.85 V and widened the envelope it was supposed to
    # narrow. `power` is one of the four that is read. **`rcs` declares a profile named `tight`
    # and has never had the rule applied to it.**
    #
    # Both shapes are read now, and a domain that declares neither list while declaring a
    # `profile_selection` at all is refused rather than skipped — because "no alternatives" and
    # "alternatives somewhere this does not look" are the same silence, and only one of them is a
    # decision.
    # --------------------------------------------------------------------------------------
    nested = selection.get("alternatives") or []
    top_level = profiles_doc.get("alternatives") or []
    if nested and top_level:
        report.refuse(
            f"domains/{path.name}/profiles.yaml",
            "declares `alternatives` both at the top level and inside `profile_selection`. One of "
            "the two is not read by whatever looks for the other, and a fleet would be offered "
            "whichever list its reader happened to consult",
        )
    alternatives = top_level or nested
    if not alternatives and not selection:
        return
    where = f"domains/{path.name}/profiles.yaml"
    # --------------------------------------------------------------------------------------
    # `profiles` and `alternatives` are two lists of the same kind of thing, and nothing joined
    # them. A deletion test removed `profiles` — every domain's base declaration — and no tool
    # noticed, which is the seventh of the nine-and-then-seven blocks the folder does not miss.
    #
    # What the join is for: an id has to mean one profile. `profiles` names the base a domain
    # operates under and `alternatives` the ones a fleet may select *instead*, so an id in both
    # lists is a name that resolves to two different sets of factors, and which one a fleet got
    # would depend on which list the reader consulted.
    # --------------------------------------------------------------------------------------
    base = [str(p.get("id")) for p in profiles_doc.get("profiles") or [] if isinstance(p, dict)]
    if len(base) != len(set(base)):
        report.refuse(
            f"{where}:profiles",
            f"declares {base}, which repeats an id. Two base profiles under one name is a "
            "revision history wearing a list",
        )
    clashes = sorted(set(base) & {str(a.get("id")) for a in alternatives if isinstance(a, dict)})
    if clashes:
        report.refuse(
            f"{where}",
            f"declares {clashes} in both `profiles` and `profile_selection.alternatives`. An id "
            "has to mean one profile: the base a domain operates under and the alternatives a "
            "fleet may select instead are different sets of factors under the same name",
        )
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
            # ------------------------------------------------------------------------------
            # **A scalar `factor` is an unmet requirement, not a false claim**, and the
            # difference decides which instrument this is.
            #
            # Thirteen alternatives across seven domains declare one number where the rule needs
            # one per comparator, and none of them has ever been read — the loop looked for
            # `alternatives` at the top level while those seven nest the list under
            # `profile_selection`. What each one *should* say is a judgement about that profile's
            # direction, and a judgement is not something a linter may supply: `rcs.conservative`
            # declares `factor: 1.5` for "a wider deadband and a *lower* authority floor", and
            # 1.5 is a legitimate factor for a `below` threshold that is being *raised* and a
            # forbidden one for a floor being lowered. Which of those the profile means is the
            # author's statement to make.
            #
            # So a domain that has declared the two-key form and got a direction wrong is
            # **refused** — it made a claim and the claim is false. A domain that has declared one
            # number is **owed**: the obligation is named, counted and put in the worklist, which
            # is what this folder does with a value that is needed and absent. Refusing here would
            # have been a red build for thirteen honest gaps and would have said nothing about
            # which of them is which.
            # ------------------------------------------------------------------------------
            if isinstance(alt.get("factor"), (int, float)):
                report.debt(
                    f"{awhere}.factors",
                    f"is unset and this profile declares a single `factor: {alt['factor']}`. One "
                    "number cannot tighten both a ceiling and a floor, so the direction has to be "
                    "stated per comparator — `below` at least 1, `above` at most 1 — and which way "
                    "this profile moves is its author's judgement rather than the linter's",
                )
            else:
                report.refuse(
                    awhere,
                    "declares neither `factors` nor a `factor`. A profile that changes nothing is "
                    "not a profile",
                )
            continue
        if not alt.get("revision"):
            report.refuse(awhere, "declares no `revision`")
        # The alternative's provenance goes through `check_basis`, which is the file's one
        # provenance rule, rather than through a demand for a field named `reason`. The demand was
        # this check's own second rule and it disagreed with the first: the corpus writes `reason`
        # for a `chosen` basis and `source` + `note` for `historical` and `apollo`, consistently
        # across all seventeen alternatives, and eight of them were refused for following the
        # convention. `check_basis` already requires a reason of a `chosen` value — so the extra
        # demand was redundant where it was right and wrong where it was not.
        prov = alt.get("provenance") or {}
        check_basis(f"{awhere}.provenance", prov.get("basis"), prov, report)
        # Every comparator the domain's *valued* thresholds use needs a factor, and every factor
        # the profile declares is checked for direction **whether or not a threshold uses it
        # today**. Iterating `comparators` alone left an unused key unvalidated, which is a claim
        # waiting for the threshold that activates it: a domain with no `above` thresholds can
        # carry `above: 1.4` in silence, and the first ceiling anybody adds makes it a profile that
        # widens what it was selected to narrow.
        for comparator in sorted(set(comparators) | set(map(str, factors))):
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
        # --------------------------------------------------------------------------------------
        # A profile that scales nothing has to say what it does instead.
        #
        # Four of the vehicle's alternatives declare the identity, and each is accurate rather than
        # lazy: `comms.burst` multiplies a *rate*, `gnc.radar_aided` weights a *measurement*,
        # `rcs.conservative` widens a controller *deadband*, and `rcs.safe_vector` is a mode whose
        # content is the safe target. None of them moves a published limit, so a factor that
        # scaled one would be the wrong number — and the identity on its own is indistinguishable
        # from a profile somebody scaled by one and never thought about.
        # --------------------------------------------------------------------------------------
        if all(float(factors.get(c, 1.0)) == 1.0 for c in comparators) and not alt.get(
            "factors_note"
        ):
            report.refuse(
                f"{awhere}.factors",
                "scales every threshold by one and does not say what it changes instead. A profile "
                "that moves no limit is a statement about something else — a rate, a measurement "
                "weight, a control parameter — and `factors_note` is where that is written",
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
    declared_phases: set[str] | None = None,
    all_verbs: dict[str, str] | None = None,
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
    check_placeholders(docs, report)
    check_pointer_notes(
        ((f"domains/{name}/{filename}", doc) for filename, doc in docs.items()), report
    )
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
    # **A threshold that derives its limit is not an independent obligation.** Three `gnc`
    # thresholds take their limit from a component field the domain already owes, so counting the
    # threshold as well counted one missing datum twice — and `drift_deg_per_h` three times, since
    # a fault's seeding names it too. The debt belongs where the quantity lives; this is the same
    # instrument round 74 used on `assert`/`clear`, applied across two files instead of two fields.
    derived_trails: set[str] = set()
    # `position` rather than `index`, which is this function's `ChannelIndex` parameter — the
    # first version of this loop shadowed it and the failure landed thirty lines away, in a
    # membership test that had been handed an integer.
    for position, threshold in enumerate((docs.get("profiles.yaml") or {}).get("thresholds") or []):
        if isinstance(threshold, dict) and threshold.get("derives_from"):
            for field in ("assert", "clear"):
                derived_trails.add(f"profiles.yaml.thresholds[{position}].{field}")

    unset_trails = walk_unset(docs)
    unset_set = set(unset_trails)
    for trail in unset_trails:
        if trail in derived_trails:
            continue
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
    # --------------------------------------------------------------------------------------
    # **A check that iterates a block reports nothing when the block is gone**, and three of
    # this file's checks were written that way. Deleting `quality_assignment` from
    # `domains/avionics/components.yaml` — ninety-three lines — left the linter composing
    # cleanly, because every rule inside it is guarded by `if isinstance(quality, dict)` and an
    # absent block skips the lot. The same deletion test found `display_contract` and
    # `atmosphere_model` behaving identically, and between them the three are the whole
    # quality-assignment function, the display contract and the atmosphere model: **four
    # hundred lines of the vehicle that a build would not notice losing.**
    #
    # The distinction that makes this worth a refusal rather than a shrug is that these blocks
    # are not optional. `simulator-design.md:496-508` requires a quality assignment; the display
    # contract is what the perception bound is cross-checked against; the atmosphere model is
    # what makes a cabin's gas a species rather than a mass. A domain that has one and loses it
    # has lost a requirement, and a linter that cannot tell that from a domain that never had one
    # is a linter reporting on nothing.
    # --------------------------------------------------------------------------------------
    if quality is None and name == "avionics":
        report.refuse(
            f"{where}:quality_assignment",
            "is absent. `simulator-design.md:496-508` requires the quality function to be "
            "declared, and every rule about it lives inside the block — so a missing block skips "
            "the check rather than failing it, which is how ninety-three lines of the quality "
            "assignment could be deleted with the build still green",
        )
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
        else:
            # ----------------------------------------------------------------------------------
            # **`layer`, which is the registry's and is restated here unchecked.**
            #
            # The registry validates its own copy — a channel with a layer outside
            # `{measurement, estimate, service}` is refused, and so is one with no layer at all —
            # and the domain's `points.yaml` repeats the field for the same channel with nothing
            # comparing the two. That is round 79's `mass_kg` exactly: one quantity declared twice
            # with only one of the two read, except that here it is the *copy* that is unchecked.
            #
            # What a disagreement decides is not cosmetic, and `presentation.yaml` says so:
            # a service state is layer A with a different subject, and the mapping "decides whether
            # a commanded valve position carries a quality code". A domain that marked a service
            # channel as a measurement would put a SUSPECT code on a statement of what the vehicle
            # did, and a fleet would go looking for a sensor fault in `rcs.mode`.
            #
            # All 142 points agree today — which is the condition under which the 143rd is added
            # wrong, and the reason the join is worth having.
            # ----------------------------------------------------------------------------------
            registered = index.row(str(cid)) or {}
            stated = point.get("layer") if isinstance(point, dict) else None
            if stated != registered.get("layer"):
                report.refuse(
                    f"{pwhere}.layer",
                    f"is {stated!r} and `channels.yaml` registers {cid!r} as "
                    f"{registered.get('layer')!r}. The registry's layer is the one the epistemic "
                    "mapping is written against, so a point that restates it differently decides "
                    "on its own whether the channel carries a quality code",
                )
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
        # A keyed state publishes a *value per key*, and this binding compared only the value
        # vocabulary: change a channel's `map[hatch_id,...]` to `map[latch_id,...]` and the linter
        # composed, which is how the fixture for this found it. So the key is compared too. The
        # asymmetry is deliberate — a channel that promises a key over a state that holds a single
        # value is publishing a key the state cannot hold, and is refused; a *projection* of a
        # keyed state, like `structure.docking_latches`' count of `docking_latch_state`'s map, is a
        # legitimate read and is the debt its own `open_debts` already carries.
        chan_keys = re.findall(r"map\[([^,\]]*)", str(row.get("unit") or ""))
        state_keys = re.findall(r"map\[([^,\]]*)", str(state.get("unit") or ""))
        if chan_keys and (not state_keys or chan_keys[0].strip() != state_keys[0].strip()):
            report.refuse(
                f"domains/{name}/points.yaml:{cid}",
                f"publishes {str(row.get('unit'))!r} from `{source}`, whose unit is "
                f"{str(state.get('unit'))!r}. A keyed channel publishes a value *per key*, so the "
                "key is half the claim and a channel whose key the state does not have is "
                "publishing a value the vehicle cannot hold",
            )
            continue
        if state_keys and not chan_keys and "[" not in str(cid):
            # A channel whose *name* carries a placeholder is the keyed state instantiated one key
            # at a time — `avionics.sensor_health_[class]` publishes `sensor_health`'s value for one
            # class — and seven of those exist. A channel whose name carries no placeholder and
            # whose unit has the value vocabulary but no key is publishing something *about* the
            # map: `cw.active_lights` is a list of which systems are lit. The first version of this
            # note fired on all seven instantiations too, describing them as projections, which is
            # the mirror of round 7's refusal that described a test it had not run.
            #
            # A channel with no `enum[` at all — `structure.docking_latches`' count of
            # `docking_latch_state`'s members — left this block further up, where the value
            # vocabularies are compared, and is a projection whose own debt carries what it owes.
            report.note(
                f"domains/{name}/points.yaml:{cid}",
                f"publishes {str(row.get('unit'))!r} from the keyed state `{source}` "
                f"({str(state.get('unit'))!r}) and its own name carries no placeholder, so it "
                "publishes something *about* the map rather than a key of it",
            )
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
        # --------------------------------------------------------------------------------------
        # Five fields on the fifty-eight verbs that nothing validated, found by mutating each one
        # in turn and watching which mutations composed. Every one of them is *read* by something,
        # which is what makes them worth checking: `execution_class` decides whether a command may
        # be held for a due time, `maximum_queue_age_s` decides when it expires, `allowed_phases`
        # decides which phases offer the verb, `help` is what a fleet reads, and `gate.kind` is
        # read by nothing at all.
        # --------------------------------------------------------------------------------------
        execution = verb.get("execution_class")
        if execution not in EXECUTION_CLASSES:
            report.refuse(
                f"{vwhere}.execution_class",
                f"is {execution!r}, not one of {sorted(EXECUTION_CLASSES)}. Three tools compare this "
                "field against the literal `deferred`, so a misspelling does not fail — it makes a "
                "deferrable verb take effect in the tick it is accepted",
            )
        queue_age = verb.get("maximum_queue_age_s")
        if not isinstance(queue_age, (int, float)) or queue_age <= 0:
            report.refuse(
                f"{vwhere}.maximum_queue_age_s",
                f"is {queue_age!r}. The console expires a due command past this age, so zero or a "
                "negative number is a deferral that expires before it is accepted",
            )
        declared_phases = declared_phases or set()
        phases = verb.get("allowed_phases")
        if not isinstance(phases, list) or not phases:
            report.refuse(
                f"{vwhere}.allowed_phases",
                "is absent or empty. A verb offered in no phase is a verb no fleet can use, and an "
                "*absent* list is not the same as one that names every phase — the check that "
                "reads it can tell the difference and a reader cannot",
            )
        else:
            unknown = sorted({str(ph) for ph in phases} - declared_phases)
            if unknown:
                report.refuse(
                    f"{vwhere}.allowed_phases",
                    f"names {unknown}, which `mission.yaml` does not declare. Its phases are "
                    f"{sorted(declared_phases)}",
                )
        gate = verb.get("gate")
        if isinstance(gate, dict) and gate.get("kind") not in GATE_KINDS:
            report.refuse(
                f"{vwhere}.gate.kind",
                f"is {gate.get('kind')!r}, not one of {sorted(GATE_KINDS)}. **No tool reads this "
                "field**, so a value outside the set is a class of gate that exists only in the "
                "sentence that named it — D-03's agent-writable preference is the one there is",
            )
        if not str(verb.get("help") or "").strip():
            report.refuse(
                f"{vwhere}.help",
                "is empty. `HELP.md` is generated from this field and is the whole of what a fleet "
                "is told about the verb before it calls it",
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
        # --------------------------------------------------------------------------------------
        # `conflict_domain`, which the console groups commands by and nothing validated.
        #
        # The field says which commands must not both take effect in one tick: the console expands
        # it and the first valid command in a domain wins. A typo therefore does not fail — it
        # makes the command's collisions silently stop happening, and two contradictory orders are
        # both accepted. The console already knows this: `conflict_domains` falls back to the
        # literal text when a template will not expand, with the comment that "a domain that cannot
        # be expanded is a domain nothing can collide in, which is worse than a wrong one". A
        # fallback at runtime is what the linter is for at build time.
        #
        # Two rules, and the second is the one with a defect behind it. The first segment must be a
        # domain by either of its two names — `PREFIX_DOMAIN` exists so a reference may be
        # qualified by the directory or by the channel prefix, and the corpus uses both, `res` and
        # `prop` for two domains and the directory name for the rest. And every `<placeholder>`
        # must name an **enum argument of its own verb**, which is exactly the rule the gate check
        # above applies to the gate template: `power.source_<id>` names no argument, because the
        # verb's argument is called `source`.
        #
        # **Four verbs had that defect, and they are the residue of the ten the gate check was
        # written for.** Round 43's note records eight verbs writing "a bare `<id>` where the
        # argument was `antenna`, `source`, `load`, `breaker`, `battery`, `engine`, `pump`, `hatch`
        # or `loop`" — the gate templates were fixed and the conflict domains beside them were not,
        # because the check was written for the field rather than for the defect.
        # --------------------------------------------------------------------------------------
        conflict = str(verb.get("conflict_domain") or "").strip()
        if not conflict:
            report.refuse(vwhere, "declares no `conflict_domain`, so its collisions are undeclared")
        else:
            head = conflict.split(".")[0]
            if head not in DOMAINS and head not in PREFIX_DOMAIN:
                report.refuse(
                    f"{vwhere}.conflict_domain",
                    f"begins {head!r}, which is neither a domain directory ({sorted(DOMAINS)}) nor "
                    f"a channel prefix ({sorted(PREFIX_DOMAIN)}). A command grouped under a name no "
                    "other command shares is a command whose conflicts never happen",
                )
            for placeholder in re.findall(r"<([^>]+)>", conflict):
                spec = (verb.get("argument_schema") or {}).get(placeholder)
                if not isinstance(spec, dict) or spec.get("type") != "enum":
                    report.refuse(
                        f"{vwhere}.conflict_domain",
                        f"declares {conflict!r}, whose placeholder <{placeholder}> names no enum "
                        f"argument of this verb (it has "
                        f"{sorted(verb.get('argument_schema') or {})}). The console expands the "
                        "template by name, so a placeholder that names nothing leaves the command "
                        "in a domain of its own",
                    )

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
    # The same absent-block silence as `quality_assignment`: this loop is the entire check, so
    # `or {}` made two hundred and three lines of the display contract optional to the build.
    if not contract and name == "crew":
        report.refuse(
            f"{where}:display_contract",
            "is absent. The contract is what the crew perception bound is cross-checked against "
            "\u2014 `channels.yaml#crew_positions` says what is perceptible and this says what an "
            "instrument in front of a person actually shows, and the gap between them is where a "
            "leak in a crew line hides. A missing block made the whole comparison vacuous",
        )
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

        # And the position's own `controls`, which is the unchecked half of the pair above: a
        # panel's `shows` is held to the registry *and* to the perception bound, and the same
        # position's `controls` — the channels a crew member can operate — was held to neither. A
        # control a person cannot find on their own panel is a control the record will accept and
        # the crew will report as absent, which is exactly the gap the display contract exists to
        # close.
        for cid in position.get("controls") or []:
            if cid not in index:
                report.refuse(
                    pwhere,
                    f"controls {cid!r}, which is not a registered channel. What a position can "
                    "operate and what it can see are two lists, and only the second was checked",
                )
            elif cid not in allowed:
                report.refuse(
                    pwhere,
                    f"controls {cid!r}, which this position cannot perceive. A crew member who can "
                    "operate a switch and cannot see its state is a crew member operating blind",
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
        elif "hazard" in seeding and seeding.get("unit") not in SEEDING_RATE_UNITS:
            # The unit is the rate's *time basis*, and `faults.py` scales the hazard by it over the
            # mission's 192 h ladder. `per_hour` reads like `per_h` and is not it, and nothing
            # compared the field: the whole vocabulary is one word, declared 66 times and validated
            # nowhere, which is the shape that makes a misspelling a silent change of rate rather
            # than a refusal.
            report.refuse(
                f"{fwhere}.seeding.unit",
                f"is {seeding.get('unit')!r}, not one of {sorted(SEEDING_RATE_UNITS)}. The unit is "
                "the rate's time basis — `faults.py` scales the hazard by it across the mission's "
                "ladder — so a word that merely reads like the right one changes the rate",
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

    # --------------------------------------------------------------------------------------
    # The `internal` sentinel's intra-domain order, which was exempt from the rule that exists
    # to fix exactly this.
    #
    # `check_domains` requires every node with more than one producing state to declare a
    # `state_order` or to claim `independent`, and its own comment states the rule generally:
    # *"each node with more than one producing state declares either an order or that it has
    # none."* The loop that implements it reads `if node and node != "internal"`, and nothing
    # in `plant.md` or `coupling.yaml` says why. So **54 states across nine domains are still
    # decided by the alphabet**, which is the outcome the rule was written against: the comment
    # beside it records that the tiebreak sorted `link_snr` before `tx_power` on a node where
    # transmit power is a term in the link budget.
    #
    # The sentinel is worse than a node in this respect, not better. A node's producing states
    # are at least joined by edges that say what feeds what, so the graph is a second opinion
    # when the order is wrong; `internal` states have **no edges at all** — that is what the
    # sentinel means — so the alphabet is the only signal there is.
    #
    # The order is a within-domain fact and so it is declared within the domain: `internal` is
    # one sentinel shared by eleven domains, and `plant.md` §2 forbids a domain calling another,
    # so a single vehicle-wide list would be asserting an order across domains that do not run
    # in one. Nine domains owe this declaration today and none of them can be filled in from the
    # corpus, so it is reported as a debt rather than refused: the honest instrument for a
    # declaration that is needed and that no source supplies.
    # --------------------------------------------------------------------------------------
    # --------------------------------------------------------------------------------------
    # The guidance model: three blocks that were read by nothing, and all three are checkable.
    #
    # `gnc/estimator`, `gnc/trajectory_segment` and `gnc/flight_rules` were the last of the nine
    # blocks a deletion test found the vehicle does not notice losing. They are not prose: the
    # estimator declares a 15-dimensional error state and a unit per block, the trajectory
    # declares the six quintic coefficients as formulas, and the flight rules name states and
    # values. Every one of those claims is true today and none of them had a reader.
    # --------------------------------------------------------------------------------------
    estimator = components.get("estimator")
    if estimator is None and name == "gnc":
        report.refuse(
            f"{where}:estimator",
            "is absent. `gnc_diode.md:755-850` specifies the filter this vehicle flies, and the "
            "`innovation_window` state in `domains/avionics/` has been waiting for it since that "
            "domain landed — a normalized innovation needs the covariance this block describes",
        )
    if isinstance(estimator, dict):
        ewhere = f"{where}:estimator"
        vector = [str(v) for v in estimator.get("error_vector") or []]
        units = estimator.get("covariance_units_per_block") or {}
        if not vector:
            report.refuse(ewhere, "declares no error vector, so the filter estimates nothing")
        elif set(map(str, units)) != set(vector):
            report.refuse(
                f"{ewhere}.covariance_units_per_block",
                f"names {sorted(map(str, units))} and the error vector is {sorted(vector)}. The "
                "covariance is block-diagonal over the error state, so a block with no unit is a "
                "variance nobody can size and a unit with no block is a dimension the filter does "
                "not carry",
            )
        # Each block is a 3-vector — a position, a velocity, a small rotation, two biases — so the
        # state dimension is three times the block count. `gnc_diode.md:815-825` gives the vector
        # and the reference implementation carries 15; the arithmetic is the check.
        dimension = estimator.get("dimension")
        if not isinstance(dimension, int):
            report.refuse(
                f"{ewhere}.dimension", f"is {dimension!r}, so the filter's order is not a number"
            )
        elif dimension != 3 * len(vector):
            report.refuse(
                f"{ewhere}.dimension",
                f"is {dimension} and the error vector has {len(vector)} blocks of three, which is "
                f"{3 * len(vector)}. A dimension that disagrees with its own error state is a "
                "covariance of the wrong size",
            )

    # The quintic segment's coefficients, re-derived. `rederive`'s idiom — a value that states its
    # own arithmetic is re-derived on every run — applied to six *formulas* rather than one number,
    # which is possible because the boundary conditions they claim to satisfy are themselves stated:
    # position, velocity and acceleration at both ends. The check substitutes random boundary
    # values, evaluates the declared formulas and requires the six conditions to hold, so a
    # mistyped coefficient is caught by the algebra rather than by a reader.
    segment = components.get("trajectory_segment")
    if isinstance(segment, dict):
        coefficients = segment.get("coefficients") or {}
        if coefficients:
            check_quintic_segment(where, coefficients, report)

    # The flight rules name a state and the values it may not take, so both have to exist. A rule
    # about a mode the vehicle does not have is a rule about nothing, and this is the third place
    # in this file where an enum of state values is the thing being resolved against.
    by_state = {
        str(s.get("id")): str(s.get("unit") or "")
        for s in components.get("state") or []
        if isinstance(s, dict)
    }
    profiles_here = docs.get("profiles.yaml") or {}
    for rule in profiles_here.get("flight_rules") or []:
        if not isinstance(rule, dict):
            continue
        rwhere = f"{where}:flight_rule {rule.get('id')}"
        text = str(rule.get("rule") or "")
        if not text:
            report.refuse(rwhere, "declares no rule")
            continue
        # Every state this domain declares is quoted in the rule text, and every value it names
        # has to be one of that state's. The parse is deliberately narrow: a backticked token is
        # either a state id or a value, and a value is only checked when the state it belongs to is
        # named in the same sentence.
        named = re.findall(r"`([^`]+)`", text)
        for token in named:
            if token in by_state:
                continue
            owners = [
                sid
                for sid in by_state
                if sid in text and f"`{token}`" in text and token in by_state[sid]
            ]
            if owners:
                continue
            if token in by_state.values():
                continue
            # A value is legitimate when some state in this domain can take it; a state id when
            # this domain declares it. Anything else is a name the rule cannot be about.
            takes_it = [
                sid
                for sid, unit in by_state.items()
                if re.search(rf"\benum\[[^\]]*\b{re.escape(token)}\b", unit)
            ]
            if takes_it:
                continue
            report.refuse(
                f"{rwhere}",
                f"names `{token}`, which is neither a state this domain declares nor a value any "
                f"of them can take. The states are {sorted(by_state)}",
            )

    # --------------------------------------------------------------------------------------
    # Two blocks that no tool read at all, and both of them name things that exist elsewhere.
    #
    # A deletion test — remove the block, run every tool, diff the output — found nine blocks
    # the vehicle does not notice losing. Three were checks that pass vacuously (see
    # `quality_assignment` above); these two were read by **nothing**, and what makes them worth
    # wiring rather than declaring prose is that both are full of *references*:
    # `consumables/ledgers` names twelve channel ids and `crew/alert_overlays` names three
    # overlay ids that a verb can set. A reference nothing resolves is a reference that is
    # already free to be wrong; all fifteen happen to be right today, which is exactly the
    # condition under which the sixteenth is added wrong.
    # --------------------------------------------------------------------------------------
    for row in components.get("ledgers") or []:
        if not isinstance(row, dict):
            continue
        resource = str(row.get("resource"))
        lwhere = f"{where}:ledger {resource}"
        if resource not in node_ids:
            report.refuse(
                lwhere,
                f"names {resource!r}, which is not a coupling node. A ledger reconciles an "
                "account against an observation of the same tank, so the resource it names has to "
                "be the node the tank is",
            )
        for field in ("ledger_channel", "observed_channel", "residual_channel"):
            cid = row.get(field)
            if cid is None:
                report.refuse(lwhere, f"declares no `{field}`")
            elif index.row(str(cid)) is None:
                report.refuse(
                    f"{lwhere}.{field}",
                    f"is {str(cid)!r}, which is not a registered channel. The reconciliation is "
                    "published as three channels \u2014 the ledger, the observation and their "
                    "residual \u2014 and a name that resolves to nothing is a reconciliation a "
                    "fleet cannot read",
                )
        # A third rule was written and removed, and the reason is worth keeping because it is a
        # property of the *registry* rather than of this check: `res.recon_[resource]_kg` declares
        # its inputs as `res.[resource]_kg` and `res.ledger_[resource]_kg` — **template forms** —
        # while a ledger row names concrete instances, and not always the ones the residual is
        # computed from. `prop_main`'s observation is `prop.propellant_remaining_pct`, a
        # percentage, because that is the channel a crew reads; the residual is computed from the
        # kilogram quantity behind it. So the check refused all four correct declarations, and the
        # lesson is this folder's own: a template and its instantiations are two vocabularies, and
        # comparing across them needs an instantiation rule the registry does not have yet.

    overlays = components.get("alert_overlays") or {}
    declared_overlays = [
        str(o.get("id")) for o in overlays.get("overlays") or [] if isinstance(o, dict)
    ]
    if declared_overlays:
        # The verb that sets them is the vocabulary's, not this file's, so the two are joined
        # rather than repeated: an overlay the vehicle declares and no verb can select is a
        # suppression a fleet is told about and cannot use.
        settable: set[str] = set()
        for verb in commands.get("commands") or []:
            if not isinstance(verb, dict):
                continue
            for argument in (verb.get("argument_schema") or {}).values():
                if isinstance(argument, dict) and argument.get("type") == "enum":
                    settable.update(str(v) for v in argument.get("values") or [])
        unreachable = sorted(o for o in declared_overlays if o not in settable)
        if unreachable:
            report.refuse(
                f"{where}:alert_overlays",
                f"declares {unreachable}, and no verb's argument can take them. An overlay is a "
                "suppression the crew applies, so one nothing can select is a capability "
                "declared and not offered",
            )

    # --------------------------------------------------------------------------------------
    # `moved_by`: what moves a discrete state, which forty-three of them never said.
    #
    # The corpus declares a discrete state's vocabulary (`unit`), its guard (`hysteresis` or
    # `dwell`), its irreversibility (`one_way`, `requires_arm`) — and not what changes it. The gap
    # is not academic: `domains/crew/components.yaml` carries a debt that says so in as many words
    # — *"Nothing declares what moves the crew. `crew_location` can take `surface_eva` and
    # `crew_availability` can take `suit`, and no verb writes either"* — and that debt has been the
    # only place the question was asked.
    #
    # It is also not derivable, which is worth recording because I tried. Two mechanical rules both
    # fail. A verb's `gate` or `conflict_domain` names the state in **three** cases out of
    # forty-three (`set_computer_mode`, `request_imu_alignment`, and `mode`, whose name five verbs
    # contain). And overlapping enum *values* are actively misleading: `bus_tie_closed` shares
    # `open`/`closed` with `set_hatch_valve`, so the rule would have the hatch moving the bus tie.
    # So the declaration is an author's judgement and the linter's job is to check it, not to
    # invent it.
    #
    # Three movers, and the third is what most of them are. A `command:<verb>` is a verb of this
    # domain whose argument vocabulary reaches the state's — checked, because a command that cannot
    # express the value it is said to set is a command that never sets it. An `event:<id>` is a
    # declared `one_way_event`. And `logic` is the vehicle's own machinery: FDIR conclusions, the
    # undervoltage ladder, a geometric occultation, an allocation verdict. `logic` is a *claim* and
    # it needs a reason, which is the same discipline `independent` gets for a node's state order.
    # --------------------------------------------------------------------------------------
    verbs_here = {
        str(v.get("verb")): v
        for v in (docs.get("commands.yaml") or {}).get("commands") or []
        if isinstance(v, dict)
    }
    events_here = {
        str(e.get("id")) for e in components.get("one_way_events") or [] if isinstance(e, dict)
    }
    for state in components.get("state") or []:
        if not isinstance(state, dict) or state.get("method") != "discrete":
            continue
        sid = str(state.get("id"))
        mwhere = f"{where}:state {sid}.moved_by"
        movers = state.get("moved_by")
        if movers is None:
            report.debt(
                mwhere,
                "is unset. A discrete state declares its vocabulary and its guard and not what "
                "changes it, so nothing in the definition says whether a verb writes it, an event "
                "does, or the vehicle computes it",
            )
            continue
        if movers == "UNCONFIGURED":
            if not state.get("moved_by_note"):
                report.refuse(
                    f"{mwhere}",
                    "is UNCONFIGURED with no `moved_by_note`. An undeclared mover is a decision "
                    "about the vehicle — the crew debt is the one this field exists to make "
                    "countable — and the note says what would close it",
                )
            continue
        if not isinstance(movers, list) or not movers:
            report.refuse(f"{mwhere}", f"is {movers!r}, which names no mover")
            continue
        values = set()
        enum = re.search(r"enum\[([^\]]*)\]", str(state.get("unit") or ""))
        for match in re.findall(r"enum\[([^\]]*)\]", str(state.get("unit") or "")):
            values.update(v.strip() for v in match.split(","))
        del enum
        for mover in movers:
            text = str(mover)
            kind, _, name = text.partition(":")
            if kind == "logic":
                # `logic:<reason>` — the reason is the claim.
                if len(name.strip()) < 12:
                    report.refuse(
                        f"{mwhere}",
                        f"declares {text!r}. `logic` is a claim that the vehicle computes this "
                        "state, so it carries the reason the way `independent` carries one",
                    )
                continue
            if kind not in ("command", "event"):
                report.refuse(
                    f"{mwhere}",
                    f"declares {text!r}; a mover is `command:<verb>`, `event:<id>` or "
                    "`logic:<reason>`",
                )
                continue
            if kind == "event":
                if name not in events_here:
                    report.refuse(
                        f"{mwhere}",
                        f"names event {name!r}, which this domain does not declare in "
                        f"`one_way_events` ({sorted(events_here)})",
                    )
                continue
            # The verb may live in **another** domain, and three of the movers do: the crew's
            # `breaker_panel` is written by `power`'s `set_breaker`, `maneuver_state` by
            # `propulsion`'s `load_burn`, and `lcl_tripped` by `avionics`'s
            # `reset_latched_fault`. A command is an effect on the executive rather than a call
            # between domains, so the state it moves need not be its own domain's — but the verb
            # must exist somewhere, which is what the vehicle-wide map is for.
            verb = verbs_here.get(name)
            home = name if verb is not None else (all_verbs or {}).get(name)
            if verb is None and home is None:
                report.refuse(
                    f"{mwhere}",
                    f"names verb {name!r}, which no domain registers. This domain's are "
                    f"{sorted(verbs_here)}",
                )
                continue
            if verb is None:
                # `path` is this domain's directory, so a sibling is one level up and back down.
                verb = load(path.parent / str(home) / "commands.yaml", Report()) or {}
                verb = next(
                    (
                        c
                        for c in (verb.get("commands") or [])
                        if isinstance(c, dict) and str(c.get("verb")) == name
                    ),
                    {},
                )
            offered = set()
            for argument in (verb.get("argument_schema") or {}).values():
                if isinstance(argument, dict) and argument.get("type") == "enum":
                    offered.update(str(v) for v in argument.get("values") or [])
            # A mover that is a *trigger* rather than a setter — `request_imu_alignment` starts an
            # alignment the vehicle then drives, `arm_event` mints a token whose lifecycle the
            # state follows — cannot express the state's values and is not claimed to. Those are
            # `logic` with the verb named in the reason, and this rule is what draws the line:
            # a `command:` mover has to be able to say the value it is said to set.
            if values and not (values & offered):
                report.refuse(
                    f"{mwhere}",
                    f"names {name!r}, whose arguments can take {sorted(offered)}, and {sid} can take "
                    f"{sorted(values)}. A command that cannot express the value it is said to set "
                    "is a command that never sets it — if the verb only *triggers* the change, the "
                    "mover is `logic` with the verb written into the reason",
                )

    # --------------------------------------------------------------------------------------
    # A stock's initial condition, which the plant now integrates from and which the
    # configuration declared nowhere.
    #
    # `advance()`'s stock branch used to return the tick's net flux *as* the node's new value,
    # so it never read the level and never needed one. With that fixed the level is required,
    # and `vehicle.yaml#consumables`, `coupling.yaml`'s `preloaded` prose and the atmosphere
    # model all turned out to carry the numbers already — in three places, none of them the
    # state that integrates the tank. That is the folder's recurring shape exactly, and the
    # reason it went unnoticed for so long is worth stating: **a branch that never reads a
    # value never reports it missing.**
    #
    # Two rules. Every stock declares `initial`, because a tank with no declared starting
    # amount is a tank that begins at whatever the code initialises it to. And a *numeric*
    # initial says where it came from — either a resolvable path into another document
    # (`initial_source`, checked by `check_initial_sources` where both documents are loaded) or
    # its own provenance block — because a number with neither is a guess wearing a unit.
    # --------------------------------------------------------------------------------------
    for state in components.get("state") or []:
        if not isinstance(state, dict) or state.get("method") != "stock":
            continue
        sid = str(state.get("id"))
        swhere = f"{where}:state {sid}"
        if "initial" not in state:
            report.refuse(
                f"{swhere}.initial",
                "is a stock and declares no initial condition. A stock is the one class that "
                "carries a value across ticks, so its starting amount is a number the plant must "
                "have before it can subtract anything from it",
            )
            continue
        initial = state.get("initial")
        if initial == "UNCONFIGURED":
            if not state.get("initial_note"):
                report.refuse(
                    f"{swhere}.initial",
                    "is UNCONFIGURED with no `initial_note`. An owed starting amount is a decision "
                    "about the mission, and the note is where what would close it is written",
                )
            continue
        if not isinstance(initial, (int, float)):
            report.refuse(
                f"{swhere}.initial",
                f"is {initial!r}, which is neither a number nor `UNCONFIGURED`",
            )
            continue
        source = state.get("initial_source")
        provenance = state.get("initial_provenance")
        if not source and not isinstance(provenance, dict):
            report.refuse(
                f"{swhere}.initial",
                f"is the number {initial!r} with neither `initial_source` nor "
                "`initial_provenance`. A grounding is what separates a published load from a "
                "figure somebody typed",
            )
        elif isinstance(provenance, dict):
            check_basis(
                f"{swhere}.initial_provenance",
                provenance.get("basis"),
                provenance,
                report,
            )

    on_sentinel = sorted(
        str(state.get("id"))
        for state in components.get("state") or []
        if isinstance(state, dict) and str(state.get("node")) == "internal" and state.get("id")
    )
    if len(on_sentinel) > 1:
        order = components.get("internal_order")
        iwhere = f"{where}:internal_order"
        if order is None:
            report.debt(
                iwhere,
                f"is unset, and this domain advances {len(on_sentinel)} states on the `internal` "
                f"sentinel ({', '.join(on_sentinel)}). Nothing joins them and no edge can, so the "
                "frozen lexicographic tiebreak alone decides which advances first — the outcome "
                "`state_order` exists to prevent on a node",
            )
        elif order == "independent":
            if not components.get("internal_order_note"):
                report.refuse(
                    iwhere,
                    "declares its `internal` order independent without a reason. Independence is a "
                    "claim about the physics and it needs to be on the record",
                )
        elif not isinstance(order, list):
            report.refuse(iwhere, f"is {order!r}, which is neither a list nor 'independent'")
        elif sorted(str(s) for s in order) != on_sentinel:
            report.refuse(
                iwhere,
                f"lists {sorted(str(s) for s in order)} and this domain's states on the sentinel "
                f"are {on_sentinel}. An order naming a state that is not here is an order that has "
                "drifted from the states it orders",
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
    declared_phases: set[str] | None = None,
) -> None:
    """Every `domains/<name>/` composes, and the linter names the ones still owed."""
    node_ids = set((coupling or {}).get("nodes") or {})
    index = ChannelIndex(registry)
    positions = {
        p.get("id"): [str(c) for c in (p.get("perceivable") or [])]
        for p in (channels_doc or {}).get("crew_positions") or []
    }
    domains_dir = root / "domains"
    # Every verb on the vehicle, by the domain that registers it. A state's mover may live
    # elsewhere — the crew's breaker panel is written by `power`'s `set_breaker` — because a
    # command is an effect on the executive rather than a call between domains.
    all_verbs: dict[str, str] = {}
    if domains_dir.is_dir():
        for domain_path in sorted(p for p in domains_dir.iterdir() if p.is_dir()):
            commands = load(domain_path / "commands.yaml", Report()) or {}
            for command in commands.get("commands") or []:
                if isinstance(command, dict) and command.get("verb"):
                    all_verbs[str(command["verb"])] = domain_path.name
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

    # --------------------------------------------------------------------------------------
    # **Fault ids are unique across the whole vehicle, because they key the randomness.**
    #
    # `faults.py` derives every stochastic stream from `(master_seed, domain, component_id,
    # purpose)` and `--check` asserts that name-keying holds — "no existing fault's events moved"
    # is the property that makes a run reproducible when a fault is added. Two faults sharing an id
    # inside one domain, or across two, would take the same stream: their draws would be the same
    # numbers in the same order, and a vehicle with two faults would behave like a vehicle with one
    # of them applied twice.
    #
    # Nothing compared the ids. The corpus has 128 of them and they are all distinct today, and it
    # is the per-domain check that makes an *adversary* safe to extend: adding a fault is the one
    # edit this folder expects to happen often.
    # --------------------------------------------------------------------------------------
    seen_faults: dict[str, str] = {}
    for domain_name, policy in sorted(policies.items()):
        for fault in (policy or {}).get("faults") or []:
            if not isinstance(fault, dict) or not fault.get("id"):
                continue
            fid = str(fault["id"])
            if fid in seen_faults:
                report.refuse(
                    f"domains/{domain_name}/fault_policy.yaml:fault {fid}",
                    f"is declared again — it is already in `domains/{seen_faults[fid]}/`. Fault ids "
                    "key the stochastic streams, so two faults under one id take the same draws in "
                    "the same order and the vehicle behaves as though it had one of them twice",
                )
            else:
                seen_faults[fid] = domain_name

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
                declared_phases,
                all_verbs,
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
                # `internal` is not a node — it is the sentinel for "this domain advances it
                # itself, in no declared tick position" — so it is not in `coupling.yaml#nodes`
                # and cannot carry a `state_order` here. It is not exempt from the *rule*, only
                # from this loop: `check_domain` requires the same declaration per domain, as
                # `components.yaml#internal_order`. Leaving the sentinel out of both places was
                # how 54 states across nine domains came to be ordered by the alphabet, and the
                # exclusion sat here uncommented and unjustified in `plant.md` and in
                # `coupling.yaml` alike.
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
        # --------------------------------------------------------------------------------------
        # **Every state on the node has to be advanced by something.**
        #
        # `radiator_reject` holds two and its `state_order` puts `zone_radiator_t` **first** — the
        # round-71 fix, so that rejection (`epsilon x sigma x A x T^4`) is computed from *this*
        # tick's temperature rather than last tick's. But both of the node's inbound edges,
        # `E-ENV-RAD` and `E-WATER-RAD`, declare `advances: radiator_rejection_w`. So the state the
        # order puts first is the one state on the node that nothing advances, and the order is a
        # statement about a sequence that never runs.
        #
        # The `state_order` check above verifies that an order names the node's states and that a
        # multi-state node has one. Nothing verified that the order *can* execute. This is the
        # round-71 `zone_csm_cabin_t` finding generalised: a state on a node is not driven because
        # an edge reaches the node — it is driven because an edge **says it advances it**.
        # --------------------------------------------------------------------------------------
        if sorted(str(s) for s in order) != names:
            report.refuse(
                where,
                f"declares state_order {sorted(str(s) for s in order)} but the node is advanced "
                f"by {names}: the list has to be the group, in the order it advances",
            )
        if isinstance(order, list):
            # The states an edge is the only mechanism for: a method with an integrator and no
            # `preloaded` exemption. `states_by_node` is not available here, so the domain files are
            # consulted — the same three classes `advance` refuses on.
            advanced_for: set[str] = set()
            for domain_path in sorted(p for p in (root / "domains").iterdir() if p.is_dir()):
                doc = load(domain_path / "components.yaml", Report()) or {}
                for state in doc.get("state") or []:
                    if (
                        isinstance(state, dict)
                        and state.get("node") == node
                        and state.get("method") in {"lag", "stock", "delay", "dynamics"}
                    ):
                        advanced_for.add(str(state.get("id")))
            producers = {state_id for _, state_id in producers}
            advanced = {
                str(edge.get("advances"))
                for edge in (coupling or {}).get("edges") or []
                if isinstance(edge, dict)
                and edge.get("to") == node
                and edge.get("id") not in ((coupling or {}).get("back_edges") or [])
                and edge.get("advances")
            }
            # Only a state whose *method* needs an edge is in question. A state moved by a
            # command, an event or the domain's own logic is advanced by something the coupling
            # graph was never going to carry: `alert_lifecycle` and `alarm_horn` are the alert
            # system's own computations, and the four states on `structure_config` are moved by
            # irreversible events. Demanding an edge for those would be demanding the graph model
            # a mechanism it does not describe — so the rule is `advance`'s own: a `lag`, `stock`,
            # `delay` or `dynamics` state needs a driver, unless it is a tank filled at the pad.
            # **Only the head of the order.** A state that comes after another on the same node
            # may be derived from its predecessor — that is what an intra-node order is *for*, and
            # `source_converter_v` follows `fuel_cell_power_w` exactly that way. A state the order
            # puts **first** has no predecessor to be derived from, so an edge is the only thing
            # that can drive it. Demanding an edge for the later ones would demand the graph model
            # a derivation the order already declares.
            head = str(order[0])
            undriven = sorted({head} & advanced_for - advanced)
            for state_id in undriven:
                report.debt(
                    f"{where}",
                    f"declares a state_order over {sorted(producers)} and no inbound edge advances "
                    f"{state_id!r}. An edge that reaches the node without naming a state advances "
                    "nothing on it, so the order describes a sequence one of its steps never runs",
                )
            continue
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
    """Mass closure, the configuration names the mission refers to, and the file's own debts."""
    check_vehicle_sections(doc, report)
    # --------------------------------------------------------------------------------------
    # `vehicle.yaml#open_debts` is counted here, and until it was, it was counted nowhere.
    # `VEHICLE_SECTIONS` has carried the comment *"counted and printed, like every other
    # `open_debts` in the folder"* since the list was written, and it was not true: `channels.yaml`,
    # `coupling.yaml` and the eleven domains each report theirs, and this file's **nine** entries
    # were read by no code at all. Dropping eight of the nine left the headline count at 223.
    #
    # The nine are not marginal. They are the power inventory's unchecked relationships, the
    # inertia tensor and centre of mass per configuration, the thermal zones' heat capacities and
    # conductances, the LM sublimator's rejection, the minimum impulse bit for all three RCS
    # systems, the forty-four thrusters' geometry, the fuel cell's reactant consumption and the
    # ullage motors — which is to say **the vehicle's largest remaining unknowns were the ones
    # missing from the number that exists to count them.** A file's own statement that a section
    # is read is not evidence that it is, which is this folder's oldest finding pointed at the
    # tool that enforces it.
    # --------------------------------------------------------------------------------------
    for row in doc.get("open_debts") or []:
        report.debt("vehicle.yaml:open_debts", str(row))
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
            # ------------------------------------------------------------------------------
            # `mass_kg` and `mass_breakdown_total_kg` are the same number written twice, and
            # for as long as both have existed only one of them was read by anything: the
            # breakdown was summed against `mass_breakdown_total_kg`, and `mass_kg` went to
            # the rocket equation as a burn's wet mass (`check_propulsion`). **Nothing
            # required the two to agree**, and a 400 kg disagreement on every configuration
            # composed cleanly — with the README quoting the unchecked one in its mass-closure
            # table. Two fields for one quantity is the shape that drifts, and this is the
            # third time it has been found in a different pair: the phase sum (round 45), the
            # cabin leak (67), the bay's mass-and-conductance (73), the threshold's
            # `assert`/`clear` (74). The difference here is that both names are right — the
            # breakdown's total *is* the configuration's mass — so the fix is not to collapse
            # them but to make them say the same thing.
            # ------------------------------------------------------------------------------
            if isinstance(cfg.get("mass_kg"), (int, float)) and abs(cfg["mass_kg"] - stated) > 1.0:
                report.refuse(
                    where,
                    f"declares `mass_kg: {cfg['mass_kg']:g}` and `mass_breakdown_total_kg: "
                    f"{stated:g}`, which are the same quantity. The rocket equation flies the "
                    "first and the breakdown is summed against the second, so a fleet's "
                    "trajectory and its mass closure would be working from different vehicles",
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


# Keys that are never a quantity two files both state: identity, prose, and the link itself —
# plus `thrusters`, which is compared by its own arithmetic rule rather than for equality, because a
# string's eight and a system's sixteen are both correct. Everything else two views of one engine
# share is compared, and that is the point: **a hand-written list of what to compare is how a shared
# field gets left out.** This check's own first version listed four fields and the engines share six,
# which is precisely the defect three rounds had been spent finding in other joins.
def comparable(entry: dict[str, Any], subject: dict[str, Any], structural: set[str]) -> list[str]:
    """The keys two views of one machine both state, and neither of which is prose.

    This is the intersection rule the four joins use, written once because it is the same rule four
    times. A hand-written list of what to compare is how a shared field gets left out — correct on
    the day it is written and silently wrong the day somebody adds a field to both files — so what
    is compared is `set(a) & set(b)` minus a declared structural set.

    The `*_note` suffix is excluded for the same reason `note` is. A note is a sentence attached to
    a claim (`state_order_note`, `initial_note`, `pattern_note`), and two files describing one
    machine never owe each other the same sentence; comparing one would refuse the moment either
    side's wording improved.
    """
    return sorted(
        key
        for key in set(entry) & set(subject)
        if key not in structural and not key.endswith("_note")
    )


PROPULSION_STRUCTURAL = {
    "id",
    "kind",
    "class",
    "vehicle",
    "vehicle_keys",
    "provenance",
    "note",
    "notes",
    "why",
    "reason",
    "source",
    "ref",
    "relation",
    "basis",
    "unit",
    "inputs",
    "count",
    "thrusters",
}


def check_propulsion_bindings(root: Path, vehicle: dict[str, Any], report: Report) -> None:
    """The engine figures the mission is flown on, declared twice and joined by nothing.

    `check_propulsion` walks the mission's Δv budget through each tank and decides whether it
    closes. It reads `isp_s` and the propellant load from `vehicle.yaml#propulsion` and nothing
    else, which is right: that file is the vehicle-level view the mass closure also sums.

    What it does not read is the other copy. `domains/propulsion/components.yaml` declares `isp_s`
    on all three main engines and `domains/rcs/components.yaml` declares it on the 100 lbf article,
    and those are the copies the plant uses — the RCS domain computes its mass flow as
    `mdot = F / (Isp * g0)` from its own 290 s. So the same numbers exist twice with nothing
    between them, and the direction a divergence fails in is the one this folder exists to
    prevent: an SPS re-rated in the domain and not in `vehicle.yaml` leaves the budget check
    reporting a healthy reserve while the plant burns propellant at the domain's Isp. The mission
    looks flyable and is not — `check_propulsion`'s own docstring, arrived at from the other side.

    The link is declared rather than inferred for the same reason `domain_group` is: the two files
    name one engine differently (`lm_dps` against `dps`), and one component stands for three
    vehicle entries where the article is identical — `thruster_100lbf` is all forty-four, because
    its own note says forty-four near-identical entries would be forty-four places for the
    arithmetic to differ.

    Three closures, and the third is arithmetic rather than equality: an engine's `isp_s` and
    thrust figures are the vehicle entry's; a thruster's `count` is the sum of the `thrusters` its
    vehicle entries declare; and the strings claiming a system sum to that system's `thrusters`.

    The comparison is the **intersection of the keys the two files share**, minus
    `PROPULSION_STRUCTURAL`, rather than a list of field names. The first version of this check was
    a list of four names and the engines share six — `engine`, `qualified_restarts`, `throttleable`
    and `throttle_ratio` sat outside it, agreeing by luck, which is the defect this check was
    written to catch arriving inside the fix for it. A hand-written list of what to compare is how
    a shared field gets left out, so there is no list.
    """
    propulsion = (vehicle or {}).get("propulsion") or {}
    if not propulsion:
        report.debt(
            "vehicle.yaml#propulsion",
            "is missing, so the engines the domains declare have nothing to be held against",
        )
        return

    claiming: dict[str, list[str]] = {}
    declared: dict[str, list[int]] = {}
    for rel, cls in (("propulsion", "engine"), ("rcs", "thruster"), ("rcs", "string")):
        doc = load(root / "domains" / rel / "components.yaml", report) or {}
        for c in doc.get("components") or []:
            if not isinstance(c, dict) or c.get("class") != cls:
                continue
            cid = str(c.get("id"))
            where = f"domains/{rel}/components.yaml:{cid}"
            keys = c.get("vehicle_keys")
            if not isinstance(keys, list) or not keys:
                report.refuse(
                    where,
                    f"is a {cls!r} and declares no `vehicle_keys`. Its figures are the ones the "
                    "plant computes with, so a component claiming no vehicle entry is a copy "
                    "nothing holds against the entry the mission is flown on",
                )
                continue
            for key in (str(k) for k in keys):
                if key not in propulsion:
                    report.refuse(
                        where,
                        f"names vehicle engine {key!r}, which vehicle.yaml#propulsion does not "
                        f"declare; it declares {sorted(propulsion)}",
                    )
                    continue
                claiming.setdefault(key, []).append(cid)
                one = propulsion[key]
                if cls == "string":
                    declared.setdefault(key, []).append(int(c.get("thrusters") or 0))
                    continue
                if "isp_s" not in c:
                    report.refuse(
                        where,
                        f"claims vehicle engine {key!r} and declares no `isp_s`, so the mass flow "
                        "through it cannot be computed from this file at all",
                    )
                for field in comparable(one, c, PROPULSION_STRUCTURAL):
                    if one[field] != c[field]:
                        report.refuse(
                            where,
                            f"declares {field} {c[field]!r} and vehicle.yaml#propulsion.{key} "
                            f"declares {one[field]!r}. Two views of one engine: the vehicle-level "
                            "file is what the Δv budget and the mass closure are walked with, and "
                            "this is what the plant computes with, so a field the two disagree "
                            "about is one machine described twice",
                        )
            # A thruster article's count is the number of thrusters its vehicle entries add up to.
            if cls == "thruster" and "count" in c:
                total = sum(int(propulsion[str(k)].get("thrusters") or 0) for k in keys)
                if total and int(c["count"]) != total:
                    report.refuse(
                        where,
                        f"declares {c['count']} articles and the vehicle entries it claims "
                        f"({', '.join(str(k) for k in keys)}) total {total} thrusters",
                    )

    for key in sorted(propulsion):
        if key not in claiming:
            report.refuse(
                f"vehicle.yaml:propulsion {key}",
                "is claimed by no component in the propulsion or RCS domain. Every engine the "
                "mission burns is one the plant has to be able to compute, so an unclaimed entry "
                "is an engine that exists in the budget and not on the vehicle",
            )
    for key, counts in sorted(declared.items()):
        want = int(propulsion[key].get("thrusters") or 0)
        if want and sum(counts) != want:
            report.refuse(
                f"domains/rcs/components.yaml:strings for {key}",
                f"declare {counts} = {sum(counts)} thrusters against the {want} that "
                f"vehicle.yaml#propulsion.{key} carries",
            )


# The keys of a comms component that are never a quantity the two files both state. Identity and
# the prose keys are the bulk of it. The four that are particular to this join:
#
#   `vehicle_keys`  the link itself, which is the subject of the comparison rather than a value in it
#   `levels`        the domain's per-power-level table, which is compared entry by entry below
#   `beams`         the vehicle's per-feed table on the one antenna that has three feeds; the
#                   component takes the *narrow* feed's figures, which its own provenance argues
#                   for, and a feed table is a list of sub-entries rather than a scalar
#   `count`         the four omni helices NR publishes. That is a fact about articles, and neither
#                   file states a per-entry article count for an omni, so there is no second copy
#                   of it to compare — which is why it is here and not a closure below.
COMMS_STRUCTURAL = {
    "id",
    "kind",
    "class",
    "vehicle_keys",
    "levels",
    "beams",
    "count",
    "provenance",
    "note",
    "notes",
    "why",
    "reason",
    "source",
    "ref",
    "relation",
    "basis",
    "unit",
    "inputs",
}


# The ideal-aperture constant, in the form the corpus states it: `G = 41253 / theta^2`, with `G`
# linear and `theta` the half-power beamwidth in degrees. 41253 is `4 * pi` steradians carried into
# degrees squared, and the figure is written in three places — `vehicle.yaml`'s own note on the
# high-gain antenna, `domains/comms/components.yaml`'s note on `hga`, and `E-GEOM-LINK`'s relation —
# and evaluated in none of them.
IDEAL_APERTURE_PRODUCT = 41253.0

# What an antenna's `pattern` may say. `aperture` means the two published figures are one figure:
# the half-power beamwidth is the gain's own consequence, so a change to either re-derives the
# other. `near_isotropic` means they are independent — a flush omni's 180 degrees is a nominal
# coverage figure rather than a half-power width this close to isotropic — and the claim carries a
# `pattern_note`, the way `state_order: independent` carries a `state_order_note`.
ANTENNA_PATTERNS = {"aperture", "near_isotropic"}

# How far a declared beamwidth may sit from the one a gain implies, in degrees. The corpus declares
# beamwidths to one decimal place, so half a unit in the last place is the widest two figures can
# differ and still be the same declared number: 26.7 dB gives 9.3915 degrees and the entry says
# 9.4. It is a *degree* tolerance rather than a relative one because the precision is in degrees,
# and it is tight enough to catch a one-step gain change: 26.8 dB would need 9.35.
BEAMWIDTH_TOLERANCE_DEG = 0.05


def check_antenna_patterns(root: Path, vehicle: dict[str, Any], report: Report) -> None:
    """The gain and the beamwidth are one figure, and the corpus says so in three files and checks it nowhere.

    `vehicle.yaml`'s note on the high-gain antenna is the sentence this check exists for:

        "the gains are sourced and the beamwidths are **derived**, because the first version of this
         entry carried `beamwidth_deg: 1.0` and that number is not physically possible beside a
         26.7 dB gain. Any aperture antenna obeys the gain-beamwidth product `G = 41253 / theta^2`
         with theta in degrees, so 26.7 dB (467.7 linear) is a 9.4 deg beam — a 1.0 deg beam would
         need 46 dB."

    So the folder knows the relation, has already caught one impossible number with it, and left it
    as prose in three files. The four declarations it governs are:

    | antenna | gain | beamwidth | `10^(G/10) * theta^2` |
    |---|---|---|---|
    | `high_gain` (narrow) | 26.7 dB | 9.4 deg | 41,329 |
    | `high_gain.beams.wide` | 9.2 dB | 70.4 deg | 41,272 |
    | `high_gain.beams.medium` | 20.7 dB | 18.7 deg | 41,224 |
    | `high_gain.beams.narrow` | 26.7 dB | 9.4 deg | 41,329 |

    — every one within 0.2 % of 41,253, against a tolerance of half a unit in the last declared
    place. The figure is not decoration: `E-GEOM-LINK`'s -0.54 dB per degree is
    `-24 * 2 / 9.4^2`, so the beamwidth this identity determines is what every pointing loss the
    fleet ever reads is scaled by. A gain and a width that cannot both be true make that slope a
    consequence of a number nobody checked — and because the antenna is declared in two files, the
    *join* would catch a divergence between them while this is the only thing that can catch the
    vehicle-level pair drifting together.

    The rule needs one declaration to be universal, and the corpus already makes the case for it:
    the omni's 180 degrees is *not* a half-power width. 2 dBi against the product would be 161
    degrees, and the domain's own note says the product "is meaningless this close to isotropic".
    So every antenna that declares both figures declares which relation governs them, `aperture` or
    `near_isotropic`, and the second is a claim that carries its reason — the discipline
    `state_order: independent` gets, arriving at the one quantity on this vehicle where a wrong
    number was already found.
    """
    antennas = ((vehicle or {}).get("comms") or {}).get("antennas") or []
    if not antennas:
        report.debt(
            "vehicle.yaml#comms.antennas",
            "is missing or empty, so the gain-beamwidth product has nothing to be evaluated against",
        )
        return

    for antenna in antennas:
        if not isinstance(antenna, dict) or not antenna.get("id"):
            continue
        aid = str(antenna["id"])
        where = f"vehicle.yaml:comms.antennas.{aid}"
        pattern = antenna.get("pattern")
        # A beam is a feed on the same aperture, so the antenna's `pattern` governs its beams too:
        # there is no way for one feed of a dish to be an aperture and another not to be.
        figures: list[tuple[str, Any, Any]] = [
            ("", antenna.get("gain_db"), antenna.get("beamwidth_deg"))
        ]
        for beam in antenna.get("beams") or []:
            if isinstance(beam, dict):
                figures.append(
                    (
                        f".beams.{beam.get('id')}",
                        beam.get("gain_db"),
                        beam.get("beamwidth_deg"),
                    )
                )
        # A figure is a pair only when both halves are there. One without the other is not this
        # check's business: `sband_steerable` owes both, and the debt that says so is
        # `check_comms_bindings`'.
        pairs = [
            (label, gain, width)
            for label, gain, width in figures
            if isinstance(gain, (int, float)) and isinstance(width, (int, float))
        ]
        if not pairs:
            continue
        # The declaration is one per antenna, so its absence and its reason are reported once.
        # Asking it per figure would report one gap four times on the high-gain antenna — its own
        # pair and its three feeds — which is the same lie as reporting it in only one of them.
        if pattern not in ANTENNA_PATTERNS:
            label, gain, width = pairs[0]
            report.refuse(
                where,
                f"declares a gain of {gain} dB and a beamwidth of {width} deg and no `pattern`, so "
                "nothing says whether the two are one figure or two. Declare one of "
                f"{sorted(ANTENNA_PATTERNS)}: an aperture's half-power beamwidth is its gain's own "
                "consequence through `G = 41253 / theta^2`, and a near-isotropic radiator's is a "
                "nominal coverage figure that the product does not determine",
            )
            continue
        if pattern == "near_isotropic":
            if not antenna.get("pattern_note"):
                report.refuse(
                    where,
                    "declares its pattern near-isotropic without a `pattern_note`. The claim "
                    "exempts its two figures from the gain-beamwidth product every other antenna is "
                    "held to, and an exemption without a reason is how a rule stops being one — the "
                    "same discipline `state_order: independent` carries",
                )
            continue
        for label, gain, width in pairs:
            fwhere = f"{where}{label}"
            derived = math.sqrt(IDEAL_APERTURE_PRODUCT / (10 ** (float(gain) / 10.0)))
            if abs(derived - float(width)) > BEAMWIDTH_TOLERANCE_DEG:
                product = (10 ** (float(gain) / 10.0)) * float(width) * float(width)
                report.refuse(
                    fwhere,
                    f"declares {gain} dB with a {width}-degree beamwidth, and the ideal-aperture "
                    f"product `G = 41253 / theta^2` puts the half-power width of a {gain} dB "
                    f"aperture at {derived:.2f} degrees (the declared pair gives {product:,.0f} "
                    "against 41,253). Two figures that are one figure: this beamwidth is what "
                    "`E-GEOM-LINK`'s pointing loss is scaled by, so a gain and a width that cannot "
                    "both be true make every SNR the fleet reads a consequence of a number nobody "
                    "checked",
                )


def check_comms_bindings(root: Path, vehicle: dict[str, Any], report: Report) -> None:
    """The link's hardware, declared twice, and the fourth join with nothing between the copies.

    `vehicle.yaml#comms` is the vehicle-level view: the transmitters with their DC and RF watts,
    the antennas with their gains and beamwidths, the two telemetry rates and the VHF channels —
    the bill of materials NR ch.18 and `apollo_diode.md:157-168` supply. When the tenth domain
    landed, `domains/comms/components.yaml` was written as the domain's view of the same hardware,
    and its own header states the claim this check exists to keep true:

        "All of it is already in `vehicle.yaml#comms`; these entries are the domain's view of it,
         and they exist so that a threshold or a fault can name the object it is about."

    Nothing read that claim. Four components mirror four vehicle entries — one antenna standing
    for two (`omni` for `omni_a`/`omni_b`) and one transmitter for two power levels (`lm_sband` for
    `lm_sband_low`/`lm_sband_high`) — and the five fields they share, `gain_db`, `beamwidth_deg`,
    `steerable`, `dc_w` and `rf_w`, were compared by no tool in this folder.

    **The figures are not decoration, and the copies have already been used to derive things.**
    `E-GEOM-LINK`'s -0.54 dB per degree is computed *from* the 9.4-degree beamwidth, and its own
    relation cites `vehicle.yaml#comms.antennas.high_gain` by name; `E-AMP-LOAD`'s 0.0357 A per
    watt is applied to "the 36 W transceiver and the 72 W power amplifier", which its relation says
    are "`dc_w` in vehicle.yaml#comms.transmitters". So the vehicle-level copy is the one the
    graph's arithmetic was argued from, while the domain's copy is the one a fault names — COM-02,
    COM-07 and COM-08 happen to `sband_power_amplifier` and `sband_transceiver` — and the one the
    domain's code will read when the link budget is implemented. A component re-rated on one side
    and not the other leaves the pointing loss belonging to an antenna the domain no longer
    describes, which is this folder's recurring failure arriving through a file that derives from
    the quantity rather than one that states it.

    The link is declared rather than inferred, for the reason `domain_group` and the propulsion
    engines' `vehicle_keys` are: the files name one antenna `high_gain` and `hga`, and one
    transmitter `lm_sband_low`/`lm_sband_high` against a single `lm_sband`, so a rule guessed from
    the string would have to know that `hga` is short for `high_gain` and that one article with two
    power levels is two entries in a bill of materials. `levels` is where the two shapes meet: it is
    keyed by the vehicle entry ids, so the link and the figures are one declaration rather than two
    that have to be kept in step.

    The comparison is the **intersection of the keys the two views share**, minus
    `COMMS_STRUCTURAL`, following `check_propulsion_bindings`. The reverse direction is a *debt*
    rather than a refusal, and the difference is deliberate: a vehicle entry nothing claims is a
    gap in the modelling rather than a contradiction between two files, and today's one instance is
    the LM's steerable antenna, whose gain and beamwidth `vehicle.yaml` already owes and which
    `select_antenna` offers anyway.
    """
    comms = (vehicle or {}).get("comms") or {}
    domain = load(root / "domains" / "comms" / "components.yaml", report) or {}
    components = domain.get("components") or []
    if not comms or not components:
        report.debt(
            "vehicle.yaml#comms",
            "either the vehicle's comms section or the comms domain's component list is missing, "
            "so the two views of the link's hardware cannot be compared",
        )
        return

    # A class name against a section name, joined by hand because the domain's is singular and the
    # vehicle file's is plural. Two pairs, and neither is derivable from the other.
    sections = {
        "transmitter": ("transmitters", comms.get("transmitters") or []),
        "antenna": ("antennas", comms.get("antennas") or []),
    }
    entries: dict[tuple[str, str], dict[str, Any]] = {}
    for cls, (_, rows) in sections.items():
        for entry in rows:
            if isinstance(entry, dict) and entry.get("id"):
                entries[(cls, str(entry["id"]))] = entry

    claiming: dict[tuple[str, str], str] = {}
    for component in components:
        if not isinstance(component, dict):
            continue
        cls = str(component.get("class"))
        if cls not in sections:
            continue
        cid = str(component.get("id"))
        cwhere = f"domains/comms/components.yaml:{cid}"
        keys = component.get("vehicle_keys")
        if not isinstance(keys, list) or not keys:
            report.refuse(
                cwhere,
                f"is a {cls!r} and declares no `vehicle_keys`. Its figures are the ones the link is "
                "argued from, so a component claiming no vehicle entry is a copy nothing holds "
                "against the entry the vehicle is described by",
            )
            continue
        levels = component.get("levels")
        if levels is not None and not isinstance(levels, dict):
            report.refuse(
                cwhere, f"declares `levels` as a {type(levels).__name__}, which is not a mapping"
            )
            levels = None
        for key in (str(k) for k in keys):
            entry = entries.get((cls, key))
            if entry is None:
                report.refuse(
                    cwhere,
                    f"names vehicle {cls} {key!r}, which vehicle.yaml#comms does not declare; it "
                    f"declares {sorted(k for c, k in entries if c == cls)}",
                )
                continue
            claiming[(cls, key)] = cid
            # The subjects a vehicle entry is held against. A component with one position is one
            # subject; a component standing for a multi-position article is two — its own top level,
            # which says what is true of the article, and the level the entry belongs to. Comparing
            # the top level as well is what makes the rule exceptionless: a figure written one
            # level up from where it belongs is still a figure two files both state, and leaving it
            # out would be a declaration no rule reads, which is the defect this check exists for.
            subjects: list[tuple[str, dict[str, Any]]] = [(cid, component)]
            if levels is not None:
                level = levels.get(key)
                if not isinstance(level, dict):
                    report.refuse(
                        cwhere,
                        f"claims {key!r} and declares no `levels.{key}`, so the vehicle entry has "
                        "no figures on this side to be held against",
                    )
                    continue
                subjects.append((f"{cid}.levels.{key}", level))
            section = sections[cls][0]
            for label, subject in subjects:
                for field in comparable(entry, subject, COMMS_STRUCTURAL):
                    if entry[field] != subject[field]:
                        report.refuse(
                            cwhere,
                            f"declares {field} {subject[field]!r} at `{label}` and "
                            f"vehicle.yaml#comms.{section}.{key} declares {entry[field]!r}. Two "
                            "views of one article: the vehicle-level file is what the link budget "
                            "and the coupling edges are argued from and this is what the domain "
                            "names in a fault, so a figure the two disagree about is one antenna or "
                            "transmitter described twice",
                        )
        if isinstance(levels, dict):
            for extra in sorted(set(levels) - {str(k) for k in keys}):
                report.refuse(
                    cwhere,
                    f"declares `levels.{extra}`, which no entry of `vehicle_keys` claims: a power "
                    "level with no vehicle entry behind it is a figure nothing is held against",
                )

    for cls, key in sorted(entries):
        if (cls, key) in claiming:
            continue
        section = sections[cls][0]
        if cls == "antenna":
            consequence = (
                "`select_antenna` offers it, so a fleet can put the vehicle on an antenna this "
                "domain has no object for: no fault, no threshold and no link budget here can be "
                "about it, and its figures would come from the vehicle-level file alone"
            )
        else:
            consequence = (
                "a transmitter's DC draw is the bus load the link costs, so an unclaimed one is a "
                "load no rule on this side can add"
            )
        report.debt(
            f"vehicle.yaml:comms.{section}.{key}",
            f"is declared by vehicle.yaml and claimed by no component of class {cls!r} in the "
            f"comms domain, so nothing holds its figures against the copy the domain would use. "
            f"{consequence}",
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
        # The third of the three. This one returned *early* rather than guarding a loop, which is
        # the same silence with a clearer signature: the check announces to the reader that it
        # ran and says nothing, and a missing atmosphere model is indistinguishable from a
        # symmetric one.
        report.refuse(
            "domains/eclss/components.yaml:atmosphere_model",
            "declares no gases, so there is nothing for the symmetry check to compare. The model "
            "is what makes a cabin's contents a set of species rather than one mass, and every "
            "rule about the cabins being symmetric lives behind this early return",
        )
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
    registry: dict[str, Any],
    presentation: dict[str, Any],
    report: Report,
    root: Path | None = None,
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

    # --------------------------------------------------------------------------------------
    # **Paired by name is not paired in fact.**
    #
    # The comparison above asks whether a channel has an `lm_` twin, and until round 102 that was
    # the whole of the pairing rule. `eclss.lm_co2_pp_1h_avg_mmhg` had a twin and read
    # `csm_cabin_co2_kg` with `eclss.co2_pp_mmhg` as its input — the **CSM's** cabin — so the pair
    # was paired by id and unpaired in fact, and its note was verbatim the CSM channel's. During
    # `descent`, `surface` and `ascent_rendezvous` — 27.5 h with the crew in the LM and the CSM
    # empty — a fleet watching it against the 3 mmHg one-hour limit would have been watching the
    # compartment nobody was in.
    #
    # So the twin comparison is extended to the *state each one reads*: a channel's twin must read
    # a state in the other compartment. The compartments are the coupling nodes, and the LM's are
    # the ones whose names carry `lm`.
    # --------------------------------------------------------------------------------------
    if root is not None and paired:
        node_of_state: dict[str, str] = {}
        reads: dict[str, str] = {}
        for domain_path in sorted((root / "domains").glob("*")):
            if not domain_path.is_dir():
                continue
            components = load(domain_path / "components.yaml", Report()) or {}
            for state in components.get("state") or []:
                if isinstance(state, dict) and state.get("id"):
                    node_of_state[str(state["id"])] = str(state.get("node"))
            points = load(domain_path / "points.yaml", Report()) or {}
            for row in points.get("points") or []:
                if isinstance(row, dict) and row.get("channel") and row.get("from"):
                    reads[str(row["channel"])] = str(row["from"])
        for cid in sorted(paired):
            twin = cid.replace("eclss.", "eclss.lm_", 1)
            # A point's `from` is **a state in this domain or a coupling node**, so a name that is
            # not a state *is* the node. Treating the two alike is what stops this check skipping
            # every point that reads a node — which is most of the consumables ledger.
            mine = node_of_state.get(reads.get(cid, "")) or reads.get(cid, "")
            theirs = node_of_state.get(reads.get(twin, "")) or reads.get(twin, "")
            if not mine or not theirs:
                continue
            if mine == theirs:
                report.refuse(
                    f"domains/eclss/points.yaml:{twin}",
                    f"is the `lm_` twin of {cid!r} and both read the state on {mine!r}. A pair is "
                    "two compartments, so a twin that reads its sibling's state is paired by name "
                    "and unpaired in fact",
                )


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

    # The same asymmetry, in the same round and for the same reason: this file's four `open_debts`
    # were read by no code at all. They are the conformance rows the probe cannot exercise, the
    # `service` layer's commanded-versus-derived split, the precision-and-rate confirmation owed to
    # the plant, and the honest half of a mirror bound — none of them marginal, and all of them
    # outside the number that exists to count what is owed.
    for row in doc.get("open_debts") or []:
        report.debt("presentation.yaml:open_debts", str(row))

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
    percentage whose denominator was declared nowhere, and `battery_charge_j` is a stock that
    declared no capacity — so what the vehicle carries in joules existed only as a product nobody
    computed. It is declared now, and this check is what holds it: `load_budget` states the energy
    in watt-hours and the cells' own `ah x v_nominal` has to agree, which is also what the stock's
    `initial` is derived from.

    Five relationships. The fifth is the one the flagship chain rests on and nothing read at all:
    `inrush_w` is declared on every one of the 25 loads and no two numbers were ever compared, so
    the field whose own comment says it "is what makes a marginal bus drop a pump" was free to be
    smaller than the steady draw it settles to. Both relationships now require the figures they
    relate to exist, because a sum that reads a missing `demand_w` as zero closes on a hole.
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

    # 1. The demand closes, per vehicle, against the total the file states. The sum reads a load
    #    that declares no `demand_w` as zero, which is how a closure holds on a hole, so the figure
    #    is required before it is summed.
    by_vehicle: dict[str, int] = {}
    for row in loads:
        demand = row.get("demand_w")
        if not isinstance(demand, (int, float)) or isinstance(demand, bool):
            report.refuse(
                f"domains/power/components.yaml:load {row.get('id')}",
                f"declares demand_w={demand!r}. The per-vehicle total below adds a load like this as "
                "nothing, so the load budget would close on arithmetic over a hole",
            )
            continue
        by_vehicle[str(row.get("vehicle"))] = by_vehicle.get(str(row.get("vehicle")), 0) + int(
            demand
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

    # 5. A load's starting transient is not below the steady draw it settles to, and every load
    #    declares one. This is the last quantity in the domain that nothing read: 25 declarations of
    #    `inrush_w` against 25 of `demand_w`, and no comparison between them anywhere — the field
    #    the file's own comment introduces as the reason "a marginal bus drop[s] a pump — the
    #    flagship chain of the experiment", free to say a load draws less while starting than while
    #    running. The bound is the fields' own definition and nothing more; it is *not* the bus peak
    #    at switching, which is the check that would actually use the number and which needs the
    #    tie's routing, so that stays a debt.
    for row in loads:
        demand, transient = row.get("demand_w"), row.get("inrush_w")
        if not isinstance(transient, (int, float)) or isinstance(transient, bool):
            report.refuse(
                f"domains/power/components.yaml:load {row.get('id')}",
                f"declares inrush_w={transient!r}. A load with no starting transient is one the "
                "flagship chain cannot happen to, and the demand it settles to is not a substitute: "
                "the two are equal on 17 of these 25 loads, and the eight that differ are the ones "
                "with a motor in them",
            )
            continue
        if not isinstance(demand, (int, float)) or isinstance(demand, bool):
            # Already refused above, as relationship 1's precondition. Comparing against a demand
            # that is not there would invent the figure the first refusal says is missing.
            continue
        if float(transient) < float(demand):
            report.refuse(
                f"domains/power/components.yaml:load {row.get('id')}",
                f"declares a steady draw of {demand} W and a starting transient of {transient} W. "
                "By the file's own two definitions — `demand_w` the steady draw, `inrush_w` the "
                "starting transient — the transient is the higher of the two, so this load draws "
                "less while starting than while running, and its start is the one event on this bus "
                "that cannot be detected",
            )


def check_one_way_configurations(root: Path, vehicle: dict[str, Any], report: Report) -> None:
    """An irreversible event's two endpoints, joined against the configurations they name.

    Every `one_way_event` declares `from_configuration` and `to_configuration`, and until this
    check existed **no tool read either one**. What was read was `verb`, `arm_required` and
    `observable` — so the three fields that say an event is irreversible were checked and the two
    that say *what it irreversibly does* were not.

    Writing the join found the defect it was written for. `lm_ascent_jettison` declared
    `csm_alone -> csm_alone`: a transition that changes nothing, on the one armed event whose whole
    consequence is that *the LM ascent stage leaves*. `csm_alone` is defined as "CSM alone after
    the LM is jettisoned", so the event began in the configuration it produces — which is only
    possible because there was no configuration for the vehicle it actually begins in. The mission
    spends **7.5 hours** in `lunar_orbit_docked`, a phase whose own name says "docked again" and
    whose single declared configuration said `csm_alone`.

    Two rules, and the second is the one that generalises. A name must resolve, because a dangling
    endpoint is a transition nobody can place in the mission. And an **armed** event must actually
    change configuration, because arming exists to gate exactly that: a one-way action that leaves
    the vehicle in the configuration it found it is either a consumable decision wearing an
    irreversible verb — which is `hatch_open_surface`, unarmed, and says so in its own
    `irreversibility` — or it is an endpoint that has been written wrong.
    """
    configurations = {
        str(c.get("id")) for c in (vehicle or {}).get("configurations") or [] if isinstance(c, dict)
    }
    if not configurations:
        return
    # Two endpoints are deliberately not configuration ids, and each says why in its own words:
    # `pyro_fire` acts on one of five devices, so the vehicle it leaves is a function of the
    # device rather than of the event. Both are declared here rather than recognised by pattern,
    # so a third one cannot be introduced by writing prose that happens to look like these.
    sentinels = {"any", "depends on the device"}
    for path in sorted((root / "domains").glob("*/components.yaml")):
        components = load(path, Report()) or {}
        for event in components.get("one_way_events") or []:
            if not isinstance(event, dict):
                continue
            ewhere = f"domains/{path.parent.name}/components.yaml:one_way_event {event.get('id')}"
            ends = {}
            for field in ("from_configuration", "to_configuration"):
                value = event.get(field)
                if value is None:
                    report.refuse(ewhere, f"declares no `{field}`")
                    continue
                text = str(value)
                ends[field] = text
                if text not in configurations and text not in sentinels:
                    report.refuse(
                        ewhere,
                        f"`{field}: {text!r}` is neither a configuration `vehicle.yaml` declares "
                        f"nor one of the two non-configuration endpoints {sorted(sentinels)}. "
                        "Nothing read this field before, so a renamed configuration left the event "
                        "pointing at a vehicle that no longer existed",
                    )
            start, end = ends.get("from_configuration"), ends.get("to_configuration")
            if event.get("arm_required") and start in configurations and start == end:
                report.refuse(
                    ewhere,
                    f"needs arming and runs `{start} -> {end}`, which changes nothing. Arming "
                    "exists to gate an irreversible change of configuration, so an armed event "
                    "that leaves the vehicle as it found it has either written an endpoint wrong "
                    "or is a consumable decision wearing an irreversible verb — which is what "
                    "`hatch_open_surface` is, and it is unarmed",
                )


def resolve_dotted(document: Any, dotted: str) -> Any:
    """Walk a dotted path through a parsed document, stepping sequences of `id`-keyed rows.

    `coupling.yaml` holds its nodes as a sequence with `id` fields rather than as a mapping, and
    `domains/*/components.yaml` does the same with its components, so a path through either has to
    step by name. Returns `None` for a path that does not resolve — which is the caller's business
    to report, because "the name is gone" and "the value is unset" are different answers.
    """
    node: Any = document
    for step in dotted.split("."):
        if isinstance(node, dict) and step in node:
            node = node[step]
        elif isinstance(node, list):
            found = next(
                (row for row in node if isinstance(row, dict) and str(row.get("id")) == step),
                None,
            )
            if found is None:
                return None
            node = found
        else:
            return None
    return node


G0_M_S2 = 9.80665
"""Standard gravity, for the one place a corpus statistic crosses into `g`."""

LB_TO_KG = 0.45359237
"""The international pound, for the two places the corpus publishes a flow in lb/hr."""

# Which stage of the vehicle an engine belongs to, as the prefix its own components take in a
# configuration's mass breakdown. The pairing is derived rather than listed: an engine flies a
# configuration that carries its stage, which is a fact about the breakdown's keys.
ENGINE_STAGE_PREFIX = {"sps": "csm_", "dps": "lm_descent_", "aps": "lm_ascent_"}


def corpus_statistics(documents: dict[str, Any], channels: Any = None) -> dict[str, dict[str, Any]]:
    """Quantities that are *properties of the corpus* rather than keys written in a file.

    Two thresholds take their limit from something that is derivable but nowhere declared as a
    number, and the honest way to give them a source is to compute it. `slowest_publish_period_ms`
    is a property of the channel list — the registry declares each channel's `rate_hz` and no field
    holds the slowest — and `max_explainable_acceleration_g` is a property of the configuration set
    crossed with the engine set.

    So they are computed here and merged into the documents a `derives_from` may point into, which
    means they are re-derived on every run rather than being figures somebody typed once. The table
    is deliberately short and named: a statistic that needs a paragraph to explain is a statistic
    that belongs in the file it describes, and the two here are the ones that genuinely cannot be.

    `max_explainable_acceleration_g` is the ceiling an accelerometer's reading has to exceed before
    it is uncommanded: **the largest acceleration any engine the vehicle carries can produce in any
    configuration that carries that engine's stage.** The LM's ascent engine wins, on the ascent
    stage alone — 15,569 N against 4,888 kg, which is 0.32479 g, a hair above the CSM's own
    0.32279 g with the LM gone. That near-tie is the reason the statistic is computed rather than
    quoted: it is not obvious which of the two it is, and a reader who guessed would have a
    threshold 0.6 % wrong in the direction that misses the event.
    """
    statistics: dict[str, dict[str, Any]] = {}

    if isinstance(channels, dict):
        rates = [
            float(row["rate_hz"])
            for rows in channels.values()
            if isinstance(rows, list)
            for row in rows
            if isinstance(row, dict)
            and isinstance(row.get("rate_hz"), (int, float))
            and row["rate_hz"] > 0
        ]
        if rates:
            # The slowest *publishing* channel. Channels at zero are published on change rather
            # than on a cadence, so they have no period to be stale against.
            statistics.setdefault("channels.yaml", {})["slowest_publish_period_ms"] = 1000.0 / min(
                rates
            )

    vehicle = documents.get("vehicle.yaml") or {}
    propulsion = documents.get("domains/propulsion/components.yaml") or {}
    engines = {
        str(component.get("id")): component.get("thrust_n") or component.get("thrust_max_n")
        for component in propulsion.get("components") or []
        if isinstance(component, dict) and component.get("class") == "engine"
    }
    best = 0.0
    for engine, thrust in engines.items():
        prefix = ENGINE_STAGE_PREFIX.get(engine)
        if not isinstance(thrust, (int, float)) or prefix is None:
            continue
        for configuration in vehicle.get("configurations") or []:
            if not isinstance(configuration, dict):
                continue
            breakdown = configuration.get("mass_breakdown") or {}
            mass = configuration.get("mass_kg")
            if not isinstance(mass, (int, float)) or mass <= 0:
                continue
            if not any(str(key).startswith(prefix) for key in breakdown):
                continue
            best = max(best, float(thrust) / float(mass))
    if best:
        statistics.setdefault("vehicle.yaml", {})["max_explainable_acceleration_g"] = best / G0_M_S2

    return statistics


def load_documents(
    root: Path, vehicle: Any, coupling: Any, mission: Any, channels: Any = None
) -> dict[str, Any]:
    """Every document a declared source may point into, keyed by the path a source writes.

    `channels.yaml` is present but *computed* rather than read: the registry's publishing
    statistics are properties of the channel list rather than keys in the file, and the one that
    matters is the slowest period, because that is what an aggregate staleness limit is twice. It
    is synthesised here so `derives_from` can name it like any other source, which means the number
    is re-derived on every run instead of being a figure somebody typed once.
    """
    # **Copies, not the caller's objects.** The statistics below are merged into this map, and
    # merging them into `vehicle` itself mutated the document the caller still holds: building the
    # map *before* the vehicle checks — which is what resolving `initial_source` against every
    # document requires — made `check_vehicle_sections` refuse a top-level
    # `max_explainable_acceleration_g` that no file declares, because `load_documents` had put it
    # there. A function that edits its arguments is a function whose *call order* silently matters,
    # and nothing said so until the order changed.
    documents = {
        "vehicle.yaml": dict(vehicle or {}),
        "coupling.yaml": dict(coupling or {}),
        "mission.yaml": dict(mission or {}),
    }
    domains = root / "domains"
    if domains.is_dir():
        for path in sorted(domains.glob("*/*.yaml")):
            documents[path.relative_to(root).as_posix()] = load(path, Report()) or {}
    # **After** the domain files, not before. The acceleration ceiling is a property of the engine
    # set crossed with the configuration set, and the engines live in
    # `domains/propulsion/components.yaml` — which is not in `documents` until this loop has run.
    # Merging first computed the channels statistic and silently skipped the vehicle one, which is
    # the failure mode this whole session keeps meeting: a computation whose inputs are absent
    # produces no value and reports no error.
    for filename, statistics in corpus_statistics(documents, channels).items():
        documents.setdefault(filename, {}).update(statistics)
    return documents


def check_initial_sources(
    root: Path,
    vehicle: dict[str, Any],
    coupling: dict[str, Any],
    report: Report,
    documents: dict[str, Any] | None = None,
) -> None:
    """A stock's `initial_source` must resolve, and the two documents must agree.

    This is the half of the initial condition that keeps it from becoming a *second* declaration
    of a number that already exists. `vehicle.yaml#consumables` says the CSM carries 279 kg of
    oxygen and `o2_csm_kg` says it starts at 279; without a check those are two numbers that
    happen to match today, which is precisely the shape this folder has spent ten rounds
    removing. The field is named `initial_source` rather than `initial_provenance` for that
    reason: a provenance block *describes* where a value came from, and a source *is* the other
    declaration, resolvable and comparable.

    The path is dotted and rooted at a top-level section, so `vehicle.yaml:consumables.csm.o2_kg`
    and `coupling.yaml:absorber_capacity_csm.exhausted_at` both resolve. A source that names a
    missing document, a missing key, or a non-numeric value is refused rather than skipped: each
    of those is a link that has stopped linking, which is how the absorber's man-hour ratings and
    the LM's leak both survived review in the first place.
    """
    # The whole corpus, not the two files this check was written against. It resolved
    # `initial_source` against `vehicle.yaml` and `coupling.yaml` alone, which was every source a
    # stock had until the battery's charge turned out to be declared in the *power* domain's load
    # budget — the figure `check_power_inventory` already holds against the cells' `ah x v_nominal`.
    # `derives_from` and an edge's `derivation` have resolved against every document since they
    # were written; this is the third of the three path idioms and it was the narrowest.
    documents = documents or {
        "vehicle.yaml": vehicle or {},
        "coupling.yaml": coupling or {},
    }
    for path in sorted((root / "domains").glob("*/components.yaml")):
        components = load(path, Report()) or {}
        for state in components.get("state") or []:
            if not isinstance(state, dict) or state.get("method") != "stock":
                continue
            swhere = f"domains/{path.parent.name}/components.yaml:state {state.get('id')}"
            # An initial that is a *computed consequence* of published figures binds them, the way
            # an edge's sensitivity does. Two states declared `basis: derived` and stated their
            # arithmetic as a relation — prose, which cannot be evaluated — so the cabin oxygen
            # loads were literals nothing could re-derive, written to seven figures from a sum done
            # once by hand. `check_declared_derivation` is the same rule the edges and the
            # consumables rates use, and this is its third binding site.
            derivation = state.get("initial_derivation")
            if derivation is not None:
                check_declared_derivation(
                    f"{swhere}.initial_derivation",
                    derivation,
                    state.get("initial"),
                    "`initial`",
                    documents,
                    report,
                )
            elif (
                (state.get("initial_provenance") or {}).get("basis") == "derived"
                and not state.get("initial_source")
            ):
                report.refuse(
                    f"{swhere}.initial_provenance",
                    "declares this initial as `derived` and binds nothing. A relation is prose and "
                    "prose cannot be evaluated, so the value beside it is a literal with a note: "
                    "declare `initial_derivation` over the figures the relation names, and the "
                    "starting amount re-derives on every run as an edge's sensitivity does",
                )
            source = state.get("initial_source")
            if not source:
                continue
            text = str(source)
            if ":" not in text:
                report.refuse(
                    f"{swhere}.initial_source",
                    f"is {text!r}, which names no document. The form is `<file>.yaml:<dotted.path>`",
                )
                continue
            filename, dotted = text.split(":", 1)
            document = documents.get(filename)
            if document is None:
                report.refuse(
                    f"{swhere}.initial_source",
                    f"names {filename!r}, and the documents this check can resolve are "
                    f"{sorted(documents)}",
                )
                continue
            # `resolve_dotted` is the same walk `derives_from` and an edge's `derivation` use.
            # This function had its own copy of it — the second implementation of one rule, which
            # is the defect this folder keeps finding in the corpus and had here in the tool.
            node = resolve_dotted(document, dotted)
            if node is None:
                report.refuse(
                    f"{swhere}.initial_source",
                    f"is {text!r} and {filename} has no {dotted!r}. A link that has stopped "
                    "linking reads exactly like a link that works",
                )
                continue
            if not isinstance(node, (int, float)):
                report.refuse(
                    f"{swhere}.initial_source",
                    f"resolves to {node!r}, which is not a number to compare an initial against",
                )
                continue
            # The factor, which a threshold has had since `derives_from` was written and an initial
            # never did. It is not decoration: the source of a charge is published in Wh and the
            # state integrates joules, so the conversion is 3,600 — and without it the honest
            # answer for such a state was to leave the initial owed and say so.
            factor = state.get("initial_factor", 1)
            if not isinstance(factor, (int, float)) or isinstance(factor, bool) or factor == 0:
                report.refuse(
                    f"{swhere}.initial_factor",
                    f"is {factor!r}, which is not a number to convert a source with",
                )
                continue
            expected = float(node) * float(factor)
            if abs(expected - float(state["initial"])) > 1e-9:
                report.refuse(
                    f"{swhere}",
                    f"declares `initial: {state['initial']}` and `initial_source: {text}` resolves "
                    f"to {node}"
                    + (f", times `initial_factor: {factor}`" if factor != 1 else "")
                    + ". One of the two is the load and the other is a copy of it, and nothing but "
                    "this check keeps them the same number",
                )

    # And the other end of the same chain, which was missing its last link.
    #
    # `absorber_capacity_csm.exhausted_at` is 72 man-hours and `absorber_capacity_lm`'s is 41.
    # Both are `vehicle.yaml#consumables.co2_removal`'s published ratings written a second time,
    # and both are what `domains/consumables/components.yaml`'s `absorber_man_hours_*` resolves its
    # own `initial` against — so the chain ran *stock initial -> node rating -> nothing*: the last
    # link, from the node back to the declaration the number came from, was the one no check held.
    # The edge beside it cannot see the gap either, because its sensitivity is man-hours per kg and
    # the rating **cancels** out of it: change `csm_element_man_hours` to 80 and `E-ATM-ABSORB`
    # still derives 26.37 while the counter it feeds is exhausted at 72.
    #
    # A rating that comes from another declaration names it, and the link is checked the way an
    # `initial_source` is. A rating with no source is reported as a debt rather than refused: a
    # chosen rating is a legitimate thing for a counter to have, and what is owed is the decision
    # rather than a number.
    # `nodes` is a mapping keyed by node id; `edges` is a sequence. Both shapes appear in this one
    # file, and reading the nodes as a sequence is how this loop first ran zero times and reported
    # nothing — a check that passes because it never looked, which is the failure this whole effort
    # is about, committed by the check written to catch it.
    for node_id, node in sorted(((coupling or {}).get("nodes") or {}).items()):
        if not isinstance(node, dict) or not isinstance(node.get("exhausted_at"), (int, float)):
            continue
        where = f"coupling.yaml:node {node_id}.exhausted_at"
        source = node.get("exhausted_at_source")
        if not source:
            report.debt(
                where,
                "is a numeric rating and names no source. A counter's rating is either a published "
                "figure this vehicle carries — in which case `<file>.yaml:<dotted.path>` says which "
                "— or a choice, and the two are told apart by the field rather than by the reader",
            )
            continue
        text = str(source)
        if ":" not in text:
            report.refuse(
                f"{where}_source",
                f"is {text!r}, which names no document. The form is `<file>.yaml:<dotted.path>`",
            )
            continue
        filename, dotted = text.split(":", 1)
        document = documents.get(filename)
        if document is None:
            report.refuse(
                f"{where}_source",
                f"names {filename!r}, and the documents this check can resolve are "
                f"{sorted(documents)}",
            )
            continue
        resolved = resolve_dotted(document, dotted)
        if resolved is None:
            report.refuse(
                f"{where}_source",
                f"is {text!r} and {filename} has no {dotted!r}. A link that has stopped linking "
                "reads exactly like a link that works",
            )
            continue
        if not isinstance(resolved, (int, float)) or isinstance(resolved, bool):
            report.refuse(
                f"{where}_source",
                f"resolves to {resolved!r}, which is not a number to compare a rating against",
            )
            continue
        if abs(float(resolved) - float(node["exhausted_at"])) > 1e-9:
            report.refuse(
                where,
                f"declares {node['exhausted_at']} and `exhausted_at_source: {text}` resolves to "
                f"{resolved}. One of the two is the rating and the other is a copy of it",
            )


def check_consumers(documents: dict[str, Any], report: Report) -> None:
    """Every draw on a stock, against the stock it draws and the edge it says carries it.

    `domains/consumables/components.yaml#consumers` is the domain's ledger of consumers — twelve
    entries, each naming the stock it draws down, the coupling edge it corresponds to and a rate —
    and until this round **the word `consumers` appeared in no tool in the folder**. What made the
    omission visible is the block's own header, which promises something checkable: *"Every draw on
    a stock, with the edge it corresponds to in coupling.yaml **or the reason it has none**."*

    Four rules, and the first three are that sentence taken literally:

      - the `stock` is a node this domain advances with a stock state, because a consumer of a node
        nobody integrates is a rate with nothing to subtract it from;
      - a named `edge` is a declared edge, and **it touches that stock**. Half of a pair of edges
        about one quantity reads exactly like the other half — which is how two of these came to
        name the edge that *computes* the draw rather than the one that drains the stock;
      - an entry with no `edge` carries the reason, because prose is the only form a reason can
        take and the header says there is one;
      - a declared `derivation` is evaluated against the entry's rate by `check_declared_derivation`,
        the rule the edges already use, rather than by a second copy of it.

    Writing it found a number that had already been caught once, one file over. `E-FC-WATER`'s note
    records that its sensitivity *"read 0.45 until the arithmetic was done, and 0.45 is not a number
    this reaction can produce: it asserts that 55 % of the reactant mass becomes neither electricity
    nor water nor heat"*. The edge was corrected to 1.126 and the check that re-evaluates every
    `computation` was written for it — and the **consumer** entry for the same reaction still
    declared `rate_kg_s: 0.45`, with the identical sentence as its relation, in the one block no
    tool opened.
    """
    coupling = documents.get("coupling.yaml") or {}
    by_id = {
        str(e.get("id")): e
        for e in coupling.get("edges") or []
        if isinstance(e, dict)
    }
    for key in sorted(documents):
        if not (key.startswith("domains/") and key.endswith("/components.yaml")):
            continue
        components = documents[key]
        entries = components.get("consumers") if isinstance(components, dict) else None
        if not entries:
            continue
        name = key.split("/")[1]
        stocks = {
            str(state.get("node"))
            for state in components.get("state") or []
            if isinstance(state, dict) and state.get("method") == "stock"
        }
        for position, entry in enumerate(entries):
            if not isinstance(entry, dict):
                report.refuse(
                    f"{key}:consumers[{position}]",
                    f"is a {type(entry).__name__}, which is not a mapping",
                )
                continue
            where = f"{key}:consumers[{position}] {entry.get('id')}"
            stock = entry.get("stock")
            if stock is None:
                report.refuse(
                    f"{where}.stock",
                    "is not declared, so there is nothing to subtract the rate from",
                )
            elif str(stock) not in stocks:
                report.refuse(
                    f"{where}.stock",
                    f"names {stock!r}, which no stock state of {name} advances. A consumer of a "
                    "node this domain does not integrate is a rate nothing spends",
                )
            edge_id = entry.get("edge")
            if edge_id is None:
                if not (entry.get("provenance") or {}).get("note"):
                    report.refuse(
                        f"{where}.edge",
                        "is not declared and no `provenance.note` says where the draw is carried "
                        "instead. The block's header promises \"the edge it corresponds to in "
                        "coupling.yaml or the reason it has none\", and for this entry it is "
                        "neither",
                    )
            else:
                edge = by_id.get(str(edge_id))
                if edge is None:
                    report.refuse(
                        f"{where}.edge",
                        f"names {edge_id!r}, which coupling.yaml does not declare",
                    )
                elif str(stock) not in (str(edge.get("from")), str(edge.get("to"))):
                    report.refuse(
                        f"{where}.edge",
                        f"names `{edge_id}`, which carries {edge.get('from')!r} to "
                        f"{edge.get('to')!r} and never touches {stock!r}. The edge that computes a "
                        "draw and the edge that takes the stock down are one hop apart and read "
                        "alike",
                    )
            derivation = entry.get("derivation")
            if derivation is None:
                continue
            rates = [field for field in ("rate_kg_s", "rate_man_hours_s") if field in entry]
            if len(rates) != 1:
                report.refuse(
                    f"{where}.derivation",
                    f"is declared and the entry declares {rates or 'no rate'}. A derivation holds "
                    "one rate against its own arithmetic, and which rate it holds is the field name",
                )
                continue
            check_declared_derivation(
                f"{where}.derivation",
                derivation,
                entry.get(rates[0]),
                f"`{rates[0]}`",
                documents,
                report,
            )


def check_domain_reads(documents: dict[str, Any], report: Report) -> None:
    """A domain's declared reads against the edges that make it read them.

    `reads`/`writes` are the domain-level half of the coupling graph, and the domain files say so
    at the top: power's is the shortest — *"Reads and writes are node ids from coupling.yaml.
    Domains do not call each other (plant.md §2); these declarations are what the scheduler
    orders."* `writes` has been held in **both** directions since the domain check was written: a
    state that advances a node its domain does not declare writing is refused, and a declared write
    that no state advances is refused because the node never changes. `reads` was held in neither.
    Every entry had to resolve to a coupling node — that is all — so the eleven declarations could
    name the wrong nodes, or omit the right ones, and the only thing that would notice is a reader.

    Two directions are checkable and they fail differently, which is why only one of them is a
    refusal.

    **An edge whose source the consumer does not declare** is a dependency the graph asserts and
    the domain's own declaration denies. `plant.md` §4 lists exactly this among the things the
    linter refuses — *"A domain that reads a node it does not declare, or writes one it does not
    own"* — and the write half was implemented while the read half was not. It is a refusal here
    because it is a statement about one edge and one missing name: the fix is to declare it, and the
    eleven that were missing are all real physics — the fuel cell's oxygen draw that the reaction
    water is computed from, the two removal rates that spend the absorber counters, the coldplate the
    battery is derated by, the transmitter load on the bus, the command node three domains take,
    `guidance` on two more, and the water the crew drink.

    **A declared read that no edge carries** is a dependency the domain says it has and the graph
    does not have. It is *not* refused, because the corpus contains both of the things it can mean
    and nothing tells them apart: `eclss` reads `absorber_capacity_csm` and writes the removal rate
    that drains it, so the return half of that loop lives only in the declaration — the schedule
    sees a DAG, orders the two domains one way, and whichever of the pair runs second is reading a
    stale value that looks like physics, with no `coupling.yaml#cycles` entry saying so. `gnc`
    reads `bus_a` because a guidance computer with no bus is a guidance computer that stops, and
    that is a dependency too. But `consumables` reads `battery_energy` for a resource ledger, which
    is an observation and needs no edge at all. One key, two meanings, nothing distinguishing them
    — the same shape as `range` before `range_kind` and `source` before it was split in three. What
    is owed is the distinction, so this direction is counted as a debt with the numbers in it.
    """
    coupling = documents.get("coupling.yaml") or {}
    edges = [e for e in coupling.get("edges") or [] if isinstance(e, dict)]
    domains = {
        key.split("/")[1]: doc
        for key, doc in documents.items()
        if key.startswith("domains/") and key.endswith("/components.yaml") and isinstance(doc, dict)
    }
    if not edges or not domains:
        return

    # Which domain writes each node. A node no domain writes is not a domain's product: the command
    # node is the diode's, and the two telemetry edges end on nodes the presentation layer owns.
    writers: dict[str, set[str]] = {}
    for name, doc in domains.items():
        for node in doc.get("writes") or []:
            writers.setdefault(str(node), set()).add(name)
    reads = {name: [str(n) for n in doc.get("reads") or []] for name, doc in domains.items()}
    declared = {name: set(nodes) for name, nodes in reads.items()}
    feeds: dict[str, set[str]] = {}
    for edge in edges:
        feeds.setdefault(str(edge.get("from")), set()).add(str(edge.get("to")))

    # Direction one: every cross-domain edge source is declared by the domain that consumes it.
    for edge in edges:
        owners = writers.get(str(edge.get("to"))) or set()
        if len(owners) != 1:
            continue
        name = next(iter(owners))
        source = str(edge.get("from"))
        # A node the reading domain writes itself is a read inside one tick, not a cross-domain
        # one: `E-O2-FC` carries `fc_o2_draw` back into `fuel_cell` and both are power's.
        if source in declared[name] or name in writers.get(source, set()):
            continue
        report.refuse(
            f"domains/{name}/components.yaml:reads",
            f"does not declare {source!r}, which `{edge.get('id')}` carries into "
            f"{edge.get('to')!r} — a node {name} writes. plant.md §4: a domain that reads a node it "
            "does not declare is a dependency the scheduler cannot see, so the two may be ordered "
            "either way and the value read is whichever the tiebreak happened to give",
        )

    # Direction two: counted, not refused. A read is *carried* when some edge from that node lands
    # on a node this domain writes. It is *declared feedback* when the only thing closing it is an
    # edge `coupling.yaml#cycles` already names as a back-edge — then the read is the delayed half of
    # a loop the corpus declares, which is what a back-edge means. It is *undeclared feedback* when
    # something this domain writes feeds the node it reads and no cycle names that edge, so the loop
    # exists only in the declaration and the schedule sees a DAG. Anything else is one-way.
    declared_back_edges = {
        str(cycle.get("back_edge"))
        for cycle in coupling.get("cycles") or []
        if isinstance(cycle, dict) and cycle.get("back_edge")
    }
    declared_feedback: list[str] = []
    feedback: list[str] = []
    one_way: list[str] = []
    for name in sorted(domains):
        own = {node for node, owners in writers.items() if name in owners}
        for node in reads[name]:
            owners = writers.get(node) or set()
            if not owners or name in owners:
                continue
            if feeds.get(node, set()) & own:
                continue
            trail = f"{name}->{node}"
            closing = {
                str(edge.get("id"))
                for written in own
                for edge in edges
                if str(edge.get("from")) == written and str(edge.get("to")) == node
            }
            if closing & declared_back_edges:
                declared_feedback.append(trail)
            elif closing:
                feedback.append(trail)
            else:
                one_way.append(trail)
    if feedback or one_way:
        report.debt(
            "domains/*/components.yaml:reads",
            f"declares {len(feedback) + len(one_way)} cross-domain reads that no edge carries and "
            f"no declared back-edge closes — {len(feedback)} of them return into a node the reading "
            f"domain writes ({', '.join(feedback)}), so the loop exists only in the declaration and "
            f"the schedule sees a DAG, and {len(one_way)} are one-way dependencies the graph does "
            f"not contain at all ({', '.join(one_way)}). {len(declared_feedback)} more are the "
            "delayed half of a loop `coupling.yaml#cycles` does name "
            f"({', '.join(declared_feedback)}) and those are declared. Nothing distinguishes an "
            "unmodelled dependency from an observation — `consumables` reading `battery_energy` for "
            "a ledger and `gnc` reading `bus_a` to stay alive are written the same way — so what is "
            "owed is either the missing edges or the second key that tells the two apart",
        )


# The keys of a thermal component that are never a quantity the two files both state: identity, the
# prose keys, and the link itself. What is deliberately *not* here is the point — `fluid`,
# `flow_l_min`, `vehicle`, `coolant_mass_kg`, `panels`, `area_m2` and `rejection_w` are the figures
# both halves of this join exist to hold, and a field added to both files later is compared without
# anybody remembering to come back here.
THERMAL_STRUCTURAL = {
    "id",
    "kind",
    "class",
    "vehicle_keys",
    "provenance",
    "note",
    "notes",
    "why",
    "reason",
    "source",
    "ref",
    "relation",
    "basis",
    "unit",
    "inputs",
}


# The keys of a thermal zone that are never a quantity the two files both state. Identity and the
# prose keys — and deliberately *not* `regulated`, `volume_m3`, `limit_c` or the cabin's nominal
# pressure and temperature, which are exactly what two views of one zone have to agree about.
#
# What the intersection cannot reach is a field only one side carries, and the two zone lists are
# not the same shape: `source` and `vehicle` are on the domain's zones and not on `vehicle.yaml`'s,
# while `limit_c`, `dwell_min_s` and `implemented_by` are the other way round. A one-sided field is
# invisible here by construction, so each needs its own reader — `source` got one when the probe
# for this round found that renaming `heater_bank_csm` composed, and `vehicle` has not.
ZONE_STRUCTURAL = {
    "id",
    "provenance",
    "note",
    "notes",
    "why",
    "reason",
    "ref",
    "relation",
    "basis",
    "unit",
    "inputs",
}


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
        # `coolant_mass_kg` was the *only* field the two files share that the list omitted:
        # `loop_lm` declares it in both, they agree at 11.3 kg, and nothing read either copy. A
        # hand-written list of what to compare is correct on the day it is written and silently
        # wrong the day somebody adds a field to both files, which is the defect this whole family
        # of joins has now produced four times — so the list is gone, here as it is in the
        # propulsion, comms and rejection joins. The comparison is the intersection of the keys the
        # two views share, and `THERMAL_STRUCTURAL` is the declared set of keys that are never a
        # quantity either states.
        for field in comparable(one, two, THERMAL_STRUCTURAL):
            if one[field] != two[field]:
                report.refuse(
                    f"{where}.{loop_id}",
                    f"gives {field} as {one[field]!r} and the thermal domain gives {two[field]!r}. "
                    "One machine, two files: a loop re-rated in one and not the other is a vehicle "
                    "whose cooling was sized against a loop nobody flies",
                )
        # Only the LM's loop declares a coolant mass, and the asymmetry is a fact about the
        # sources rather than an oversight: TN D-6724 publishes the LM's as "about 25 lb" = 11.3 kg,
        # which is why `loop_lm` carries it in both files, while TN D-6718 publishes the CSM's
        # circuit as volumes and flows and never as a mass. The figure is nonetheless *recoverable*
        # — the thermal domain's own `loop_primary_thermal_mass` relation says "25 L of 62.5/37.5
        # glycol-water at 1,050 kg/m3 is 26.25 kg" — so this is not a missing datum. It is a datum
        # that lives in a sentence, and the quantity a heat-exchanger transient turns on should be a
        # scalar the plant can read. Either the density becomes one (and the mass derives from the
        # volume each loop already declares), or the mass is declared and the relation cites it.
        # The two loops differ, so this counts once per loop: one unknown, one name.
        if "coolant_mass_kg" not in one and "coolant_mass_kg" not in two:
            report.debt(
                f"{where}.{loop_id}",
                "declares no coolant mass, while `loop_lm` declares one in both files. The figure "
                "is recoverable — the thermal domain's `loop_primary_thermal_mass` relation states "
                "25 L at 1,050 kg/m3 is 26.25 kg — but it is written in prose rather than declared, "
                "so the mass a heat-exchanger transient depends on is not a scalar the plant can read",
            )
        # The fluid is one substance described three ways, and until this round the corpus
        # described it in the *relations* of two states: `coolant_loop_t` worked out "25 L of
        # 62.5/37.5 glycol-water at 1,050 kg/m3 is 26.25 kg" and `loop_transport_t` worked out
        # "25 L at the published 200 lb/hr ... gives 1,042 s of transit", and a relation is prose
        # that nothing reads. The declarations exist now, and these are the two relations between
        # them — both stated in that prose and neither ever evaluated.
        density = one.get("fluid_density_kg_m3", two.get("fluid_density_kg_m3"))
        mass = one.get("coolant_mass_kg", two.get("coolant_mass_kg"))
        volume = one.get("loop_volume_l", one.get("volume_l"))
        if volume is None:
            volume = two.get("volume_l")
        if all(isinstance(v, (int, float)) for v in (density, mass, volume)):
            derived = float(volume) * float(density) / 1000.0
            if not agrees_with_derivation(float(mass), derived):
                report.refuse(
                    f"{where}.{loop_id}",
                    f"declares {volume} L of fluid at {density} kg/m3, which is {derived:g} kg, and "
                    f"a coolant mass of {mass} kg. The mass *is* the density applied to the volume, "
                    "so one of the three is a copy of a number the other two determine",
                )
        # And the loop's two flow figures, in their two sources' units. `flow_l_min` is apollo's
        # operating band and `nominal_flow_lb_per_h` is TN D-6718's published flow; the density is
        # what makes them one quantity, so the nominal converted at the density has to land inside
        # the band. 200 lb/hr is 1.43998 L/min at 1,050 kg/m3, against a 1.3-1.7 band.
        nominal = one.get("nominal_flow_lb_per_h", two.get("nominal_flow_lb_per_h"))
        band = one.get("flow_l_min", two.get("flow_l_min"))
        if (
            isinstance(nominal, (int, float))
            and isinstance(density, (int, float))
            and isinstance(band, list)
            and len(band) == 2
        ):
            as_l_min = float(nominal) * LB_TO_KG / 60.0 / float(density) * 1000.0
            low, high = (float(band[0]), float(band[1]))
            if not low <= as_l_min <= high:
                report.refuse(
                    f"{where}.{loop_id}",
                    f"declares a nominal flow of {nominal} lb/hr and a band of {low}-{high} L/min, "
                    f"and at the declared {density} kg/m3 the nominal is {as_l_min:.4g} L/min — "
                    "outside its own band. The two figures are one flow in two units, and the "
                    "density is the conversion between them",
                )
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

    # And the same three articles as the *domain names them*. The block above holds the vehicle's
    # entry against `radiator_model`, which is where its arithmetic is derived into; it does not
    # hold it against `radiator_csm`, `evaporator_csm` and `sublimator_lm` — the components a fault
    # happens to. TCS-03 names `radiator_csm` and argues its solar load against the model's 2,588 W
    # capacity while the component's own `panels` and `area_m2` were read by nothing; TCS-04 names
    # `evaporator_csm`, whose `rejection_w` of 2,345 appears in `vehicle.yaml` as well and was
    # compared in neither place. So the same blind spot the four joins before this one closed is
    # here too, one file over: the vehicle-level entry and the component that stands for it.
    #
    # The link is declared because the files name one panel from opposite ends. The comparison is
    # the intersection of the keys the two share, minus the structural set — the rule the
    # propulsion and comms joins use, and the reason `panels` and `area_m2` were compared the
    # moment the link existed rather than when somebody remembered to add them.
    built_by_key = {
        str(c.get("id")): c
        for c in domain.get("components") or []
        if isinstance(c, dict) and c.get("class") in ("radiator", "evaporator")
    }
    claimed: set[str] = set()
    for cid, component in sorted(built_by_key.items()):
        cwhere = f"domains/thermal/components.yaml:{cid}"
        keys = component.get("vehicle_keys")
        if not isinstance(keys, list) or not keys:
            report.refuse(
                cwhere,
                f"is a {component.get('class')!r} and declares no `vehicle_keys`. It is the article "
                "a fault happens to, so a component claiming no vehicle entry is a copy nothing "
                "holds against the entry the vehicle's heat balance is written from",
            )
            continue
        for key in (str(k) for k in keys):
            entry = radiators.get(key)
            if entry is None:
                report.refuse(
                    cwhere,
                    f"names vehicle radiator {key!r}, which vehicle.yaml#thermal.radiators does not "
                    f"declare; it declares {sorted(radiators)}",
                )
                continue
            claimed.add(key)
            for field in comparable(entry, component, THERMAL_STRUCTURAL):
                if entry[field] != component[field]:
                    report.refuse(
                        cwhere,
                        f"declares {field} {component[field]!r} and vehicle.yaml#thermal.radiators."
                        f"{key} declares {entry[field]!r}. Two views of one article, and the one a "
                        "fault names is this side: a radiator re-rated here and not in the vehicle "
                        "file is heat the loop's own fault injection moves and the balance does not",
                    )
    for key in sorted(set(radiators) - claimed):
        report.debt(
            f"vehicle.yaml:thermal.radiators.{key}",
            "is declared by vehicle.yaml and claimed by no radiator or evaporator component of the "
            "thermal domain, so nothing holds its figures against the object a fault would name",
        )

    # A component that says which loop it is on, and the loops that have nothing on them.
    #
    # Three pumps and a bypass valve carry a `loop` field naming the circuit they belong to, and
    # **no tool read it**: the field is the whole of the membership question — which machine is on
    # which string — and the model answers it in one word per component. Writing the link down
    # found the other half immediately. `loop_primary` has both CSM pumps and the bypass valve,
    # `loop_lm` has its own, and `loop_secondary` has **nothing**: no pump to drive it, no valve to
    # route it, no node and no state. The fleet can still select it — `set_coolant_loop`'s `mode`
    # enum exists to choose between the strings and three verbs take a `loop` argument whose values
    # include it — so a command team can put the vehicle on a loop that cannot flow. That is a debt
    # rather than a refusal: the second string is a real thing `apollo_diode.md:97` describes, and
    # what is missing is whether the model carries its pump, or treats it as a passive spare the
    # commands should not offer.
    on_loop: dict[str, list[str]] = {}
    for component in domain.get("components") or []:
        if not isinstance(component, dict) or not component.get("loop"):
            continue
        cid = str(component.get("id"))
        target = str(component["loop"])
        if target not in set(declared):
            report.refuse(
                f"domains/thermal/components.yaml:{cid}",
                f"declares `loop: {target}`, which is not a loop vehicle.yaml#thermal.loops "
                f"declares; it declares {sorted(declared)}. A component on a circuit that does "
                "not exist is a machine the cooling model cannot place",
            )
            continue
        on_loop.setdefault(target, []).append(cid)
    for loop_id in sorted(declared):
        if loop_id not in on_loop:
            report.debt(
                f"vehicle.yaml#thermal.loops.{loop_id}",
                "is declared by both files and has no component of the thermal domain on it — no "
                "pump drives it, so the loop cannot flow, and three verbs offer it as a `loop` "
                "argument. Either the loop gets the pump and the valve the vehicle has, or it is a "
                "passive string the command surface should not offer",
            )

    # The zones. Both files list the same six, and the domain's `vehicle` is what binds a zone to
    # the compartment whose atmosphere it is; a zone named in one file and not the other is a
    # compartment that is either unregulated or unwatched, and the two are hard to tell apart.
    #
    # The *ids* were all this compared until this round, and the two entries share more than that:
    # `regulated` is on all six in both files and was read by nothing, so a zone the vehicle
    # regulates and the domain does not is a compartment with a heater in one file and none in the
    # other. The comparison is the intersection of the keys the two share, minus `ZONE_STRUCTURAL`
    # — the rule the loops, the engines, the comms hardware and the rejection components all use,
    # and the reason the cabin's nominal pressure and temperature are compared the moment they are
    # declared rather than when somebody remembers to add them here.
    zone_one = {
        str(z.get("id")): z
        for z in ((vehicle or {}).get("thermal") or {}).get("zones") or []
        if isinstance(z, dict)
    }
    zone_two = {str(z.get("id")): z for z in domain.get("zones") or [] if isinstance(z, dict)}
    if set(zone_one) != set(zone_two):
        report.refuse(
            "vehicle.yaml#thermal.zones",
            f"lists {sorted(zone_one)} and the thermal domain lists {sorted(zone_two)}",
        )
    # A zone's `source` is the heater bank that drives it, and **nothing resolved it**: rename
    # `heater_bank_csm` and the raw value moves under both copies at once, so the zone join is
    # silent too, and the zone goes on naming a bank the vehicle does not have. Three of the six
    # zones declare no source at all, which is the honest answer for an unregulated compartment —
    # so the rule is that a source, where it is declared, is a component of this domain.
    heater_banks = {
        str(c.get("id"))
        for c in domain.get("components") or []
        if isinstance(c, dict) and c.get("class") == "heater"
    }
    for zid, zone in sorted(zone_two.items()):
        source = zone.get("source")
        if source is None:
            continue
        if str(source) not in heater_banks:
            report.refuse(
                f"domains/thermal/components.yaml#zones.{zid}",
                f"declares `source: {source}`, and no component of class `heater` in this domain "
                f"carries that id; it declares {sorted(heater_banks)}. A zone names the bank that "
                "drives it, and three of the six name nothing because they are unregulated",
            )

    for zid in sorted(set(zone_one) & set(zone_two)):
        one, two = zone_one[zid], zone_two[zid]
        for field in comparable(one, two, ZONE_STRUCTURAL):
            if one[field] != two[field]:
                report.refuse(
                    f"vehicle.yaml#thermal.zones.{zid}",
                    f"gives {field} as {one[field]!r} and the thermal domain gives {two[field]!r}. "
                    "One compartment, two files: a zone the vehicle regulates and the domain does "
                    "not is a heater that exists on one side of the join, and a zone with a "
                    "different `source` is a different heater bank driving it",
                )

    # And the band a regulated zone declares has to be the band a threshold implements.
    #
    # `thermal_diode.md:131` requires "paired heat/cool thresholds with a minimum dwell for any
    # regulated zone", and the zone is where the vehicle declares both: `limit_c` is the band and
    # `dwell_min_s` is the dwell the threshold must hold before it alarms. Neither was joined to
    # the domain's thresholds. `limit_c` is read by `check_cabin_equilibrium` — as a band the
    # cabin's equilibrium has to lie inside — and `dwell_min_s` was read by **nothing at all**,
    # on any of the six zones, while the thresholds beside it carry their own `dwell_assert_ms`.
    #
    # The link is declared, the way `vehicle_keys` and `domain_group` are, because it cannot be
    # inferred: `csm_avionics_bay`'s band is implemented by `avionics_plate_high`, which watches
    # `thermal.avionics_plate_c` rather than a zone channel, and `lm_descent_bay`'s band has a
    # warning threshold *inside* it that is not the band at all.
    thresholds = {}
    for path in sorted((root / "domains").glob("*/profiles.yaml")):
        doc = load(path, Report()) or {}
        for row in doc.get("thresholds") or []:
            if isinstance(row, dict) and row.get("id"):
                thresholds[str(row["id"])] = (path.parent.name, row)
    for zid in sorted(zone_one):
        zone = zone_one[zid]
        names = zone.get("implemented_by")
        band = zone.get("limit_c") or [None, None]
        dwell_s = zone.get("dwell_min_s")
        # A *regulated* zone is the case the rule is about: `thermal_diode.md:131` requires paired
        # thresholds with a minimum dwell for any zone with a control loop, so a regulated zone
        # that declares a band and names nothing implementing it is a compartment the vehicle
        # claims to hold at a temperature with nothing watching. An unregulated zone — the service
        # bay, the descent bay, the radiator loop — has a band that matters through what it
        # contains and no requirement to alarm on it.
        if not names:
            if zone.get("regulated") and isinstance(band[0], (int, float)):
                report.refuse(
                    f"vehicle.yaml#thermal.zones.{zid}",
                    "is regulated and declares a band with no `implemented_by`, so nothing says "
                    "which threshold holds the compartment at it. `thermal_diode.md:131` requires "
                    "paired heat/cool thresholds with a minimum dwell for a regulated zone, and the "
                    "link is declared rather than inferred because it is not always the zone's own "
                    "channel: `csm_avionics_bay`'s band is held by a plate threshold",
                )
            continue
        if not isinstance(names, list):
            report.refuse(
                f"vehicle.yaml#thermal.zones.{zid}",
                f"declares `implemented_by` as {names!r}, which is not a list of threshold ids",
            )
            continue
        for name in (str(n) for n in names):
            entry = thresholds.get(name)
            if entry is None:
                report.refuse(
                    f"vehicle.yaml#thermal.zones.{zid}",
                    f"names threshold {name!r}, which no domain's profiles.yaml declares. A zone "
                    "saying which threshold implements its band is a link, and a link that has "
                    "stopped linking reads exactly like one that works",
                )
                continue
            domain_name, row = entry
            limit = row.get("assert")
            low, high = band[0], band[1]
            inside = isinstance(limit, (int, float)) and (
                (isinstance(low, (int, float)) and limit < low and row.get("comparator") == "above")
                or (isinstance(high, (int, float)) and limit > high and row.get("comparator") == "below")
                or (
                    isinstance(low, (int, float))
                    and isinstance(high, (int, float))
                    and low <= limit <= high
                )
            )
            if not inside:
                report.refuse(
                    f"vehicle.yaml#thermal.zones.{zid}",
                    f"declares the band {band} and names {name!r}, whose limit is {limit!r} — "
                    f"outside the band it is said to implement. `domains/{domain_name}/profiles.yaml` "
                    "reports that limit by path, so the two are one requirement written twice",
                )
            if isinstance(dwell_s, (int, float)):
                held_ms = row.get("dwell_assert_ms")
                if isinstance(held_ms, (int, float)) and held_ms < float(dwell_s) * 1000.0:
                    report.debt(
                        f"vehicle.yaml#thermal.zones.{zid}",
                        f"declares a minimum dwell of {dwell_s} s and names {name!r}, which "
                        f"`domains/{domain_name}/profiles.yaml` gives {held_ms:g} ms — shorter than "
                        "the zone's own minimum. One of the two is what the vehicle means: either "
                        "the threshold holds the dwell the zone requires, or the zone's minimum is "
                        "the cabin's written onto a compartment whose instrument moves faster",
                    )


# The statuses the conformance table may use. Four say how the vehicle satisfies a check and one
# says why it is moot, and the distinction the table turns on is that `far_side` is not a failure:
# a check the operator's side satisfies is one this side cannot demonstrate and must not claim to.
CONFORMANCE_STATUSES = {
    "demonstrated",
    "satisfied_by_configuration",
    "far_side",
    "shared",
    "vacuous",
}


def check_cabin_volumes(root: Path, vehicle: dict[str, Any], report: Report) -> None:
    """The denominator of every partial pressure the crew read, declared seven times.

    `atmosphere_model` states the law the whole life-support model rests on —
    `P = (sum_i n_i) R T / V` — and `V` is the cabin volume. It is also the figure that turns a gas
    *mass* into the partial pressure a crew member reads and an agent decides on, so it sits
    underneath `eclss.pp_o2_mmhg` on one side and every thermal time constant on the other.

    It is declared seven times in three files: twice in `vehicle.yaml#thermal.zones`, twice in
    `domains/thermal/components.yaml#zones`, twice as `class: volume` components in
    `domains/eclss/components.yaml`, and once more as `atmosphere_model.volume_m3` in that same
    file. Until this check, **no tool read any of them.** `check_thermal_bindings` holds the two
    zone *id sets* equal and compares no field; `csm_cabin_volume` is a component of a class no
    check looks at.

    The corpus knows it is duplicated and undercounts itself doing it: the LM oxygen initial's own
    relation says "The corpus states the relation once and the volume twice." It is seven times, and
    the note that says two is itself evidence for why this folder keeps finding the same shape —
    a copy nobody reads is a copy nobody counts.

    The link is not invented. `check_thermal_bindings` already holds the zone ids equal between the
    two files that carry zones, and the thermal domain's zones carry the `vehicle` each one is. So
    the authority is `atmosphere_model.volume_m3`, keyed by vehicle — the one place the physics law
    itself reads — and every other declaration is held against it.
    """
    eclss = load(root / "domains" / "eclss" / "components.yaml", report) or {}
    thermal = load(root / "domains" / "thermal" / "components.yaml", report) or {}
    model = eclss.get("atmosphere_model") or {}
    volumes = model.get("volume_m3")
    if not isinstance(volumes, dict) or not volumes:
        report.debt(
            "domains/eclss/components.yaml:atmosphere_model",
            "declares no `volume_m3` per compartment, so the ideal-gas relation stated beside it "
            "has no denominator and no cabin volume in the vehicle can be held against anything",
        )
        return
    volumes = {str(k): v for k, v in volumes.items()}

    claimed: set[str] = set()
    for c in eclss.get("components") or []:
        if not isinstance(c, dict) or c.get("class") != "volume":
            continue
        cid = str(c.get("id"))
        where = f"domains/eclss/components.yaml:{cid}"
        which = str(c.get("vehicle"))
        if which not in volumes:
            report.refuse(
                where,
                f"names vehicle {which!r}, which atmosphere_model.volume_m3 does not declare; it "
                f"declares {sorted(volumes)}",
            )
            continue
        claimed.add(which)
        if c.get("volume_m3") != volumes[which]:
            report.refuse(
                where,
                f"declares {c.get('volume_m3')!r} m3 for the {which} cabin and "
                f"atmosphere_model.volume_m3.{which} declares {volumes[which]!r}. This is `V` in "
                "the law the crew's partial pressures come from, so two values is two atmospheres "
                "— and the one the crew read is whichever the plant happened to load",
            )
    for which in sorted(volumes):
        if which not in claimed:
            report.refuse(
                "domains/eclss/components.yaml:atmosphere_model",
                f"declares a cabin volume for {which!r} and no component of class `volume` carries "
                "it, so the compartment the law is stated for is not one the domain models",
            )

    zones_two = {str(z.get("id")): z for z in thermal.get("zones") or [] if isinstance(z, dict)}
    zones_one = {
        str(z.get("id")): z
        for z in ((vehicle or {}).get("thermal") or {}).get("zones") or []
        if isinstance(z, dict)
    }
    # The volume the law divides by belongs to the *cabin* zone of each vehicle, and the zones are
    # named for it. Nothing here is keyed on a zone's `vehicle` field: `atmosphere_model.volume_m3`
    # is keyed by vehicle, and every other zone of that vehicle — the avionics bay, the service bay,
    # the descent bay — has a volume of its own that the atmosphere model says nothing about. The
    # first version of this check compared any zone of the right vehicle against the cabin, which
    # would have refused a perfectly good service-bay volume for disagreeing with a cabin.
    for which in sorted(volumes):
        zid = f"{which}_cabin"
        for label, doc in (
            ("vehicle.yaml#thermal.zones", zones_one.get(zid)),
            ("domains/thermal/components.yaml#zones", zones_two.get(zid)),
        ):
            if not isinstance(doc, dict):
                report.refuse(
                    label,
                    f"lists no zone {zid!r}, and atmosphere_model.volume_m3 declares a "
                    f"{which} compartment. The law is stated for a cabin this file does not carry",
                )
                continue
            if "volume_m3" not in doc:
                report.refuse(
                    f"{label}.{zid}",
                    "declares no `volume_m3`, so the ideal-gas relation the crew's partial "
                    "pressures come from has no denominator for this compartment",
                )
                continue
            if doc["volume_m3"] != volumes[which]:
                report.refuse(
                    f"{label}.{zid}",
                    f"declares {doc['volume_m3']!r} m3 for the {which} cabin and "
                    f"atmosphere_model.volume_m3.{which} declares {volumes[which]!r}. The volume is "
                    "the denominator of every partial pressure the crew read and of every thermal "
                    "time constant in this zone, so the two must be one number",
                )


def check_presentation_references(
    root: Path,
    presentation: dict[str, Any],
    coupling: dict[str, Any],
    mission: dict[str, Any],
    report: Report,
    channels: dict[str, Any] | None = None,
) -> None:
    """Four path references and a conformance table, none of which anything resolved.

    A deletion test over every top-level block in the folder left seven that no tool noticed
    losing, and these are the five that are *claims about other files*. Each is a reference in the
    folder's own sense — a name whose whole value is that it points at something — and a reference
    nothing resolves is a reference that is already free to be wrong. All five are right today.

    `presentation.yaml` names the frozen contract and the probe that tests it; `coupling.yaml`
    names the corpus document it was generated from and the section of the design that fixes its
    provenance rule. The paths are written relative to the repository root, so this resolves them
    by walking up from the vehicle directory — which also means the check survives the move the
    folder is destined for, when `docs/diode-contract.md` stops being two levels up. A path that
    resolves nowhere is refused, and so is one that resolves only because a *different* file
    happens to share its name at a shallower level.

    The conformance table is the vehicle's claim to satisfy `docs/diode-contract.md` §9, and it is
    twelve rows against the contract's twelve numbered checks. Nothing joined the two, so a row
    dropped in an edit would leave a check nobody claims and a row duplicated would claim one
    twice — neither visible in a table that reads perfectly.
    """

    def resolve(target: str) -> Path | None:
        # The vehicle directory and its ancestors first, then the process's own working directory
        # and *its* ancestors. The second half is not a convenience: these paths are
        # repository-relative, and a test that copies the definition into a temporary directory
        # takes the vehicle out of the repository without taking the repository away. Resolving
        # only by walking up refused four correct references on every fixture copy — which is the
        # check working, on a question about where the vehicle is rather than about what it says.
        for base in (root, *root.parents, Path.cwd(), *Path.cwd().parents):
            candidate = base / target
            if candidate.exists():
                return candidate
        return None

    for where, value in (
        ("presentation.yaml:contract", presentation.get("contract")),
        ("presentation.yaml:contract_probe", presentation.get("contract_probe")),
        ("coupling.yaml:generated_from", coupling.get("generated_from")),
        ("coupling.yaml:provenance_rules", coupling.get("provenance_rules")),
    ):
        if value is None:
            report.refuse(where, "is absent. The reference is the whole content of the field")
            continue
        target = str(value).split("#", 1)[0]
        if not target.strip():
            report.refuse(f"{where}.{value}", "names no file")
        elif resolve(target) is None:
            report.refuse(
                where,
                f"names {value!r} and no such file is reachable from the vehicle directory. A "
                "reference whose target has been renamed reads exactly like a reference whose "
                "target is there",
            )

    rows = presentation.get("conformance")
    if isinstance(rows, list) and rows:
        numbers = [row.get("check") for row in rows if isinstance(row, dict)]
        if not all(isinstance(n, int) for n in numbers):
            report.refuse(
                "presentation.yaml:conformance",
                f"numbers its rows {numbers}, and a check number that is not an integer cannot be "
                "matched against the contract's own list",
            )
        else:
            duplicated = sorted({n for n in numbers if numbers.count(n) > 1})
            missing = sorted(set(range(1, max(numbers) + 1)) - set(numbers))
            if duplicated:
                report.refuse(
                    "presentation.yaml:conformance",
                    f"claims checks {duplicated} more than once. Two rows for one check is one "
                    "check claimed twice and another claimed by nobody",
                )
            elif missing:
                report.refuse(
                    "presentation.yaml:conformance",
                    f"covers checks {sorted(numbers)} and skips {missing}. The table is the "
                    "vehicle's claim to satisfy the contract, so a gap is a check nobody claims "
                    "and a reader cannot tell whether it was missed or conceded",
                )
        for row in rows:
            if not isinstance(row, dict):
                continue
            cwhere = f"presentation.yaml:conformance check {row.get('check')}"
            if row.get("status") not in CONFORMANCE_STATUSES:
                report.refuse(
                    f"{cwhere}.status",
                    f"is {row.get('status')!r}, which is not one of "
                    f"{sorted(CONFORMANCE_STATUSES)}. `far_side` in particular is not a failure: a "
                    "check the operator's side satisfies is one this side cannot demonstrate and "
                    "must not claim to",
                )
            # The third key is `disposition` because it was `vehicle`, and that one word was doing
            # three jobs across the corpus: a spacecraft id sixty-seven times, the *document*
            # (`mission.yaml`'s `vehicle: vehicle.yaml`) once, and this narrative twelve times. A
            # key with three meanings is a key no rule can be written about, which is why sixty-seven
            # declarations of which spacecraft a component belongs to were resolved by nothing.
            for field in ("property", "disposition"):
                if not str(row.get(field) or "").strip():
                    report.refuse(cwhere, f"declares no `{field}`")

    # The epistemic mapping's keys are the registry's layer vocabulary, and nothing compared them.
    # It is a three-word vocabulary declared in one file and *used* in another, which is the shape
    # that drifts: a fourth layer added to the registry would be a kind of channel with no mapping
    # to the contract, and the check that reads the mapping would skip it silently. Every layer the
    # registry declares must have a mapping, and the mapping must map nothing else.
    mapping = ((presentation.get("epistemic_layers") or {}).get("mapping")) or {}
    layers = {
        str(row.get("layer"))
        for rows in (channels or {}).values()
        if isinstance(rows, list)
        for row in rows
        if isinstance(row, dict) and row.get("layer")
    }
    if mapping and layers:
        unmapped = sorted(layers - set(map(str, mapping)))
        extra = sorted(set(map(str, mapping)) - layers)
        if unmapped:
            report.refuse(
                "presentation.yaml:epistemic_layers.mapping",
                f"does not map {unmapped}, which `channels.yaml` uses as a layer. A layer with no "
                "mapping is a kind of channel the contract has no layer for, and the publisher "
                "would have to invent one",
            )
        if extra:
            report.refuse(
                "presentation.yaml:epistemic_layers.mapping",
                f"maps {extra}, which is not a layer any channel declares. A mapping for a layer "
                "that does not exist is a decision about nothing",
            )

    # `mission.yaml`'s seed is the one declaration determinism rests on — `plant.md` §6 keys every
    # stochastic stream to it — and its provenance was a block nothing validated.
    seed = mission.get("random_seed_provenance")
    if isinstance(seed, dict):
        check_basis("mission.yaml:random_seed_provenance", seed.get("basis"), seed, report)


# The characters a re-derivable expression may use *after* its declared inputs have been
# substituted. Identifiers are allowed in the expression the file writes, and are gone by the time
# this is applied — which is what keeps the property `rederive` was built to have: the linter never
# evaluates anything but arithmetic over numbers.
NUMERIC_EXPRESSION = re.compile(r"[0-9eE+\-*/(). \t]+")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def evaluate_expression(expression: str) -> float:
    """Evaluate a numeric expression, or raise. One evaluator, so one definition of what is safe."""
    if not NUMERIC_EXPRESSION.fullmatch(expression):
        raise ValueError("not a numeric expression")
    return float(eval(expression, {"__builtins__": {}}, {}))  # noqa: S307


def check_edge_derivations(
    coupling: dict[str, Any], documents: dict[str, Any], report: Report
) -> None:
    """An edge whose sensitivity is another declaration's function binds its inputs by name.

    `rederive` already checks a sensitivity that states its own arithmetic: an edge carrying
    `computation: "1 / 2.45e6"` is re-evaluated against its `value` on every run, which is how
    `E-RAD-WATER`'s 7 % disagreement was caught. That idiom has one weakness, and it is the weakness
    this folder spends its rounds on: **the numbers inside the expression are copies.** They are read
    from nowhere, so they cannot be held to the declaration they came from, and the check can only
    catch an edge that disagrees with *itself*.

    `E-GEOM-LINK` is the case that matters. Its -0.54 dB per degree is `-24 * 2 / 9.4^2`, and both
    numbers belong to other files: 9.4 is `vehicle.yaml#comms.antennas.high_gain`'s half-power
    beamwidth — which since the last round is itself tied to the gain through the ideal-aperture
    product — and 2 is the pointing error the derivative is taken at, which until this round existed
    only inside a sentence. Re-rate the antenna from 26.7 dB to 28 dB, in **both** files and
    consistently, so that no join can complain and no product check can either: the beamwidth becomes
    8.1 degrees and this edge goes on scaling every pointing loss the fleet reads by the slope of a
    beam that no longer exists.

    So a sensitivity may declare a `derivation` instead — an arithmetic expression over named inputs,
    where each input is either a number or a `"<file>.yaml:<dotted.path>"` source in the syntax
    `derives_from` already uses. Three rules make it a declaration rather than a program:

    - every identifier in the expression must be an input the file declares, and every declared input
      must appear in the expression, so neither a name nor a binding can go unread;
    - a source that does not resolve is refused rather than tolerated, because a renamed source reads
      exactly like an unset one, and a source that is `UNCONFIGURED` checks nothing, because the
      obligation is counted where the quantity lives rather than at its use;
    - the expression is evaluated **after** substitution and only if what remains is arithmetic over
      numbers, so the configuration still cannot become executable.

    A `computation` is the degenerate case of this — an expression with no inputs — and stays as it
    is: most relations that state arithmetic are genuinely about their own literals, and the one
    place a literal was a *copy* of another declaration is what this check is for.
    """
    for index, edge in enumerate((coupling or {}).get("edges") or []):
        if not isinstance(edge, dict):
            continue
        sensitivity = edge.get("sensitivity")
        if not isinstance(sensitivity, dict) or sensitivity.get("derivation") is None:
            continue
        check_declared_derivation(
            f"coupling.yaml:edges[{index}] {edge.get('id')}.sensitivity.derivation",
            sensitivity["derivation"],
            sensitivity.get("value"),
            "the sensitivity",
            documents,
            report,
        )


def check_declared_derivation(
    where: str,
    derivation: Any,
    value: Any,
    subject: str,
    documents: dict[str, Any],
    report: Report,
) -> None:
    """One `derivation`, evaluated against the value it claims to produce.

    Extracted so that the rule has **one implementation** when a second kind of declaration wants
    it: an edge's `sensitivity` has carried a `derivation` since the round that found `E-GEOM-LINK`
    scaling every pointing loss by the slope of a beam that no longer existed, and a consumables
    consumer's `rate_kg_s` is the same shape of claim — an arithmetic over named inputs, where each
    input is either a number or a `"<file>.yaml:<dotted.path>"` source. A second copy of these
    twelve refusals is the defect this folder spends its rounds removing, so the second binding
    site calls this one instead.
    """
    if not isinstance(derivation, dict):
        report.refuse(where, f"is a {type(derivation).__name__}, which is not a mapping")
        return
    expression = derivation.get("expression")
    if not isinstance(expression, str) or not expression.strip():
        report.refuse(f"{where}.expression", f"is {expression!r}, not an expression")
        return
    inputs = derivation.get("inputs")
    if not isinstance(inputs, dict) or not inputs:
        report.refuse(
            f"{where}.inputs",
            f"is {inputs!r}. Every name the expression uses is bound here, and an expression "
            "with no bindings is a `computation` rather than a derivation",
        )
        return

    named = set(IDENTIFIER.findall(expression))
    for unknown in sorted(named - set(inputs)):
        report.refuse(
            f"{where}.inputs",
            f"binds no {unknown!r}, which the expression uses. An unbound name is a number the "
            "expression expects from somewhere this file does not say",
        )
    for unused in sorted(set(inputs) - named):
        report.refuse(
            f"{where}.inputs",
            f"binds {unused!r}, which the expression does not use, so nothing reads it",
        )
    if named - set(inputs) or set(inputs) - named:
        return

    values: dict[str, float] = {}
    complete = True
    for key in sorted(inputs):
        raw = inputs[key]
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            values[key] = float(raw)
            continue
        text = str(raw)
        if ":" not in text:
            report.refuse(
                f"{where}.inputs.{key}",
                f"is {text!r}, which is neither a number nor a source. A source names its "
                "document, in the `<file>.yaml:<dotted.path>` form `derives_from` uses",
            )
            complete = False
            continue
        filename, dotted = text.split(":", 1)
        if filename not in documents:
            report.refuse(
                f"{where}.inputs.{key}",
                f"names {filename!r}, and the documents this check can resolve are "
                f"{sorted(documents)}",
            )
            complete = False
            continue
        resolved = resolve_dotted(documents[filename], dotted)
        if resolved is None:
            report.refuse(
                f"{where}.inputs.{key}",
                f"is {text!r} and {filename} has no {dotted!r}. A source that has been renamed "
                "reads exactly like a source that is unset",
            )
            complete = False
            continue
        if resolved == "UNCONFIGURED":
            # The obligation is counted where the quantity lives, and this is a use of it rather
            # than a second unknown. Nothing to check until it lands.
            complete = False
            continue
        if not isinstance(resolved, (int, float)) or isinstance(resolved, bool):
            report.refuse(
                f"{where}.inputs.{key}",
                f"resolves to {resolved!r}, which is not a number to derive a value from",
            )
            complete = False
            continue
        values[key] = float(resolved)
    if not complete:
        return

    if not isinstance(value, (int, float)) or isinstance(value, bool):
        report.refuse(
            where,
            f"states a derivation and {subject} is {value!r}, which there is nothing to hold "
            "it against",
        )
        return
    # `values=values` binds the mapping as a default rather than closing over the loop's
    # variable, which is the same substitution written so that it cannot become a late binding.
    substituted = IDENTIFIER.sub(
        lambda match, values=values: repr(values[match.group(0)]), expression
    )
    try:
        derived = evaluate_expression(substituted)
    except Exception as exc:  # noqa: BLE001 - any failure is a refusal
        report.refuse(f"{where}.expression", f"cannot be evaluated: {exc}")
        return
    if not agrees_with_derivation(float(value), derived):
        shown = ", ".join(f"{k}={values[k]:g}" for k in sorted(values))
        report.refuse(
            where,
            f"derives {derived:.6g} from {expression!r} at {shown}, and {subject} "
            f"declares {value!r}. A derived value that no longer re-derives is a value nobody "
            "has checked since the declaration it came from moved",
        )


def check_spacecraft_vocabulary(documents: dict[str, Any], report: Report) -> None:
    """Every `vehicle:` names one of the spacecraft the vehicle declares, and it declares them once.

    `vehicle:` is written **sixty-seven times** across the domains — every power source, load and
    bus, every atmosphere, every thermal zone and loop, every component the crew can be in — and
    until this round no list existed to hold a value against. The probe is one word: `vehicle: csmx`
    on a power component composed, and so did a thermal zone moved to a spacecraft that does not
    exist. Sixty-seven declarations of *which spacecraft a piece of hardware belongs to*, resolved
    by nothing, in a corpus whose oldest finding is that a declaration no tool reads has already
    drifted.

    The rule could not simply be written, and that is the round's real finding: **the key carried
    three meanings.** A spacecraft id, sixty-seven times; the *document* `mission.yaml` registers
    its channels against (`vehicle: vehicle.yaml`), once; and the narrative of what this side does
    about a contract check, twelve times in `presentation.yaml`'s conformance table — where the
    linter *required* it to be non-empty, so the overload was enforced. The two minority senses are
    `vehicle_document` and `disposition` now, which is what makes a rule about the majority one
    possible, and the list they are held against is `vehicle.yaml#spacecraft` — the only place in
    the corpus that says how many spacecraft there are.

    Two of the sixty-seven are not a component's and are held here as well: the keys of
    `atmosphere_model.volume_m3`, which `check_cabin_volumes` uses as the authority for the cabin
    volume and which were a second, silent enumeration of the same fact.
    """
    spacecraft = (documents.get("vehicle.yaml") or {}).get("spacecraft")
    # A *shape* fault rather than an owed value: absence and emptiness are the section check's
    # business, and this is the guard that stops a malformed list — `spacecraft: csm, lm` parses to
    # a string, and iterating a string gives its characters — turning into sixty-seven refusals
    # about spacecraft named 'c' and 's'.
    if not isinstance(spacecraft, list) or not all(
        isinstance(entry, str) and entry for entry in spacecraft
    ):
        report.refuse(
            "vehicle.yaml:spacecraft",
            f"is {spacecraft!r}, not a non-empty list of spacecraft names. The sixty-seven "
            "`vehicle:` fields across the domains are held against it, so a list that is not one "
            "leaves every one of them unresolved",
        )
        return
    if not spacecraft:
        report.refuse("vehicle.yaml:spacecraft", "declares no spacecraft at all")
        return
    declared = {str(s) for s in spacecraft}

    found: dict[str, list[str]] = {}

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                here = f"{path}.{key}"
                if key == "vehicle" and isinstance(value, str):
                    found.setdefault(value, []).append(here)
                walk(value, here)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    for name, document in sorted(documents.items()):
        walk(document, name)

    for value in sorted(found):
        if value in declared:
            continue
        for where in sorted(found[value])[:3]:
            report.refuse(
                where,
                f"names spacecraft {value!r}, which vehicle.yaml#spacecraft does not declare; it "
                f"declares {sorted(declared)}. Hardware on a spacecraft that does not exist is "
                "hardware nothing can be asked about, and the field was resolved by nothing until "
                "this check",
            )
    for which in sorted(
        ((documents.get("domains/eclss/components.yaml") or {}).get("atmosphere_model") or {}).get(
            "volume_m3"
        )
        or {}
    ):
        if str(which) not in declared:
            report.refuse(
                f"domains/eclss/components.yaml:atmosphere_model.volume_m3.{which}",
                f"states the ideal-gas law for {which!r}, which vehicle.yaml#spacecraft does not "
                f"declare; it declares {sorted(declared)}. The law's compartments and the vehicle's "
                "spacecraft are one list",
            )


def check_argument_vocabularies(documents: dict[str, Any], report: Report) -> None:
    """An argument a fleet can send names something the vehicle has.

    `pump_1`, `pump_2` and `pump_lm` are components of the thermal domain, and `set_coolant_pump`
    offers them as an enum. Rename one, or add a fourth that does not exist, and **nothing
    noticed**: adding `pump_9` to the schema composed, and adding it to the schema is exactly how a
    fleet comes to be offered a pump the vehicle does not have. The console validates a command
    against this enum, so a name in it is a name the record will accept.

    The binding is inferred from the argument's *name* wherever it can be: an argument called
    `pump` names pumps, `loop` names loops, `engine` names engines, `hatch` names hatches. That
    works for fourteen arguments across six domains and it is silent on vocabularies, which is the
    point — `mode: [primary, secondary, series, isolated]` names no object and must not be read as
    one. The alternative trigger, "every value in this enum is a component id", was measured rather
    than guessed and produces five false refusals: `select_sensor.group` names `imu` and `radar`
    beside `cabin_pressure` and `co2`, `ask_crew.position` names `tunnel` beside five crew
    stations, `select_nav_source.source` names `radar` beside four modes, and two more. A rule that
    is wrong five times on the corpus it is written for is a rule nobody will keep.

    Two arguments name inventory rather than machinery, and one of them has to say so:
    `select_antenna.antenna`'s values are the *vehicle-level* antenna ids — `high_gain`, `omni_a`,
    `sband_steerable` — which the domain's components claim through `vehicle_keys`, except for
    `sband_steerable`, which nothing claims. That gap is already one report, in
    `check_comms_bindings`, and a second here would be the same unknown counted twice. So an
    argument may declare `names: vehicle_entry`, and the test is then that every value is an id
    `vehicle.yaml` declares — which `sband_steerable` is.

    What the rule does *not* reach is an argument whose name matches no class and which declares
    nothing: `set_heater.bank`, `set_battery_contactor.battery`, `set_breaker.breaker` and
    `set_bus_tie.tie` name components in their domains and are unchecked, because nothing about the
    word `bank` says it means a `heater`. Declaring `names: component` on them is what fixes that,
    and it is the same declaration the rule uses when the name does match.
    """
    vehicle_ids: set[str] = set()

    def collect(node: Any) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("id"), str):
                vehicle_ids.add(node["id"])
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)

    collect(documents.get("vehicle.yaml") or {})

    for filename in sorted(documents):
        if not filename.endswith("commands.yaml"):
            continue
        domain = filename.split("/")[1]
        components = documents.get(f"domains/{domain}/components.yaml") or {}
        classes: dict[str, set[str]] = {}
        for key in ("components", "state"):
            for entry in components.get(key) or []:
                if isinstance(entry, dict) and entry.get("id") and entry.get("class"):
                    classes.setdefault(str(entry["class"]), set()).add(str(entry["id"]))
        every = set().union(*classes.values()) if classes else set()
        for verb in (documents[filename].get("commands") or []):
            if not isinstance(verb, dict):
                continue
            for argument, spec in (verb.get("argument_schema") or {}).items():
                if not isinstance(spec, dict) or spec.get("type") != "enum":
                    continue
                declared = str(spec.get("names") or "")
                named_class = argument if argument in classes else ""
                if not declared and not named_class:
                    continue
                where = f"domains/{domain}/commands.yaml:{verb.get('verb')}.{argument}"
                values = [str(v) for v in spec.get("values") or []]
                if declared == "vehicle_entry":
                    for value in values:
                        if value not in vehicle_ids:
                            report.refuse(
                                f"{where}",
                                f"names {value!r}, which vehicle.yaml does not declare as any "
                                "object's id. An argument declared to name vehicle-level inventory "
                                "may only name inventory that exists",
                            )
                    continue
                if declared and declared != "component":
                    report.refuse(
                        f"{where}",
                        f"declares `names: {declared!r}`, and the only vocabularies this rule knows "
                        "are `component` and `vehicle_entry`",
                    )
                    continue
                # `names: component` widens the test to every class in the domain, which is what
                # an argument like `set_source.source` needs: a bus's sources are its fuel cells
                # *and* its batteries, and the two are different classes. The name-match narrowing
                # is the default, not an override of what the file declared.
                known = every if declared == "component" else classes.get(named_class, set())
                narrow = bool(named_class) and declared != "component"
                for value in values:
                    if value in known:
                        continue
                    # The message has to describe the test that actually ran. The first version
                    # kept the class wording whenever the *name* matched a class, so an argument
                    # that had declared the wider vocabulary refused with "not a 'source'" while
                    # the test was "not a component of this domain" — a refusal that misdescribes
                    # its own rule is the thing this whole effort exists to remove, and a fixture
                    # whose needle did not appear is what found it.
                    if narrow:
                        report.refuse(
                            f"{where}",
                            f"names {value!r}, which is not a {named_class!r} in this domain; it "
                            f"declares {sorted(known)}. An argument called `{argument}` names "
                            f"{argument}s, and a name the vehicle does not have is a command the "
                            "record accepts and nothing can execute",
                        )
                    else:
                        report.refuse(
                            f"{where}",
                            f"names {value!r}, which is not a component of this domain; it declares "
                            f"{sorted(known)}",
                        )


def check_threshold_derivations(root: Path, documents: dict[str, Any], report: Report) -> None:
    """A threshold whose limit *is* another declared quantity says so, and is checked against it.

    Three `gnc` thresholds name a quantity the domain already declares and owes: the alignment
    error is measured "against the platform's alignment budget", the gyro bias against
    `imu.drift_deg_per_h`, the radar's altitude against `landing_radar.range_km`. All three of
    those component fields are `UNCONFIGURED`, so **each missing datum was counted twice** — once
    where the quantity lives and once where it is used — and `drift_deg_per_h` was counted *three*
    times, because a fault's seeding names it as well.

    That is the inflation round 74 removed from `assert`/`clear`, arriving through a different
    door. The instrument is the same in spirit: name the dependency so the use is not a second
    obligation. A threshold declaring `derives_from` reports no debt of its own — the walk skips it
    — and when the source resolves, the threshold's limit is the source times `derives_factor`,
    which is checked here. The factor is not decoration: the bias threshold is in deg/s and the
    drift it is sized from is in deg/h, so the conversion is 1/3600, and the radar's range is
    published in kilometres while the channel is in metres.

    A path that no longer resolves is refused rather than tolerated, because a source that has been
    renamed reads exactly like a source that is unset.
    """
    for name, document in sorted(documents.items()):
        if not name.endswith("profiles.yaml"):
            continue
        for index, threshold in enumerate((document or {}).get("thresholds") or []):
            if not isinstance(threshold, dict):
                continue
            source = threshold.get("derives_from")
            if not source:
                continue
            where = f"{name}:thresholds[{index}] {threshold.get('id')}"
            if not str(threshold.get("derives_note") or "").strip():
                report.refuse(
                    f"{where}.derives_note",
                    "is absent. A limit taken from another declaration is a claim about the "
                    "*relationship* — that this threshold is that quantity rather than a multiple "
                    "of it, and what the factor converts — and the note is where the claim is made",
                )
            text = str(source)
            if ":" not in text:
                report.refuse(
                    f"{where}.derives_from",
                    f"is {text!r}, which names no document. The form is `<file>.yaml:<dotted.path>`",
                )
                continue
            filename, dotted = text.split(":", 1)
            if filename not in documents:
                report.refuse(
                    f"{where}.derives_from",
                    f"names {filename!r}, and the documents this check can resolve are "
                    f"{sorted(documents)}",
                )
                continue
            resolved = resolve_dotted(documents[filename], dotted)
            factor = threshold.get("derives_factor", 1)
            if not isinstance(factor, (int, float)) or factor == 0:
                report.refuse(f"{where}.derives_factor", f"is {factor!r}")
                continue
            if resolved is None:
                report.refuse(
                    f"{where}.derives_from",
                    f"is {text!r} and {filename} has no {dotted!r}. A source that has been renamed "
                    "reads exactly like a source that is unset",
                )
                continue
            if resolved == "UNCONFIGURED":
                # The obligation is counted where the quantity lives, and this threshold is a use
                # of it rather than a second unknown. Nothing to check until it lands.
                continue
            if not isinstance(resolved, (int, float)):
                report.refuse(
                    f"{where}.derives_from",
                    f"resolves to {resolved!r}, which is not a number to derive a limit from",
                )
                continue
            wanted = float(resolved) * float(factor)
            if threshold.get("assert") == "UNCONFIGURED":
                report.refuse(
                    f"{where}.assert",
                    f"is UNCONFIGURED while `derives_from` resolves to {resolved!r}. A threshold "
                    f"that declares where its limit comes from takes it: {wanted:g} here",
                )
            elif abs(float(threshold["assert"]) - wanted) > abs(wanted) * 1e-3 + 1e-12:
                # **Tighter than `rederive`'s one per cent, and the reason is specific.** A derived
                # threshold is not an independent measurement that happens to agree with its
                # derivation; it *is* the derivation, restated, so the only slack it needs is the
                # rounding in its own decimal places. One per cent was wide enough to hide the whole
                # gap this check exists to protect: `uncommanded_acceleration`'s ceiling is the LM
                # ascent engine at 0.32479 g, a hair above the CSM's own 0.32279 g with the LM gone,
                # and weakening the ascent engine by 3 kN moves the ceiling to the CSM figure — a
                # 0.63 % change that a one-per-cent tolerance accepted in silence.
                report.refuse(
                    f"{where}.assert",
                    f"is {threshold['assert']} and `derives_from` resolves to {resolved!r} x "
                    f"{factor:g} = {wanted:g}. One of the two is the quantity and the other is a "
                    "copy of it, and only this check keeps them the same number",
                )


# What an objective can be. `outcome` is a thing that either happened or did not, `margin` is a
# quantity that has to stay inside a bound, and `research` is a question the mission exists to ask —
# the three the corpus uses, and the set is declared so a fourth is a decision rather than a typo.
OBJECTIVE_KINDS = {"outcome", "margin", "research"}
# Which end of a margin is the good one. `higher_is_better` is a reserve that is spent down and
# `closest_to_limit` is a band a zone ran near without crossing; naming the set is what keeps a
# third from being spelled into existence, in a block whose whole job is to be the score.
OBJECTIVE_SENSES = {"higher_is_better", "closest_to_limit"}


def check_mission_model(mission: dict[str, Any], report: Report) -> None:
    """The postures, the transitions between them and the objectives — six fields nothing validated.

    Found the way the command registry's five were: mutate each field in turn and see which
    mutations compose. These are the blocks that decide what the *challenge* is — a posture is what
    a fleet is authorized to do, a transition is the guard that moves it, and an objective is the
    score — so a field in them that nothing reads is a hole in the experiment rather than in a
    subsystem.

    The transition strings are parsed rather than matched, because two of the five are not a plain
    `A -> B`: `execute -> hold on stale evidence` appends the *reason* to the target, and
    `any -> aborting` has no source posture at all. So the rule is that the source is `any` or a
    declared posture, and the target's **first word** is a declared posture — which accepts both
    forms and still refuses `safe -> standbye`.

    `requires` must be *present*, and an empty one is allowed. That is not a loophole: `any ->
    aborting` declares `requires: {}` and its note is the most important sentence in the block —
    *"Abort requires no fresh evidence because requiring it would make the abort conditional on the
    very instrumentation whose failure is a reason to abort."* A present-but-empty list is that
    decision; an **absent** one is indistinguishable from an oversight, which is the same
    distinction `interlocks: none` draws for a command.

    And this file's own `open_debts` are counted here, which is how the round found the fourth
    instance of an asymmetry that had already been repaired in three places. The round wrote one
    entry into this list — the permission below, and the verbs it is not joined to — and **the
    headline count did not move**: `channels.yaml`'s eight are counted since the section existed,
    `coupling.yaml`'s eight when its shopping list turned out to be read by nobody, `vehicle.yaml`'s
    nine and the domains' twenty-four when a search for an EVA note opened the file it was sitting
    in, and this file, written last, was still outside the number. A debt that is not counted is not
    fatal under `--strict`, which is the only thing a debt is for.
    """
    for row in mission.get("open_debts") or []:
        report.debt("mission.yaml:open_debts", str(row))

    postures = [
        str(p.get("id"))
        for p in mission.get("postures") or []
        if isinstance(p, dict) and p.get("id")
    ]
    known = set(postures)

    # A posture is a mode the vehicle is in and the executive authorizes against. Two of them under
    # one id is a mode whose authority depends on which entry a reader found first.
    repeated = sorted({name for name in postures if postures.count(name) > 1})
    if repeated:
        report.refuse(
            "mission.yaml:postures",
            f"declares {repeated} more than once. A posture is a mode a fleet is authorized "
            "against, so two entries under one id is an authority that depends on the reader",
        )
    for posture in mission.get("postures") or []:
        if not isinstance(posture, dict):
            continue
        where = f"mission.yaml:posture {posture.get('id')}"
        if not isinstance(posture.get("terminal"), bool):
            report.refuse(
                f"{where}.terminal",
                f"is {posture.get('terminal')!r}, not a boolean. `terminal` says whether the "
                "mission is over, and a word that merely reads like yes is a mode the executive "
                "cannot decide about",
            )
        for field in ("name", "entry", "exit"):
            if not str(posture.get(field) or "").strip():
                report.refuse(f"{where}.{field}", "is empty")

    # The transitions. `any` is a declared source meaning the guard applies from wherever the
    # vehicle is, and the target's first word is the posture it arrives at.
    for row in mission.get("transition_evidence") or []:
        if not isinstance(row, dict):
            continue
        text = str(row.get("transition") or "")
        where = f"mission.yaml:transition {text!r}"
        if "->" not in text:
            report.refuse(
                f"{where}", "is not of the form `<from> -> <to>`, so it names no pair of postures"
            )
            continue
        source, _, target = text.partition("->")
        source, target = source.strip(), target.strip()
        if source != "any" and source not in known:
            report.refuse(
                f"{where}",
                f"begins {source!r}, which is neither a declared posture nor `any`. A guard on a "
                f"transition from nowhere is a guard nothing evaluates. The postures are "
                f"{sorted(known)}",
            )
        head = target.split()[0] if target.split() else ""
        if head not in known:
            report.refuse(
                f"{where}",
                f"arrives at {head!r}, which is not a declared posture. The target's first word is "
                "the posture — `execute -> hold on stale evidence` is a legal form and `safe -> "
                "standbye` is not",
            )
        if "requires" not in row:
            report.refuse(
                f"{where}.requires",
                "is absent. An empty list is a decision — `any -> aborting` declares one and gives "
                "the reason it must stay empty — but an absent field is indistinguishable from an "
                "oversight, and this is the evidence a safety transition is judged on",
            )

    # `hazardous_actions_permitted` is the field that decides whether a fleet may act at all, and
    # nothing read it: the block's other fields were validated and this one was declared ten times
    # and compared to nothing. *`mission_diode.md`:1727* puts "no hazardous verb is enabled in
    # ABORTED" in the list a startup must verify statically, and TV-E (`:1395`) refuses any
    # hazardous request with the abort latch set — so the field is a safety property with a
    # published test vector behind it, declared per posture and unread.
    permitting: set[str] = set()
    terminals: set[str] = set()
    for posture in mission.get("postures") or []:
        if not isinstance(posture, dict):
            continue
        pid = str(posture.get("id"))
        where = f"mission.yaml:posture {pid}"
        permitted = posture.get("hazardous_actions_permitted")
        if not isinstance(permitted, bool):
            report.refuse(
                f"{where}.hazardous_actions_permitted",
                f"is {permitted!r}, not a boolean. This says whether a fleet may act from this "
                "posture at all, and a value that merely reads like a yes is a permission nothing "
                "can evaluate",
            )
            continue
        if posture.get("terminal") is True:
            terminals.add(pid)
            if permitted:
                report.refuse(
                    f"{where}.hazardous_actions_permitted",
                    "is true on a terminal posture. `mission_diode.md`:1727 lists *no hazardous verb "
                    "is enabled in ABORTED* among the properties to verify statically: a mission "
                    "that is over is not one a fleet may still act from",
                )
        elif permitted:
            permitting.add(pid)
    if postures and not permitting:
        report.refuse(
            "mission.yaml:postures",
            "permits hazardous actions in no posture, so the machine can authorize nothing and the "
            "mission it exists to run cannot be run",
        )

    # And the two things a transition is for, neither of which was read either: where it *arrives*
    # decides whether the latch has to be re-read, and a posture with no outbound row is a mode
    # whose guard reads nothing.
    sources: set[str] = set()
    for row in mission.get("transition_evidence") or []:
        if not isinstance(row, dict):
            continue
        text = str(row.get("transition") or "")
        if "->" not in text:
            continue
        source, _, target = text.partition("->")
        source, target = source.strip(), target.strip()
        head = target.split()[0] if target.split() else ""
        sources.add(source)
        # Invariant E, vehicle-side, and the one place it can be held: TV-E refuses any hazardous
        # request with the abort latch set, so every transition *into* a posture that permits
        # hazardous actions is a last chance to read the latch. `prepare -> execute` does.
        if head in permitting:
            requires = row.get("requires")
            if isinstance(requires, dict) and "mission.abort_latched" not in requires:
                report.refuse(
                    f"mission.yaml:transition {text!r}",
                    f"arrives at {head!r}, which permits hazardous actions, and requires no "
                    "`mission.abort_latched`. Invariant E: a latched abort dominates every hazardous "
                    "effect, and this transition is the last place the latch can be read before one",
                )
    for pid in postures:
        if pid in terminals or pid in sources:
            continue
        report.refuse(
            f"mission.yaml:posture {pid}",
            "is the source of no transition in `transition_evidence`, so the guard that leaves it "
            "declares no telemetry dependency. `mission_diode.md`:1720-1729 puts *every guard "
            "declares its telemetry dependencies* in the list a startup verifies statically, and "
            "`any -> aborting` is not a way out of a posture: it is the abort path, which every "
            "posture has and which is deliberately evidence-free",
        )

    # The objectives are the score. A challenge whose scoring is unvalidated is a challenge whose
    # result nobody can defend.
    objectives = [
        str(o.get("id"))
        for o in mission.get("objectives") or []
        if isinstance(o, dict) and o.get("id")
    ]
    repeated = sorted({name for name in objectives if objectives.count(name) > 1})
    if repeated:
        report.refuse(
            "mission.yaml:objectives",
            f"declares {repeated} more than once. Two objectives under one id is one of them "
            "unscored and the other counted twice",
        )
    for objective in mission.get("objectives") or []:
        if not isinstance(objective, dict):
            continue
        where = f"mission.yaml:objective {objective.get('id')}"
        if objective.get("kind") not in OBJECTIVE_KINDS:
            report.refuse(
                f"{where}.kind",
                f"is {objective.get('kind')!r}, not one of {sorted(OBJECTIVE_KINDS)}. The kind says "
                "what sort of claim the objective makes, and a value outside the set is a claim "
                "nothing can score",
            )
        # `channels` may be empty, and only when `evaluated_by` says the vehicle contributes
        # nothing. `coordination` is the case: twenty metrics "computed externally and never shown
        # to the fleet", so it declares no channels at all — and the field that makes that
        # legitimate is `evaluated_by: external`, which is already a declaration about who settles
        # it. For `vehicle` and `far_side` an empty list is a scoring rule with no observation
        # behind it, and `far_side` is not an exemption: the corpus says plainly that what the
        # vehicle owes the operator's verdict is *the record*.
        if not objective.get("channels") and objective.get("evaluated_by") != "external":
            report.refuse(
                f"{where}.channels",
                "is empty and `evaluated_by` is not `external`. An objective is settled from "
                "somewhere — the record, the operator, or neither — and a list with nothing in it "
                "is a claim that nothing observes it while `evaluated_by` claims otherwise",
            )
        # A margin is a quantity judged against a bound, so which direction is *good* is the whole
        # of what the objective adds to the channel it names.
        if objective.get("kind") == "margin" and objective.get("sense") not in OBJECTIVE_SENSES:
            report.refuse(
                f"{where}.sense",
                f"is {objective.get('sense')!r}, not one of {sorted(OBJECTIVE_SENSES)}. A margin "
                "without a direction is a number whose good end nobody wrote down",
            )


def check_gnc_substepping(root: Path, mission: dict[str, Any], report: Report) -> None:
    """The filter's rates are a claim about the *plant's* clock, and the two must agree.

    `gnc/estimator#sub_stepping` declares three rates and a note that reads like a design decision
    — "the major cycle is a sub-step inside the 50 Hz tick through the plant's integer-microsecond
    event queue, **not a second plant rate (C-07)**". The 50 Hz is the vehicle's tick, and the
    vehicle's tick is declared once, in `mission.yaml#met_epoch_provenance.tick_hz`, where the
    34,560,000-tick figure is derived from it.

    So this is a cross-file equality that nothing compared, in the direction that matters: change
    the mission's tick rate and the filter's sub-stepping silently becomes a claim about a clock
    the vehicle no longer has. The other two rates are checked against the tick as divisibility
    rather than equality, because a sub-step has to land on tick boundaries — a 100 Hz major cycle
    inside a 50 Hz tick is two sub-steps, and a 10 Hz guidance update is one every five ticks, and
    either one failing to divide would mean a sub-step that straddles a tick.
    """
    estimator = load(root / "domains" / "gnc" / "components.yaml", Report()) or {}
    sub = (estimator.get("estimator") or {}).get("sub_stepping")
    if not isinstance(sub, dict):
        return
    tick = (mission or {}).get("met_epoch_provenance", {}).get("tick_hz")
    if not isinstance(tick, (int, float)) or not tick:
        return
    where = "domains/gnc/components.yaml:estimator.sub_stepping"
    declared = sub.get("plant_tick_hz")
    if declared != tick:
        report.refuse(
            f"{where}.plant_tick_hz",
            f"is {declared!r} and `mission.yaml#met_epoch_provenance.tick_hz` is {tick:g}. The "
            "block's own note says the major cycle is a sub-step *inside the plant tick* rather "
            "than a second plant rate, so a tick rate here that is not the mission's is a claim "
            "about a clock the vehicle does not have",
        )
    for field in ("major_cycle_hz", "guidance_update_hz"):
        rate = sub.get(field)
        if not isinstance(rate, (int, float)) or not rate:
            continue
        if rate < tick and abs(tick / rate - round(tick / rate)) > 1e-9:
            report.refuse(
                f"{where}.{field}",
                f"is {rate:g} Hz against a {tick:g} Hz tick, which is not a whole number of ticks "
                "per update. A rate that does not divide the tick is an update that straddles one",
            )
        if rate > tick and abs(rate / tick - round(rate / tick)) > 1e-9:
            report.refuse(
                f"{where}.{field}",
                f"is {rate:g} Hz against a {tick:g} Hz tick, which is not a whole number of "
                "sub-steps. A sub-step that does not divide the tick lands mid-tick, and the "
                "plant's event queue is integer microseconds for exactly that reason",
            )


def check_thermal_budget(root: Path, report: Report) -> None:
    """The rejection total is a closure, and nothing summed the two things it closes over.

    `domains/thermal/components.yaml#load_budget.total_rejection_capacity_w` states 4,933 W and its
    own relation says what that is: "2,588 W of radiator plus 2,345 W of evaporator". Both of those
    live in the same file — the radiator's in `radiator_model.csm.rejection_w` and the evaporator's
    in the `evaporator_csm` component — and **no tool read the block at all.** A deletion test that
    removed all sixteen lines and diffed every tool's output found nothing.

    That is the mass closure's shape for the fifth time, and it is worth wiring for the reason the
    other four were: a total nobody sums is a total that drifts, and this one is the denominator a
    fleet reasons about when it decides whether the vehicle can reject what it is generating. The
    evaporator's LM twin is `UNCONFIGURED`, so the sum is taken over the parts that have values and
    the missing one is reported as the debt it already is rather than silently omitted.
    """
    thermal = load(root / "domains" / "thermal" / "components.yaml", report) or {}
    budget = thermal.get("load_budget") or {}
    total = budget.get("total_rejection_capacity_w")
    if not isinstance(total, (int, float)):
        return
    parts: dict[str, float] = {}
    radiator = ((thermal.get("radiator_model") or {}).get("csm") or {}).get("rejection_w")
    if isinstance(radiator, (int, float)):
        parts["radiator_model.csm"] = float(radiator)
    for component in thermal.get("components") or []:
        if not isinstance(component, dict):
            continue
        value = component.get("rejection_w")
        if isinstance(value, (int, float)):
            parts[str(component.get("id"))] = float(value)
    if not parts:
        report.refuse(
            "domains/thermal/components.yaml:load_budget",
            "states a total rejection capacity and nothing declares a part of it, so the total is "
            "a number with no arithmetic behind it",
        )
        return
    stated = sum(parts.values())
    if abs(stated - float(total)) > 1.0:
        report.refuse(
            "domains/thermal/components.yaml:load_budget.total_rejection_capacity_w",
            f"is {total:g} W and the parts this file declares sum to {stated:g} W "
            f"({', '.join(f'{k} {v:g}' for k, v in sorted(parts.items()))}). A total whose parts "
            "do not reach it is a capacity the vehicle does not have",
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

    **And until this round it found all four of its inputs by convention**, which is the defect it
    exists to catch arriving at the check itself. The loop came from a hand-written map, the heat
    state from `f"cabin_heat_{zone.split('_')[0]}_w"`, the cabin's temperature state from a scan for
    a node named `cabin_zone_t` or `lm_cabin_zone_t`, and the equilibrium state from
    `f"cabin_eq_{zone.split('_')[0]}_k"` — and every lookup ended in a `continue` or an
    `if ... is not None`. So **renaming `cabin_heat_csm_w` composed**: the check that makes a
    swapped supply visible went quiet and said nothing, and so did a rename of any of the other
    three. The zone declares the four links now, a link that does not resolve is refused, and the
    two tests that encode the pairing read the declaration rather than repeating it.
    """
    thermal = load(root / "domains" / "thermal" / "components.yaml", report) or {}
    eclss = load(root / "domains" / "eclss" / "components.yaml", report) or {}
    if not thermal or not vehicle:
        return
    # Which compartments the atmosphere model is stated for, which is the declared link between a
    # vehicle and its cabin zone: `check_cabin_volumes` already uses it, and it is keyed by vehicle
    # while the zones are named for it.
    cabins = (eclss.get("atmosphere_model") or {}).get("volume_m3")
    if not isinstance(cabins, dict) or not cabins:
        return
    zones = {str(z.get("id")): z for z in (vehicle.get("thermal") or {}).get("zones") or []}
    loops = {str(x.get("id")): x for x in (vehicle.get("thermal") or {}).get("loops") or []}
    states = {str(s.get("id")): s for s in thermal.get("state") or [] if isinstance(s, dict)}

    for which in sorted(cabins):
        zone_id = f"{which}_cabin"
        where = f"vehicle.yaml#thermal.zones.{zone_id}"
        zone = zones.get(zone_id)
        if not isinstance(zone, dict):
            report.refuse(
                where,
                "is the compartment `atmosphere_model.volume_m3` states the ideal-gas law for, and "
                "no zone in this file carries it, so nothing holds its equilibrium anywhere",
            )
            continue
        loop = loops.get(str(zone.get("cooled_by")))
        if loop is None:
            report.refuse(
                f"{where}.cooled_by",
                f"names {zone.get('cooled_by')!r}, which is not a loop vehicle.yaml#thermal.loops "
                f"declares; it declares {sorted(loops)}. The cabin's equilibrium is its coolant "
                "supply plus its own heat rise, so a cabin with no loop has no supply temperature",
            )
            continue

        # The three states, resolved rather than assumed. Every one of these was a `continue`
        # before, which is how a rename turned the whole check off.
        resolved: dict[str, dict[str, Any]] = {}
        unresolved = False
        for field in ("temperature_state", "heat_state", "equilibrium_state"):
            name = zone.get(field)
            state = states.get(str(name))
            if state is None:
                report.refuse(
                    f"{where}.{field}",
                    f"names {name!r}, which is not a state in domains/thermal/components.yaml; it "
                    f"declares {sorted(states)}. This check used to find this one by convention and "
                    "skip in silence when it could not",
                )
                unresolved = True
                continue
            resolved[field] = state
        if unresolved:
            continue
        cabin = resolved["temperature_state"]
        heat = resolved["heat_state"]
        eq_state = resolved["equilibrium_state"]

        supply = loop.get("supply_c")
        if not isinstance(supply, (int, float)):
            report.refuse(
                f"vehicle.yaml#thermal.loops.{loop.get('id')}",
                f"serves {zone_id} and states no single `supply_c` figure. A band here is the "
                "evaporator outlet's range rather than the mixed supply the cabin sees, and the "
                "difference is several kelvin of cabin temperature",
            )
            continue
        conductance = cabin.get("conductance_w_per_k")
        if not isinstance(conductance, (int, float)) or conductance == 0:
            report.refuse(
                f"domains/thermal/components.yaml:state {cabin.get('id')}",
                f"carries no numeric `conductance_w_per_k`, and {zone_id} names it as the state it "
                "is modelled by. The cabin's rise above its supply is Q/G, so a conductance that is "
                "absent is a rise that cannot be computed",
            )
            continue
        # `total_w` was read as `float(heat.get('total_w') or 0)`, which is zero for an unset value
        # and a `ValueError` for the string `UNCONFIGURED` — so the one input this check most
        # depends on was the one that would either vanish or crash it.
        total_w = heat.get("total_w")
        if not isinstance(total_w, (int, float)):
            report.refuse(
                f"domains/thermal/components.yaml:state {heat.get('id')}",
                f"declares {total_w!r} as its `total_w`, and {zone_id} names it as the heat this "
                "compartment's equipment puts in. The equilibrium is `supply + Q/G`, so a heat rate "
                "that is not a number is a cabin whose temperature cannot be checked",
            )
            continue
        equilibrium_c = float(supply) + float(total_w) / float(conductance)

        # The state that carries this figure is re-derived too, in kelvin. Two declarations of one
        # quantity is the shape that drifts, and here it would drift in the quiet direction: the
        # cabin would relax toward a stale equilibrium while the loads and the supply moved on.
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
        bands = zone.get("limit_c") or [None, None]
        low, high = (bands + [None, None])[:2]
        if isinstance(low, (int, float)) and equilibrium_c < low:
            report.refuse(
                f"vehicle.yaml#thermal.zones.{zone_id}",
                f"has a {low} C floor and its equipment's {total_w} W over a "
                f"{conductance} W/K conductance puts it at {equilibrium_c:.2f} C on a "
                f"{supply} C supply. The vehicle would trip its own cabin-low alarm in a nominal "
                "mission, which means one of the four declarations is wrong",
            )
        if isinstance(high, (int, float)) and equilibrium_c > high:
            report.refuse(
                f"vehicle.yaml#thermal.zones.{zone_id}",
                f"has a {high} C ceiling and its equilibrium at {equilibrium_c:.2f} C is above it",
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
            # `chemistry` is the second shared field and it was the one outside this loop, which is
            # the shape `check_thermal_bindings` was fixed for one round ago: a field two views of
            # one machine both carry, that nothing compares. It matters more than the Ah does.
            # `electrical_diode.md:124` gives chemistry its own row in the table of what changes the
            # model — "voltage, temperature, charge limits, SOC model" — so it is not a label, it is
            # the derating curve, and `consumables_diode.md:145` leaves it UNSPECIFIED with the
            # usable-energy model "supplied externally". Two files disagreeing about it is two
            # batteries wearing one name.
            if "chemistry" in battery and unit.get("chemistry") != battery["chemistry"]:
                report.refuse(
                    bwhere,
                    f"declares {battery['chemistry']!r} and {unit.get('id')!r} is built as "
                    f"{unit.get('chemistry')!r}. Chemistry is what sets the discharge curve and the "
                    "usable-energy derating, so a group and its units disagreeing is a battery "
                    "sized against one electrochemistry and flown on another",
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

        # And the field is declared for one group of the vehicle's three. The two LM groups carry
        # neither, in either file — and the specification is explicit that this is a gap rather than
        # a silence: `electrical_diode.md:18` lists battery chemistry among the quantities that are
        # unspecified and "must be resolved before a flight design is frozen", and `:124` names a
        # reference default of Li-ion which this vehicle deliberately does not follow, because the
        # cells Apollo flew were silver-zinc. The choice is right and the departure from the
        # published default is recorded nowhere, which is the part worth a debt.
        if not battery.get("chemistry"):
            report.debt(
                bwhere,
                "declares no `chemistry`, while `csm_entry` declares one in both files. It is not a "
                "silence the spec permits — `electrical_diode.md:124` gives chemistry its own row "
                "for what it changes, and `consumables_diode.md:145` leaves the usable-energy model "
                "it drives UNSPECIFIED. Owed: the LM cells' electrochemistry, or a note saying the "
                "reference Li-ion default is declined here for the same reason it is on the CSM",
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
        # The prose walk joins the scalar walk over the same documents. A note is not a value
        # and cannot be counted as a debt, but it can be *empty*, and the two failures live in
        # the same fields: `walk_unset` reads `basis`, `check_pointer_notes` reads the `note`
        # beside it.
        check_pointer_notes(((name, doc),), report)

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
    all_faults: dict[str, list[Any]] = {}
    if (root / "domains").is_dir():
        for path in sorted(p for p in (root / "domains").iterdir() if p.is_dir()):
            profiles = load(path / "profiles.yaml", Report()) or {}
            for threshold in profiles.get("thresholds") or []:
                if threshold.get("id"):
                    threshold_ids.add(str(threshold["id"]))
            all_commands[path.name] = load(path / "commands.yaml", Report()) or {}
            policy = load(path / "fault_policy.yaml", Report()) or {}
            all_faults[path.name] = policy.get("faults") or []
    check_profile_immutability(all_commands, threshold_ids, report)
    # The vehicle-wide half of a claim each domain already makes for itself. It needs every
    # domain's policy at once, which is why it is here rather than inside `check_domains`.
    check_layer_coverage(channels, all_faults, report)
    check_domains(
        root,
        registry,
        coupling,
        channels,
        report,
        declared_phases={
            str(phase.get("id"))
            for phase in (mission or {}).get("phases") or []
            if isinstance(phase, dict) and phase.get("id")
        },
    )
    # `vehicle.yaml` is the file every cross-file check joins against, so when it cannot be
    # loaded the answer is not to run them with a hole in the middle of the argument list — it is
    # to say once that the joins are unavailable and carry on with the checks that do not need it.
    # The alternative was measured: the file was made unparseable and the linter died with
    # `AttributeError: 'NoneType' object has no attribute 'get'` **from inside a check**, losing
    # the report that named the parse error. A linter that crashes on the input it exists to
    # diagnose is worse than one that misses a fault, because the operator sees a traceback and
    # reasonably concludes the tool is broken rather than the definition.
    # Built here rather than below, because `check_initial_sources` resolves a stock's initial
    # against any document now and this is the one place they are all loaded.
    documents = load_documents(root, vehicle, coupling, mission, channels)
    if vehicle is not None:
        check_vehicle(vehicle, report)
        check_electrical_bindings(root, vehicle, report)
        check_thermal_bindings(root, vehicle, report)
        check_comms_bindings(root, vehicle, report)
        check_antenna_patterns(root, vehicle, report)
        check_one_way_configurations(root, vehicle, report)
        check_initial_sources(root, vehicle, coupling, report, documents)
    else:
        report.refuse(
            "vehicle.yaml",
            "could not be loaded, so every check that joins another file against it — the "
            "electrical and thermal inventories, the comms hardware, the phase-to-configuration "
            "names, the crew placements and the propulsion budgets — is unavailable for this run",
        )
    check_power_inventory(root, report)
    check_thermal_heat_inputs(root, report)
    check_gnc_substepping(root, mission, report)
    check_mission_model(mission or {}, report)
    check_threshold_derivations(root, documents, report)
    check_edge_derivations(coupling or {}, documents, report)
    check_domain_reads(documents, report)
    check_consumers(documents, report)
    check_argument_vocabularies(documents, report)
    check_spacecraft_vocabulary(documents, report)
    check_presentation_references(
        root, presentation or {}, coupling or {}, mission or {}, report, channels
    )
    check_thermal_budget(root, report)
    check_cabin_volumes(root, vehicle, report)
    check_cabin_equilibrium(root, vehicle, report)
    check_metabolic_rules(root, vehicle, mission, report)
    if mission is not None:
        if vehicle is not None:
            check_mission(mission, vehicle, report)
            check_propulsion(mission, vehicle, report)
            check_propulsion_bindings(root, vehicle, report)
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
    check_cabin_pairing(registry, presentation, report, root)
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
