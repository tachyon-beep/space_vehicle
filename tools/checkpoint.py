#!/usr/bin/env python3
"""The executive's private checkpoint: format v1, its writer, its reader, and the two generations.

`docs/decisions/0002-mission-clock-and-continuity.md` (ADR 0002) decides that a restart *resumes*
a world from a checkpoint the executive wrote for itself, never from anything an agent can write.
This module is child 1 of that record: the file format and nothing else. It does not wire a
resume into the executive (child 3), does not know where `--state-dir` is (child 2), and does not
write the per-cycle record (child 4). What it fixes is what a checkpoint *is*, so those children
can land as sections and flags rather than as formats.

**The file.** One header line, then the body. The header is a small JSON object — `format`,
`engine`, `git_commit`, `python`, `platform`, `tick_hz`, `world_id`, `tick`, `segments`, and the
two integrity fields `body_sha256` and `body_bytes` — so a reader that wants only the identity
(`scripts/status.py`, per ADR 0002 H) reads one line. The body is the state, encoded as sorted
compact JSON with `allow_nan=False` and a `default` that raises: the encoding is a function of the
value and of nothing else, so two equal states are two equal byte strings, a non-JSON value is a
refusal at capture time rather than a string that collides with an equal `str`, and a `NaN` in the
truth cannot be written into a file nothing can read. The body carries a copy of the identity the
header states, and the reader holds the two together: the hash covers the body (ADR 0002 I), so a
byte flipped in the header is caught by the copy the hash does cover. The one header field that
copy cannot protect is `format`, which is read before anything else because it says how to read
the rest.

**What the body carries** is ADR 0002 G's list, section by section: `identity`; `run` (scenario,
seed, phase, `phase_entry_seq`, the tripped interlocks, `--max-batch`, the allowance ceiling);
`clock` (`m`, `k`, `N`, the burst bound, the lag ceiling — the keys exist from this version, and
hold `null` until child 5 gives the executive the inputs, because a checkpoint missing a field the
engine needs is refused rather than defaulted, and a field that exists can be required later
without a format change); `executive` (the tick, every key of the truth — `__delay` rings,
`__residual` accumulators and `__shortfall` records included — the dwell, the lineage head, the
receipt counter); `rng` (reserved: today the lineage is the seed alone, and WP09 adds fields to
this section rather than a section); `segments` (one entry per boot, child 4's to write); and
`windows`, one row per slug with `ring_slots`, `seq`, `receipts`, `spend`, `deferred`, `arms` and
`published_tick`. `spend` is **opaque and versioned**: an object with an integer `version`, carried
and never read, so that E's answer (child 7) lands as a section. Until then a version-0 section
carries what today's window counts, `accepted`, so the one budget figure a window has is not lost.
A window's `boot_id` is deliberately *not* in the body: ADR 0002 L says a restart changes it.

**Capture and restore are pure functions over an executive-shaped object.** `capture_state` reads
the attributes `tools/console.py`'s `Executive` and `Window` have today and, for the fields later
children add, the attributes they will add under these names — `clock`, `rng`, `segments`,
`phase_entry_seq`, `allowance_ceiling` on the executive; `spend`, `published_tick` on a window —
writing `null` or the reserved default where the attribute does not exist yet. `restore_state`
sets the same attributes back, so an executive restored from a body captures to the body's own
bytes, which is the property child 3 wires a resume on. Restore refuses a phase that disagrees
with the executive's (the gate names were computed from it) and a set of attached windows that
is not the checkpoint's (adding a window to a live world is a decision, not a restore).

**The write** is ADR 0002 I's sequence, each step named in `STEPS`: write the bytes to a temporary
under a random name opened `O_CREAT | O_EXCL | O_NOFOLLOW` relative to the directory handle;
`fsync` it; rename the current generation to `checkpoint.prev.json`; rename the temporary over
`checkpoint.json`; `fsync` the directory. Every step is relative to a directory handle opened
without following a link, the discipline `console.py` uses for a window, and the writer cleans
nothing up on its way out — a process killed mid-write cleans nothing up either, so what the
tests observe after a simulated kill is what the disk would hold. A temporary a dead process left
is swept at the start of the next write. `os.replace` over a link replaces the link, not what it
pointed to.

**The read** distinguishes four refusals by class, each naming the check and the file, because the
operator reads the message and the chooser reads the class (ADR 0002 K): `CheckpointCorrupt` — a
link in the file's place, not a regular file, no header line, a header that is not an object, a
body whose length or SHA-256 disagrees with the header, a body that is not JSON or not the shape
above (a missing `rng` is this), or a header that disagrees with the body's identity copy;
`CheckpointIncompatible` — `format`, `engine`, `python`, `platform` or `tick_hz` is not this
engine's; `CheckpointForeign` — a verifying checkpoint of another `world_id` than the caller's;
`CheckpointAbsent` — no file. `choose_generation` is K2: the current generation if it verifies;
the previous one when the current is corrupt *or absent* (a kill between the two renames leaves
no current) and the previous verifies and its world matches; a refusal naming both files when both
fail; `None` when neither exists; and an **incompatible** current is refused at once and the
previous generation is never opened, because it was written by the same build.

**`engine`** is a SHA-256 over the bytes of `plant.corpus_files(root)` and of `tools/plant.py`,
`tools/console.py` and `tools/faults.py` — what the physics and the executive read — rather than
the git commit, which is recorded beside it and never checked: with the commit, a README round
would end every saved run (ADR 0002 I). This module is not in the hash on purpose: a change to how
a checkpoint is read is a change of `format`, not of engine.

    python3 tools/checkpoint.py --state-dir .scratch/state       # which generation verifies, and its header
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import json
import os
import platform
import re
import secrets
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from plant import corpus_files, load_world  # noqa: E402

FORMAT = "vehicle.checkpoint.v1"
CURRENT = "checkpoint.json"
PREVIOUS = "checkpoint.prev.json"
# The tools whose bytes are part of the engine identity, beside the corpus (ADR 0002 I).
ENGINE_TOOLS = ("tools/plant.py", "tools/console.py", "tools/faults.py")
# A checkpoint is a few hundred kilobytes today (ADR 0002's evidence section); a file past this
# bound is refused unread rather than loaded, the way every bounded read in `console.py` is.
MAX_CHECKPOINT_BYTES = 64 * 1024 * 1024
# The write sequence, in order, and the names the kill test knows them by.
STEPS = ("write", "fsync", "rename_previous", "rename_current", "fsync_directory")
TEMPORARY_PREFIX = ".checkpoint."
TEMPORARY_SUFFIX = ".tmp"
# The clock inputs ADR 0002 A–C and J name, as the keys the `clock` section always has.
CLOCK_INPUTS = ("m", "k", "N", "burst_bound", "lag_ceiling_s")
RNG_VERSION = 1
SPEND_PLACEHOLDER_VERSION = 0


# -- encoding ---------------------------------------------------------------------------------
def _refuse_non_json(value: Any) -> Any:
    raise TypeError(
        f"{type(value).__name__} is not a JSON value and cannot be checkpointed; a `default` that "
        "stringified it would collide with an equal str"
    )


def _no_json_constants(name: str) -> Any:
    raise ValueError(f"{name} is not a JSON value (RFC 8259)")


def _require_string_keys(value: Any, where: str) -> None:
    """`json.dumps` would write an int key as a string and the round trip would not be one."""
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{where} has a {type(key).__name__} key {key!r}; a checkpoint's keys are strings")
            _require_string_keys(item, f"{where}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            if isinstance(item, (dict, list, tuple)):
                _require_string_keys(item, f"{where}[{index}]")


def encode(payload: Any) -> bytes:
    """Sorted compact JSON, no `NaN`, no stringified foreign types, one trailing newline, UTF-8."""
    _require_string_keys(payload, "payload")
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False, default=_refuse_non_json)
    return (text + "\n").encode("utf-8")


def decode(raw: bytes) -> Any:
    return json.loads(raw.decode("utf-8"), parse_constant=_no_json_constants)


# -- identity -----------------------------------------------------------------------------------
def engine_identity(root: Path) -> str:
    """SHA-256 over the corpus and the three engine tools, each framed by its path and length."""
    root = Path(root)
    digest = hashlib.sha256()
    names = [path.relative_to(root).as_posix() for path in corpus_files(root)] + list(ENGINE_TOOLS)
    for name in names:
        path = root / name
        if not path.is_file():
            raise FileNotFoundError(f"{path} is part of the engine identity and is missing")
        data = path.read_bytes()
        digest.update(f"{name}\n{len(data)}\n".encode())
        digest.update(data)
    return digest.hexdigest()


def git_commit(root: Path) -> str | None:
    """The vehicle's commit, for the manifest; `None` where there is none (a fixture, no git)."""
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    sha = done.stdout.strip()
    return sha if done.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", sha) else None


