# AGENTS.md — the vehicle

This is the vehicle's own repository: the far side of the window `space_chassis` deliberately does
not contain. It is vendored into `space_chassis` as a git submodule at
`docs/deep_research/vehicle`, so a checkout of the chassis has a vehicle to run — and a second
vehicle would be a second submodule beside it, not a second folder in here.

The only interface between the two halves is `docs/diode-contract.md`, which stays frozen in
`space_chassis`. Nothing in this repository serves the fleet, and none of these tools may be
imported by the operator-side services, which are standard library only — the tools here need
PyYAML, deliberately, and are wired into nothing.

**Paths.** Everything in this repository is relative to its root, which is this folder. The corpus
and the contract are *outside* it now (`space_chassis`'s `docs/deep_research/` and
`docs/diode-contract.md`): cite them by name and line, never read them from here, because a suite
that reads across the repository boundary is a suite that cannot run without the other half.

## What is input, and what is yours

| | |
|---|---|
| **Frozen — cite, never edit, never read from here** | `apollo_diode.md`, the twelve `*_diode.md` subsystem specs, `integration/simulator-design.md`, `integration/corpus-review.md`, `integration/review-findings.md`, and `docs/diode-contract.md` — all of them `space_chassis`'s, all unchanged since its initial commit. A round that "fixes" one of them is editing the evidence, and a tool here that *opens* one has broken the boundary the submodule exists to keep. |
| **Yours** | everything in this repository, `tests/test_vehicle_config.py` included. The one changelog row per file in `space_chassis`'s `integration/reconciliation/README.md` is maintained on that side now: the residue suite there (`tests/test_vehicle_reconciliation.py`) holds the row to what this repository's tools say. |

Where the corpus and the vehicle disagree, the disagreement is *recorded* — in
`space_chassis`'s `integration/reconciliation/01-conflict-register.md` for a decision about the
vehicle as a whole, and in this repository's own `open_debts` for one it can carry — and the linter
cites the line it is disagreeing with. `apollo` supplies the vehicle and the subsystem specs supply
the per-domain contracts; the rule is an override, not a tie-break, and `corpus-review.md` carries
the three corrections that make it narrower than it sounds.

## The corpus

| File | What it is |
|---|---|
| `README.md` | The round log *and* the status board. Every finding this folder has made is a section in it, in the order it was made. |
| `plant.md` | The core contract: what `step()` is, the integrator classes, determinism, the event queue, the truth/evidence boundary, and what the plant may never do. |
| `vehicle.yaml` | Globals: frames, scalar conventions, environment, configurations and their mass closure, propulsion, loads, zones, antennas. |
| `mission.yaml` | The profile: phases, the posture machine, freshness manifest, occultation, MET epoch, crew, Δv budget, objectives. |
| `coupling.yaml` | The graph as data: typed edges, cycles, the failure chains, `open_debts`. The linter derives the tick order from it. |
| `presentation.yaml` | The vehicle's side of the frozen window: the epistemic mapping, the six files, the frame envelope, the mirror and its bound, cadence classes. |
| `channels.yaml` | The point dictionary: every canonical channel with unit, precision, rate, priority and event class. |
| `domains/<name>/` | One landed subsystem, five files (below). |
| `tools/` | The linter, the reference plant, the fault scheduler, the help generator, the reference console. |

A domain is five files, and a value belongs in exactly one of them: `components.yaml` (states,
equipment, loads), `points.yaml` (published channels and `not_published`), `profiles.yaml`
(thresholds, profiles, selection), `commands.yaml` (verbs and `not_implemented`), `fault_policy.yaml`
(faults and coverage). A new domain also has to appear in `coupling.yaml#nodes` — **as node ids, not
domain names**, because the scheduler sorts nodes and a domain order cannot in general exist — and
in `space_chassis`'s canonical vocabulary at
`docs/deep_research/integration/reconciliation/02-canonical-vocabulary.md`, which is checked on that
side (a vocabulary this repository cannot read is a vocabulary it cannot check).

## The two gates

```sh
python3 tools/check_vehicle.py            # report; exit 0 with declared debts
python3 tools/check_vehicle.py --strict   # exit 2 while any debt stands — the gate for a *finished*
                                          # build, not for CI: a debt is a deliverable
python3 tools/check_vehicle.py --order    # the derived tick order, dependencies first
python3 tools/check_vehicle.py --debts    # the owed list grouped by what each one wants
python3 tools/plant.py --readiness        # the build order: ready, blocked, and by what
python3 tools/plant.py --build-order      # what blocks every state, derived
python3 tools/generate_help.py            # HELP.md, from the command registries

python3 -m pytest -q tests/test_vehicle_config.py     # the referee's own referee
uvx ruff check . --no-cache
```

