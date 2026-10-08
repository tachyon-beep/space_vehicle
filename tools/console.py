#!/usr/bin/env python3
"""The vehicle's side of the frozen window: one executive, one physical world, any number of windows.

`docs/diode-contract.md` is the only thing this repository and the chassis share, and this file is
the vehicle's implementation of its side of it. It used to be a *reference console*: one process
per `<slug>` directory, each with its own copy of the plant's initial conditions, never stepping the
plant, counting cycles as ticks and timing dwell on the wall clock — so ten agents flew ten vehicles,
none of which moved. `docs/decisions/0001-shared-executive.md` (accepted) replaces that with the
shape `plant.md` §1 draws: **one `Executive` owns one `World`, one truth map, one tick counter, one
receipt sequence and one state lineage**, and a **`Window`** is the per-slug ingress and
publication view attached to it — its console, its results, its gate preferences, its deferral
queue, its ring — with no physical state of its own.

Each cycle the executive visits every window in a deterministic order (sorted slugs rotated by the
tick, ADR choice A), settles that window's due deferrals, claims its console destructively and
atomically, validates every line in order — empty, unknown verb, phase and gate, interlocks, dwell,
argument resolution, conflict — and turns the accepted state-staging commands into
`plant.Effect`s stamped at `offset_us = 0` in arbitration order. Then it steps the plant **exactly
once**, writes every result (refusals included, one file each, each ending in a receipt block),
rewrites every window's mirror and record, and appends one frame per window from the stepped truth.
Validation and effect therefore read the same truth at the same simulated instant.

What this slice substitutes, and says so (ADR 0001 "not established"):

  - **Identity instrument.** `plant.emit_frame` publishes the declared channels straight from the
    stepped truth — no noise, lag, quantisation or quality model. Instruments are WP09's.
  - **Unevaluated interlocks are refused.** Threshold evaluation is WP05's, so a verb that declares
    any interlock is refused `INTERLOCK UNEVALUATED` naming each one and why it cannot be read
    (choice C). `set_bus_tie`, `start_burn`, `arm_event` and `execute_event` are all in that set.
  - **One tick per cycle, multiplier undecided.** The loop inherits one tick per cycle from the old
    console. How many ticks a wall second carries, and the pause, overload and downtime policy, are
    WP08's and are **not** decided here; `--poll` is a wall-clock sleep between cycles and nothing more.
  - **No persistence, no restart.** The old cross-process resume of ticks, arm tokens, dwell and
    deferrals from the agent-writable `pending.json` is withdrawn: it restored authority from a file
    an agent can write. A window is bound to the first executive that ticks it, and a second
    executive refuses it (choice D) until WP08 defines continuity. `--init` prepares a window's
    identity without binding it.

Lineage and truth are never written into a window file. A truth hash published to agents would be a
side channel that reveals hidden changes; `--journal PATH` writes them as JSONL for the operator and
is refused if the path lies inside `--diode-dir`.

    python3 tools/console.py --diode-dir .scratch/diode --slug alpha --slug bravo --init
    python3 tools/console.py --diode-dir .scratch/diode --slug alpha --slug bravo --cycles 60 --poll 1
    python3 contract/diode_probe.py --diode-dir .scratch/diode --slug alpha --poll-seconds 1
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from faults import load_faults, load_postures, scenario_report  # noqa: E402
from generate_help import generate as generate_help  # noqa: E402
from generate_readme import generate as generate_readme  # noqa: E402
from plant import (  # noqa: E402
    Effect,
    Unconfigured,
    World,
    _stages_a_state,
    apply_command,
    capability_snapshot,
    command_dwell,
    command_targets,
    emit_frame,
    gate_instantiations,
    initial_values,
    load_world,
    state_hash,
    state_level,
    step,
    tick_seconds,
)

# The contract's own bound on a read of an agent-writable file. The operator-side services use
# `MAX_READ_BYTES` for the same reason and the same number is not a coincidence: an ingress file
# is written by something this process does not control.
MAX_READ_BYTES = 1_000_000

# **The three values a window remembers and a caller may also name, and the defaults are `None`.**
# `--scenario`, `--seed` and `--ring-slots` are each written into `pending.json` and read back by the
# next process — which makes each of them two things at once: a thing the window remembers and a
# thing a caller can say. Those two are only distinguishable if *the caller named nothing* has a
# representation of its own, and a parser whose `--scenario` defaults to `"nominal"` does not have
# one: `--scenario nominal` and no flag at all are the same `args.scenario`, so the restore cannot
# know which it is looking at. `check_console_flags` holds the parser to this.
#
# So the flag defaults are `None`, `None` means **the caller named nothing**, and the resolution
# happens once, in `main`, where the window's record and the mission's declared postures are both
# in hand. The declared defaults live here instead, and they are what an unnamed flag resolves to on
# a window that has no record — a fresh one. Under ADR 0001 the record is read back for exactly two
# things: this resolution, and the binding check. Never for ticks, arm tokens, dwell or deferrals.
DEFAULT_SCENARIO = "nominal"
DEFAULT_SEED = 0
DEFAULT_RING_SLOTS = 300

# ADR 0001 choice B: a batch longer than this is refused whole, with one result, by the same rule as
# a malformed one. It is an operator ceiling — a window cannot raise it — and it must be at least one.
DEFAULT_MAX_BATCH = 32

# The exclusive lock that makes a directory one world. Held for the life of the process; taken only
# while initialising under `--init`.
LOCK_FILE = ".executive.lock"


def recorded_run(root: Path) -> dict[str, Any]:
    """The window's own record of itself — `pending.json` — or an empty map where there is none.

    `pending.json` is the one file in the window that is read back as input; `state.json` is
    published state and the contract is explicit that editing it changes nothing. What a start reads
    from it is the window's identity (scenario, seed, ring) and its binding (`world_id`, `ticks`), and
    nothing else: the old console also restored arm tokens, dwell, deferrals and counters from here,
    which is authority restored from a file an agent can write (ADR 0001).
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

    This is the *record's* reader — `pending.json`, which the start resolves identity from — and it
    is deliberately not the ingress reader: `read_ingress` below tells its six failures apart,
    because each of them is a refusal result a fleet has to be able to read.
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