@dataclass(frozen=True)
class Compatibility:
    """What a checkpoint must agree with to be read: ADR 0002 I's five fields."""

    engine: str
    python: str
    platform: str
    tick_hz: int | float
    format: str = FORMAT

    @classmethod
    def current(cls, world: Any) -> Compatibility:
        """This engine, on this interpreter, on this platform, at the mission's declared tick."""
        tick_hz = (world.documents.get("mission.yaml") or {}).get("tick_hz")
        return cls(
            engine=engine_identity(world.root),
            python=platform.python_version(),
            platform=platform.platform(),
            tick_hz=tick_hz,
        )


# -- capture and restore -----------------------------------------------------------------------
def _allowance_ceiling(executive: Any) -> int:
    ceiling = getattr(executive, "allowance_ceiling", None)
    if ceiling is None:
        # The operator's ceiling is a console constant today. Imported here and not at the top so
        # that `console` may import this module when child 3 wires the resume.
        from console import DEFAULT_ALLOWANCE

        ceiling = DEFAULT_ALLOWANCE
    return int(ceiling)


def _capture_window(window: Any) -> dict[str, Any]:
    spend = getattr(window, "spend", None)
    if spend is None:
        spend = {"version": SPEND_PLACEHOLDER_VERSION, "accepted": int(window.accepted)}
    return {
        "ring_slots": int(window.ring_slots),
        "seq": int(window.seq),
        "receipts": int(window.receipts),
        "spend": spend,
        "deferred": list(window.deferred),
        "arms": dict(window.arms),
        "published_tick": getattr(window, "published_tick", None),
    }


