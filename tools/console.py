#!/usr/bin/env python3
"""The vehicle's side of the frozen window: a reference console loop.

`docs/diode-contract.md` is the only thing this repository and the vehicle share, and until now
the vehicle's side of it was *declared* and not *implemented*. `presentation.yaml#conformance`
claims which of §9's twelve checks the vehicle satisfies by configuration and which belong to the
far side; `tools/plant.py --state` emits one `state.json`; `tools/generate_help.py` prints `HELP.md`
to stdout. Nothing claimed the console, wrote a result file, or advanced telemetry without being
asked — so `contract/diode_probe.py`, which is the instrument for exactly this, had never been run
against the vehicle at all.

This is that loop, and it is deliberately the smallest thing that can be probed:

  - it knows **no physics**. The probe says of itself "it deliberately does not test physics,
    mission structure, or whether the verbs are any good. Those are judgements about a vehicle.
    This only answers: does the window behave the way the world is built to expect?" This loop is
    the same shape: it answers the window and refuses the rest by name.
  - it knows **no verbs of its own**. The vocabulary comes from `domains/*/commands.yaml`, which is
    the registry the linter already refuses to let drift.
  - what it refuses to do, it refuses *in a result file*, because the contract is explicit that
    "a refusal is a result. Unavailable verb, allowance exhausted, bad argument, internal error:
    one file each, same shape. The agent must read the content; the shape tells it nothing."

What it does not do is fly. A command that an available verb names is *accepted* and reported as
accepted; the physics that would follow is `plant.md`'s, and `tools/plant.py` is where that stops
at the first thing it cannot compute. Accepting a command and simulating its consequence are
different claims, and conflating them here would hide which one this file is making.

    python3 tools/console.py --diode-dir .scratch/diode --slug vehicle --init
    python3 tools/console.py --diode-dir .scratch/diode --slug vehicle --cycles 60 --poll 1
    python3 contract/diode_probe.py --diode-dir .scratch/diode --slug vehicle --poll-seconds 1
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from faults import load_faults, load_postures, scenario_report  # noqa: E402
from generate_help import generate as generate_help  # noqa: E402
from generate_readme import generate as generate_readme  # noqa: E402
from plant import (  # noqa: E402
    Unconfigured,
    World,
    apply_command,
    capability_snapshot,
    command_dwell,
    gate_instantiations,
    initial_values,
    load_world,
)

# The contract's own bound on a read of an agent-writable file. The operator-side services use
# `MAX_READ_BYTES` for the same reason and the same number is not a coincidence: an ingress file
# is written by something this process does not control.
MAX_READ_BYTES = 1_000_000

# **The three values a run remembers and a caller may also name, and the defaults are `None`.**
# `--scenario`, `--seed` and `--ring-slots` are each written into `pending.json` and read back on
# the next process — which makes each of them two things at once: a thing the vehicle remembers and
# a thing a caller can say. Those two are only distinguishable if *the caller named nothing* has a
# representation of its own, and a parser whose `--scenario` defaults to `"nominal"` does not have
# one: `--scenario nominal` and no flag at all are the same `args.scenario`, so the restore cannot
# know which it is looking at and a resumed run ignored every one of the three. Round 55's own
# comment promised the opposite in as many words — *"a resumed console keeps its scenario unless
# the caller names another"* — and nothing could have kept that promise.
#
# So the flag defaults are `None`, `None` means **the caller named nothing**, and the resolution
# happens once, in `main`, where the window's record and the mission's declared postures are both
# in hand. The declared defaults live here instead, and they are what an unnamed flag resolves to on
# a window that has no record — a fresh one.
DEFAULT_SCENARIO = "nominal"
DEFAULT_SEED = 0
DEFAULT_RING_SLOTS = 300


def recorded_run(root: Path) -> dict[str, Any]:
    """The run's own record of itself — `pending.json` — or an empty map where there is none.

    `pending.json` is the one file in the window that is read back as input; `state.json` is
    published state and the contract is explicit that editing it changes nothing. Everything a
    restart has to remember lives here, which is why the resolution of a *named* flag against a
    *remembered* one has to read it before the console is constructed rather than inside it: the
    console can restore a value, but it cannot see the flag that lost to it.
    """
    record = read_json_bounded(root / "pending.json")
    return record if isinstance(record, dict) else {}


def resolve_remembered(named: Any, recorded: Any, default: Any) -> Any:
    """The caller's value if the caller named one, else the record's, else the declared default.

    **The first branch is the whole fix.** `named is not None` is a question the parser could not
    be asked while the defaults were the same values a caller could type, and it is the question
    the round turned on. Written out three times rather than once it would also be three chances to
    reach for `or` — and `args.seed or recorded_seed or 0` looks right and is wrong in the one case
    a seed exists to be: a caller who names `0` means `0`, and `or` reads that as *named nothing*.
    "Named nothing" has exactly one representation and it is `None`; falsiness is a different
    question and this is not it.
    """
    if named is not None:
        return named
    if recorded is not None:
        return recorded
    return default

# The result filename's bound, in *bytes*, from `docs/diode-contract.md:96-99`: "the whole
# truncated at 160 bytes, so no separator, traversal sequence, or partial multi-byte character can
# reach the filesystem." Slicing a Python `str` at 160 characters is not that bound, and a
# multi-byte verb argument would produce a filename with a broken character in it — which is the
# failure the contract's phrasing exists to prevent.
FILENAME_LIMIT_BYTES = 160


def utc_now() -> datetime:
    return datetime.now(UTC)


def stamp(moment: datetime) -> str:
    """The contract's stamp: UTC with microseconds, so the ordering is total."""
    return moment.strftime("%Y%m%dT%H%M%S_%fZ")


def sanitise(command: str) -> str:
    """Every non-alphanumeric character to an underscore, truncated at 160 *bytes*.

    Truncating on the encoded bytes rather than on characters is the whole point: the contract
    asks for no partial multi-byte character, and `text[:160]` gives exactly that for any argument
    that is not ASCII.
    """
    replaced = re.sub(r"[^A-Za-z0-9]", "_", command)
    encoded = replaced.encode("utf-8")
    if len(encoded) <= FILENAME_LIMIT_BYTES:
        return replaced
    return encoded[:FILENAME_LIMIT_BYTES].decode("utf-8", "ignore")


def write_text_atomic(path: Path, text: str) -> None:
    """Write through a temporary file and rename, the way the contract requires of the claim.

    A reader that opens the path sees either the old file or the new one. `os.replace` is atomic
    within a filesystem, which is the reason the temporary lives beside the target rather than in
    a temporary directory.
    """
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def write_json_atomic(path: Path, payload: Any) -> None:
    write_text_atomic(path, json.dumps(payload, indent=2, sort_keys=False) + "\n")


def read_json_bounded(path: Path) -> dict[str, Any] | None:
    """The file's contents, or `None` if it is absent, unreadable, oversized or not an object.

    `None` and `{}` are different answers and the caller treats them differently: an absent console
    is a directory that is not ready, while an empty object is a console with nothing in it. A
    half-written file — which the contract explicitly permits an agent to produce — lands in the
    same bucket as a malformed one and is refused with a reason rather than silently skipped.
    """
    try:
        if not path.exists() or path.stat().st_size > MAX_READ_BYTES:
            return None
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