def read_ingress(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """The console's payload, or the sentence that says why it is not one.

    **The old reader returned `{}` for invalid JSON, so a half-written console was silently an empty
    batch.** The contract explicitly permits an agent to produce one ("an agent writing the file in
    place"), and its answer is that refusing is correct and the result file says so. Every shape of
    not-a-console is named here so the one refusal result can carry the reason: absent, unreadable,
    oversized, not UTF-8, not JSON, not an object. The caller claims the console in every one of
    those cases, so a broken writer produces one refusal and not one per cycle (ADR 0001).
    """
    try:
        if not path.exists():
            return None, "console.json is absent — the window has no console to claim"
        size = path.stat().st_size
        if size > MAX_READ_BYTES:
            return None, (
                f"console.json is {size} bytes, past the {MAX_READ_BYTES} bytes this vehicle will "
                "read of an agent-writable file"
            )
        raw = path.read_bytes()
    except OSError as exc:
        return None, f"console.json could not be read ({type(exc).__name__}: {exc})"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return None, f"console.json is not UTF-8 ({exc.reason} at byte {exc.start})"
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, (
            f"console.json is not valid JSON ({exc.msg} at character {exc.pos}); a half-written "
            "file lands here too, and the contract's answer is that refusing is correct"
        )
    if not isinstance(loaded, dict):
        return None, f"console.json is a JSON {type(loaded).__name__}, not an object"
    return loaded, None


@dataclass
class Verdict:
    """One result in the making: what a window's line became, and the receipt it was given.

    A verdict is created at validation, which is when the receipt number is drawn — so a loser can
    name the winner's receipt in the same tick — and its body is finished after the step for the
    commands that staged an effect, because what changed in truth is not known until then.
    """

    command: str
    state: str  # accepted | refused | settled | duplicate | superseded | deferred
    body: str
    receipt: int
    tick: int
    window: str
    verb: str | None = None
    arguments: dict[str, str] = field(default_factory=dict)
    effect: Effect | None = None
    settled: bool = False


class Executive:
    """One physical world, stepped once per cycle, commanded and observed through its windows.

    Everything physical lives here and nowhere else: the truth map (`initial_values`, then
    `plant.step` each tick), the integer tick, the receipt sequence, the per-tick lineage, the
    commanded-state dwell records in simulated microseconds, the conflict claims of the current
    tick, and the attached windows. A `Window` can read the truth to validate and to publish; it
    cannot hold any.

    `world_id` is drawn fresh per instance and is not derived from the truth, so it reveals nothing
    about the state. The lineage is `sha256(lineage_{n-1} + state_hash(truth_n) + digest of the
    tick's accepted effects)`, kept in-process (and in `--journal`) and never in a window file.
    """

    def __init__(
        self,
        world: World,
        diode_dir: Path,
        *,
        phase: str,
        tripped_interlocks: set[str] | None = None,
        scenario: str = DEFAULT_SCENARIO,
        seed: int = DEFAULT_SEED,
        max_batch: int = DEFAULT_MAX_BATCH,
        journal: Path | None = None,
    ) -> None:
        if int(max_batch) < 1:
            raise ValueError(f"max_batch must be at least 1, not {max_batch!r}")
        self.world = world
        self.diode_dir = Path(diode_dir)
        self.phase = phase
        # Live interlock state the *operator* has asserted, and the default is nothing tripped. A
        # declared interlock is a guard evaluated when a command arrives, not a standing condition;
        # a tripped one refuses by name before the unevaluated rule gets to speak.
        self.tripped = set(tripped_interlocks or ())
        self.scenario = scenario
        self.seed = int(seed)
        self.max_batch = int(max_batch)
        self.dt = tick_seconds(world)
        self.tick_us = int(round(self.dt * 1_000_000))
        self.truth: dict[str, Any] = initial_values(world)
        self.tick = 0
        self.world_id = uuid.uuid4().hex
        self.receipt = 0
        self.lineage: list[str] = [self._lineage_link("", self.truth, [])]
        # When each commanded state last changed, in simulated microseconds, and what it left. The
        # old console kept this per window on the wall clock; a dwell is a fact about the vehicle's
        # one machine on a clock that cannot jump (`plant.md` §5), so it is the executive's and it is
        # `tick * tick_us + offset_us`.
        self.dwell: dict[str, dict[str, Any]] = {}
        # Conflict domains claimed *this tick*, mapping the domain to the winner. `apollo_diode.md:578`:
        # "Within one conflict domain, first valid command received wins for that tick", and `:579`:
        # "Later commands are not silently discarded: they receive CONFLICT_SUPERSEDED." One map for
        # the whole world, because the old per-window maps meant two windows never conflicted at all.
        self.claimed: dict[str, Verdict] = {}
        self.windows: dict[str, Window] = {}
        # Isolation failures: a window whose claim or publication raised `OSError`, recorded here
        # and on stderr, and skipped for that tick while the tick and the other windows proceeded.
        self.failures: list[dict[str, Any]] = []
        self.journal: Path | None = None
        if journal is not None:
            journal = Path(journal)
            if journal.resolve().is_relative_to(self.diode_dir.resolve()):
                raise ValueError(
                    f"--journal {journal} lies inside --diode-dir {self.diode_dir}: the lineage and "
                    "the truth hash are the executive's and never go where an agent can read them"
                )
            self.journal = journal
        # Generated once, because the configuration does not change while an executive runs.
        self.readme_text = generate_readme(world.root)
        self.help_text = generate_help(world.root)

    # -- windows ------------------------------------------------------------------------------
    def attach(self, slug: str, *, ring_slots: int = DEFAULT_RING_SLOTS) -> Window:
        """Create the view for one slug and prepare its directory. A slug attaches once.

        The window's record is written unbound (`world_id: null`): the first cycle that ticks it
        binds it, which is also what `--init` leaves behind.
        """
        if slug in self.windows:
            raise ValueError(f"slug {slug!r} is already attached to world {self.world_id}")
        window = Window(self, self.diode_dir / slug, slug, ring_slots=ring_slots)
        window.prepare()
        self.windows[slug] = window
        return window

    def order(self) -> list[Window]:
        """This tick's visiting order: sorted slugs rotated left by `tick % n` (ADR choice A).

        No name holds a permanent priority, file arrival time decides nothing, and the order is a
        pure function of (slugs, tick) — so two executives fed the same ingress arbitrate the same.
        """
        slugs = sorted(self.windows)
        if not slugs:
            return []
        k = self.tick % len(slugs)
        return [self.windows[s] for s in slugs[k:] + slugs[:k]]

    # -- the cycle ----------------------------------------------------------------------------
    def cycle(self) -> list[Path]:
        """One tick: settle, claim and validate every window, step the plant once, publish.

        One tick per cycle is inherited from the old loop and is **not** a decision about the clock:
        how many ticks a wall second carries is WP08's. What *is* decided is that every window's
        validation reads the same truth, every accepted effect lands in the same step at
        `offset_us = 0` in arbitration order, and every window's frame for this tick is built from
        the same stepped truth.
        """
        tick = self.tick
        self.claimed = {}
        verdicts: dict[str, list[Verdict]] = {}
        effects: list[Effect] = []
        applied: list[Verdict] = []
        order = self.order()
        for window in order:
            try:
                settled = self._settle(window)
                claimed = self._ingest(window)
            except OSError as exc:
                self._record_failure(window, "claim", exc, tick)
                continue
            verdicts[window.slug] = settled + claimed
            for verdict in verdicts[window.slug]:
                if verdict.effect is not None:
                    effects.append(verdict.effect)
                    applied.append(verdict)

        before = self.truth
        self.truth = step(self.world, before, self.dt, None, effects)
        for verdict in applied:
            verdict.body += self._report_effect(verdict, before, self.truth)
        self.tick += 1
        self.lineage.append(self._lineage_link(self.lineage[-1], self.truth, effects))
        self._journal(effects, verdicts)

        written: list[Path] = []
        for window in order:
            if window.slug not in verdicts:
                continue
            try:
                written.extend(window.publish(verdicts[window.slug]))
            except OSError as exc:
                self._record_failure(window, "publish", exc, tick)
        return written

    def _record_failure(self, window: Window, stage: str, exc: OSError, tick: int) -> None:
        """Isolation: the failure is recorded, on stderr and here, and the tick goes on without the window."""
        entry = {
            "tick": tick,
            "window": window.slug,
            "stage": stage,
            "error": f"{type(exc).__name__}: {exc}",
        }
        self.failures.append(entry)
        sys.stderr.write(
            f"[console] tick {tick}: window {window.slug!r} skipped at {stage}: "
            f"{entry['error']}\n"
        )

    def _lineage_link(self, previous: str, truth: dict[str, Any], effects: list[Effect]) -> str:
        digest = hashlib.sha256(
            json.dumps(
                [[e.offset_us, e.verb, e.arguments] for e in effects], sort_keys=True
            ).encode("utf-8")
        ).hexdigest()
        return hashlib.sha256((previous + state_hash(truth) + digest).encode("utf-8")).hexdigest()

    def _journal(self, effects: list[Effect], verdicts: dict[str, list[Verdict]]) -> None:
        if self.journal is None:
            return
        line = {
            "tick": self.tick,
            "world_id": self.world_id,
            "lineage": self.lineage[-1],
            "state_hash": state_hash(self.truth),
            "effects": [[e.offset_us, e.verb, e.arguments] for e in effects],
            "receipts": [
                {"seq": v.receipt, "window": v.window, "state": v.state, "command": v.command}
                for rows in verdicts.values()
                for v in rows
            ],
        }
        with self.journal.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, sort_keys=True) + "\n")

    # -- ingress ------------------------------------------------------------------------------
    def _verdict(self, window: Window, command: str, state: str, body: str, **extra: Any) -> Verdict:
        self.receipt += 1
        return Verdict(command, state, body, self.receipt, self.tick, window.slug, **extra)

    def _ingest(self, window: Window) -> list[Verdict]:
        """Claim one window's console and validate what it held, in order."""
        claimed = window.claim()
        if isinstance(claimed, str):
            return [
                self._verdict(
                    window,
                    "console_unreadable",
                    "refused",
                    f"refused: {claimed}. The console has been claimed — commands emptied, "
                    "variables preserved — so this is one refusal and not one per cycle; rewrite "
                    "the whole file to submit again.\n",
                )
            ]
        if claimed and not all(isinstance(item, str) for item in claimed):
            # "One malformed element refuses the whole batch. If any element of `commands` is not a
            # string, none of it runs, and exactly one result file records that. Partial execution
            # of a malformed batch is worse than none."
            kinds = ", ".join(sorted({type(item).__name__ for item in claimed}))
            return [
                self._verdict(
                    window,
                    "malformed_batch",
                    "refused",
                    "refused: the whole batch. It contained a non-string element "
                    f"({kinds}), so none of its {len(claimed)} command(s) ran. Exactly one result "
                    "records this, which is the contract's requirement and not a convenience: "
                    "partial execution of a malformed batch is worse than none.\n",
                )
            ]
        if len(claimed) > self.max_batch:
            return [
                self._verdict(
                    window,
                    "batch_over_cap",
                    "refused",
                    f"refused: the whole batch. It held {len(claimed)} command(s) and this "
                    f"executive's cap is {self.max_batch} per window per cycle (`--max-batch`, an "
                    "operator ceiling a window cannot raise — ADR 0001 choice B). None of it ran, "
                    "by the same rule as a malformed batch: one result, and a window that floods "
                    "fills only its own output.\n",
                )
            ]
        return [self.validate(window, command) for command in claimed]

    def _settle(self, window: Window) -> list[Verdict]:
        """Run every deferral of this window that has come due, and give each its own result.

        A due command is **revalidated** through the whole chain — phase, gate, interlocks, dwell,
        arguments, conflict — at the moment of effect, which is §9 check 9 and the reason the
        acceptance-time answer is not reused. It **expires** if it has waited longer than its own
        `maximum_queue_age_s`, in simulated seconds: `(tick - accepted_tick) * dt`, so the poll
        interval no longer decides whether a command's intent has gone stale. And it **reports in a
        file of its own**, because a command that takes longer than one cycle reports when it is done.
        """
        verdicts: list[Verdict] = []
        still_waiting: list[dict[str, Any]] = []
        for entry in window.deferred:
            if self.tick < int(entry.get("due_tick", 0)):
                still_waiting.append(entry)
                continue
            verb = str(entry.get("verb"))
            command = str(entry.get("command", verb))
            accepted_tick = int(entry.get("accepted_tick", self.tick))
            age = (self.tick - accepted_tick) * self.dt
            limit = entry.get("maximum_queue_age_s")
            if isinstance(limit, (int, float)) and age > float(limit):
                verdicts.append(
                    self._verdict(
                        window,
                        command,
                        "refused",
                        f"refused: EXPIRED. {verb!r} was accepted at tick {accepted_tick} and has "
                        f"waited {age:.1f} s of simulated time, past its own maximum_queue_age_s of "
                        f"{limit:g}. V-02 makes `EXPIRED` a refusal reached from REVALIDATING, so "
                        "the command is re-checked when it is due and it is *this* check that fires "
                        "first — a command whose intent has gone stale is declined rather than run "
                        "late.\n",
                        verb=verb,
                        settled=True,
                    )
                )
                continue
            verdict = self.validate(window, command, settling=entry)
            verdicts.append(verdict)
        window.deferred = still_waiting
        return verdicts

    @staticmethod
    def parse_arguments(text: str) -> dict[str, str]:
        """`key=value` pairs from a command line, which is the form the contract's examples use.

        The executive deliberately does not validate arguments — that is the verb's
        `argument_schema` and the plant's business — so this returns whatever it finds and the
        callers that need a field say so themselves when it is missing.
        """
        arguments: dict[str, str] = {}
        for token in text.split()[1:]:
            if "=" in token:
                key, _, value = token.partition("=")
                arguments[key] = value
        return arguments

    def conflict_domains(self, spec: dict[str, Any], arguments: dict[str, str]) -> list[str]:
        """The domain a command collides in: its verb's `conflict_domain`, instantiated.

        Most domains are plain names; a few are templates — `prop.<engine>.run` is one domain per
        engine. A placeholder names the argument it varies over, so the command's own argument fills
        it where it is given; where it is not, every instantiation is claimed, because a command
        that does not say which engine it means collides with all of them. The expansion rule is
        `gate_instantiations`', so the published gate variables and the conflict domains cannot
        disagree about how a template is read.
        """
        declared = spec.get("conflict_domain")
        if not declared:
            return []
        template = str(declared)
        for name in re.findall(r"<([^>]+)>", template):
            if name in arguments:
                template = template.replace(f"<{name}>", str(arguments[name]), 1)
        if "<" not in template:
            return [template]
        try:
            return gate_instantiations(template, spec.get("argument_schema") or {}, str(spec.get("verb")))
        except Unconfigured:
            # A domain that cannot be expanded is a domain nothing can collide in, which is worse
            # than a wrong one, so it is claimed under its literal text and the linter's business is
            # to refuse the template rather than this file's to guess.
            return [template]

    def value_of(self, values: dict[str, Any], state: Any) -> list[str]:
        """The current value(s) of a state, as text, wherever the map keeps them."""
        if state.node == "internal":
            value = (values.get("internal") or {}).get(state.id)
        else:
            value = values.get(state.node)
        if isinstance(value, dict):
            return [str(v) for v in value.values()]
        return [] if value is None else [str(value)]

    def dwell_refusal(self, verb: str, staged: dict[str, Any] | None) -> str | None:
        """The minimum-dwell guard, in simulated seconds, evaluated at the moment of effect.

        `min_on_s` is how long the state must hold a value before it may change; `min_off_s` is
        how long it must stay away from a value before it may return to it. Both are measured from
        the last change on the executive's clock — `tick * tick_us + offset_us`, which cannot jump
        and does not care how long a fleet thought — and a state that has never changed has no
        floor, because the vehicle starts with its machine where the configuration put it.
        """
        now_us = self.tick * self.tick_us
        for state, min_on_s, min_off_s in command_dwell(self.world, verb):
            last = self.dwell.get(state.id)
            if last is None:
                continue
            if min_on_s is None or min_off_s is None:
                return (
                    f"refused: GUARD OWED. {verb!r} moves {state.id!r}, whose dwell is "
                    "UNCONFIGURED — so the vehicle cannot say how long that mode must be held "
                    "before it may be commanded again, and a command it cannot guard is a command "
                    "it must not accept. An owed dwell must stay visible until its command "
                    "policy or hardware path is resolved.\n"
                )
            held = (now_us - int(last["changed_at_us"])) / 1_000_000
            if held < min_on_s:
                return (
                    f"refused: DWELL. {verb!r} moves {state.id!r}, which was last changed "
                    f"{held:.1f} s ago and must hold a value for {min_on_s:g} s before it may "
                    "change. This is the guard that makes a commanded state machine a machine "
                    "rather than a relay — `check_domain` refuses a hysteresis band on one for "
                    "the same reason: a band answers 'has the quantity crossed', and a command "
                    "does not cross anything.\n"
                )
            was = last.get("was")
            if was is not None and min_off_s > 0 and staged is not None:
                away = (now_us - int(was["left_at_us"])) / 1_000_000
                becomes = self.value_of({**self.truth, **staged}, state)
                if becomes == was["value"] and away < min_off_s:
                    return (
                        f"refused: DWELL. {verb!r} would return {state.id!r} to a value it left "
                        f"{away:.1f} s ago, and it must stay away for {min_off_s:g} s before it "
                        "may go back. `propulsion.sps_state`'s five seconds are the case this "
                        "exists for: 'a five-second floor between burns is what keeps a fleet from "
                        "spending its restart budget in a minute'.\n"
                    )
        return None

    def interlock_reason(self, name: str) -> str:
        """Why one declared interlock cannot be evaluated this tick, from the configuration.

        The chain is the registry's own: the interlock names a threshold (`profiles.yaml`), the
        threshold watches a point, the point is a reading of a state, and the state either has a
        value this tick or does not. Threshold evaluation against the comparator is WP05's in every
        case; the sentence says which link is missing *first*, because that is what an implementer
        reads. `tie_dv_limit` → `power.dc_bus_a_v` → `bus_a_v`, an `algebraic` state with no rule, is
        the case the ADR names.
        """
        threshold = None
        bare = name.split(".", 1)[1] if "." in name else name
        for document, content in sorted(self.world.documents.items()):
            if not document.endswith("profiles.yaml") or not isinstance(content, dict):
                continue
            for row in content.get("thresholds") or []:
                if isinstance(row, dict) and str(row.get("id")) in (name, bare):
                    threshold = row
                    break
            if threshold is not None:
                break
        if threshold is None:
            return f"`{name}` resolves to no threshold this executive can read"
        point = str(threshold.get("point") or "")
        row = self.world.points.get(point)
        if row is None:
            return (
                f"`{name}` watches `{point}`, which is not a channel the frame publishes — a "
                "template family or an unregistered point — so there is no reading to compare"
            )
        source = str(row.get("from") or "")
        state = next((s for s in self.world.states if s.id == source), None)
        if state is None:
            return (
                f"`{name}` watches `{point}`, read from `{source}`, which is not a state the plant "
                "advances"
            )
        level = state_level(self.truth, state)
        if level is None:
            if state.method == "algebraic":
                why = "its algebraic rule is UNCONFIGURED"
            elif state.owed:
                why = f"it owes {', '.join(state.owed)}"
            else:
                why = "no tick has produced it"
            return (
                f"`{name}` watches `{point}`, read from `{source}` ({state.method}), which has no "
                f"value this tick: {why}"
            )
        return (
            f"`{name}` watches `{point}`, read from `{source}`, which holds {level!r}, but comparing "
            "it against the threshold's band and dwell is not implemented in this slice (WP05)"
        )

    def validate(self, window: Window, command: str, settling: dict[str, Any] | None = None) -> Verdict:
        """The verdict for one line, in the chain's order; the first refusal wins.

        Empty; unknown verb; phase, gate and tripped interlock (the capability snapshot, with this
        window's closed gates); interlocks the executive cannot evaluate (ADR choice C); dwell;
        argument resolution; conflict. Everything this refuses, it refuses by naming what refused
        it, because that is the property §9's checks 4 and 5 test. A verdict carries an `Effect`
        only when the command stages a state; the plant's `step` is the only thing that applies it.
        """
        text = command.strip()
        verb = text.split()[0] if text else ""

        def refused(body: str, state: str = "refused", **extra: Any) -> Verdict:
            if settling is not None and state == "refused":
                body = (
                    f"refused: INHIBITED. {verb!r} was valid when it was accepted at tick "
                    f"{settling.get('accepted_tick')} and is not valid now that it is due at tick "
                    f"{self.tick} — validity at admission is not validity at effect, which is the "
                    "whole reason V-02 calls the state REVALIDATING (contract §9 check 9). The "
                    f"re-check said: {body}"
                )
                extra.setdefault("settled", True)
            return self._verdict(window, command, state, body, verb=verb or None, **extra)

        if not text:
            return refused("refused: empty command. There is no verb here to resolve.\n")
        spec = self.world.verbs.get(verb)
        if spec is None:
            return refused(
                f"refused: unknown verb {verb!r}. This vehicle's vocabulary is closed and is "
                f"published in HELP.md and in state.json's `available_commands`; {verb!r} is not "
                "in it. No effect, no spend, no state change.\n"
            )
        row = next((r for r in window.capability() if r["verb"] == verb), None)
        if row is None:
            return refused(f"refused: {verb!r} is registered and could not be resolved.\n")
        if not row["available"]:
            return refused(
                f"refused: {verb!r} is closed. {row['availability_reason']}. The gate variable is "
                f"published in state.json.variables as {row['gate_variables'][0]!r}.\n"
            )
        arguments = self.parse_arguments(text)

        # The two-step verbs. `execute_event` checks its token *before* the interlock rule, so a
        # fleet is told "not armed" rather than "unevaluated" when it has no authority at all — and
        # the interlock refusal that follows does not consume a token it did not use.
        if verb == "execute_event":
            event = str(arguments.get("event", ""))
            offered = str(arguments.get("arm_token", ""))
            outstanding = window.arms.get(event)
            if outstanding is None:
                return refused(
                    f"refused: {verb!r} fires {event!r}, which is not armed in this window. This is "
                    "the arm-then-commit pattern the one-way event taxonomy is built on "
                    "(domains/structure/components.yaml#one_way_events): a pyro, an undocking and "
                    "a staging are irreversible, so the arming step is what separates a decision "
                    "from an accident. A token is bound to the window that armed it; nothing "
                    "written into pending.json is read back as one. Nothing has been fired.\n"
                )
            if not offered or offered != outstanding:
                return refused(
                    f"refused: {verb!r} offered arm_token {offered!r}, which is not the outstanding "
                    f"token for {event!r} in this window. A token is bound to one event and one "
                    "arming; a token for another event, or one already consumed, is not authority "
                    "to fire this one. Nothing has been fired.\n"
                )

        interlocks = spec.get("interlocks")
        declared = [str(i) for i in interlocks] if isinstance(interlocks, list) else []
        if declared:
            reasons = "\n".join(f"  - {self.interlock_reason(name)}" for name in declared)
            return refused(
                f"refused: INTERLOCK UNEVALUATED. {verb!r} declares {len(declared)} interlock(s) "
                "and this executive cannot evaluate them, so it must not accept the command "
                "(ADR 0001 choice C: a command it cannot guard is a command it must not accept — "
                f"the same rule an owed dwell is refused by):\n{reasons}\n"
                "Threshold evaluation is WP05's; a point with no value is owed by the model that "
                "produces it. Nothing has been staged"
                + (" and no arm token has been consumed" if verb == "execute_event" else "")
                + ".\n"
            )

        # The effect is resolved on a copy of the truth *before* anything is stamped, so `step`
        # never raises for an argument the corpus has not answered, and the dwell's return-to-value
        # half can see what the command would make the state.
        staged: dict[str, Any] | None = None
        unconfigured: Unconfigured | None = None
        if _stages_a_state(self.world, verb):
            probe = {**self.truth, "internal": dict(self.truth.get("internal") or {})}
            try:
                staged = apply_command(self.world, probe, verb, arguments)
            except Unconfigured as exc:
                unconfigured = exc
        guard = self.dwell_refusal(verb, staged)
        if guard is not None:
            return refused(guard)
        if unconfigured is not None:
            return refused(
                f"refused: NOT IMPLEMENTED. {verb!r} is a valid command and the vehicle cannot "
                f"apply it yet: {unconfigured}. The declaration is complete and the value is not — "
                "the corpus says which state this command moves and what each of its argument's "
                "values becomes, and what it does not yet say is what one of those values *is*.\n"
            )

        # The conflict policy, checked *after* everything that could refuse the command on its own:
        # `:578` says "first **valid** command", and a refusal that claimed the domain would let a
        # closed gate block a working command behind it.
        domains = self.conflict_domains(spec, arguments)
        for domain in domains:
            winner = self.claimed.get(domain)
            if winner is None:
                continue
            if winner.command.strip() == text:
                return refused(
                    f"accepted: DUPLICATE. {verb!r} is the same text as receipt #{winner.receipt} "
                    f"from window {winner.window!r}, the first valid command in conflict domain "
                    f"{domain!r} this tick, so it is accepted as a duplicate of it and stages "
                    "nothing further: a command takes effect at most once per tick, and a repeated "
                    "line is a repeated command rather than a second effect.\n",
                    "duplicate",
                    arguments=arguments,
                )
            return refused(
                f"refused: CONFLICT_SUPERSEDED. {verb!r} is valid but {winner.verb!r} from window "
                f"{winner.window!r} (receipt #{winner.receipt}) was the first valid command in "
                f"conflict domain {domain!r} this tick, and first valid wins (apollo_diode.md:578). "
                "Nothing is silently discarded — apollo:579 is explicit that the loser must be told, "
                "which is why this is a result file and not a dropped line. Re-issue next tick if "
                "the effect is still wanted.\n",
                "superseded",
                arguments=arguments,
            )

        if verb == "arm_event":
            # Reached only once `arm_event`'s own interlocks can be evaluated (WP05). The token is
            # bound to this window and this event, minted from the window's identity, and it is
            # never read back from any file.
            event = str(arguments.get("event", ""))
            if not event:
                return refused(f"refused: {verb!r} names no event, so there is nothing to arm.\n")
            self.receipt += 1
            token = hashlib.sha256(
                f"{window.boot_id}:{window.slug}:{event}:{self.tick}:{self.receipt}".encode()
            ).hexdigest()[:16]
            window.arms[event] = token
            window.accepted += 1
            return Verdict(
                command,
                "accepted",
                f"accepted: {verb!r} armed {event!r} for window {window.slug!r}. Token: {token}\n"
                "This is the only result on the vehicle that carries a token, and it is bound to "
                "this window and this event: `execute_event` will refuse a token minted for a "
                "different one, from a different window, or already consumed. Arming does nothing "
                "physical — every guard is evaluated at the moment of effect, not now.\n",
                self.receipt,
                self.tick,
                window.slug,
                verb=verb,
                arguments=arguments,
            )

        verdict = self._verdict(window, command, "accepted", "", verb=verb, arguments=arguments)
        for domain in domains:
            self.claimed.setdefault(domain, verdict)
        window.accepted += 1

        if row["deferrable"] and settling is None:
            # §9 check 9's whole content is *when* the re-check happens — "a deferred command is
            # re-checked when it is due, not when it was scheduled" — and the contract is equally
            # clear that the result arrives later. So the deferral is a live queue entry in ticks,
            # and `_settle` runs the whole chain again when it is due.
            due = self.tick + 1
            window.deferred.append(
                {
                    "verb": verb,
                    "command": text,
                    "accepted_tick": self.tick,
                    "due_tick": due,
                    "maximum_queue_age_s": spec.get("maximum_queue_age_s"),
                    "receipt": verdict.receipt,
                }
            )
            verdict.state = "deferred"
            verdict.body = (
                f"accepted: {verb!r} deferrable. Recorded at tick {self.tick}, due at tick {due}; "
                "it will be re-checked against the phase, its gate, its interlocks, its dwell and "
                "its conflict domain when it is due rather than now (contract §9 check 9), and it "
                f"expires if it is still waiting after {spec.get('maximum_queue_age_s')} s of "
                "simulated time (V-02's `EXPIRED`). The result arrives as its own file when it "
                "settles, because a command that takes longer than one cycle reports when it is "
                "done.\n"
            )
            return verdict

        if settling is not None:
            verdict.state = "settled"
            verdict.settled = True
            age = (self.tick - int(settling.get("accepted_tick", self.tick))) * self.dt
            verdict.body = (
                f"settled: {verb!r} at tick {self.tick}, {age:.1f} s after acceptance at tick "
                f"{settling.get('accepted_tick')}, re-checked against the phase, its gate, its "
                "interlocks, its dwell and its conflict domain at the moment of effect rather than "
                "at the moment it was scheduled. "
            )
        else:
            verdict.body = (
                f"accepted: {verb!r}. Authority {spec.get('authority')}, phase {self.phase}, gate "
                f"{row['gate_variables'][0]!r} open, no interlocks declared. "
            )
        if staged is None:
            verdict.body += (
                f"No physical state staged: {verb!r} reads, configures or reports, so the plant's "
                "value map is unchanged by it and the tick carried on.\n"
            )
            return verdict
        verdict.effect = Effect(0, verb, arguments)
        return verdict

    def _report_effect(self, verdict: Verdict, before: dict[str, Any], after: dict[str, Any]) -> str:
        """What the step made of the command's own target states, written after the step.

        The sentence names the states the command declares it moves (`command_targets`) and their
        values in the stepped truth — the command's own declared effect, not hidden truth beyond
        it. The wording stays the old console's ("Changed: bus_tie=closed") so readers can follow.
        The dwell's clock is written only where a value actually moved: a command that set what was
        already set has changed nothing and must not restart the floor.
        """
        verb = str(verdict.verb)
        changed: list[str] = []
        for state in command_targets(self.world, verb):
            old = self.value_of(before, state)
            new = self.value_of(after, state)
            if old == new:
                continue
            level = state_level(after, state)
            if state.node == "internal":
                changed.append(f"internal:{state.id}={level}")
            else:
                changed.append(f"{state.node}={level}")
        if changed:
            now_us = verdict.tick * self.tick_us + verdict.effect.offset_us
            for state, _, _ in command_dwell(self.world, verb):
                previous = self.dwell.get(state.id, {}).get("value")
                self.dwell[state.id] = {
                    "changed_at_us": now_us,
                    "value": self.value_of(after, state),
                    "was": {"value": previous, "left_at_us": now_us} if previous is not None else None,
                }
            return (
                f"succeeded: {verb!r} applied at tick {verdict.tick}, offset 0 µs. "
                f"Changed: {', '.join(changed)}.\n"
            )
        return (
            f"succeeded: {verb!r} applied at tick {verdict.tick}, offset 0 µs. No value changed: "
            "the states it moves already held what it sets, which is what `idempotent: true` "
            "means.\n"
        )