def capture_state(executive: Any, compat: Compatibility, *, git_commit: str | None = None) -> dict[str, Any]:
    """The body of a checkpoint of this executive, as a fresh dict that shares nothing with it."""
    tick = int(executive.tick)
    segments = list(getattr(executive, "segments", None) or [])
    clock = getattr(executive, "clock", None) or {}
    rng = getattr(executive, "rng", None)
    if rng is None:
        rng = {"version": RNG_VERSION, "master_seed": int(executive.seed), "streams": {}}
    body = {
        "identity": {
            "format": compat.format,
            "engine": compat.engine,
            "git_commit": git_commit,
            "python": compat.python,
            "platform": compat.platform,
            "tick_hz": compat.tick_hz,
            "world_id": executive.world_id,
            "tick": tick,
            "segments": [segment.get("segment") if isinstance(segment, dict) else segment for segment in segments],
        },
        "run": {
            "scenario": executive.scenario,
            "seed": int(executive.seed),
            "phase": executive.phase,
            "phase_entry_seq": int(getattr(executive, "phase_entry_seq", 0)),
            "tripped_interlocks": sorted(executive.tripped),
            "max_batch": int(executive.max_batch),
            "allowance_ceiling": _allowance_ceiling(executive),
        },
        "clock": {name: clock.get(name) for name in CLOCK_INPUTS},
        "executive": {
            "tick": tick,
            "truth": executive.truth,
            "dwell": executive.dwell,
            "lineage_head": executive.lineage_head,
            "receipt": int(executive.receipt),
        },
        "rng": rng,
        "segments": segments,
        "windows": {slug: _capture_window(window) for slug, window in executive.windows.items()},
    }
    # Through the encoding and back: the caller gets a copy that shares nothing with the live
    # object, and a value the format cannot carry is refused here, not at the write.
    return decode(encode(body))


def _restore_spend(window: Any, section: dict[str, Any]) -> None:
    restorer = getattr(window, "restore_spend", None)
    if callable(restorer):
        restorer(section)
        return
    if section.get("version") == SPEND_PLACEHOLDER_VERSION:
        window.accepted = int(section.get("accepted", 0))
        window.spend = None
        return
    window.spend = section