class Console:
    """One `<slug>` directory, driven a cycle at a time."""

    def __init__(
        self,
        world: World,
        root: Path,
        slug: str,
        *,
        phase: str,
        tripped_interlocks: set[str] | None = None,
        scenario: str = DEFAULT_SCENARIO,
        seed: int = DEFAULT_SEED,
        ring_slots: int = DEFAULT_RING_SLOTS,
    ) -> None:
        self.world = world
        # **The ring's slot count, which the contract demands and nothing bounded.**
        # `docs/diode-contract.md:183-189`: "**Publish a ring, not a snapshot.** An agent that was
        # blocked inside one long conversation turn wakes up blind if all it has is the latest frame
        # ... A bonded ring of recent frames with a **fixed slot count** is self-describing about its
        # own cadence and its own losses — and it teaches the agents to reason about missed frames,
        # which is the correct epistemology for telemetry."
        #
        # `presentation.yaml#ring` declares the cadence *structure* and deliberately declines to set
        # the count — "a slot count is a memory decision" — and `write_frame` wrote `NNN.json` and
        # never removed one, so the ring was unbounded and nothing accounted for a loss. The bound is
        # the runner's, which is where the plan says it belongs: the vehicle declares the structure,
        # the run declares the memory it is willing to spend on it.
        self.ring_slots = max(1, int(ring_slots))
        # **Which scenario this run is, and it is two fields rather than a story.** Criterion 3
        # asks for a runner that "plays nominal, degraded and crisis", and `mission.yaml` already
        # declares the three: a posture is a row of `apollo_diode.md:370-374`'s difficulty scaling
        # (hazard ×1/×5/×10, demand failure ×1/×5/×20) plus a selector over which faults are
        # *placed* rather than left to chance. `tools/faults.py` has consumed both since the round
        # that found the difficulty knob doing nothing; what no tool had was a run's identity — so
        # two crisis runs were indistinguishable in the record and neither could be replayed.
        #
        # The pair is recorded rather than narrated, on purpose. `mission.yaml`'s own debt says it:
        # *"the pool is a set of faults, the guaranteed seed is drawn from it, and the chain that
        # results is whichever one that fault realises — so a `crisis` run is a crisis, and not
        # necessarily the crisis a chain names."* Naming a chain here would be answering a question
        # the vehicle deliberately leaves to the experiment, so this file records the two things
        # that make a run reproducible and nothing more.
        self.scenario = scenario
        self.seed = int(seed)
        # The value map the frames carry, seeded once from the stocks' declared initial
        # conditions. It lives on the console rather than being rebuilt per frame because it is
        # the thing a tick will advance: `step()` takes it and returns it committed, so when the
        # plant can advance a state this is where the new value lands.
        self.values: dict[str, Any] = initial_values(world)
        self.root = root
        self.slug = slug
        self.phase = phase
        # Live interlock state, and the default is *nothing tripped* rather than *everything
        # tripped*. A declared interlock is a guard evaluated when a command arrives (D-03's
        # service-owned half), not a standing condition; reading the declaration as a tripped
        # guard made 32 of this vehicle's 58 verbs refuse every command, which the console would
        # have reported as "closed" — a lie about a verb that would have run.
        self.tripped = set(tripped_interlocks or ())
        self.console = root / "console.json"
        self.output = root / "output"
        self.telemetry = root / "telemetry"
        self.state = root / "state.json"
        self.help_file = root / "HELP.md"
        # **The sixth file, and the one nothing wrote.** `docs/diode-contract.md:31-35` lists it as
        # `vehicle -> agent  (the protocol, in the vehicle's words)`, and the console made five of
        # the six: `console.json`, `state.json`, `HELP.md`, `telemetry/` and `output/`. A window with
        # no README is a protocol a fleet has to infer from `HELP.md`'s verb list, which answers
        # *what may I ask for* and not *how does this work*.
        self.readme = root / "README.md"
        self.pending = root / "pending.json"
        self.variables: dict[str, Any] = {}
        self.ticks = 0
        # When each commanded state last changed, and what it left.
        #
        # **The first version kept this in memory, and that made the guard fire never.** The
        # console is a process per invocation, so a fleet's two commands arrive in two processes
        # and the second found an empty clock — which is the same defect `pending.json` had before
        # round 45: a record that does not survive the process boundary cannot be a guard across
        # one, and every real command is across one. It is durable for the reason the counters and
        # the deferral queue are, and it lives in the vehicle's own record rather than in
        # `state.json`, which the contract says is never read back as input.
        self.dwell: dict[str, dict[str, Any]] = {}
        self.seq = 0
        self.boot_id = f"{int(time.time()):032x}"[-32:]
        self.started = utc_now()
        self.accepted: list[dict[str, Any]] = []
        self.deferred: list[dict[str, Any]] = []
        # Conflict domains already claimed *this tick*, mapping the domain to the verb that won it.
        # `apollo_diode.md:576-578` is the policy and it is one sentence: "Each command declares or
        # is statically assigned a `conflict_domain`... Within one conflict domain, **first valid
        # command received wins for that tick**", and `:579` adds the half that makes it a policy
        # rather than a race: "Later commands are not silently discarded: they receive
        # `CONFLICT_SUPERSEDED`." V-02 lists that as a first-class refusal and explicitly rejects
        # "silent discard (`apollo:579` is explicit that the loser must be told)". The 58
        # `conflict_domain` declarations in `domains/*/commands.yaml` reached nobody until this
        # dict existed: every command was accepted, so two agents commanding one actuator in one
        # tick both succeeded and the physics that followed was undefined.
        self.claimed: dict[str, str] = {}
        # Outstanding arm tokens, by event. `arm_event`'s own help fixes the contract: "Arming does
        # nothing physical: it returns a short-lived token bound to this specific event, and it is
        # the only verb on the vehicle whose result carries a [token]." Nothing issued one and
        # nothing checked one, so `execute_event` — which fires the pyro, the undocking and the
        # staging, the vehicle's irreversible events — accepted any `arm_token` at all, including
        # none. `requires_arm` was enforced on the *state* by the linter and on no *path*, which
        # left F-15 (premature staging) reachable by a single unarmed command.
        self.arms: dict[str, str] = {}

    # -- setup ------------------------------------------------------------------------------
    def initialise(self) -> None:
        """Create the directory the probe insists on finding before it will run.

        It refuses with exit 2 if there is no `console.json` at all, and notes rather than refuses
        if `output/` is missing — "results will have nowhere to land". Both are created here, along
        with `telemetry/`, because a probe that has to create the window it is testing is a probe
        testing itself.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        self.output.mkdir(exist_ok=True)
        self.telemetry.mkdir(exist_ok=True)
        # **Generated once, at boot, and cached — because the configuration does not change while a
        # console runs, and the file is 7 kB.** The first version called the generator from
        # `publish()`, so every cycle re-derived a document that could not have moved: 20 ms a cycle
        # at one frame per cycle, which is 2 % of a tick and enough to make the probe's own mirror
        # check flaky — that check doctors `state.json` and waits `1.5 x poll_seconds` for a
        # republish, and a console slower than the probe's patience *is* a console that looks like it
        # reads its own mirror back. The two files below are the configuration's, not the tick's.
        self.readme_text = generate_readme(self.world.root)
        self.help_cache: str | None = None
        write_text_atomic(self.readme, self.readme_text)
        if not self.pending.exists():
            write_json_atomic(self.pending, {"pending": []})
        # **`console.json` is written last, and that ordering is the whole content of this
        # method.** It is the file a reader treats as "the vehicle is here" — the contract's own
        # probe refuses with exit 2 if it is absent, and a fleet watches for it — so a window that
        # has the console and not yet the mirror is a vehicle that appears to publish nothing.
        # That is not hypothetical: this method used to create the console first and publish on
        # its first cycle, and a probe attaching in the gap reported "HELP.md and state.json named
        # none" for a vehicle that was about to name 228 gates. Publishing first and announcing
        # second costs one cycle of latency at boot and removes the gap entirely.
        # **The queue is loaded, not restarted.** `pending.json` is "the vehicle's own deferral
        # queue" and this file wrote it every cycle and read it never — so a deferral could not
        # survive a process boundary and, since the console is a process per invocation, could
        # never settle at all. It is the same shape as the fields the last four rounds turned up: a
        # file produced and consumed by nobody. Restoring it here is what makes the deferred path
        # real rather than a record of an intention.
        restored = read_json_bounded(self.pending)
        if isinstance(restored, dict):
            entries = restored.get("pending")
            if isinstance(entries, list):
                self.deferred = [e for e in entries if isinstance(e, dict)]
            # **The vehicle's own counters, and they are not the mirror.** A restart that reset
            # `ticks` to zero made `due_tick` meaningless — a deferral accepted at tick 0 and due
            # at tick 1 could never come due, because the next process was also at tick 0 — and it
            # restarted `seq`, so a second run's frames overwrote the first run's in the ring.
            # `state.json` is published state and the contract is explicit that it is never read
            # back as input ("editing it changes nothing"), so the counters live here instead:
            # `pending.json` is the vehicle's own record, `vehicle -> vehicle`, which is exactly
            # what a counter that must survive a restart is.
            if isinstance(restored.get("arms"), dict):
                self.arms = {str(k): str(v) for k, v in restored["arms"].items()}
            # The dwell's clock, restored for the reason the counters are: a guard that forgets
            # across a restart is a guard that never fires.
            if isinstance(restored.get("dwell"), dict):
                self.dwell = {
                    str(k): v for k, v in restored["dwell"].items() if isinstance(v, dict)
                }
            for key in ("ticks", "seq"):
                value = restored.get(key)
                if isinstance(value, int) and value >= 0:
                    setattr(self, key, value)
            # **The run's identity and the ring's bound are resolved in `main`, not restored here.**
            # They are the three values a caller can also name, and the console cannot tell a flag
            # that named a value from a flag left at its default — `args.scenario` was `"nominal"`
            # whichever way the caller meant it. What `main` passes in is therefore already the
            # answer: the caller's if the caller named one, the record's if not, the declared
            # default on a window with no record. A second restore here would be a second answer,
            # and it would be the wrong one — it is what silently overrode `--scenario crisis` on a
            # window recorded as `degraded`, and what made `--seed` and `--ring-slots` no-ops too.
            #
            # `boot_id` is restored below and is not a flag, so it has no such ambiguity.
            if isinstance(restored.get("boot_id"), str) and restored["boot_id"]:
                self.boot_id = restored["boot_id"]
        self.publish()
        if not self.console.exists():
            write_json_atomic(self.console, {"commands": [], "variables": {}})

    # -- the claim --------------------------------------------------------------------------
    def claim(self) -> list[Any] | str:
        """Read the console and clear it, *before* acting on anything.

        The contract is unambiguous about the order: "**Intake is destructive and atomic.** Each
        cycle the vehicle reads the file, then rewrites it with `commands` emptied and `variables`
        preserved... **Clear before you act.** The claim happens before any command runs. A crash
        mid-batch loses the rest of that batch; it never replays it." So the rewrite happens here
        and the commands are returned for the caller to run afterwards, and the two are not
        combined into one function for the reason the contract gives.
        """
        payload = read_json_bounded(self.console)
        if payload is None:
            return "the console is not readable"
        commands = payload.get("commands")
        variables = payload.get("variables")
        # `variables` is persistent and the vehicle "never clears it". It is also the probe's
        # marker channel: the probe submits `probe_marker` and then checks it survived the claim.
        if isinstance(variables, dict):
            self.variables.update(variables)
        if not isinstance(commands, list):
            commands = (
                [] if commands is None else [commands] if not isinstance(commands, dict) else []
            )
        write_json_atomic(self.console, {"commands": [], "variables": self.variables})
        return commands

    # -- results ----------------------------------------------------------------------------
    def write_result(self, command: str, body: str) -> Path:
        """One file per command, refusals included, and nothing ever overwritten.

        The name is `<stamp>_<slug>_<sanitised command>.txt`. The probe checks two properties of
        it: that it carries the submitting agent's slug, and that it carries no separator or
        traversal sequence. Both follow from the contract's own construction rather than from a
        check here — the sanitised command has no `/` left to give.
        """
        moment = utc_now()
        name = f"{stamp(moment)}_{self.slug}_{sanitise(command)}.txt"
        path = self.output / name
        # A filename collision inside one microsecond is possible when a batch repeats a command,
        # and the contract forbids overwriting — so the second one gets a suffix rather than
        # replacing the first. Silence here would be the one place `output/` could lose a result.
        counter = 1
        while path.exists():
            path = self.output / f"{stamp(moment)}_{self.slug}_{sanitise(command)}_{counter}.txt"
            counter += 1
        write_text_atomic(path, body)
        return path

    @staticmethod
    def parse_arguments(text: str) -> dict[str, str]:
        """`key=value` pairs from a command line, which is the form the contract's examples use.

        The console deliberately does not validate arguments — that is the verb's
        `argument_schema` and the plant's business — so this returns whatever it finds and the
        callers that need a field say so themselves when it is missing.
        """
        arguments: dict[str, str] = {}
        for token in text.split()[1:]:
            if "=" in token:
                key, _, value = token.partition("=")
                arguments[key] = value
        return arguments

    def conflict_domains(self, spec: dict[str, Any]) -> list[str]:
        """The domains a verb's commands collide in, with templates expanded.

        56 of the 58 domains are plain names; two are templates — `prop.<engine>.run` is claimed by
        two verbs and means three domains in practice. The expansion rule is `gate_instantiations`'
        and deliberately so: a placeholder names the argument it varies over, so `<engine>` is the
        `engine` argument and one rule covers both the published gate variables and the conflict
        domains rather than two rules that can disagree.
        """
        declared = spec.get("conflict_domain")
        if not declared:
            return []
        try:
            return gate_instantiations(
                str(declared), spec.get("argument_schema") or {}, str(spec.get("verb"))
            )
        except Unconfigured:
            # A domain that cannot be expanded is a domain nothing can collide in, which is worse
            # than a wrong one, so it is claimed under its literal text and the linter's business is
            # to refuse the template rather than this file's to guess.
            return [str(declared)]

    def dwell_refusal(self, verb: str) -> str | None:
        """The minimum-dwell guard, evaluated at the moment of effect like every other guard.

        `plant.md` step 2 makes revalidation the executive's — "Nothing is captured at schedule
        time" — and a dwell is a guard of exactly that kind: it says how long the *vehicle* has
        held a mode, which only the vehicle knows. **Fifteen commanded states declare one and no
        tool read it**, so a fleet could re-command `set_rcs_mode` every tick, or close and open
        the bus tie inside its 0.2 s, and the record would show a machine that chattered while
        every declaration said it could not.

        `min_on_s` is how long the state must hold a value before it may change; `min_off_s` is
        how long it must stay away from a value before it may return to it. Both are wall-clock
        seconds since the last change, which is the same clock `maximum_queue_age_s` expires on —
        and a state that has never changed has no floor, because the vehicle starts with its
        machine where the configuration put it.
        """
        now = utc_now()
        for state, min_on_s, min_off_s in command_dwell(self.world, verb):
            last = self.dwell.get(state.id)
            if last is None:
                continue
            if min_on_s is None or min_off_s is None:
                return (
                    f"refused: GUARD OWED. {verb!r} moves {state.id!r}, whose dwell is "
                    "UNCONFIGURED — so the vehicle cannot say how long that mode must be held "
                    "before it may be commanded again, and a command it cannot guard is a command "
                    "it must not accept. `rcs.thruster_valve`'s two values are the pulse "
                    "generator's `t_min_on` and `t_min_off` (`rcs_dode.md:801-804`), which no "
                    "source reached publishes.\n"
                )
            held = (now - datetime.fromisoformat(str(last["changed_at"]))).total_seconds()
            if held < min_on_s:
                return (
                    f"refused: DWELL. {verb!r} moves {state.id!r}, which was last changed "
                    f"{held:.1f} s ago and must hold a value for {min_on_s:g} s before it may "
                    "change. This is the guard that makes a commanded state machine a machine "
                    "rather than a relay — `check_domain` refuses a hysteresis band on one for "
                    "the same reason: a band answers 'has the quantity crossed', and a command "
                    "does not cross anything.\n"
                )
            if last.get("was") is not None and min_off_s > 0:
                away = (
                    now - datetime.fromisoformat(str(last["was"]["left_at"]))
                ).total_seconds()
                if str(last["was"]["value"]) in self.value_of(state) and away < min_off_s:
                    return (
                        f"refused: DWELL. {verb!r} would return {state.id!r} to a value it left "
                        f"{away:.1f} s ago, and it must stay away for {min_off_s:g} s before it "
                        "may go back. `propulsion.sps_state`'s five seconds are the case this "
                        "exists for: 'a five-second floor between burns is what keeps a fleet from "
                        "spending its restart budget in a minute'.\n"
                    )
        return None

    def value_of(self, state: Any) -> list[str]:
        """The current value(s) of a state, as text, wherever the map keeps them."""
        if state.node == "internal":
            value = (self.values.get("internal") or {}).get(state.id)
        else:
            value = self.values.get(state.node)
        if isinstance(value, dict):
            return [str(v) for v in value.values()]
        return [] if value is None else [str(value)]

    def apply(self, verb: str, arguments: dict[str, str]) -> str:
        """The effect, and the sentence that says what it was.

        **This reference implementation used to stop here**, and said so in every result it wrote:
        *"it does not simulate the effect."* What it lacked until round 30 was the declaration —
        which argument carries the value and what each of its values becomes — and what it lacked
        until round 32 was a place to put a state that lives on the `internal` sentinel, where
        eight of the twenty-four command→state links land.

        An effect the corpus has not answered is **refused rather than skipped**. `apply_command`
        raises `Unconfigured` naming the field and saying what would close it, and the honest thing
        to hand a fleet is that sentence: a command accepted, acknowledged and silently ignored is
        the one outcome a fleet cannot tell from success.
        """
        guard = self.dwell_refusal(verb)
        if guard is not None:
            return guard
        try:
            staged = apply_command(self.world, self.values, verb, arguments)
        except Unconfigured as exc:
            return (
                f"refused: NOT IMPLEMENTED. {verb!r} is a valid command and the vehicle cannot "
                f"apply it yet: {exc}. The declaration is complete and the value is not — the "
                "corpus says which state this command moves and what each of its argument's values "
                "becomes, and what it does not yet say is what one of those values *is*.\n"
            )
        changed: list[str] = []
        for node, value in sorted(staged.items()):
            if node == "internal":
                for state_id in sorted(value):
                    if (self.values.get("internal") or {}).get(state_id) != value[state_id]:
                        # Qualified, because the sentinel is not a node and a bare state id
                        # would be indistinguishable from a node of the same name.
                        changed.append(f"internal:{state_id}={value[state_id]}")
            elif self.values.get(node) != value:
                changed.append(f"{node}={value}")
        self.values.update(staged)
        # The dwell's clock, written only where a value actually moved — a command that set what
        # was already set has changed nothing and must not restart the floor.
        if changed:
            for state, _, _ in command_dwell(self.world, verb):
                previous = self.dwell.get(state.id, {}).get("value")
                # **Text, not a `datetime`.** The first version stored the objects and the record
                # is JSON, so `publish()` raised `TypeError: Object of type datetime is not JSON
                # serializable` — *after* the effect had been applied and the result written, so
                # the tick's whole record was lost and the run looked successful. Every timestamp
                # in this file is an ISO string for the same reason `accepted_at` is: the record
                # has to be readable by the next process, which is the process that enforces the
                # guard.
                stamp = utc_now().isoformat()
                self.dwell[state.id] = {
                    "changed_at": stamp,
                    "value": self.value_of(state),
                    "was": (
                        {"value": previous, "left_at": stamp} if previous is not None else None
                    ),
                }
        if not changed:
            return (
                f"succeeded: {verb!r} applied. No value changed: the states it moves already held "
                "what it sets, which is what `idempotent: true` means, or it moves none — "
                "thirty-seven of the fifty-eight verbs are reads, events and configuration.\n"
            )
        return f"succeeded: {verb!r} applied. Changed: {', '.join(changed)}.\n"

    def resolve(self, command: str) -> str:
        """The result body for one command. Availability is the registries', not a decision here.

        Everything this refuses, it refuses by naming what refused it, because that is the property
        §9's checks 4 and 5 test: "an unknown verb is refused by name" and "a closed gate is
        refused by name, and its variable is published". The unknown-verb text says *unknown*
        rather than merely quoting the name, because the probe looks for either and a fleet
        reading a result should be told which kind of no it got.
        """
        text = command.strip()
        if not text:
            return "refused: empty command. There is no verb here to resolve.\n"
        verb = text.split()[0]
        spec = self.world.verbs.get(verb)
        if spec is None:
            return (
                f"refused: unknown verb {verb!r}. This vehicle's vocabulary is closed and is "
                f"published in HELP.md and in state.json's `available_commands`; {verb!r} is not "
                "in it. No effect, no spend, no state change.\n"
            )
        row = next(
            (
                r
                for r in capability_snapshot(
                    self.world, phase=self.phase, tripped_interlocks=self.tripped
                )
                if r["verb"] == verb
            ),
            None,
        )
        if row is None:
            return f"refused: {verb!r} is registered and could not be resolved.\n"
        if not row["available"]:
            return (
                f"refused: {verb!r} is closed. {row['availability_reason']}. The gate variable is "
                f"published in state.json.variables as {row['gate_variables'][0]!r}.\n"
            )
        # The conflict policy, and it is checked *after* availability: a command that is refused
        # for a closed gate does not claim its domain, because it never became valid. `:578` says
        # "first **valid** command", and a refusal that claimed the domain would let a closed gate
        # block a working command behind it.
        for domain in self.conflict_domains(spec):
            winner = self.claimed.get(domain)
            if winner is not None and winner != verb:
                return (
                    f"refused: CONFLICT_SUPERSEDED. {verb!r} is valid but {winner!r} was the first "
                    f"valid command in conflict domain {domain!r} this tick, and first valid wins "
                    "(apollo_diode.md:578). Nothing is silently discarded — apollo:579 is explicit "
                    "that the loser must be told, which is why this is a result file and not a "
                    "dropped line. Re-issue next tick if the effect is still wanted.\n"
                )
        for domain in self.conflict_domains(spec):
            self.claimed.setdefault(domain, verb)
        arguments = self.parse_arguments(text)
        if verb == "arm_event":
            event = str(arguments.get("event", ""))
            if not event:
                return f"refused: {verb!r} names no event, so there is nothing to arm.\n"
            token = hashlib.sha256(f"{self.boot_id}:{event}:{self.ticks}".encode()).hexdigest()[:16]
            self.arms[event] = token
            self.accepted.append({"verb": verb, "text": text, "at": utc_now().isoformat()})
            return (
                f"accepted: {verb!r} armed {event!r}. Token: {token}\n"
                "This is the only result on the vehicle that carries a token, and it is bound to "
                "this event: `execute_event` will refuse a token minted for a different one, and "
                "it will refuse an event that was never armed. Arming does nothing physical — every "
                "guard is evaluated at the moment of effect, not now.\n"
            )
        if verb == "execute_event":
            event = str(arguments.get("event", ""))
            offered = str(arguments.get("arm_token", ""))
            outstanding = self.arms.get(event)
            if outstanding is None:
                return (
                    f"refused: {verb!r} fires {event!r}, which is not armed. This is the "
                    "arm-then-commit pattern the one-way event taxonomy is built on "
                    "(domains/structure/components.yaml#one_way_events): a pyro, an undocking and "
                    "a staging are irreversible, so the arming step is what separates a decision "
                    "from an accident. Nothing has been fired.\n"
                )
            if not offered or offered != outstanding:
                return (
                    f"refused: {verb!r} offered arm_token {offered!r}, which is not the outstanding "
                    f"token for {event!r}. A token is bound to one event and one arming; a token "
                    "for another event, or one already consumed, is not authority to fire this "
                    "one. Nothing has been fired.\n"
                )
            del self.arms[event]
            self.accepted.append({"verb": verb, "text": text, "at": utc_now().isoformat()})
            return (
                f"succeeded: {verb!r} fired {event!r} with a valid arm token, which is now "
                "consumed — the same token cannot fire twice. This reference implementation "
                "resolves, refuses and settles; it does not simulate the effect, so the event is "
                "authorised here and its physical consequence is the plant's.\n"
            )
        self.accepted.append({"verb": verb, "text": text, "at": utc_now().isoformat()})
        if row["deferrable"]:
            # §9 check 9's whole content is *when* the re-check happens — "a deferred command is
            # re-checked when it is due, not when it was scheduled" — and the contract is equally
            # clear that the result arrives later: "A command that takes longer than one cycle — a
            # burn, a deploy, a self-test — completes asynchronously and reports when it is done."
            # So the deferral is a live queue entry with the four things the effect-time check
            # needs, and `settle()` runs it.
            due = self.ticks + 1
            self.deferred.append(
                {
                    "verb": verb,
                    "command": text,
                    "accepted_tick": self.ticks,
                    "due_tick": due,
                    # Wall clock, not `time.monotonic()`. A monotonic reading is meaningless in
                    # the next process, and the queue is restored in the next process — so the age
                    # has to be computable from a timestamp that outlives the run.
                    "accepted_at": utc_now().isoformat(),
                    "maximum_queue_age_s": spec.get("maximum_queue_age_s"),
                }
            )
            return (
                f"accepted: {verb!r} deferrable. Recorded at tick {self.ticks}, due at tick {due}; "
                f"it will be re-checked against the phase, its gate and its interlocks when it is "
                f"due rather than now (contract §9 check 9), and it expires if it is still waiting "
                f"after {spec.get('maximum_queue_age_s')} s (V-02's `EXPIRED`). The result arrives "
                "as its own file when it settles, because a command that takes longer than one "
                "cycle reports when it is done.\n"
            )
        return (
            f"accepted: {verb!r}. Authority {spec.get('authority')}, phase {self.phase}, gate "
            f"{row['gate_variables'][0]!r} open, {len(spec.get('interlocks') or [])} interlock(s) "
            f"clear. {self.apply(verb, arguments)}"
        )

    # -- the cycle --------------------------------------------------------------------------
    def settle(self) -> list[Path]:
        """Run every deferral that has come due, and write each outcome as its own result.

        Three things happen here and V-02 names all three. A due command is **revalidated** — the
        phase, the gate and the interlocks are read again at the moment of effect, which is the
        whole of §9 check 9 and the reason the acceptance-time answer is not reused. It
        **expires** if it has waited longer than its own `maximum_queue_age_s`, which is declared
        once per verb and was until now read only by the generator that prints it. And it
        **reports asynchronously**, in a file of its own, because the contract says a command that
        takes longer than one cycle "completes asynchronously and reports when it is done" — a
        result written at acceptance would be a claim about the future dressed as a report.

        The queue age is wall-clock seconds, so it is the poll interval that makes deferrals
        expire. That is honest rather than convenient: `maximum_queue_age_s` is a statement about
        how long a command's intent stays valid in *time*, and a console ticking at 1 Hz with a
        5-second age is a vehicle that forgets. Under `--poll 0` no time passes and nothing ever
        expires, which is worth knowing before using that flag to test the expiry path.
        """
        written: list[Path] = []
        still_waiting: list[dict[str, Any]] = []
        for entry in self.deferred:
            if self.ticks < int(entry.get("due_tick", 0)):
                still_waiting.append(entry)
                continue
            verb = str(entry.get("verb"))
            command = str(entry.get("command", verb))
            accepted_at = str(entry.get("accepted_at", ""))
            try:
                age = (utc_now() - datetime.fromisoformat(accepted_at)).total_seconds()
            except ValueError:
                # An entry with no readable stamp is one written by something else, and an age of
                # zero is the honest answer: it has not been shown to be old, so it is not expired.
                age = 0.0
            limit = entry.get("maximum_queue_age_s")
            if isinstance(limit, (int, float)) and age > float(limit):
                written.append(
                    self.write_result(
                        command,
                        f"refused: EXPIRED. {verb!r} was accepted at tick "
                        f"{entry.get('accepted_tick')} and has waited {age:.1f} s, past its own "
                        f"maximum_queue_age_s of {limit:g}. V-02 makes `EXPIRED` a refusal reached "
                        "from REVALIDATING, so the command is re-checked when it is due and it is "
                        "*this* check that fires first — a command whose intent has gone stale is "
                        "declined rather than run late.\n",
                    )
                )
                continue
            spec = self.world.verbs.get(verb) or {}
            row = next(
                (
                    r
                    for r in capability_snapshot(
                        self.world, phase=self.phase, tripped_interlocks=self.tripped
                    )
                    if r["verb"] == verb
                ),
                None,
            )
            if row is None or not row["available"]:
                reason = row["availability_reason"] if row else "the verb is no longer registered"
                written.append(
                    self.write_result(
                        command,
                        f"refused: INHIBITED. {verb!r} was valid when it was accepted at tick "
                        f"{entry.get('accepted_tick')} and is not valid now: {reason}. This is the "
                        "re-check §9 check 9 is about — validity at admission is not validity at "
                        "effect, and the difference is the whole reason the state is called "
                        "REVALIDATING rather than REVALIDATED (V-02).\n",
                    )
                )
                continue
            self.accepted.append(
                {"verb": verb, "text": command, "at": utc_now().isoformat(), "settled": True}
            )
            written.append(
                self.write_result(
                    command,
                    f"settled: {verb!r} at tick {self.ticks}, "
                    f"{age:.1f} s after acceptance, re-checked against the phase, its gate and its "
                    "interlocks at the moment of effect rather than at the moment it was "
                    "scheduled. Interlocks: "
                    f"{'none declared' if not spec.get('interlocks') else str(spec.get('interlocks'))}. "
                    f"{self.apply(verb, self.parse_arguments(command))}",
                )
            )
        self.deferred = still_waiting
        return written

    def cycle(self) -> list[Path]:
        """One pass: settle, claim, act, publish. Returns the results written."""
        written = self.settle()
        claimed = self.claim()
        if isinstance(claimed, str):
            # An unreadable console is not an empty one, and the contract's answer to "an agent
            # writing the file in place" is that refusing is correct and the result file says so.
            written.append(self.write_result("console_unreadable", f"refused: {claimed}.\n"))
        elif claimed and not all(isinstance(item, str) for item in claimed):
            # "One malformed element refuses the whole batch. If any element of `commands` is not a
            # string, none of it runs, and exactly one result file records that. Partial execution
            # of a malformed batch is worse than none."
            kinds = ", ".join(sorted({type(item).__name__ for item in claimed}))
            written.append(
                self.write_result(
                    "malformed_batch",
                    "refused: the whole batch. It contained a non-string element "
                    f"({kinds}), so none of its {len(claimed)} command(s) ran. Exactly one result "
                    "records this, which is the contract's requirement and not a convenience: "
                    "partial execution of a malformed batch is worse than none.\n",
                )
            )
        else:
            for command in claimed:
                written.append(self.write_result(command, self.resolve(command)))

        self.ticks += 1
        # The claim is per *tick*: `:578` says "for that tick", so the next cycle starts empty and a
        # superseded command can be re-issued successfully. Clearing at the end rather than the
        # start means a crash mid-batch leaves the claim standing, which is the same choice the
        # contract makes about the batch itself — a command takes effect at most once.
        self.claimed.clear()
        self.publish()
        return written

    def publish(self) -> None:
        """Rewrite `state.json`, `HELP.md`, `pending.json` and append a frame. Every cycle.

        `state.json` is rewritten "whether or not anything was submitted", which is what makes the
        probe's `check_state_is_a_mirror` meaningful: it doctors the file, waits, and requires the
        doctored content to be gone. A publisher that only wrote on change would leave the doctored
        copy standing and fail a check it was never trying to pass.
        """
        rows = capability_snapshot(self.world, phase=self.phase, tripped_interlocks=self.tripped)
        variables = {
            name: name not in {k for k, v in self.variables.items() if not v}
            for row in rows
            for name in row["gate_variables"]
        }
        # The console's own variables win where they name a published gate, because the console is
        # the fleet's hand and the registry is only the default. This is also what makes a lowered
        # allowance stay lowered: §9 check 8's direction is that the console may lower and never
        # raise, so a gate the registry had open and the console has closed stays closed.
        for name, value in self.variables.items():
            if name in variables:
                variables[name] = bool(value)
        state = {
            "published_at": utc_now().isoformat(),
            "available_commands": [row["verb"] for row in rows if row["available"]],
            "variables": variables,
            "budget": {
                "used_this_window": sum(1 for _ in self.accepted),
                "limit_per_window": int(self.variables.get("allowance", 120)),
                "window_seconds": 3600,
                "oldest_expires_in_seconds": 3600 - int((utc_now() - self.started).total_seconds()),
            },
            "queue_depth": len(self.deferred),
            # **The ring, described rather than only published.** The contract's one sentence about
            # the ring is the one it is most careful with — "self-describing about its own cadence
            # and its own losses" — and until this key existed there was nothing to describe: the
            # ring was unbounded and no frame was ever lost, so "losses" was a field with no possible
            # value. The three numbers are the bound, what is held, and what fell out of the far end,
            # and `held + losses` is what the vehicle has produced.
            "ring": {
                "slots": self.ring_slots,
                "held": len(self.ring_frames()),
                "losses": self.ring_losses(),
                "newest_seq": max(0, self.seq - 1),
            },
            # `posture` is the *execution* machine's posture (`mission.yaml#postures`), which the
            # console does not drive and which stays empty; `scenario` is the run's difficulty
            # identity and is the one this file owns. Two names because they are two things, and
            # putting the scenario in the posture's slot would make the mirror claim a state the
            # execution machine never entered.
            "vehicle": {
                "phase": self.phase,
                "posture": "",
                "scenario": self.scenario,
                "abort_latched": False,
            },
            "capability": rows,
        }
        write_json_atomic(self.state, state)
        # Written every cycle from the cached text, for the reason `HELP.md` is: a hand-edit lasts
        # until the next cycle. What is *derived* is done once, because the configuration is fixed
        # for the life of a console — a cadence class that moved, a channel withdrawn or a refusal
        # code added all require a restart, which is the same statement the capabilities snapshot
        # makes by being rebuilt per cycle from a world that has not changed.
        write_text_atomic(self.help_file, self.help_text())
        write_text_atomic(self.readme, self.readme_text)
        write_json_atomic(
            self.pending,
            {
                "pending": self.deferred,
                "arms": self.arms,
                "dwell": self.dwell,
                "ticks": self.ticks,
                "seq": self.seq,
                "boot_id": self.boot_id,
                # The run's identity lives in the vehicle's own record rather than in the mirror,
                # for the reason `ticks` and `seq` do: `state.json` is published state and the
                # contract says it is never read back as input, so anything a restart has to
                # remember is `vehicle -> vehicle`.
                "scenario": self.scenario,
                "seed": self.seed,
                # The ring's own accounting: what it is bounded to, and what it has lost. Derived
                # from the files at write time, so a reader that trusts `pending.json` and a reader
                # that counts the directory get the same answer.
                "ring_slots": self.ring_slots,
                "ring_losses": self.ring_losses(),
            },
        )
        self.write_frame()

    def ring_frames(self) -> list[Path]:
        """The frames the ring holds, oldest first. One scan, used by both the pruner and a reader."""
        return sorted(self.telemetry.glob("*.json"), key=lambda path: path.name)

    def ring_losses(self) -> int:
        """Frames this vehicle has produced that the ring no longer holds.

        **Derived rather than counted, and that is the property that makes it self-describing.** The
        sequence number is how many frames have been written; the ring holds some of them; the
        difference is what fell out of the far end. A counter kept beside the files would be a second
        declaration of a quantity the files already answer — and it would be the one that drifted
        the first time a frame was removed by hand.
        """
        return max(0, self.seq - len(self.ring_frames()))

    def write_frame(self) -> None:
        """Append one frame to the ring, and drop the ones that fall out of the far end.

        `NNN.json` is the *sequence* number, so a name is never reused and a reader can always tell
        a new frame from a rewritten one. The names are not zero-padded to a fixed width on purpose:
        `f"{seq:03d}"` stops padding at 1000 and the sort is lexicographic, so a fixed width would
        order 1000 before 999 the moment a run was long enough — which a 192-hour mission at one
        frame per cycle is.
        """
        frame = {
            "schema": "aurora.capsule.telemetry.v1",
            "seq": self.seq,
            "sim_step": self.ticks,
            "boot_id": self.boot_id,
            "met_s": float(self.ticks),
            "sensor_time_s": float(self.ticks),
            "publish_time_s": float(self.ticks),
            "state_revision": self.ticks,
            "vehicle": "csm",
            "phase": self.phase,
            # The declared initial conditions rather than nothing. `plant.md` puts the
            # measurements and estimates in the *ring* and the software state in the mirror, and
            # this field had been empty since it was written: a fleet could issue fifty-four
            # commands and read back no quantity at all. See `plant.initial_values` for why the
            # keys are nodes rather than published channel names.
            "values": dict(self.values),
            "quality": {},
            "injected": False,
        }
        write_json_atomic(self.telemetry / f"{self.seq:03d}.json", frame)
        self.seq += 1
        # **The bound, and it is a deletion rather than a wrap.** A ring that reused names would make
        # "a new frame arrived" and "an old frame was rewritten" the same observation, which is the
        # one distinction a reader of a ring has to be able to make. So the names stay monotone and
        # the *oldest* file is removed, which also means the directory listing is the ring.
        held = self.ring_frames()
        for stale in held[: max(0, len(held) - self.ring_slots)]:
            # A reader can unlink between the listing and this, and that is not an error: the frame
            # is gone either way, which is the only thing the prune is for.
            with contextlib.suppress(FileNotFoundError):
                stale.unlink()

    def help_text(self) -> str:
        """`HELP.md`, from the one generator, because a second one is a second source of truth.

        The first version of this method wrote its own HELP.md, and that was wrong in a way the
        folder names explicitly: `presentation.yaml` says "`HELP.md` is *generated* from
        `commands.yaml` and hand-editing it would create a second source of truth for the
        vocabulary", and a second generator is hand-editing with extra steps. The contract leaves
        HELP.md's *voice* to the vehicle's builder (contract §8) but the *vocabulary* has one
        source, and `tools/generate_help.py` reads it.

        What writing a second one exposed is worth keeping, though, because it is a real fork in
        the window. `contract/diode_probe.py#published_verbs` learns the vehicle's verbs from
        `state.json`'s `available_commands` and **falls back** to scanning HELP.md for lines of the
        form ``- `name` ``. In this vehicle's HELP.md that form is used for **arguments** — the
        first four such lines are `mode`, `reason`, `preserve_memory`, `group` — so the fallback
        would read argument names as verbs and then submit `mode probe`, get an unknown-verb
        refusal, and report a vocabulary failure that is not one. This loop always writes
        `state.json` before `HELP.md` and always writes both, so the fallback is never taken; the
        vehicle's obligation under the contract is the `available_commands` key and it meets it.
        The mismatch is recorded in `presentation.yaml#conformance` rather than papered over by
        giving HELP.md a verb list it does not otherwise need.

        Cached after the first call, for the reason `readme_text` is generated once: the
        configuration is fixed for the life of a console, and regenerating a 58-verb document every
        cycle is work that cannot produce a different answer.
        """
        if self.help_cache is None:
            self.help_cache = generate_help(self.world.root)
        return self.help_cache


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dir", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--diode-dir", default=".scratch/diode")
    parser.add_argument("--slug", default="vehicle")
    parser.add_argument("--phase", default="translunar_coast")
    parser.add_argument("--poll", type=float, default=1.0, help="seconds between cycles")
    parser.add_argument("--cycles", type=int, default=0, help="0 runs until interrupted")
    parser.add_argument(
        "--closed-interlock",
        action="append",
        default=[],
        metavar="THRESHOLD",
        help="an interlock that is currently tripped, by threshold id; repeatable",
    )
    parser.add_argument("--init", action="store_true", help="create the directory and stop")
    parser.add_argument(
        "--ring-slots",
        type=int,
        default=None,
        metavar="N",
        help="how many frames the telemetry ring holds. `docs/diode-contract.md:186` requires a "
        "fixed slot count and `presentation.yaml#ring` declines to set it — 'a slot count is a "
        "memory decision' — so the run declares it",
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help="print what this scenario and seed decide — the scheduled faults, the placed ones and "
        "the factors — and stop, without running a cycle",
    )
    parser.add_argument(
        "--plan-json",
        action="store_true",
        help="the same plan as JSON, for a caller that wants to store or diff it",
    )
    parser.add_argument(
        "--scenario",
        default=None,
        metavar="POSTURE",
        help="the run's difficulty identity: a `mission.yaml#scenario_postures` id "
        "(nominal, degraded, crisis). Recorded in the mirror and in the vehicle's own record, and "
        "a resumed run keeps the one it recorded unless this names another",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="the run's master seed, for the fault streams `tools/faults.py` draws from. "
        "A scenario and a seed together are what make a run reproducible, and a resumed run keeps "
        "the pair it recorded unless this names another",
    )
    args = parser.parse_args(argv)

    try:
        world = load_world(Path(args.dir))
    except Exception as exc:  # noqa: BLE001 - the loader's refusals are the point
        sys.stderr.write(f"the configuration cannot be loaded: {exc}\n")
        return 3

    # **A scenario that names no posture is refused rather than accepted and ignored**, which is
    # the failure mode `mission.yaml`'s own debt records one level up: the difficulty knob existed
    # for rounds and did nothing, because the scheduler read the policies and never the postures.
    # The list comes from the mission file through the module that already consumes it, so there is
    # one declaration of what the scenarios are.
    try:
        postures = load_postures(Path(args.dir))
    except Exception as exc:  # noqa: BLE001 - a refusal is the answer here too
        sys.stderr.write(f"the scenario postures cannot be loaded: {exc}\n")
        return 3

    # ---- the run's identity, resolved once -------------------------------------------------
    #
    # A caller who names a value gets it. A caller who names nothing inherits whatever the window's
    # own record holds. A window with no record gets the declared default. The order matters and it
    # is the round's whole subject: the middle case is only reachable because the flag defaults are
    # `None`, and it is *unreachable* when `--scenario` defaults to the string the record would
    # hold anyway.
    window = Path(args.diode_dir) / args.slug
    recorded = recorded_run(window)
    recorded_scenario = recorded.get("scenario")
    if not isinstance(recorded_scenario, str) or not recorded_scenario:
        recorded_scenario = None
    scenario = resolve_remembered(args.scenario, recorded_scenario, DEFAULT_SCENARIO)
    # The refusal is made on the **resolved** value rather than on the flag, so a record naming a
    # posture the mission no longer declares is caught here too. It exits 3 either way and the two
    # sentences differ, because "you named a scenario that does not exist" and "this window is in a
    # scenario that does not exist" want different repairs.
    if scenario not in postures:
        if args.scenario is not None:
            sys.stderr.write(
                f"scenario {scenario!r} is not one of the vehicle's "
                f"{len(postures)}: {sorted(postures)}\n"
            )
        else:
            sys.stderr.write(
                f"the window at {window} records scenario {scenario!r}, which is not one of the "
                f"vehicle's {len(postures)}: {sorted(postures)}. Name one that is, or point "
                f"`--diode-dir` and `--slug` at another window\n"
            )
        return 3
    # The seed rides the same rule, and the reason is the pair rather than either half: a run is
    # reproducible from its scenario *and* its seed, so a seed flag that lost to the record would
    # leave a run whose own numbers do not reproduce it.
    recorded_seed = recorded.get("seed")
    if not isinstance(recorded_seed, int) or recorded_seed < 0:
        recorded_seed = None
    seed = resolve_remembered(args.seed, recorded_seed, DEFAULT_SEED)
    # **The ring is the one remembered value a restart may not re-bound, and this is where that
    # decision is made audible.** Its own logic is unchanged — whatever bound the frames on disk
    # were written under is the bound the run keeps, so the count on disk, the loss accounting and
    # the declaration cannot disagree about how many frames exist. What changes is that a caller
    # who names a *different* bound is told, instead of being handed the old one with no remark.
    recorded_slots = recorded.get("ring_slots")
    if not isinstance(recorded_slots, int) or recorded_slots <= 0:
        recorded_slots = None
    if args.ring_slots is not None and recorded_slots is not None and args.ring_slots != recorded_slots:
        sys.stderr.write(
            f"--ring-slots {args.ring_slots} names a bound the window at {window} does not have: "
            f"its record holds {recorded_slots}, and the frames on disk were written under it. A "
            f"restart keeps the bound the ring already has — re-bounding it would make the frames "
            f"held, the losses accounted and the declared slot count three answers to one question. "
            f"Point `--slug` at a new window to run a differently bounded ring\n"
        )
        return 3
    ring_slots = resolve_remembered(args.ring_slots, recorded_slots, DEFAULT_RING_SLOTS)

    # **`--plan` answers "what will this run be" before it is run**, which is the half of criterion
    # 3a that a console cannot answer for itself: the console is the window, and the faults are the
    # adversary's. Both read the same `scenario_report`, so the plan a run announces and the plan it
    # flies are one computation rather than two that agree until somebody edits one — and it takes
    # the *resolved* pair for that reason, so `--plan` on a window recorded as `crisis` describes
    # the crisis that window is in rather than the default it would have had.
    if args.plan or args.plan_json:
        faults = load_faults(Path(args.dir))
        if not faults:
            sys.stderr.write(f"no faults under {Path(args.dir) / 'domains'}\n")
            return 3
        phases = [
            float(row.get("duration_h", 0))
            for row in (yaml.safe_load((Path(args.dir) / "mission.yaml").read_text()) or {}).get(
                "phases"
            )
            or []
        ]
        hours = sum(phases)
        try:
            plan = scenario_report(Path(args.dir), faults, postures, scenario, seed, hours)
        except Exception as exc:  # noqa: BLE001 - the refusal is the answer
            sys.stderr.write(f"the scenario cannot be planned: {exc}\n")
            return 3
        plan.pop("_armed_faults", None)
        if args.plan_json:
            json.dump(plan, sys.stdout, indent=2)
            sys.stdout.write("\n")
            return 0
        print(
            f"scenario {plan['posture']!r} at seed {plan['master_seed']}, over {plan['hours']:g} h"
        )
        print(
            f"  hazard x{plan['hazard_factor']:g}, demand x{plan['on_demand_factor']:g}"
            f"  ·  seeded: {plan['seeded_faults']}"
        )
        print(f"  {len(plan['events'])} scheduled event(s), {len(plan['armed'])} armed fault(s)")
        for event in plan["events"][:10]:
            print(
                f"    MET {event['met_h']:8.3f} h  {event['fault']:38} {event['kind']:22} "
                f"-> {', '.join(event['perturbs'][:2])}"
            )
        if len(plan["events"]) > 10:
            print(f"    … and {len(plan['events']) - 10} more")
        return 0

    console = Console(
        world,
        window,
        args.slug,
        phase=args.phase,
        tripped_interlocks=set(args.closed_interlock),
        scenario=scenario,
        seed=seed,
        ring_slots=ring_slots,
    )
    console.initialise()
    if args.init:
        print(f"initialised {console.root} for slug {args.slug!r} at phase {args.phase!r}")
        return 0

    print(
        f"[console] {console.root} slug={args.slug} phase={args.phase} "
        f"scenario={console.scenario} seed={console.seed} ring={console.ring_slots} "
        f"poll={args.poll}s cycles={args.cycles or 'until interrupted'}",
        flush=True,
    )
    # `--cycles` counts the cycles **this invocation** runs, not the vehicle's lifetime. The
    # difference was a real bug the moment the tick counter became durable: a resumed console
    # comes back at tick N, so `ticks < cycles` was already false and a `--cycles 1` restart ran
    # nothing at all — which is also why the deferred queue appeared never to settle.
    ran = 0
    try:
        while args.cycles == 0 or ran < args.cycles:
            ran += 1
            written = console.cycle()
            if written:
                print(f"[console] tick {console.ticks}: {len(written)} result(s)", flush=True)
            time.sleep(args.poll)
    except KeyboardInterrupt:
        print(f"\n[console] stopped after {console.ticks} tick(s)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