class Window:
    """One `<slug>` directory: ingress, gate preferences, results, deferrals, ring. No physics.

    A window is created by `Executive.attach` and never on its own, and everything it knows about
    the vehicle it reads from the executive at publication time. What it owns is the fleet-facing
    half of the contract for one principal: the claim, the result files, the variables the agent
    may lower, the deferral queue, the arm tokens bound to it, and the ring's slot count.
    """

    def __init__(self, executive: Executive, root: Path, slug: str, *, ring_slots: int) -> None:
        self.executive = executive
        self.root = root
        self.slug = slug
        # **The ring's slot count, which the contract demands and nothing bounded.**
        # `docs/diode-contract.md:183-189`: "**Publish a ring, not a snapshot.** ... A bonded ring of
        # recent frames with a **fixed slot count** is self-describing about its own cadence and its
        # own losses". `presentation.yaml#ring` declares the cadence *structure* and deliberately
        # declines to set the count — "a slot count is a memory decision" — so the run declares it.
        self.ring_slots = max(1, int(ring_slots))
        self.console = root / "console.json"
        self.output = root / "output"
        self.telemetry = root / "telemetry"
        self.state = root / "state.json"
        self.help_file = root / "HELP.md"
        self.readme = root / "README.md"
        self.pending = root / "pending.json"
        self.variables: dict[str, Any] = {}
        self.deferred: list[dict[str, Any]] = []
        # Outstanding arm tokens, by event, bound to this window. Never read back from a file.
        self.arms: dict[str, str] = {}
        self.seq = 0
        self.boot_id = uuid.uuid4().hex
        self.started = utc_now()
        self.accepted = 0

    # -- setup ------------------------------------------------------------------------------
    def prepare(self) -> None:
        """Create the window and write its unbound record. Nothing physical is published yet.

        `console.json` is written last, because it is the file a reader treats as "the vehicle is
        here": a window with the console and not yet the mirror is a vehicle that appears to publish
        nothing. The record says `world_id: null` and `ticks: 0` — the first cycle to tick this
        window binds it — and that is also exactly what `--init` leaves behind.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        self.output.mkdir(exist_ok=True)
        self.telemetry.mkdir(exist_ok=True)
        write_text_atomic(self.readme, self.executive.readme_text)
        write_text_atomic(self.help_file, self.executive.help_text)
        write_json_atomic(self.state, self.mirror())
        write_json_atomic(self.pending, {
            "pending": [],
            "ticks": 0,
            "seq": 0,
            "scenario": self.executive.scenario,
            "seed": self.executive.seed,
            "ring_slots": self.ring_slots,
            "world_id": None,
        })
        if not self.console.exists():
            write_json_atomic(self.console, {"commands": [], "variables": {}})

    # -- the claim --------------------------------------------------------------------------
    def claim(self) -> list[Any] | str:
        """Read the console and clear it, *before* acting on anything.

        The contract is unambiguous about the order: "**Intake is destructive and atomic.** Each
        cycle the vehicle reads the file, then rewrites it with `commands` emptied and `variables`
        preserved... **Clear before you act.**" So the rewrite happens here and the commands are
        returned for the executive to validate afterwards. A console that is not one — absent,
        oversized, invalid, not an object — is *also* claimed, with the last known variables, and
        the reason is returned for the one refusal result (ADR 0001).
        """
        payload, problem = read_ingress(self.console)
        if payload is None:
            write_json_atomic(self.console, {"commands": [], "variables": self.variables})
            return str(problem)
        commands = payload.get("commands")
        variables = payload.get("variables")
        # `variables` is persistent and the vehicle "never clears it". It is also the probe's
        # marker channel: the probe submits `probe_marker` and then checks it survived the claim.
        if isinstance(variables, dict):
            self.variables.update(variables)
        write_json_atomic(self.console, {"commands": [], "variables": self.variables})
        if commands is None:
            return []
        if not isinstance(commands, list):
            return (
                f"console.json's `commands` is a JSON {type(commands).__name__}, not a list, so "
                "there is no batch here to run"
            )
        return commands

    def closed_gates(self) -> set[str]:
        return {str(name) for name, value in self.variables.items() if not value}

    def capability(self) -> list[dict[str, Any]]:
        """The capability snapshot as this window sees it: its own closed gates, the operator's trips."""
        return capability_snapshot(
            self.executive.world,
            phase=self.executive.phase,
            closed_gates=self.closed_gates(),
            tripped_interlocks=self.executive.tripped,
        )

    # -- results ----------------------------------------------------------------------------
    def write_result(self, command: str, body: str) -> Path:
        """One file per command, refusals included, and nothing ever overwritten.

        The name is `<stamp>_<slug>_<sanitised command>.txt`. A filename collision inside one
        microsecond is possible when a batch repeats a command, and the contract forbids
        overwriting — so the second one gets a suffix rather than replacing the first.
        """
        moment = utc_now()
        name = f"{stamp(moment)}_{self.slug}_{sanitise(command)}.txt"
        path = self.output / name
        counter = 1
        while path.exists():
            path = self.output / f"{stamp(moment)}_{self.slug}_{sanitise(command)}_{counter}.txt"
            counter += 1
        write_text_atomic(path, body)
        return path

    def receipt(self, verdict: Verdict) -> str:
        """The block every result ends with: where in the one world this result was decided."""
        offset_us = verdict.effect.offset_us if verdict.effect is not None else 0
        return (
            f"receipt: world={self.executive.world_id} seq={verdict.receipt} window={self.slug} "
            f"tick={verdict.tick} offset_us={offset_us} state={verdict.state}\n"
        )

    # -- publication ------------------------------------------------------------------------
    def publish(self, verdicts: list[Verdict]) -> list[Path]:
        """Results, `state.json`, `HELP.md`, `README.md`, `pending.json` and one frame. Every cycle.

        `state.json` is rewritten "whether or not anything was submitted", which is what makes the
        probe's `check_state_is_a_mirror` meaningful. The generated files are rewritten from the
        cached text for the same reason: a hand-edit lasts until the next cycle.
        """
        written = [
            self.write_result(verdict.command, verdict.body + self.receipt(verdict))
            for verdict in sorted(verdicts, key=lambda v: v.receipt)
        ]
        # The frame first, so the mirror's ring accounting describes the directory as it is: the old
        # console wrote the mirror before the frame and its `newest_seq` ran one behind the ring.
        self.write_frame()
        write_json_atomic(self.state, self.mirror())
        write_text_atomic(self.help_file, self.executive.help_text)
        write_text_atomic(self.readme, self.executive.readme_text)
        write_json_atomic(
            self.pending,
            {
                "pending": self.deferred,
                "ticks": self.executive.tick,
                "seq": self.seq,
                "boot_id": self.boot_id,
                # The window's identity lives in its own record rather than in the mirror, for the
                # reason `ticks` does: `state.json` is published state and the contract says it is
                # never read back as input.
                "scenario": self.executive.scenario,
                "seed": self.executive.seed,
                # The ring's own accounting: what it is bounded to, and what it has lost.
                "ring_slots": self.ring_slots,
                "ring_losses": self.ring_losses(),
                # The binding (ADR 0001 choice D): which world's physics this window has shown. A
                # later executive refuses a window that names another.
                "world_id": self.executive.world_id,
            },
        )
        return written

    def mirror(self) -> dict[str, Any]:
        """`state.json`: the capability snapshot, the gates, the budget, the ring, the executive."""
        executive = self.executive
        rows = self.capability()
        variables = {name: True for row in rows for name in row["gate_variables"]}
        # The window's own variables win where they name a published gate, because the console is
        # the fleet's hand and the registry is only the default. §9 check 8's direction is that the
        # console may lower and never raise, so a gate the window has closed stays closed.
        for name, value in self.variables.items():
            if name in variables:
                variables[name] = bool(value)
        return {
            "published_at": utc_now().isoformat(),
            "available_commands": [row["verb"] for row in rows if row["available"]],
            "variables": variables,
            "budget": {
                "used_this_window": self.accepted,
                "limit_per_window": int(self.variables.get("allowance", 120)),
                "window_seconds": 3600,
                "oldest_expires_in_seconds": 3600 - int((utc_now() - self.started).total_seconds()),
            },
            "queue_depth": len(self.deferred),
            # **The ring, described rather than only published**: the bound, what is held, what fell
            # out of the far end, and `held + losses` is what the window has produced.
            "ring": {
                "slots": self.ring_slots,
                "held": len(self.ring_frames()),
                "losses": self.ring_losses(),
                "newest_seq": max(0, self.seq - 1),
            },
            # `posture` is the *execution* machine's posture (`mission.yaml#postures`), which the
            # executive does not drive and which stays empty; `scenario` is the run's difficulty
            # identity. Two names because they are two things.
            "vehicle": {
                "phase": executive.phase,
                "posture": "",
                "scenario": executive.scenario,
                "abort_latched": False,
            },
            # Which world this window is a view of, and how far it has ticked. No truth, no lineage.
            "executive": {
                "world_id": executive.world_id if self.executive.tick > 0 else None,
                "tick": executive.tick,
                "windows": sorted({*executive.windows, self.slug}),
            },
            "capability": rows,
        }

    def ring_frames(self) -> list[Path]:
        """The frames the ring holds, oldest first. One scan, used by both the pruner and a reader."""
        return sorted(self.telemetry.glob("*.json"), key=lambda path: path.name)

    def ring_losses(self) -> int:
        """Frames this window has produced that the ring no longer holds — derived, not counted."""
        return max(0, self.seq - len(self.ring_frames()))

    def write_frame(self) -> None:
        """Append one frame from the stepped truth, and drop the ones that fall out of the far end.

        **The instrument between truth and this frame is the identity, until WP09.** `emit_frame`
        publishes the declared channels — never residuals, rings, the sentinel or fault plans — and
        every measurement channel carries the truth's own number with no noise, lag, quantisation or
        quality model; `quality` is empty. ADR 0001 records that substitution, and so does this.

        `NNN.json` is the *sequence* number, so a name is never reused and a reader can always tell
        a new frame from a rewritten one. The bound is a deletion rather than a wrap, for the same
        reason.
        """
        executive = self.executive
        met_s = executive.tick * executive.tick_us / 1_000_000
        frame = emit_frame(
            executive.world,
            tick=executive.tick,
            seq=self.seq,
            boot_id=self.boot_id,
            met_s=met_s,
            sensor_time_s=met_s,
            values=executive.truth,
            quality={},
            phase=executive.phase,
            vehicle="csm",
            state_revision=executive.tick,
        )
        write_json_atomic(self.telemetry / f"{self.seq:03d}.json", frame)
        self.seq += 1
        held = self.ring_frames()
        for stale in held[: max(0, len(held) - self.ring_slots)]:
            with contextlib.suppress(FileNotFoundError):
                stale.unlink()