def restore_state(executive: Any, body: dict[str, Any]) -> None:
    """Set the executive and its attached windows to the body's state. Pure, and refuses by name."""
    body = decode(encode(body))
    problem = _structure_problem(body)
    if problem is not None:
        raise ValueError(f"the checkpoint body cannot be restored: {problem[0]} — {problem[1]}")
    named = set(body["windows"])
    attached = set(executive.windows)
    if named - attached:
        raise ValueError(
            f"the checkpoint names window(s) {sorted(named - attached)} that are not attached to this "
            "executive; attach every window the checkpoint names before restoring"
        )
    if attached - named:
        raise ValueError(
            f"window(s) {sorted(attached - named)} are attached and the checkpoint does not name them; "
            "adding a window to a live world is a decision, not a restore"
        )
    run = body["run"]
    if run["phase"] != executive.phase:
        raise ValueError(
            f"the checkpoint was taken at phase {run['phase']!r} and this executive was constructed at "
            f"{executive.phase!r}; the gate names are computed from the phase, so construct it at the "
            "checkpoint's"
        )
    state = body["executive"]
    executive.world_id = body["identity"]["world_id"]
    executive.scenario = run["scenario"]
    executive.seed = run["seed"]
    executive.phase_entry_seq = run["phase_entry_seq"]
    executive.tripped = set(run["tripped_interlocks"])
    executive.max_batch = run["max_batch"]
    executive.allowance_ceiling = run["allowance_ceiling"]
    executive.clock = dict(body["clock"])
    executive.rng = body["rng"]
    executive.segments = body["segments"]
    executive.tick = state["tick"]
    executive.truth = state["truth"]
    executive.dwell = state["dwell"]
    executive.lineage_head = state["lineage_head"]
    lineage = getattr(executive, "lineage", None)
    if lineage is not None:
        lineage.clear()
        lineage.append(state["lineage_head"])
    executive.receipt = state["receipt"]
    if hasattr(executive, "claimed"):
        executive.claimed = {}
    for slug, row in body["windows"].items():
        window = executive.windows[slug]
        window.ring_slots = row["ring_slots"]
        window.seq = row["seq"]
        window.receipts = row["receipts"]
        window.deferred = list(row["deferred"])
        window.arms = dict(row["arms"])
        window.published_tick = row["published_tick"]
        _restore_spend(window, row["spend"])


# -- the shape of a body -------------------------------------------------------------------------
_NULL = type(None)
_SECTIONS: dict[str, type] = {
    "identity": dict,
    "run": dict,
    "clock": dict,
    "executive": dict,
    "rng": dict,
    "segments": list,
    "windows": dict,
}
_KEYS: dict[str, dict[str, Any]] = {
    "identity": {
        "format": str,
        "engine": str,
        "git_commit": (str, _NULL),
        "python": str,
        "platform": str,
        "tick_hz": (int, float),
        "world_id": str,
        "tick": int,
        "segments": list,
    },
    "run": {
        "scenario": str,
        "seed": int,
        "phase": str,
        "phase_entry_seq": int,
        "tripped_interlocks": list,
        "max_batch": int,
        "allowance_ceiling": int,
    },
    "clock": dict.fromkeys(CLOCK_INPUTS, object),
    "executive": {"tick": int, "truth": dict, "dwell": dict, "lineage_head": str, "receipt": int},
    "rng": {"version": int},
}
_WINDOW_KEYS: dict[str, Any] = {
    "ring_slots": int,
    "seq": int,
    "receipts": int,
    "spend": dict,
    "deferred": list,
    "arms": dict,
    "published_tick": (int, _NULL),
}


def _is(value: Any, kind: Any) -> bool:
    if kind is object:
        return True
    kinds = kind if isinstance(kind, tuple) else (kind,)
    if isinstance(value, bool) and bool not in kinds:
        return False
    return isinstance(value, kinds)


def _kind_name(kind: Any) -> str:
    kinds = kind if isinstance(kind, tuple) else (kind,)
    return " or ".join("null" if k is _NULL else k.__name__ for k in kinds)