Linter exits: `0` composes (debts and all), `1` refused — something is *wrong*, not merely missing,
`2` `--strict` with unfilled debts, `3` unreadable. **A refusal is fatal always; a debt is a
deliverable.**

The test file is the gate that matters, and it lives in this repository at
`tests/test_vehicle_config.py`. It runs the linter against broken copies: a linter that has never
been seen to refuse is indistinguishable from a linter that always passes. Run the whole file before
you commit — it is the only thing that runs every refusal. The assertions that need `space_chassis`
— the frozen corpus, the contract probe, the reconciliation README, the compose service — are *not*
here; they live in that repository's `tests/test_vehicle_reconciliation.py`, because this suite may
not read outside its own root.

## Where to start

There is no authored worklist, and that is deliberate — an authored one drifts the moment anything
lands. The worklist is derived, and it is the first thing to run:

```sh
python3 tools/plant.py --build-order      # the four buckets, with a reason per state
python3 tools/plant.py --readiness        # the same states as tick gaps, and what each one needs
python3 tools/check_vehicle.py --debts    # every obligation, grouped by what it wants
```

Read the buckets as a queue. **`ready now`** is what a tick already advances. **`owes a value`** is
the cheapest class and the debt count already tracks it. **`owes an edge`** is a coupling with no
sensitivity, or a state nothing drives. **`owes a rule`** is domain code — `algebraic`, `discrete`,
`dynamics`, `hazard` — and it is the largest block by construction, because the configuration
deliberately carries no code (`plant.md` §3).

Pick a finding, not a bucket: one commit, one defect, one refusal, one test, the pins moved, one
README section. A round that lands two findings cannot be verified as one.

## Two repositories, one round

This is the vehicle's repository. `space_chassis` vendors it as a git submodule at
`docs/deep_research/vehicle`, and holds the corpus, the contract and the reconciliation rows.

1. **Work here.** In a chassis checkout this directory *is* the submodule, so `git log` here is this
   history; commit under the vehicle's law and push to `origin` (`main`).
2. **The chassis records the new pointer in its own commit.** A submodule bump is not a vehicle
   finding and does not belong in a `vehicle:` message.
3. **The reconciliation row for a vehicle file is edited on the chassis side**
   (`docs/deep_research/integration/reconciliation/README.md`) — this suite may not read outside its
   root, and the residue suite there is what holds that row to these tools. It also holds the
   sentence that counts this suite's tests, so a round that adds or removes a test moves that
   sentence too, in a chassis commit.
4. **There is no CI here yet.** The gates above are what CI would run: `pytest -q`, and
   `check_vehicle.py` exiting 0 — never `--strict`, because a debt is a deliverable and `--strict`
   is the gate for a build that is finished.

## The law of debts

`UNCONFIGURED` is not a placeholder and must never resolve to a plausible number. It is a build
failure with a message, naming what wants it. **When the linter reports a debt, the debt is the
deliverable; the wrong response is to fill the value with something plausible so the build goes
green.** If you cannot ground a value in `apollo`, a published source about the real vehicle, or a
relation to other values here, it stays unset and you add a sentence saying who owes it and what
would answer it.

Provenance is five classes on every value that matters, and the linter refuses a claim without
support: `apollo` (carries the line in `apollo_diode.md`), `historical` (a published figure about
the real vehicle; carries the source — the corpus has no masses, so this is where the vehicle comes
from), `derived` (carries the relation, so the linter can re-derive it and disagree), `chosen`
(carries the reason), `UNCONFIGURED`. `derived` is the strong one: a value the tools recompute
cannot drift, and a value with a `relation` that no tool re-derives is next round's finding.

## A round

Work happens in rounds, one finding each, and the shape is stable:

1. **Find one defect.** The method is always one of a few shapes, and the commit log is the
   catalogue: *a declaration nothing reads*; *one thing declared twice, in two files that never
   met*; *two halves of a surface joined by nothing*; *a check that cannot run*, which is not a
   check that passed; *a debt that was answered and left standing*. The instrument is always the
   same question — **what reads this, and what does it disagree with?**
2. **Fix the corpus**, not the prose about it. If the answer is a decision rather than a value,
   it goes in `open_debts` as a sentence, not in a field as a guess.
