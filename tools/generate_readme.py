#!/usr/bin/env python3
"""Generate the window's `README.md` — the protocol, in the vehicle's own words.

`docs/diode-contract.md:31-35` lists six files, and this is the one of the six nothing wrote:

    <DIODE_DIR>/<slug>/README.md    vehicle -> agent    (the protocol, in the vehicle's words)

§8 is explicit about what that means and what it is for:

    "They are told the shape above and nothing about the vocabulary. `HELP.md`, `state.json` and
     `README.md` are the vehicle's documentation of itself, written in whatever voice the vehicle's
     builder chooses, and **they are the only place a verb name can appear**."

So the file is generated rather than written, for the same reason `HELP.md` is (`tools/generate_help.py`):
a hand-written protocol and a declaration drift, and the drift is invisible from both sides. What is
different is the *part of the interface* each one documents, and keeping them apart is the whole
design of this module:

| file | what it answers | where its facts come from |
|---|---|---|
| `README.md` | **how this window works**: the six files, the layers, the refusals, the cadence, what is not published | `presentation.yaml`, `channels.yaml#coverage`, `commands.yaml#refusals` |
| `HELP.md` | **what I may ask for**: one entry per verb, its gates, its interlocks, its schema | `domains/*/commands.yaml` |
| `state.json` | **what is true right now**: the mirror | live state |

**This file is a *log*, and that is the one decision worth recording.** A `README` that restated the
mission's objectives, the thresholds, the fault kinds or the coupling graph would delete the
experiment — `docs/design.md` §8's boundary is that the vehicle publishes what it *concluded* and
never what it *suspects about itself*. So it carries the interface and the epistemics, and nothing
that would tell a fleet where to look first.

    python3 tools/generate_readme.py            # to stdout
    python3 tools/generate_readme.py | less     # it is the file the window gets, byte for byte

**There is deliberately no `--write`, and the first version of this tool had one that wrote to
`README.md` — in the vehicle folder, which is this project's *round log*.** It overwrote a megabyte of
the folder's own history with the window protocol, and the only reason it was recoverable is that it
had been committed. A tool that generates a file called `README.md` and defaults to writing it beside
a file called `README.md` is a tool with a loaded gun in it; the console is the only writer, and it
writes into the window.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

# **The refusal vocabulary is the help generator's, imported rather than restated.** V-02's chain is
# a list of nine codes and their meanings, and it is already written once — in `generate_help.py`,
# where `HELP.md`'s refusal section comes from. A second copy here would be the defect this folder
# spends its rounds removing: two declarations of one thing, in two files that never met, drifting
# the first time a code is added. So this file reads that one, which also means the window's README
# and its HELP.md cannot disagree about what a refusal means.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from generate_help import REFUSAL_VOCABULARY as REFUSAL_FALLBACK  # noqa: E402

LAYER_NAMES = {
    "measurement": ("A", "authoritative", "what the instrument reports"),
    "estimate": ("I", "inferred", "what the vehicle concluded"),
    "service": ("A", "service", "what the vehicle did"),
}


def load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def channel_rows(channels: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for section, value in channels.items():
        if not isinstance(value, list) or section in {"open_debts", "crew_positions"}:
            continue
        for row in value:
            if isinstance(row, dict) and row.get("id"):
                rows.append(row)
    return rows


def cadence_classes(presentation: dict[str, Any]) -> list[dict[str, Any]]:
    return list(((presentation.get("ring") or {}).get("cadence_classes")) or [])


def generate(root: Path) -> str:
    """The window's `README.md`, as a string. One function, so the file and its test agree."""
    presentation = load(root / "presentation.yaml")
    channels_doc = load(root / "channels.yaml")
    command_docs = [
        load(path)
        for path in sorted((root / "domains").glob("*/commands.yaml"))
    ]
    rows = channel_rows(channels_doc)
    total = len(rows)
    by_layer: dict[str, int] = {}
    by_class: dict[str, int] = {}
    for row in rows:
        by_layer[str(row.get("layer") or "?")] = by_layer.get(str(row.get("layer") or "?"), 0) + 1
    for entry in cadence_classes(presentation):
        by_class[str(entry.get("class"))] = int(entry.get("members") or 0)

    # The refusals, from the registries rather than from a list in this file.
    refusals: list[tuple[str, str]] = []
    seen: set[str] = set()
    for document in command_docs:
        block = document.get("refusals") or document.get("refusal_chain") or []
        if isinstance(block, dict):
            block = [{"code": k, "meaning": v} for k, v in block.items()]
        for entry in block:
            if not isinstance(entry, dict):
                continue
            code = str(entry.get("code") or entry.get("id") or "")
            if code and code not in seen:
                seen.add(code)
                refusals.append((code, str(entry.get("meaning") or entry.get("why") or "")))
    if not refusals:
        refusals = REFUSAL_FALLBACK

    withheld_named = 0
    withheld_categories = 0
    for path in sorted((root / "domains").glob("*/points.yaml")):
        for entry in (load(path).get("not_published") or []):
            if not isinstance(entry, dict):
                continue
            if entry.get("channel"):
                withheld_named += 1
            else:
                withheld_categories += 1

    mirror = presentation.get("mirror") or {}
    steps = [
        ("claim", "the console is read and emptied **before** anything runs"),
        ("validate", "schema, authority, phase, gate, interlock and freshness, in that order"),
        ("act", "at most one command per conflict domain per tick, first valid wins"),
        ("publish", "`state.json` and `HELP.md` rewritten, one frame appended to the ring"),
    ]

    out: list[str] = []
    write = out.append
    write("# This window")
    write("")
    write(
        "You are talking to one spacecraft through one directory. This file is the protocol: "
        "what the files here are, what a reading *means*, what a refusal means, and what this "
        "vehicle does not publish. It is generated from the vehicle's own configuration, so it "
        "cannot describe a window that is not the one you are reading."
    )
    write("")
    write("## The files")
    write("")
    write("| file | direction | what it is |")
    write("|---|---|---|")
    write("| `console.json` | you → vehicle | your commands, and the variables you may set |")
    write(
        "| `state.json` | vehicle → you | the mirror: what is available, what is open, what is "
        "left in the allowance |"
    )
    write("| `HELP.md` | vehicle → you | every verb you may use, with its gates and its arguments |")
    write("| `README.md` | vehicle → you | this file |")
    write("| `telemetry/NNN.json` | vehicle → you | a ring of recent frames |")
    write("| `output/<stamp>_<slug>_<cmd>.txt` | vehicle → you | one result per submitted command |")
    write("| `pending.json` | vehicle → vehicle | the vehicle's own queue. Not yours to read or write |")
    write("")
    write(
        "The telemetry is a **ring**, not a snapshot, and it is bounded to a fixed number of "
        "slots: when it is full the oldest frame is dropped and the loss is counted. `state.json`'s "
        "`ring` key states the bound, what is held, and what has been lost, so a gap in what you "
        "can see is never a mystery."
    )
    write("")

    # `presentation.yaml#surface[].far_side` is the designers' obligation checklist ("the voice, the
    # ordering, the file itself"), not protocol, and the files table above already says what each file
    # is; it is no longer published (owner's direction, 2026-10-09: the window says how to use the
    # interface and leaves the analysis to the fleet).

    write("## What a reading means")
    write("")
    write(
        "Every published value carries a layer and a quality code, and the two answer different "
        "questions: the layer says *what the number is about*, and the code says *how much to trust "
        "the instrument that produced it*."
    )
    write("")
    write("| layer | means | what it is |")
    write("|---|---|---|")
    for key, (letter, name, meaning) in LAYER_NAMES.items():
        write(f"| **{letter}** — {name} | `{key}` | {meaning} |")
    write("")
    # The protocol facts, stated for the fleet. `presentation.yaml#epistemic_layers` carries the same
    # facts with the designers' reasoning and citations of files no agent can read (the contract's
    # line numbers, `points.yaml#not_published`, an aside on what a fleet would go looking for); that
    # prose is the maintainers', and publishing it did part of the fleet's analysis for it.
    write(
        "**Truth is never published.** A value is what an instrument reported, what the vehicle "
        "concluded, or what the vehicle did — never the simulation's own state."
    )
    write("")
    if "service" in ((presentation.get("epistemic_layers") or {}).get("mapping") or {}):
        # Only what the code does: frames carry the quality map the instruments fill, and no tool
        # stamps a code on a service value, so this does not promise one (review of 27aae35).
        write(
            "**A commanded state is not a sensor.** A `service` value reports what the vehicle did, "
            "not what an instrument read."
        )
        write("")

    write("## The shape of a command cycle")
    write("")
    for index, (step, what) in enumerate(steps, start=1):
        write(f"{index}. **{step}** — {what}.")
    write("")
    write(
        "A command that cannot be attempted is not an error and not a silence: it produces exactly "
        "one result file, of the same shape as a success, whose text says what refused it and why."
    )
    write("")
    write("## What a refusal means")
    write("")
    write("| code | meaning |")
    write("|---|---|")
    for code, meaning in refusals:
        write(f"| `{code}` | {meaning} |")
    write("")

    write("## How often this vehicle speaks")
    write("")
    write(
        f"{total} channels are published across {len(by_class)} cadence classes. The class a channel "
        f"is in is a statement about how fast the quantity moves, and it is why a frame's `values` "
        f"map does not have the same members twice: a frame carries the values whose period has "
        f"elapsed, so an absent slow channel is a cadence rather than a dropout."
    )
    write("")
    write("| class | rule | channels |")
    write("|---|---|---|")
    for entry in cadence_classes(presentation):
        write(f"| `{entry.get('class')}` | {entry.get('rule')} | {entry.get('members')} |")
    write("")
    frame = presentation.get("frame") or {}
    fields = [str((row or {}).get("name")) for row in (frame.get("fields") or [])]
    if fields:
        write(
            f"Every frame carries {len(fields)} envelope fields besides the values: "
            + ", ".join(f"`{name}`" for name in fields)
            + "."
        )
        write("")

    write("## What this vehicle does not tell you")
    write("")
    write(
        f"Some quantities are deliberately withheld, and saying so is part of the protocol: "
        f"**{withheld_named} named channels** and **{withheld_categories} described categories** are "
        f"not published. A "
        f"quantity that is missing from a frame is either one of those or a channel that could not "
        f"be computed this tick — never a value the vehicle declined to guess."
    )
    write("")
    # **No diagnostic commentary.** This section used to publish `presentation.yaml#single_cabin`
    # under "two asymmetries are worth knowing about, because they change what a diagnosis can rest
    # on": why an LM leak is harder to diagnose than the CSM's, which supply has no instrument, and
    # a fault id from the fault catalogue. That is the designers' analysis of the vehicle, and
    # handing it to the fleet does part of the information management the experiment asks of it
    # (owner's direction, 2026-10-09). The declaration stays for the linter's pairing check
    # (`check_vehicle.py`, single-cabin channels); the window's protocol says what a reading means
    # and leaves what it implies to the fleet.
    if mirror:
        bound = mirror.get("bound") or mirror.get("max_bytes")
        if bound:
            write(f"The mirror is bounded: {bound}.")
            write("")

    write("---")
    write("")
    write(
        "The vehicle writes this file from its own configuration. Editing it by hand changes "
        "nothing that survives the next cycle."
    )
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dir", default=str(Path(__file__).resolve().parent.parent))
    args = parser.parse_args(argv)
    sys.stdout.write(generate(Path(args.dir)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