def _structure_problem(body: Any) -> tuple[str, str] | None:
    """`(check, sentence)` for the first thing about the body's shape the format cannot vouch for."""
    if not isinstance(body, dict):
        return "body", f"the body is a JSON {type(body).__name__}, not an object"
    for section, kind in _SECTIONS.items():
        if section not in body:
            return section, f"the body has no `{section}` section; a field the engine needs is never defaulted"
        if not _is(body[section], kind):
            return section, f"`{section}` is a JSON {type(body[section]).__name__}, not {_kind_name(kind)}"
    for section, keys in _KEYS.items():
        for key, kind in keys.items():
            if key not in body[section]:
                return f"{section}.{key}", f"`{section}` has no `{key}`; a field the engine needs is never defaulted"
            if not _is(body[section][key], kind):
                return f"{section}.{key}", f"`{section}.{key}` is a JSON {type(body[section][key]).__name__}, not {_kind_name(kind)}"
    if body["identity"]["tick"] != body["executive"]["tick"]:
        return "executive.tick", "the identity and the executive disagree about the tick"
    for slug, row in body["windows"].items():
        if not isinstance(row, dict):
            return f"windows.{slug}", f"the window's row is a JSON {type(row).__name__}, not an object"
        for key, kind in _WINDOW_KEYS.items():
            if key not in row:
                return f"windows.{slug}.{key}", f"the window has no `{key}`; a field the engine needs is never defaulted"
            if not _is(row[key], kind):
                return f"windows.{slug}.{key}", f"`{key}` is a JSON {type(row[key]).__name__}, not {_kind_name(kind)}"
        if not _is(row["spend"].get("version"), int):
            return f"windows.{slug}.spend", "`spend` is opaque but versioned, and this one has no integer `version`"
    return None


# -- the file ---------------------------------------------------------------------------------
class CheckpointRefused(Exception):
    """A checkpoint this engine will not resume from: `check` is what failed, `path` is the file."""

    kind = "refused"

    def __init__(self, check: str, path: Path, why: str) -> None:
        self.check = check
        self.path = Path(path)
        self.why = why
        super().__init__(f"{self.path.name} ({self.path}) is {self.kind}: {check} — {why}")


class CheckpointCorrupt(CheckpointRefused):
    """Damaged or not a checkpoint: K2 falls back from this to the previous generation."""

    kind = "corrupt"


class CheckpointIncompatible(CheckpointRefused):
    """Written by another format, engine, Python, platform or tick: never fallen back from (K2)."""

    kind = "incompatible"


class CheckpointForeign(CheckpointRefused):
    """A verifying checkpoint of another world than the caller's directory (ADR 0002 H)."""

    kind = "foreign"


class CheckpointAbsent(CheckpointRefused):
    """No file. The chooser's business: a fresh directory, or a kill between the two renames."""

    kind = "absent"


@dataclass
class Loaded:
    """One verified generation: its path, its header, its body, and the refusal it stood in for."""

    path: Path
    header: dict[str, Any]
    body: dict[str, Any]
    fell_back: CheckpointRefused | None = None


def make_header(body: dict[str, Any], body_bytes: bytes) -> dict[str, Any]:
    """The identity the body states, plus the hash and length of the bytes it was encoded to."""
    return {
        **body["identity"],
        "body_sha256": hashlib.sha256(body_bytes).hexdigest(),
        "body_bytes": len(body_bytes),
    }