# The old name, for readers of the round log: the class that used to own the physics.
Console = Window


def positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, not {value}")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dir", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--diode-dir", default=".scratch/diode")
    parser.add_argument(
        "--slug",
        action="append",
        default=None,
        help="a window to serve from this executive; repeatable, default `vehicle`",
    )
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
    parser.add_argument("--init", action="store_true", help="prepare the windows, bind nothing, stop")
    parser.add_argument(
        "--ring-slots",
        type=int,
        default=None,
        metavar="N",
        help="how many frames each telemetry ring holds. `docs/diode-contract.md:186` requires a "
        "fixed slot count and `presentation.yaml#ring` declines to set it — 'a slot count is a "
        "memory decision' — so the run declares it",
    )
    parser.add_argument(
        "--max-batch",
        type=positive_int,
        default=DEFAULT_MAX_BATCH,
        metavar="N",
        help="the most commands one window may submit in one cycle; a longer batch is refused "
        "whole with one result (ADR 0001 choice B)",
    )
    parser.add_argument(
        "--journal",
        default=None,
        metavar="PATH",
        help="append one JSON line per tick — lineage, state hash, effects, receipts — for the "
        "operator; refused inside --diode-dir",
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
        "(nominal, degraded, crisis). Recorded in the mirror and in each window's own record, and "
        "a prepared window keeps the one it recorded unless this names another",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="the run's master seed, for the fault streams `tools/faults.py` draws from. "
        "A scenario and a seed together are what make a run reproducible, and a prepared window "
        "keeps the pair it recorded unless this names another",
    )
    args = parser.parse_args(argv)
    slugs = list(args.slug or ["vehicle"])
    repeated = sorted({s for s in slugs if slugs.count(s) > 1})
    if repeated:
        sys.stderr.write(
            f"--slug names {repeated} more than once; a slug attaches to one executive once\n"
        )
        return 3

    try:
        world = load_world(Path(args.dir))
    except Exception as exc:  # noqa: BLE001 - the loader's refusals are the point
        sys.stderr.write(f"the configuration cannot be loaded: {exc}\n")
        return 3

    # **A scenario that names no posture is refused rather than accepted and ignored.** The list
    # comes from the mission file through the module that already consumes it, so there is one
    # declaration of what the scenarios are.
    try:
        postures = load_postures(Path(args.dir))
    except Exception as exc:  # noqa: BLE001 - a refusal is the answer here too
        sys.stderr.write(f"the scenario postures cannot be loaded: {exc}\n")
        return 3

    # ---- the run's identity, resolved once per window and required to agree ------------------
    #
    # A caller who names a value gets it. A caller who names nothing inherits whatever the window's
    # own record holds. A window with no record gets the declared default. Under one executive the
    # windows must then agree about scenario and seed, because they are one world (ADR 0001).
    diode_dir = Path(args.diode_dir)
    records = {slug: recorded_run(diode_dir / slug) for slug in slugs}
    resolved_scenario: dict[str, str] = {}
    resolved_seed: dict[str, int] = {}
    resolved_slots: dict[str, int] = {}
    for slug, recorded in records.items():
        window = diode_dir / slug
        recorded_scenario = recorded.get("scenario")
        if not isinstance(recorded_scenario, str) or not recorded_scenario:
            recorded_scenario = None
        scenario = resolve_remembered(args.scenario, recorded_scenario, DEFAULT_SCENARIO)
        if scenario not in postures:
            if args.scenario is not None:
                sys.stderr.write(
                    f"scenario {scenario!r} is not one of the vehicle's "
                    f"{len(postures)}: {sorted(postures)}\n"
                )
            else:
                sys.stderr.write(
                    f"the window at {window} records scenario {scenario!r}, which is not one of "
                    f"the vehicle's {len(postures)}: {sorted(postures)}. Name one that is, or "
                    f"point `--diode-dir` and `--slug` at another window\n"
                )
            return 3
        resolved_scenario[slug] = scenario
        recorded_seed = recorded.get("seed")
        if not isinstance(recorded_seed, int) or recorded_seed < 0:
            recorded_seed = None
        resolved_seed[slug] = resolve_remembered(args.seed, recorded_seed, DEFAULT_SEED)
        # **The ring is the one remembered value a restart may not re-bound.** Whatever bound the
        # frames on disk were written under is the bound the window keeps; a caller who names a
        # *different* one is told, instead of being handed the old one with no remark.
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
        resolved_slots[slug] = resolve_remembered(args.ring_slots, recorded_slots, DEFAULT_RING_SLOTS)
    if len(set(resolved_scenario.values())) > 1 or len(set(resolved_seed.values())) > 1:
        sys.stderr.write(
            "the windows disagree about the run's identity and one executive is one world: "
            + ", ".join(
                f"{slug} records scenario={resolved_scenario[slug]!r} seed={resolved_seed[slug]}"
                for slug in slugs
            )
            + ". Name --scenario and --seed, or serve the disagreeing windows from different "
            "directories\n"
        )
        return 3
    scenario = resolved_scenario[slugs[0]]
    seed = resolved_seed[slugs[0]]

    # **`--plan` answers "what will this run be" before it is run**, and it takes the *resolved*
    # pair, so `--plan` on a window recorded as `crisis` describes the crisis that window is in.
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

    # ---- one world per directory ------------------------------------------------------------
    #
    # The lock says *a* world is here; the binding in each window's record says *which*. A second
    # executive on the directory refuses to start, and so does an executive on a window that names
    # another world, live or not — starting a fresh world on an old window would silently reset its
    # physics, and restart continuity is WP08's (ADR 0001 choice D). A legacy window — the old
    # console's record, ticks and no world — is refused the same way.
    diode_dir.mkdir(parents=True, exist_ok=True)
    lock_path = diode_dir / LOCK_FILE
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            holder = os.read(lock_fd, 256).decode("utf-8", "replace").strip() or "unknown holder"
        except OSError:
            holder = "unknown holder"
        os.close(lock_fd)
        sys.stderr.write(
            f"another executive holds {diode_dir} ({holder}). One directory is one world "
            "(ADR 0001); stop it, or point --diode-dir at another directory\n"
        )
        return 3
    for slug, recorded in records.items():
        bound = recorded.get("world_id")
        ticks = recorded.get("ticks")
        if isinstance(bound, str) and bound:
            sys.stderr.write(
                f"the window at {diode_dir / slug} is bound to world {bound}, which is not this "
                "executive's. Starting a fresh world on it would silently reset its physics, and "
                "restart continuity is WP08's to define — clear or rename the window (ADR 0001 "
                "choice D)\n"
            )
            os.close(lock_fd)
            return 3
        if isinstance(ticks, int) and ticks > 0:
            sys.stderr.write(
                f"the window at {diode_dir / slug} is a legacy console window: it records "
                f"{ticks} tick(s) and no world_id, so its frames came from a world this executive "
                "cannot continue. Clear or rename it (ADR 0001 choice D)\n"
            )
            os.close(lock_fd)
            return 3

    journal = Path(args.journal) if args.journal else None
    try:
        executive = Executive(
            world,
            diode_dir,
            phase=args.phase,
            tripped_interlocks=set(args.closed_interlock),
            scenario=scenario,
            seed=seed,
            max_batch=args.max_batch,
            journal=journal,
        )
    except ValueError as exc:
        sys.stderr.write(f"{exc}\n")
        os.close(lock_fd)
        return 3
    os.ftruncate(lock_fd, 0)
    os.write(lock_fd, f"pid={os.getpid()} world={executive.world_id}\n".encode())
    for slug in slugs:
        executive.attach(slug, ring_slots=resolved_slots[slug])
    if args.init:
        for slug in slugs:
            print(f"initialised {diode_dir / slug} for slug {slug!r} at phase {args.phase!r}")
        # `--init` binds nothing: the lock is released and the records say `world_id: null`.
        os.close(lock_fd)
        return 0

    rings = sorted(set(resolved_slots.values()))
    ring = str(rings[0]) if len(rings) == 1 else ",".join(f"{s}:{resolved_slots[s]}" for s in slugs)
    print(
        f"[console] {diode_dir} slugs={','.join(slugs)} phase={args.phase} "
        f"scenario={executive.scenario} seed={executive.seed} ring={ring} "
        f"poll={args.poll}s cycles={args.cycles or 'until interrupted'} world={executive.world_id}",
        flush=True,
    )
    # `--cycles` counts the cycles **this invocation** runs; an executive always starts at tick 0.
    ran = 0
    try:
        while args.cycles == 0 or ran < args.cycles:
            ran += 1
            written = executive.cycle()
            if written:
                print(f"[console] tick {executive.tick}: {len(written)} result(s)", flush=True)
            time.sleep(args.poll)
    except KeyboardInterrupt:
        print(f"\n[console] stopped after {executive.tick} tick(s)", flush=True)
    finally:
        os.close(lock_fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