3. **Add the refusal** to `check_vehicle.py` so the defect cannot come back. A fix with no check
   is the next round's finding.
4. **Add the test** that breaks a copy and proves the refusal fires. Test names are full sentences
   stating the property under test, and the docstring carries the reasoning — including what the
   round got wrong first.
5. **Move the pins** (below) in the same commit.
6. **Write the section** in `README.md`: `## <the finding, as a sentence>`, then the evidence —
   the two halves, the table of what was wrong, the mistakes the round made on the way, and a
   closing paragraph with the figures that moved. New sections go **above**
   `## The invariants, and which of them are enforced`; the two closing sections are last.
7. **Verify by breaking copies** in `.scratch/rNN/` (gitignored, `NN` = the round number). The
   script copies the five top-level files **and `domains/`** — a fixture that copies only the
   top-level files lets a test pass while the thing it claims to test was never read — then applies
   one break per case and asserts the linter's own words. Include the unbroken corpus composing.
8. **Commit as `vehicle: <the finding>`** — the sentence that names the defect, not the file that
   changed.

The test file is a shared instrument, not a per-round scratchpad: add to it, don't rewrite it.

## The pins

The README's status section is asserted against the tools' own output by
`test_the_readme_status_matches_the_tools`, and the test file pins counts of its own (the debt
headline, the build-order buckets, the number of `--debts` entries, the mover tallies). **When a
figure moves, every place that states it moves in the same commit.** The README carries the live
figures; do not copy them anywhere a tool cannot check them — this folder's own recurring finding
is that *a declaration no tool reads has already drifted*, and it has caught the README's status
section, the embedded `--build-order` transcript, and the tests that pin them.

Get the live numbers from the tools rather than from prose:

```sh
python3 tools/check_vehicle.py | tail -2          # channels, edges, chains, phases, debts
python3 tools/plant.py --readiness                # states, nodes, configured, edges, scalars
python3 tools/plant.py --build-order              # the four buckets
grep -c "^def test_" tests/test_vehicle_config.py
```

## The traps, in the order they have bitten

- **A key written twice in one mapping.** PyYAML lets the last win, which makes it the quietest
  structural fault in the format — and it is the *signature* of a list item absorbed into the block
  scalar above it, where the item's keys become the previous entry's keys. The linter refuses both
  the collision and the silent half (a key written where a block scalar's prose lives, which needs
  no collision and therefore survives a duplicate-key check). When you edit next to a `>-` block,
  check what you just joined.
- **Two complete forms of a sensitivity.** A scalar `value` and a `regimes` table. `usable` asks
  *can the integrator multiply by this*; `declared` asks *has the corpus answered*. They are
  different questions and conflating them made every regime edge read as undeclared. A table stays
  *unusable* on purpose: a mode selection applied as a gain is the bug the separation prevents.
- **`advances`.** An edge with no `advances` counts as an input for **every** state on its node, and
  the plant refuses a state when any of its inputs is unusable — so one unconfigured edge silently
  drags its neighbours out of the ready class.
- **The `internal` sentinel** is not a coupling node, so no edge can land on it. The states there
  are advanced with their domain, and that is the one exemption to the command-surface rule.
- **Commands are the exception to the `regimes` rule, and only commands.** A `discrete` edge out of
  physical equipment must declare a table because a mode selection is not a derivative; a command
  arriving one for one is a genuine unity gain. An edge that leans on the exception from a *service*
  node is claiming a command path — check that a state on the target node actually declares it.
- **The tiebreak is frozen and lexicographic, and it is wrong somewhere.** Nodes advanced by several
  states with no declared order are decided by it, and the linter names them on every run. It gets
  `link` wrong: `link_snr` sorts before `tx_power`, and transmit power is a term in the link budget.
- **A reversed schedule still contains every node**, which is why nothing downstream noticed when
  the tick order ran backwards. When you touch ordering, ask of each edge whether its producer comes
  first — not whether the list looks plausible.
- **"Cannot disagree" is the wrong claim about a check.** A guard that skipped its own body reported
  a conservation edge as sound for its whole life. A check that cannot run is not a check that
  passed; an undecidable case is a refusal, and the fix is to stop calling the thing by the name
  that made the check applicable.
- **PyYAML is optional to the suite** (`pytest.importorskip`), because the operator-side services
  must never gain a dependency from here. Don't add one.

## Numbers in this file

There are none, on purpose. Read them from the tools; the README states them and a test holds the
two together. If you need a figure in a new document, give it a reader in the same commit.