def _open_directory(path: Path) -> int:
    """The state directory as a handle that follows no link below the operator's own path."""
    return os.open(os.path.realpath(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


def _after_step(step: str) -> None:
    """The seam a test kills the process at: called after every named step of the write."""


def _sweep_temporaries(dir_fd: int) -> None:
    with os.scandir(dir_fd) as entries:
        stale = [
            entry.name
            for entry in entries
            if entry.name.startswith(TEMPORARY_PREFIX)
            and entry.name.endswith(TEMPORARY_SUFFIX)
            and entry.is_file(follow_symlinks=False)
        ]
    for name in stale:
        try:
            os.unlink(name, dir_fd=dir_fd)
        except FileNotFoundError:
            continue


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def write_checkpoint(state_dir: Path, body: dict[str, Any]) -> Path:
    """ADR 0002 I's durable write: temp → fsync → rename previous → rename current → fsync dir."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    body_bytes = encode(body)
    header_line = encode(make_header(body, body_bytes))
    dir_fd = _open_directory(state_dir)
    try:
        _sweep_temporaries(dir_fd)
        temporary = f"{TEMPORARY_PREFIX}{os.getpid()}.{secrets.token_hex(8)}{TEMPORARY_SUFFIX}"
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=dir_fd,
        )
        try:
            _write_all(fd, header_line + body_bytes)
            _after_step("write")
            os.fsync(fd)
        finally:
            os.close(fd)
        _after_step("fsync")
        # The first generation has nothing to become previous.
        with contextlib.suppress(FileNotFoundError):
            os.replace(CURRENT, PREVIOUS, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        _after_step("rename_previous")
        os.replace(temporary, CURRENT, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        _after_step("rename_current")
        os.fsync(dir_fd)
        _after_step("fsync_directory")
    finally:
        os.close(dir_fd)
    return state_dir / CURRENT


def _read_bytes(state_dir: Path, name: str) -> bytes:
    """The file's bytes through a handle that follows no link, or the refusal that says why not."""
    path = state_dir / name
    try:
        dir_fd = _open_directory(state_dir)
    except FileNotFoundError:
        raise CheckpointAbsent("absent", path, "its directory does not exist") from None
    except OSError as exc:
        raise CheckpointCorrupt(
            "directory", path, f"its directory cannot be opened ({errno.errorcode.get(exc.errno or 0, type(exc).__name__)})"
        ) from None
    try:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
        except FileNotFoundError:
            raise CheckpointAbsent("absent", path, "there is no such file") from None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise CheckpointCorrupt("link", path, "is a symlink, and the executive follows no link to its own state") from None
            raise CheckpointCorrupt(
                "open", path, f"cannot be opened ({errno.errorcode.get(exc.errno or 0, type(exc).__name__)})"
            ) from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise CheckpointCorrupt("regular", path, "is not a regular file")
            if info.st_size > MAX_CHECKPOINT_BYTES:
                raise CheckpointCorrupt("size", path, f"is {info.st_size} bytes, past the {MAX_CHECKPOINT_BYTES} this engine will read")
            chunks: list[bytes] = []
            remaining = MAX_CHECKPOINT_BYTES + 1
            while remaining > 0:
                chunk = os.read(fd, min(remaining, 1 << 20))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        except OSError as exc:
            raise CheckpointCorrupt("read", path, f"cannot be read ({type(exc).__name__}: {exc.strerror})") from None
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)
    raw = b"".join(chunks)
    if len(raw) > MAX_CHECKPOINT_BYTES:
        raise CheckpointCorrupt("size", path, f"grew past the {MAX_CHECKPOINT_BYTES} bytes this engine will read while it was read")
    return raw


def verify(raw: bytes, path: Path, expected: Compatibility) -> Loaded:
    """The checks, in the order the file's own structure imposes; the first failure is the answer.

    `format` first, because it says how to read the rest; then the body's length and hash, which
    is where a flipped byte or a truncation is caught; then the body's shape; then the header
    against the body's identity copy, which is how a header byte the hash does not cover is caught;
    and only then compatibility — so that a damaged file is *corrupt*, which K2 falls back from,
    and a file from another engine is *incompatible*, which it does not.
    """
    newline = raw.find(b"\n")
    if newline < 0:
        raise CheckpointCorrupt("header", path, "has no header line" if raw else "is empty")
    try:
        header = decode(raw[:newline])
    except Exception as exc:  # noqa: BLE001 - any decoder failure is "not a header"
        raise CheckpointCorrupt("header", path, f"the header line is not JSON ({type(exc).__name__})") from None
    if not isinstance(header, dict):
        raise CheckpointCorrupt("header", path, f"the header is a JSON {type(header).__name__}, not an object")
    written_as = header.get("format")
    if written_as != expected.format:
        raise CheckpointIncompatible("format", path, f"was written as format {written_as!r} and this engine reads {expected.format!r}")
    for key in ("body_bytes", "body_sha256"):
        if key not in header:
            raise CheckpointCorrupt("header", path, f"the header has no `{key}`")
    body_bytes = raw[newline + 1 :]
    if header["body_bytes"] != len(body_bytes):
        raise CheckpointCorrupt("body_bytes", path, f"the header says the body is {header['body_bytes']} bytes and {len(body_bytes)} follow it")
    digest = hashlib.sha256(body_bytes).hexdigest()
    if digest != header["body_sha256"]:
        raise CheckpointCorrupt("body_sha256", path, f"the body hashes to {digest} and the header says {header['body_sha256']}")
    try:
        body = decode(body_bytes)
    except Exception as exc:  # noqa: BLE001 - any decoder failure is "not a body"
        raise CheckpointCorrupt("body", path, f"the body is not JSON ({type(exc).__name__})") from None
    problem = _structure_problem(body)
    if problem is not None:
        raise CheckpointCorrupt(problem[0], path, problem[1])
    stated = {key: value for key, value in header.items() if key not in ("body_bytes", "body_sha256")}
    if stated != body["identity"]:
        differing = sorted(set(stated) ^ set(body["identity"]) | {k for k in stated if k in body["identity"] and stated[k] != body["identity"][k]})
        raise CheckpointCorrupt("identity", path, f"the header and the body's identity copy disagree on {differing}")
    identity = body["identity"]
    for field in ("engine", "python", "platform", "tick_hz"):
        ours = getattr(expected, field)
        if identity[field] != ours:
            raise CheckpointIncompatible(field, path, f"was written by {field} {identity[field]!r} and this engine's is {ours!r}")
    return Loaded(path, header, body)


def read_generation(state_dir: Path, name: str, expected: Compatibility) -> Loaded:
    """One generation by name, verified, or the refusal that names the check and the file."""
    state_dir = Path(state_dir)
    return verify(_read_bytes(state_dir, name), state_dir / name, expected)


def _of_this_world(loaded: Loaded, world_id: str | None) -> Loaded:
    found = loaded.body["identity"]["world_id"]
    if world_id is not None and found != world_id:
        raise CheckpointForeign(
            "world_id",
            loaded.path,
            f"records world {found!r} and this directory's world is {world_id!r}: two executives with "
            "separate state directories on one diode directory (ADR 0002 H)",
        )
    return loaded


def choose_generation(state_dir: Path, expected: Compatibility, *, world_id: str | None = None) -> Loaded | None:
    """ADR 0002 K2: the current generation, else the previous if it verifies; `None` for neither."""
    state_dir = Path(state_dir)
    try:
        current = read_generation(state_dir, CURRENT, expected)
    except CheckpointIncompatible:
        raise
    except (CheckpointCorrupt, CheckpointAbsent) as refusal:
        try:
            previous = read_generation(state_dir, PREVIOUS, expected)
        except CheckpointAbsent as none:
            if isinstance(refusal, CheckpointAbsent):
                return None
            raise type(refusal)(
                refusal.check, refusal.path, f"{refusal.why}; and there is no {PREVIOUS} to fall back to"
            ) from none
        except CheckpointRefused as second:
            raise type(second)(
                second.check,
                second.path,
                f"{second.why}; and {CURRENT} was refused first: {refusal.check} — {refusal.why}",
            ) from refusal
        previous = _of_this_world(previous, world_id)
        previous.fell_back = refusal
        return previous
    return _of_this_world(current, world_id)


# -- the operator's view -----------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dir", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--state-dir", required=True, help="the executive's private state directory")
    parser.add_argument("--world-id", default=None, help="the world this directory is expected to hold")
    args = parser.parse_args(argv)
    try:
        world = load_world(Path(args.dir))
    except Exception as exc:  # noqa: BLE001 - the loader's refusals are the point
        sys.stderr.write(f"the configuration cannot be loaded: {exc}\n")
        return 3
    expected = Compatibility.current(world)
    try:
        chosen = choose_generation(Path(args.state_dir), expected, world_id=args.world_id)
    except CheckpointRefused as refusal:
        sys.stderr.write(f"{refusal}\n")
        return 3
    if chosen is None:
        print(f"{args.state_dir}: no checkpoint; a fresh directory")
        return 0
    verb = "falls back to" if chosen.fell_back is not None else "resumes from"
    print(f"{args.state_dir}: {verb} {chosen.path.name}" + (f" ({chosen.fell_back})" if chosen.fell_back else ""))
    print(json.dumps(chosen.header, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
