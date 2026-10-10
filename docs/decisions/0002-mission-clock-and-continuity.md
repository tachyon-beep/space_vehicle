# 0002 — The mission clock, private checkpoints and restart continuity

- Status: **accepted** — the operator's choices A–F, K and L(b) on 2026-10-09; the maintainer-owned items G, H, I, J, K mechanics, L(a), M and the encoder question accepted under the owner's standing delegation, open to the maintainer's revision; **chassis review of the adapter consequences pending** (H and the cross-repository list). The **operator** owns the clock policy — A (scheduler and the *value* of `m`), C (overload), D (pause), F (downtime) — and confirmed the three fleet-visible mechanics B (publication cadence), E (budget clock) and L(b) (result stamps). The **vehicle maintainer, with the chassis reviewer**, owns the persistence and recovery mechanics — G, H, I, J, K, L(a) and M — per `docs/decisions/README.md` rows one and two. Every section ends with an `Answer:` line recording who decided what.
- Date: 2026-10-09; revised the same day after independent review ("sound with findings", on the first draft `bfed799`); accepted the same day
- Decision owner: operator for A, C, D, F and K's policy (and the confirmations B, E, L(b)); vehicle maintainer with chassis reviewer for the rest, including K's mechanics. "The owner's standing delegation" below is the operator's (`tachyon-beep`) instruction of 2026-10-09 to close open choices with the best-supported answer
- Related work package / evidence gaps: WP08 (#8) implements it; WP09 (#9) and WP10 (#10) wait on it; WP12 (#12) measures the budgets it names; ADR 0001 choice D, its "restart consequence" and its clarification on the authoritative record are amended by it
- Reviewers and review evidence: one independent review of the first draft (findings F1–F12 and nits, all addressed in the revision); the operator answered the questions on 2026-10-09 and the answers are on the `Answer:` lines; the maintainer items were accepted by the implementing session's coordinator under the owner's standing delegation (2026-10-09), as ADR 0001's clarifications were; chassis reviewer pending
- Supersedes / superseded by: amends ADR 0001 (see §Consequences for existing records); supersedes nothing

## Question and constraints

WP08 asks for three things at once: a mission clock that advances independently of agent thinking
and publication, a private coherent checkpoint of everything the executive owns, and a restart
that neither resets physics nor advances it by guessed time. The executive that WP01 landed
(`tools/console.py`, ADR 0001) deliberately left every one of those open: it ticks once per cycle,
sleeps `--poll` seconds between cycles, holds its whole state in process memory, and refuses to
start on a directory another world has bound (choice D), so a restarted vehicle container stays
dark until an operator clears its windows.

This record does two things, and keeps them apart. **Part (a)** lists what existing accepted
documents already fix, with the citation, so that nobody re-decides it. **Part (b)** states the
decisions that are genuinely open, each as a concrete question with options, the consequences for
the four things this programme weighs — the validity of the fleet experiment, determinism and
replay, the performance budget, and the chassis adapter — and a recommendation with its reasoning.
The recommendations are recommendations: ROADMAP.md says "operator decides target changes, never
silent clock dilation", and nothing below is a default the implementation may assume before the
owner's `Answer:` line is filled in.

### The four clocks

completion.md's acceptance property is that "simulation time, safety dwell time, receipt order and
wall time have defined semantics". The executive at `bdfaf8b` already runs four distinct clocks, and
the first job of this record is to say which is which, because half of the questions below are
"which clock does this quantity live on".

| Clock | Unit and origin | What lives on it today (`tools/console.py` at `bdfaf8b`) |
|---|---|---|
| **Tick / MET** | integer ticks from `mission.yaml#met_epoch_utc`, `tick_hz: 50`, `total_ticks: 34,560,000` (`plant.md` §6: "MET is an integer tick count") | `Executive.tick`; `met_s = tick × tick_us / 1e6` in every frame; `sensor_time_s` and `publish_time_s` (both `= met_s`, `plant.emit_frame`); `state_revision`; `state.json.executive.tick`; `pending.json.ticks`; phase boundaries (`plant.mission_ladder`) |
| **Simulated microseconds** | `tick × tick_us + offset_us` (`plant.md` §5; ADR 0001 "Tick and effect timing") | dwell records (`Executive.dwell`); deferral due and age (`due_tick`, `maximum_queue_age_s`); effect stamps; a result's `tick=… offset_us=…` receipt line |
| **Wall UTC** | `datetime.now(UTC)` | result filenames (`write_result`: `stamp(utc_now())`); `state.json.published_at`; `.executive.json.updated_at`; `budget.oldest_expires_in_seconds` and `Window.started`; `--poll` sleeps; the chassis's `DIODE_POLL_SECONDS` |
| **Boot** | `uuid4` per process | `Window.boot_id` (per window, drawn in `__init__`); `seq` ("monotonic within a boot", `presentation.yaml#frame`); arm tokens are salted with `boot_id`; `world_id` is per `Executive` instance and bound by the first tick |

Two of the wall-clock rows are the subject of questions below (E, and the stamps in L); the rest of
the table is fixed and is restated so the checkpoint in G knows what it is saving.

### Vocabulary this record fixes

- **Cycle** (the contract's §2.2 and §2.4 use the word without defining it on the vehicle side):
  one *publication period* — the executive claims every console, validates, steps `k` ticks with the
  accepted effects stamped at `offset_us = 0` of the first of them, and then publishes. "Rewritten
  every cycle" therefore means every `k` ticks of mission time (B, and the claim cadence in B).
- **Durable tick**: the newest tick the vehicle can provably resume at — the checkpoint's tick plus
  whatever the durable record (J) lets it replay deterministically.
- **Published tick**: per window, the newest tick a frame or mirror has described. The high-water
  mark of L and J.
- **Segment**: one boot's stretch of a run, from (re)start to stop, with its own `boot_id`, wall
  epoch and journal segment (J).

## Part (a): what accepted decisions already fix

Nothing in this list is open. Each row is a constraint the options in part (b) are checked against.

| Fixed by | What it fixes |
|---|---|
| `plant.md` §6, rule 1 | MET is an integer tick count; `t += 0.02` is forbidden. A multiplier maps wall time to a *number of ticks*, never to a float mission time. |
| `plant.md` §6, rules 2–3 | One `master_seed` is a run input, never a log line; streams derive by name. The seed is a checkpoint field, not something a restart may redraw. |
| `plant.md` §6, rule 5 | Compare-point at every tick over canonically-encoded state; full snapshot at phase boundaries, deltas between. A checkpoint cadence coarser than a phase contradicts this; a finer one is permitted. *What the compare-point covers* is fixed (the whole state); *how it is encoded* is not (A). |
| `plant.md` §6, rule 6 | Replay drives from the trace keyed by tick index, not wall time. Whatever the live scheduler does with wall time, the recorded trace must be `(tick, effects)`, and a restart that replays a tail must replay by tick. |
| `plant.md` §6, "the clock's indifference is the mechanism" | Sim time advances on the multiplier whether or not anyone is thinking; no reasoning budget is imposed. A policy that waits for agents is excluded. |
| `plant.md` §5 | Dwell timers run on a clock that cannot jump; effects carry integer-microsecond stamps and equal stamps keep the caller's order. Pause, downtime and dilation may not move a dwell. |
| `plant.md` §3, transport delay | The delay ring is state, indexed by tick, "part of the snapshot and part of the compare-point hash". A checkpoint carries `<state>__delay` whole; a compare-point that omits slots the tick did not write has changed §6's contract (A). |
| `plant.md` §4 | Stock residual accumulators (`<state>__residual`) and shortfalls are exact state and are in the checkpoint. |
| ADR 0001, Decision, "Tick and effect timing" | Validation and effect read the same truth at the same simulated instant; dwell and queue age are simulated microseconds. "How many ticks a wall second carries" is explicitly deferred to here. |
| ADR 0001, Decision, "One world per directory", choice D | A second executive on a bound directory refuses; a window bound to a different world is refused "until WP08 defines continuity". Reopened by K. |
| ADR 0001, Clarifications, "the directory's record is authoritative" | Identity lives in `<diode-dir>/.executive.json`, read under the lock. **Amended by H**: the checkpoint is authoritative and the record is the windows' copy. |
| ADR 0001, Clarifications, "a window that fails its pre-claim check keeps its due deferrals queued" | Deferrals survive a dark window and `EXPIRE` by simulated age; they survive a restart too (G). |
| ADR 0001, Clarifications, "budget semantics are WP08's" | `budget.oldest_expires_in_seconds` counts the wall clock *provisionally*. Question E. |
| ADR 0001, Clarifications, "bounds in memory" | The journal holds the whole lineage and is refused inside `--diode-dir`. The same rule governs where a checkpoint may live (H). |
| `docs/completion.md`, non-negotiable properties | "Physics advances independently of agent thinking and publication requests"; "pause, overload and downtime policies are decided before use; restarting cannot silently reset physical state or advance it by guessed time"; "the fleet cannot access truth, checkpoints, private fault plans". |
| `docs/completion.md`, performance | 34,560,000 ticks; live deadline `20 ms / m` for the executive plus amortised ten-window I/O; record p50/p95/p99/max, queue ages, missed deadlines and the agreed overload behaviour; replay goal ≈ 8.7 µs/tick; "changing these targets requires an operator decision". Replay equivalence is *same build, same platform* (I). |
| `docs/release.md`, manifests | Every acceptance manifest carries "clock and multiplier policy" and "checkpoint format/compatibility identifiers"; rollback "follows its documented checkpoint compatibility policy; do not guess that a new or old engine can read an arbitrary saved world". |
| `docs/diode-contract.md` §6 (frozen, chassis) | The vehicle may restart; nothing already submitted runs twice (the destructive claim, §2.2); nothing already executed is forgotten; the operator's ceiling counters survive a restart rather than refilling; time is UTC everywhere and the stamp in a result filename is the vehicle's clock. Row at line 244: at-most-once intake covers a restart mid-batch. |
| `docs/diode-contract.md` §2.2–2.4 | One result per command, never overwritten; `state.json` is rewritten every cycle whether or not anything was submitted (so the mirror is never shed, C); published state is never read back as input. |
| `presentation.yaml#frame` | `seq` is monotonic within a boot and, with `boot_id`, separates loss from a restart; `boot_id` exists "because six of twelve specs cannot express a restart"; `met_s` "cannot jump backward"; `sim_step` is the tick. |
| `presentation.yaml#plant_published` and `#ring` | `mission.phase_entry_seq` and `mission.posture_entry_seq` exist "so a fleet returning from a gap can tell a state from a snapshot"; `cadence_classes` make a frame carry the channels whose period has elapsed. No new channel or frame field is needed to make a gap, hold or cadence visible. |
| `channels.yaml#rate_hz` and `mission.yaml#transition_evidence` | 14 channels declare 10 Hz and 7 declare 5 Hz; five guards declare `max_age_ms` of 100 or 200 (mission.yaml's "invariant D", `mission_diode.md:363`). These bound the publication cadence from above (B). |
| `mission.yaml#met_epoch_provenance` | A fixed UTC epoch "so MET is an integer tick count from an absolute origin and every published timestamp is reproducible". |

## Evidence and measurements

`tools/measure_clock.py` is the reader for every figure below (AGENTS.md, "Numbers in this file":
a figure in a new document needs a reader in the same commit). The two tables are its output on
2026-10-09, vehicle commit `bdfaf8b` (the WP01 tools; the first draft `bfed799` changed docs only), Python 3.12.3,
`Linux-6.8.0-146-generic-x86_64-with-glibc2.39`, ext4 on NVMe, `--samples 100` (cycles are timed over 60 samples), `--windows 1 2 10`.
They are this machine's figures and are **not pins**: re-run the tool rather than copy the table;
where the two disagree, the table is what moved.

The second table matters more than the first. The coolant transport ring `loop_transport_t__delay`
is born at tick 1 with 52,100 slots (1,042 s at 50 Hz) and is fixed-size thereafter, but a slot is
`None` until the ring has wrapped once — about 17 mission minutes — and a `None` encodes in a
handful of bytes where a float encodes in two dozen. `--full-ring` fills every slot with a distinct
float and is the steady state of any run longer than one transit.

| Quantity (`python3 tools/measure_clock.py --samples 100`) | ring as warmed (10 of 52,100 slots filled) |
|---|---|
| truth keys at t=0 / after tick 1 | 114 / 131 (the 17 new keys are `__residual`, `__shortfall` and one `__delay`; all keys are strings) |
| `canonical_state` bytes / plain sorted JSON bytes | 735,722 / 265,012 |
| `state_hash` (tagged encoder, §6 before child 6) | **19.98 ms p50**, 21.43 p95 |
| `sha256(json.dumps(sort_keys, compact, default=repr))` — the floor | **0.82 ms p50**, 0.87 p95 |
| `plant.step`, no effects | 1.54 ms p50, 1.64 p95 |
| checkpoint-shaped payload, 10 windows: bytes / `json.dumps` | 268,549 / 0.76 ms |
| durable write (`write`, `fsync`, `rename`; a lower bound on §I's sequence) / without `fsync` | 1.01 ms p50, 1.52 p95, 4.12 max / 0.25 ms p50 |
| `Executive.cycle`, 1 / 2 / 10 windows | 25.9 / 28.3 / 46.7 ms p50 (27.5 / 29.7 / 49.6 p95) |

| Quantity (`python3 tools/measure_clock.py --samples 100 --full-ring`) | ring full (52,100 of 52,100) |
|---|---|
| `canonical_state` bytes / plain sorted JSON bytes | 1,094,444 / 467,464 |
| `state_hash` (tagged encoder, §6 before child 6) | **25.93 ms p50**, 34.90 p95, 53.43 max |
| `sha256(json.dumps(sort_keys, compact, default=repr))` — the floor | **7.98 ms p50**, 10.94 p95 |
| `plant.step`, no effects | 1.55 ms p50, 1.96 p95 |
| checkpoint-shaped payload, 10 windows: bytes / `json.dumps` | 471,001 / 7.70 ms |
| durable write / without `fsync` | 1.36 ms p50, 2.01 p95, 2.73 max / 0.42 ms p50 |
| `Executive.cycle`, 1 / 2 / 10 windows | 33.2 / 34.0 / 51.5 ms p50 (41.6 / 38.8 / 57.8 p95) |

**After child 6** (the encoder swap; measured on that commit's working tree, which the tool reported
as `47856fa-dirty`, same machine and Python, `--samples 100`, `--windows 1 2 10`). `state_hash` is now
SHA-256 over plain sorted compact JSON with a `default` that raises, `allow_nan=False` and an explicit
refusal of non-string keys; the tool keeps the retired tagged encoder as a reference row so the two
can be read off one run. The difference between `state_hash` and the floor row is the key check
(a `set(map(type, …))` pass over the ring, ≈ 0.4–0.5 ms); the floor itself is the `repr` of the
ring's floats and does not move without a change to §6's coverage.

| Quantity (`tools/measure_clock.py --samples 100`, after child 6) | ring as warmed (10 of 52,100) | ring full (52,100 of 52,100) |
|---|---|---|
| `canonical_state` bytes (now plain sorted JSON) / the retired tagged encoding | 265,012 / 735,722 | 467,464 / 1,094,444 |
| `state_hash` (§6, sorted compact JSON) | **1.38 ms p50**, 1.50 p95, 1.91 max | **8.50 ms p50**, 11.12 p95, 13.81 max |
| the floor (`default=repr`, no key check) | 0.84 ms p50, 1.04 p95 | 8.16 ms p50, 11.05 p95 |
| the retired tagged encoder, as a reference row | 20.30 ms p50, 25.09 p95 | 25.52 ms p50, 32.54 p95 |
| `plant.step`, no effects | 1.55 ms p50, 1.75 p95 | 1.53 ms p50, 1.63 p95 |
| `Executive.cycle`, 1 / 2 / 10 windows | 5.5 / 8.1 / 25.7 ms p50 (5.9 / 10.1 / 28.1 p95) | 13.7 / 16.2 / 33.8 ms p50 (14.3 / 19.4 / 38.6 p95) |

The same-session "before" rows from that run (`state_hash` tagged, 19.62 ms warmed / 23.99 ms full;
`Executive.cycle` 26.4 / 29.5 / 45.2 ms warmed and 30.6 / 34.0 / 50.0 ms full) agree with the two
tables above to within their own spread, so the before and after are one machine's figures.

**After child 4** (the durable record; measured on that commit's working tree, which the tool reported
as `770159e-dirty`, same machine and Python, ext4 on NVMe, `--samples 100`, `--windows 1 2 10`, each
ring shape a run of its own with nothing else running). `tools/measure_clock.py` now times one tick row
appended and `fsync`ed alone, and `Executive.cycle` with a state directory: a *quiet* cycle (no command
anywhere: one `fsync`, the row carrying every window's published-tick mark) and a *verdict* cycle (one
refused command in every window, so every window writes a result and the `results_written` note is a
second `fsync`), beside the same verdict cycle with no record. The `fsync` counts are the tool's
count of `os.fsync` calls in one such cycle, not an assumption.

| Quantity (`tools/measure_clock.py --samples 100`, after child 4) | ring as warmed (10 of 52,100) | ring full (52,100 of 52,100) |
|---|---|---|
| one tick row (690 B, ten windows' marks) appended + `fsync` / appended alone | 0.59 ms p50, 1.15 p95, 3.36 max / < 0.01 ms | 0.58 ms p50, 1.15 p95, 2.40 max / < 0.01 ms |
| `Executive.cycle`, no record (no `--journal`, no `--state-dir`), 1 / 2 / 10 windows | 5.6 / 7.8 / 26.3 ms p50 | 12.5 / 15.1 / 32.0 ms p50 |
| quiet cycle with the record (**1 `fsync`**; row 306 / 333 / 549 B), 1 / 2 / 10 windows | 6.4 / 9.6 / 28.5 ms p50 (7.8 / 12.9 / 43.3 p95) | 13.5 / 16.2 / 33.7 ms p50 (14.8 / 18.4 / 36.4 p95) |
| verdict cycle with the record (**2 `fsync`**; row 663 / 1,050 / 4,138 B), 1 / 2 / 10 windows | 7.6 / 10.3 / 31.4 ms p50 (9.9 / 14.4 / 37.4 p95) | 14.4 / 18.3 / 36.9 ms p50 (17.9 / 21.8 / 40.7 p95) |
| the same verdict cycle with no record, 1 / 2 / 10 windows | 5.6 / 8.0 / 26.1 ms p50 | 13.0 / 15.2 / 34.3 ms p50 |

So the record costs what J priced: about one `fsync` (≈ 0.6 ms p50, ≈ 1.2 ms p95 on this disk) per
quiet cycle and two per cycle that wrote results, plus the row's encoding, which is a few hundred bytes
to a few kilobytes; at ten windows the difference sits inside the cycle's own spread (2–5 ms p50).
Before child 4 a journaling executive also hashed the stepped truth twice per tick (the lineage link
and the row each called `state_hash`); the row now takes the link's hash, which saves one
`state_hash` per tick (≈ 1.4 ms warmed, ≈ 8.3 ms full, the rows above) for any run that keeps a
record — derived from those rows, not timed against the old journal, it about pays for the quiet
cycle's `fsync` on the warmed ring and several times over on the full one. A first full-ring run overlapping a short test saw a 442 ms max on the ten-window
quiet cycle that the clean run did not reproduce; like the checkpoint write's 32 ms, it is the
filesystem's tail and J's budget does not rest on the p50 alone.

**After child 4's review** (format v2: the row chain, the per-window budget, the durability of the
claim and the results; same machine, Python and disk, `--samples 100`, `--windows 1 2 10`, each ring
shape a run of its own). The `fsync` counts are counted by the tool.

| Quantity (`tools/measure_clock.py --samples 100`, after child 4's review) | ring as warmed | ring full |
|---|---|---|
| `Executive.cycle`, no record, 1 / 2 / 10 windows | 5.3 / 7.9 / 23.4 ms p50 | 13.5 / 16.6 / 35.0 ms p50 |
| quiet cycle with the record (**1 `fsync`**; row 393 / 420 / 637 B) | 6.6 / 10.2 / 24.8 ms p50 (7.9 / 13.9 / 30.6 p95) | 13.8 / 16.7 / 33.9 ms p50 (16.7 / 20.1 / 36.9 p95) |
| verdict in every window with the record (**6 / 11 / 51 `fsync`**; row 765 / 1,167 / 4,367 B) | 10.5 / 17.3 / 64.0 ms p50 (15.3 / 21.4 / 77.9 p95) | 19.4 / 25.3 / 71.2 ms p50 (24.3 / 32.0 / 82.7 p95) |
| the same verdict cycle with no record | 5.5 / 8.0 / 26.6 ms p50 | 14.1 / 16.3 / 34.3 ms p50 |

A quiet cycle costs what it did. A cycle in which a window wrote results costs `4 + r` `fsync`s for
that window (the claim's file and directory, `output/`, the note, one per result) beside the row's one.
*[The sentence that called ten single-result windows "the worst case" is withdrawn: an agent controls
`r` up to `--max-batch`; see J (xix) and the next table.]*

**After child 4's confirmation review** (same machine, `--samples 100`, `--windows 1 2 10`):

| Quantity (`tools/measure_clock.py`) | ring as warmed | ring full |
|---|---|---|
| verdict in every window with the record, 1 / 10 windows (6 / 51 `fsync`; row 771 / 4,427 B) | 10.2 / 65.4 ms p50 | 17.9 / 79.3 ms p50 |
| the same verdict cycle with no record, 1 / 10 windows | 5.8 / 27.3 ms p50 | 13.4 / 35.6 ms p50 |
| **a full batch of 32 refused commands in each of 10 windows, with the record (361 `fsync`)** | 311.8 ms p50, 328.9 p95 | 319.3 ms p50, 364.0 p95 |
| record read, 20,000 quiet rows (ten windows' marks, eight-digit ticks: 765 B each), per row: check / stream | 25.1 / 26.6 µs | 24.7 / 23.9 µs |

Mission ladder: 8 phases over `[0, 34,560,000)`; shortest phase `entry`, 180,000 ticks. An earlier
scratch measurement of the durable write on this machine saw a 32 ms p95 that neither tool run
reproduced; the tail is the filesystem's and is noted so that J does not rest on the p50 alone.

Four consequences are load-bearing for this record and are stated here rather than in the options:

1. **The per-tick compare-point cost more than the physics, and most of that was the encoder, not
   the coverage.** Until child 6 `plant.state_hash` tagged every scalar (`{"float": repr(x)}`,
   `{"int": n}`, …) and that tagging is what cost 20–26 ms; a SHA-256 over the same truth encoded as
   plain sorted compact JSON distinguishes every pair the tags existed to separate among JSON values
   (`0`/`False`, `1`/`1.0`, `0.0`/`-0.0`, `"1"`/`1`, `None`/`0`), collides list with tuple exactly
   where the tags did, needs every key to be a string (true of the truth; the encoder now *refuses*
   any other key rather than coercing it, and the tool still reports the fact) and a `default` that
   *raises* on a non-JSON value (with `default=repr` such a value would encode as a string and collide
   with an equal `str`, which the `other`/`str` tags kept apart). **Child 6 landed that encoder**,
   with NaN and ±Infinity refused as well (`allow_nan=False`; the tags hashed them as `repr`), and
   the referee holds the pairs table, the refusals and a one-slot ring change as tests. Measured:
   1.4 ms on the warmed ring and 8.5 ms on the full one, of which 0.8 / 8.2 ms is the floor — the
   `repr` of 52,100 floats, which no encoding that writes the whole ring out each tick can go below —
   and the rest is the key check. **Coverage of §6 is unchanged by swapping the encoder**; only the
   bytes hashed changed. So the live deadline `20 ms / m` was missed at every tick for every `m ≥ 1`
   before the swap, and after it one window's tick alone (step + hash) sustains `m ≈ 7` on the
   warmed ring and `m ≈ 2` on the full one, with the ceiling then set by publication I/O (B) and by
   the ring's float encoding, not by physics.
2. The WP01 README section "Measured, not fixed here (WP12)" assigns the compare-point cost to
   WP12, which depends on WP08 — and WP08's first acceptance bullet cannot be shown at `m ≥ 1`
   until the cost falls. The circularity is resolved by making the *encoder* a WP08 child (6), the
   maintainer's, with coverage unchanged — landed, and that README paragraph now says what remains
   (the float-`repr` floor); the README's other option — hashing the ring's cursor and
   the slots the tick wrote — hashes *history* rather than *state* and is a change to §6's contract
   that only the operator can make (A recommends against it).
3. Recovery by replaying `N` ticks from a snapshot costs `N × (step + hash)`: about 1.1 s at
   `N = 50` before the encoder swap on the warmed ring (≈ 1.4 s on the full one), ≈ 0.5 s after it
   on the full ring (measured: 50 × (1.53 + 8.50) ms); `N` is bounded by the restart
   time the operator will accept, not by physics (J).
4. The replay goal of ≈ 8.7 µs/tick is three orders of magnitude from today's 1.5 ms step.
   `plant.md` §10 already says this "is not a Python number"; it is restated so that no option
   below is read as reaching it.

Two observations about the deployed shape, both from `space_chassis` at the session's HEAD and
both chassis-side facts this record depends on:

- The vehicle container is `read_only: true` with `tmpfs: [/tmp]` and one bind mount,
  `./volumes/diode:/diode` (`docker-compose.yml`, service `vehicle`). **There is no private durable
  path in the deployed vehicle container today** — not for a checkpoint and not for `--journal`,
  which refuses any path inside `--diode-dir`. Every agent also mounts `./volumes/diode:/diode`
  read-write (ADR 0001's recorded unmet assumption), so the diode root is agent-writable.
  *Amended by WP08 child 2 (2026-10-09):* met by the chassis's per-slug mounts — chassis commit
  `adf38d6`, on the chassis branch `aurora-port` and not yet on chassis `main`, binds each agent to
  its own `/diode/<slug>` and mounts the diode root on the vehicle service alone; until the chassis
  adopts it, treat the root as agent-writable.
- `containers/serve_vehicle.sh` still forks one `console.py` per slug, each with `--cycles 0`, and
  on `TERM` kills the children; `console.py` catches only `KeyboardInterrupt`, so `docker stop` is
  an abrupt end today (F). ADR 0001 names the one-process change as a chassis follow-up; until it
  lands, a restart design that assumes one executive per directory is designing for a stack that
  does not run that way.

## Part (b): the decisions

Each question names its options, then the consequences in the four columns this programme weighs,
then a recommendation, then an `Answer:` line for the owner. "Fleet" means the validity of the
agent experiment; "replay" means `plant.md` §6 bit-identity and the receipt trace; "budget" means
the live `20 ms / m` deadline and the restart cost; "chassis" means what `space_chassis` must
change.

### A — How wall time maps to ticks, and the multiplier `m` (operator)

Today one cycle is one tick and `--poll` is a wall sleep, so `m = dt / poll`: 0.02 at the console's
default `--poll 1`, **0.004 in the deployed compose (`DIODE_POLL_SECONDS: 5`)**, which is one
mission in about 5.5 wall years. `plant.md` §6's "sim time advances on the multiplier whether or
not anyone is thinking" is true of this loop only in the sense that it does not wait; the multiplier
it advances on was never chosen.

| Option | What it is |
|---|---|
| A1 — tick per cycle (status quo) | `m` is an accident of `--poll` and of the cycle's cost; a slow cycle dilates mission time silently. **Excluded** by WP08's acceptance text ("slow thinking or I/O does not silently dilate mission time") and ROADMAP.md ("never silent clock dilation"); listed so the exclusion is on record. |
| A2 — fixed-rate scheduler with bounded catch-up | The segment records a wall epoch at (re)start; tick `t` is *due* at `epoch + t·dt/m`; each cycle steps every tick that is due, up to a **catch-up burst bound** (ticks per cycle), then sleeps until the next due tick. Lag (`actual − due`) is recorded per tick; a **lag ceiling** (wall seconds behind) is C3's trigger. The two bounds are different numbers: the burst bound limits how hard one cycle works, the ceiling limits how far behind the run may fall. |
| A3 — as fast as possible | No wall anchoring; the executive ticks continuously and publishes on a cadence. This is the replay/batch mode (`plant.py --mission`) and is **excluded as the live policy** by the same acceptance text: `m` becomes whatever the machine gives. |
| A4 — adaptive `m` | The executive lowers `m` when behind and raises it when caught up. Mission time then depends on machine load — the dilation the acceptance text forbids, made policy. **Excluded.** |

Consequences. *Fleet*: only A2 gives the fleet a known relationship between its wall-clock thinking
and the vehicle's drift. *Replay*: all four produce a tick-keyed trace; A2 is the only one whose live
run can be compared against its own schedule, so "missed deadline" is a measurable as completion.md
requires. *Budget*: a cycle that steps `b` due ticks has `b × 20 ms / m` to do it in; feasibility is
consequence 1 above. *Chassis*: `DIODE_POLL_SECONDS` stops meaning "one tick per poll" and is
retired or re-described (B); `m`, the burst bound and the lag ceiling are passed through
`serve_vehicle.sh` as run inputs and recorded in the checkpoint and the release manifest ("clock and
multiplier policy").

**Recommendation: A2** — the only scheduler the issue's acceptance admits, so the *mechanism* may
be built now (child 5). `m` is a run input recorded beside the seed and **required with no
default**: the flag defaults to `None` (the rule `check_console_flags` already enforces for
remembered flags), the checkpoint remembers it, and a first start that names nothing refuses, so the
deployed stack cannot inherit an accidental `m` again. **The value of `m` is the operator's** (answered
below), with the feasibility on record: before child 6 `m < 1` was all this engine sustained; after
the encoder swap one window's tick sustains `m ≈ 2` at steady state before publication I/O
(measured after child 6: step 1.53 + hash 8.50 ms on the full ring).
`m` is **not published** to the windows: it is in the journal and the manifest, and an agent that
compares `published_at` with `met_s` can infer it, which is the experiment; the frame and mirror
schemas (`presentation.yaml`) do not grow a field for it. Changing `m` between segments is
permitted and journaled (the trace is tick-keyed, so a different `m` is a different wall schedule
for the same mission).

Answer (A, scheduler): **A2** — operator, 2026-10-09. Answer (A, the value of `m`): **`m = 1`
for the first integrated run**, raised later only by an explicit manifest decision after child 6's
measurement — operator, 2026-10-09. Answer (A, lag ceiling, C3's trigger): **30 wall seconds
behind schedule** — operator, 2026-10-09. Answer (A, catch-up burst bound): **`k` — no burst**: a
scheduler that has fallen behind runs complete cycles back to back without sleeping, each claiming,
stepping `k` ticks and publishing, until it is on schedule — operator, 2026-10-09, on the
coordinator's recommendation (an interim `2k` was withdrawn). The reason is measured: publication
is paid once per `k` ticks however the ticks are batched, so per mission tick a `2k` cycle costs
what two `k` cycles cost (≈ 14 ms of wall time per 20 ms tick after child 6, ten windows) and
recovers lag no faster (≈ 0.4 s of lag per wall second either way; a 30 s lag clears in ≈ 75 s);
a burst would only save the per-cycle claim overhead, and would stretch B's frame spacing and
command latency to `2k` while catching up. With `k`, both stay exact and shedding (C2) is the only
pressure valve.

**`m = 1` for ten windows needed child 6 first.** At `m = 1, k = 5` a cycle has `5 × 20 ms = 100 ms`
of wall time. Ten windows' publication costs about 24 ms per cycle (the ten-window cycle less step
and hash in the full-ring table). Before child 6, with the tagged hash, a cycle was about
`5 × (1.55 + 25.9) + 24 ≈ 161 ms` on the full ring (≈ 133 ms warmed): every cycle overruns, the lag
reaches the 30 s ceiling within a minute or two of wall time and C3 holds the run — shedding frames
cannot rescue it, because the five ticks alone cost more than 100 ms. After the encoder swap it is
about `5 × (1.53 + 8.50) + 24 + 1.4 ≈ 76 ms` at p50 (≈ 90 ms at p95; a checkpointing cycle adds
≈ 9 ms), so `m = 1` is sustainable with modest headroom and the ten-window ceiling at `k = 5` is
about `m ≈ 1.3`. That figure, not the one-window `m ≈ 2`, is the baseline for any later decision to
raise `m`. **The first integrated run at `m = 1` therefore waited on child 6**, which has landed; the
measured after-table is in the evidence section, and the ten-window cycle it timed (one tick per
cycle, full ring) fell from 50.0 to 33.8 ms p50.

### B — Publication and claim cadence `k`, separated from the tick (maintainer; operator confirms)

If A2 steps many ticks per cycle, "publish every tick" means 50 frames per mission second per
window, and the identity instrument has no cadence of its own to thin it. WP08's outcome text is
"make physical stepping independent of agents and publication", so this is WP08's to decide — and
the cadence is bounded from above by the registry: 14 channels declare `rate_hz: 10` and 7 declare
`rate_hz: 5` (`channels.yaml`), and five `transition_evidence` guards declare `max_age_ms` of 100
or 200 (`mission.yaml`, invariant D). A frame cadence slower than a channel's declared rate strands
that channel: it can never be published at the rate the registry promises.

**Freshness guards are judged on the vehicle's internal evidence, not on published frames.** The
`transition_evidence` guards belong to the posture machine — the vehicle's own safety kernel
(WP05) — which reads the instrument layer's evidence with its sample times every tick (`plant.md`
§7: each point carries its decision age; stale evidence never satisfies a clear predicate). The
files are the fleet's view, not the kernel's input, so publication cadence, shedding (C2) and a
catch-up cycle cannot make a safety guard pass or fail and cannot change the replayed trace. What
`k` must honour is the registry's declared *publish* rates, which is what B2 does.

| Option | What it is |
|---|---|
| B1 — `k = 1`, every tick | Status quo cadence. 50 frames per mission second per window; the ring of 300 slots holds 6 mission seconds; ten windows at `m = 1` is 500 frames per wall second plus the static rewrites. No channel is stranded. |
| B2 — `k = 5` (`k ≤ tick_hz / max rate_hz`) | 10 frames per mission second; the 10 Hz channels appear in every frame and the 5 Hz ones in every second frame, per `presentation.yaml#ring.cadence_classes`. This honours every declared publish rate exactly (a 10 Hz channel has no frame to spare). The ring holds 30 mission seconds. At `m = 1` with ten windows: 100 frames per wall second. |
| B3 — `k = 50`, one frame per mission second | Strands the 14 + 7 fast channels and makes the 100/200 ms guards unsatisfiable: the fleet would see a registry promising rates the vehicle never publishes, and the posture machine's freshness half (WP05) could never hold. **Excluded** unless the operator also re-declares those rates. |

**The claim cadence is the publication cadence.** Each cycle the executive claims every console
once, validates, stamps the accepted effects at `offset_us = 0` of the cycle's first tick (ADR
0001: validation and effect read the same truth at the same instant), steps `k` ticks — the
remaining `k − 1` carry no effects — and publishes. A command written to a console therefore takes
effect at most `k` ticks of mission time after it was written, at any `m`; in wall time that is
`k × dt / m`. This is what "cycle" means for the contract's §2.2 and §2.4 on the vehicle side.

**Catching up** (A2, after the scheduler fell behind) is complete cycles run back to back without
sleeping — the burst bound is `k` (A) — so every cycle still claims, steps exactly `k` ticks and
publishes, and frame spacing and command latency stay exactly `k` ticks while catching up. While
shedding (C2), frames are skipped and the skip is visible in `seq` and the ring's `losses`; "`k`
ticks between frames" holds whenever the executive is not shedding.

Consequences. *Fleet*: B2 gives a telemetry rate and a command latency in mission time that are the
same at any `m`, which is what makes a finding at `m = 2` comparable to one at `m = 0.5`. *Replay*:
with `k` a run input, which ticks got a frame and which tick a command landed on are functions of
the trace, which completion.md requires ("validate that trace through command receipts and
permitted evidence as well as internal state"). *Budget*: publication is amortised over `k` ticks;
the static `HELP.md` (78 KB) and `README.md` (7 KB) rewrites move to a much slower cadence (C).
*Chassis*: `DIODE_POLL_SECONDS` is retired; `contract/diode_probe.py`'s "telemetry advances on its
own" is met at any `k`.

**Recommendation: B2, `k = 5`**, a run input, required with no default like `m`, remembered in the
checkpoint. The maintainer's in form; listed for the operator's confirmation because it fixes what
the fleet sees and how fast a command lands.

Answer (B): **B2, `k = 5`** — 10 Hz frames, consoles claimed at the same cadence — operator,
2026-10-09.

### C — Overload: a tick misses its `20 ms / m` deadline (operator)

| Option | What it is |
|---|---|
| C1 — dilate and record | The scheduler falls behind; mission time runs slower than `m` says; every tick's lag is journaled and the segment's missed-deadline count is in its manifest. Bounded by A2's lag ceiling. |
| C2 — shed publication first | Before dilating, skip the static `HELP.md`/`README.md` rewrites, then frames (never ticks, never results, **never the mirror** — contract §2.4 says `state.json` is rewritten every cycle and the probe checks it). `seq` and the ring's `losses` make the shedding visible and countable. |
| C3 — stop and say so | Past the lag ceiling the executive stops ticking, records an operator hold with its reason, and keeps rewriting the mirror (as in D1) until the operator resumes or ends the run. |
| C4 — drop ticks | Skip the physics of the missed ticks to catch up. **Excluded**: the trace is tick-keyed and a dropped tick is physics that never ran — "advance it by guessed time" with the guessing done by the scheduler. |

Consequences. *Fleet*: C1 within the ceiling is invisible in mission terms and visible in wall terms
(`published_at` advancing faster than `met_s`); C3 is a visible hold (`executive.tick` frozen,
`published_at` moving). *Replay*: C1–C3 leave the trace intact; only C4 breaks it. *Budget*: C1 is
what "missed deadline" means in completion.md's measurement list; C3 turns an unbounded overload into
a recorded event rather than a run whose `m` quietly became something else. *Chassis*: C3 needs the
pause control surface (D) and a status the operator can see (`scripts/status.py`; the journal is
the source).

**Recommendation: C2 (static files, then frames; never the mirror), then C1 within the lag ceiling,
then C3; never C4.** The ceiling and the burst bound are the operator's numbers and sit beside `m`
(both answered, see A).
Whether the fleet is *told* about dilation beyond what the two published clocks already reveal: **no
new signal** — the schemas are declared and the existing pair is sufficient for an agent that looks.

Answer (C): **shed (static `HELP.md`/`README.md`, then frames; never the mirror) → dilate within
the recorded bound → stop with a recorded hold** — operator, 2026-10-09. Never dropping ticks is
C4's exclusion by `completion.md`, not a choice the answer made.

### D — Pause and operator hold (operator)

Two different things share the word. `REQUEST_HOLD` is a *posture* (`mission.yaml#postures`):
in-simulation, the clock runs, `posture_entry_seq` moves, and it is WP05's. An *operator pause* is
out-of-simulation: the ticks stop and wall time passes. Only the second is this record's.

| Option | What agents see while paused |
|---|---|
| D1 — mirror continues, no new ticks, no claim | `state.json` is rewritten each cycle with `published_at` advancing and `executive.tick` frozen; no frames (a frame is a tick's); consoles are not claimed, so a command written during the pause waits in the agent's file and is claimed at the first resumed cycle. |
| D2 — publication continues as if ticking | Frames appended with an unchanging `met_s`; the ring fills with identical frames and lies about cadence. |
| D3 — the window goes dark | Nothing rewritten. Indistinguishable from a dead vehicle (F-14's frozen-value problem in `presentation.yaml#ring`). |
| D4 — no pause; only stop | The operator's only tool is to stop the process, which is F. |

Consequences. *Fleet*: D1 is distinguishable from a dead vehicle (the mirror moves) and from a
running one (the tick does not); D2 makes the ring lie; D3 teaches the fleet that silence may be
fine. *Replay*: a pause is a wall event with no tick, so none of these touch the trace; D1 journals
the pause with wall stamps. *Budget*: none. *Chassis*: a pause needs a control the operator reaches
and the fleet cannot — a file in `--state-dir` (H) or a signal to the one console process; the
chassis names the mechanism.

**Recommendation: D1.** Dwell, deferral age and budgets (under E2) do not move during a pause,
because none of them are on the wall clock. The claim is deliberately *not* performed while paused:
the contract's claim is a promise that the batch is evaluated against the current truth, and a
claimed-but-unticked command would be neither refused nor run.

Answer (D): **D1** — operator, 2026-10-09.

### E — The command budget's time base (maintainer; operator confirms)

`budget.window_seconds: 3600` and `oldest_expires_in_seconds` are in the contract's example (§2.4)
and in the mirror today: `oldest_expires_in_seconds` counts the wall clock from `Window.started`,
and the spend is `Window.accepted`, a counter that never expires within a run and is reset to zero
by a restart — which contract §6 forbids ("the operator's ceiling counters should survive a restart
rather than refilling"). The surviving is G's. The *time base* is this question.

| Option | What a window-hour is |
|---|---|
| E1 — wall hour | As the fake diode (`contract/fake_diode.py`, `DIODE_HOURLY_MAX`) counts it. At `m = 2` an agent gets twice the commands per mission hour it would get at `m = 1`. |
| E2 — mission hour | `tick_hz × 3600` ticks; spend is a list of ticks; `oldest_expires_in_seconds` is mission seconds. The budget is a property of the vehicle, like the dwell. |

Consequences. *Fleet*: E1 ties the agents' command rate to the operator's choice of `m`, so two runs
at different multipliers are not comparable; E2 makes the budget part of the vehicle the fleet is
flying. *Replay*: a refusal is a receipt, and completion.md validates replay through receipts; under
E1 a budget refusal depends on the wall clock and the receipt trace cannot be replayed. **This is the
discriminating constraint.** *Budget*: none. *Chassis*: the fake diode's wall semantics are a
fixture's; the contract's example gives the field names, not the clock.

**Recommendation: E2.** The counter-argument on record is that the contract's prose reads naturally
as wall time and the fake diode counts wall time; the reply is that the contract is silent on the
clock and explicit that receipts are the vehicle's. In the checkpoint the spend is an **opaque,
versioned subsection** (`spend: {version, …}`) so that child 1 (the format) did not need to wait on this
answer.

Answer (E): **E2, the mission hour** — operator, 2026-10-09.

### F — Downtime: the process or container is down (operator)

| Option | What happens to mission time while the vehicle is down |
|---|---|
| F1 — freeze | Mission time does not advance. The vehicle resumes at the published tick (J); the wall gap `(wall_down, wall_up, tick)` is journaled and goes in the manifest. |
| F2 — advance | On restart the executive steps the ticks the wall gap would have carried at `m`, with no commands (none could be claimed). The elapsed wall time is measured rather than guessed, so completion.md's letter is met; its spirit — the vehicle flew unattended with its consoles unread — is not. |
| F3 — the run ends | A restart invalidates the run; the operator starts a new world (`--new-world`, K). |

Consequences. *Fleet*: F1 is the only option under which every tick the vehicle ever ran was a tick
some window could have commanded. *Replay*: F1 and F2 both leave a tick-keyed trace; F2 adds a
stretch with no effects whose length depends on how fast the operator restarted. *Budget*: F2's
catch-up is C's overload at its worst. *Chassis*: `restart: unless-stopped` restarts the container
without an operator present; F1 is the one that is safe to run unattended.

**Recommendation: F1.** The `boot_id` changes, `met_s` continues, `published_at` jumped by the wall
gap: the existing surface already says "the vehicle restarted and lost no mission time", which is
the true statement; **no new signal**. Two mechanics belong with F whichever way it goes: **a
graceful stop on `SIGTERM`** — finish the current cycle, write the checkpoint and the record, exit 0
— because `serve_vehicle.sh` traps `TERM` and today `console.py` catches only `KeyboardInterrupt`;
and **a kill mid-write leaves the previous generation valid** (I), so an abrupt end costs at most
the ticks since the published tick, which J recovers.

Answer (F): **F1** — freeze; resume at the recovered tick; the gap journaled — operator,
2026-10-09.

### G — What the checkpoint contains (maintainer)

Most of this is fixed by part (a) (completion.md: "one authoritative state, event queue, mission
clock, RNG lineage and conservation ledgers"; contract §6: counters survive). What follows is the
field list the fixed decisions imply, and then the two genuine questions inside it.

Executive: `format` id; `engine`, `python`, `platform` (I); `world_id`; `scenario`; `seed`; the
clock inputs `m`, `k`, `N`, burst bound, lag ceiling (A–C, J); `tick`; `phase` and
`phase_entry_seq` (M); `tripped_interlocks` and `max_batch` (operator state, remembered like the
rest — see "remembered versus named" below); `truth` — every key of the value map, including
`<state>__delay` rings, `<state>__residual` accumulators and `<state>__shortfall`; `dwell`; the
`lineage_head`; the global `receipt` counter; `rng` (reserved, see below); `segments` — one entry
per boot with `boot_id`, wall epoch, first tick and the journal segment id. Per window: `slug`,
`ring_slots`, `seq`, `receipts`, `spend` (E, opaque and versioned), `deferred` (every entry with
`accepted_tick` and `due_tick`), `arms`, and `published_tick` (the high-water mark, J). Not in the
checkpoint: `variables` (the agent's, preserved in its own `console.json`), the generated
`HELP.md`/`README.md` (regenerated), the ring's files (the directory's), anything derived.

**RNG lineage** is, at `bdfaf8b`, the seed alone. `plant.step(world, values, dt, gaps, effects)`
has no random-number parameter at all, and `tools/faults.py` draws a fault *schedule* up front from
name-keyed streams (`stream(master_seed, fault)`), which is a pure function of the seed. Per-tick RNG
state (instrument noise, hazard accumulators) arrives with WP09. The format carries an `rng` section
from the first version so that WP09 adds fields rather than a format; a checkpoint missing a field
the engine needs is incompatible (I), never defaulted.

**Remembered versus named, on a restart.** Every run input above is remembered by the checkpoint,
and every flag that names one defaults to `None` (`check_console_flags`). On a fresh start a
missing `m`, `k` or `N` refuses. On a resume: `--phase` that disagrees with the checkpoint refuses
(the phase is derived from the tick, M); `m`, `k`, `N`, the bounds, `--max-batch` and
`--closed-interlock` may be named anew and the change is journaled as a segment event (they are the
operator's live state, and `--closed-interlock` in particular is how a tripped interlock is asserted
across a restart). `--max-batch` and the allowance ceiling join the manifest's run inputs.

| G1 — arm tokens across a restart | `arm_event` tokens are bound to the arming window and event (ADR 0001) and salted with the window's `boot_id`; nothing in the token is checked against the current boot. Options: **survive** (the token is an accepted command's outcome; `execute_event` revalidates everything at the moment of effect anyway) or **expire** with a named refusal at the first `execute_event` after a restart. Recommendation: **survive**, because contract §6 says nothing executed is forgotten and an arm *was* executed; expiring it would make a restart a safety event the crew never saw. |
| G2 — the deferral queue | Fixed by ADR 0001's clarification: deferrals keep their `accepted_tick` and `due_tick`, settle or `EXPIRE` by simulated age, and the pause/downtime policies above do not age them. Listed because the queue is per window and the obvious implementation forgets it. |

Answer (G, field list and G1 survive, G2 as fixed): **as recommended** — coordinator, under the
owner's standing delegation (2026-10-09); open to the maintainer's revision.

### H — Where the checkpoint lives (maintainer with chassis reviewer)

Fixed: not in any agent-writable path (completion.md). Today's deployed container has none that is
not agent-writable (evidence section). *Amended by WP08 child 2 (2026-10-09):* the per-slug mounts
(chassis `adf38d6`, branch `aurora-port`, not yet on chassis `main`) take the diode root out of the
agents' reach; H1's private mount is the separate chassis item 1 below, and H2 stays excluded on its
own reasoning. Until the per-slug mounts are adopted, treat the root as agent-writable.

| Option | Where |
|---|---|
| H1 — a private writable mount the chassis adds for the vehicle service alone, passed as `--state-dir` | The checkpoint, its previous generation, the lock and the journal live there; the console refuses a `--state-dir` inside `--diode-dir` by the same `is_relative_to` rule the journal uses; agents have no mount of it. |
| H2 — the diode root beside `.executive.json` | Agent-writable in the deployed compose, so a forged checkpoint is a forged world; and `plant.md` §7's one-way boundary would have truth on the agents' volume. **Excluded** even after the mounts are fixed: a private thing should not share a volume with the adversary's directory. |
| H3 — `tmpfs` (`/tmp`) | Lost on a container restart, which is the case F exists for. **Excluded.** |

**Recommendation: H1**, with three consequences for ADR 0001. (i) `.executive.lock` moves to
`--state-dir`: an exclusive lock on a volume the adversary can write is a lock the adversary can
hold. (ii) The checkpoint is authoritative for identity (`world_id`, slugs, seed, scenario, tick);
`<diode-dir>/.executive.json` remains as the windows' side copy, rewritten from the checkpoint at
every publication — and **when a verified checkpoint exists, a root record that is missing,
garbled or disagrees is not a refusal**: it is rewritten and the mismatch is journaled, because
refusing on a file the agents can write would hand them the stop button. A *readable* root record
naming a different world than the checkpoint's is a different matter — two executives with separate
state directories on one diode directory — and refuses, and the state directory records the diode
directory it serves so a mismatch is caught from either side. The legacy `pending.json`
refuse-only checks become advisory for the same reason. Without a checkpoint (a fresh directory)
the ADR 0001 rules stand unchanged. *Amended by WP08 child 2 (2026-10-09):* the refusal on a
readable record naming a different world relies on the per-slug mounts (chassis `adf38d6`): with
them the root is written only by the vehicle or the operator, so a foreign world there is two
executives or an operator's error; while the root is agent-writable it is a stop button an agent can
press at a restart, and the refusal is kept knowing that. *Amended by WP08 child 2, second review
(2026-10-09):* a directory planted at `.executive.json` beside a verified checkpoint makes the rewrite
fail (EISDIR) and the start refuses by name, at every restart; that refusal is kept on the same
ground — under H the root is the vehicle's, and with the per-slug mounts nothing else can plant there
— and child 3 must revisit it if the root is still agent-writable when resume lands. (iii) The clarification "the directory's record is
authoritative" is amended to "the checkpoint is authoritative and the record is its copy".
*Chassis*: one new volume and one new mount on one service (not the ten-edit fleet mount list);
`serve_vehicle.sh` passes `--state-dir`; `scripts/status.py` may read the checkpoint's header (never
truth) for its one line per agent. **This is the one decision that blocks deployment evidence** for
the resume path; local tests use a scratch directory and are not blocked.

Answer (H): **H1, with the three ADR 0001 consequences** — coordinator, under the owner's standing
delegation (2026-10-09); open to the maintainer's revision. The mount itself is a chassis follow-up
and the chassis reviewer is pending.

### I — Atomicity, integrity and compatibility (maintainer)

Fixed by release.md: a format identifier and a compatibility identifier are in every manifest, and
no engine may be assumed to read an arbitrary saved world. The mechanics follow:

- **Write**: serialise; write to a temporary under a random name in `--state-dir`; `fsync` the
  file; rename the current generation to `checkpoint.prev.json`; `rename` the temporary over
  `checkpoint.json`; `fsync` the directory. A kill at any point leaves one verifiable generation.
- **Integrity**: a SHA-256 of the body inside a small header, and the body's byte length; a
  checkpoint whose hash or length disagrees is *corrupt* (K).
- **Compatibility**: `format: vehicle.checkpoint.v1` (*v2 since WP08 child 3, 2026-10-10: J (xxiii), (xxiv)*); `engine`; `python` (`major.minor.micro`);
  `platform` (`platform.platform()`); `tick_hz`. A mismatch on any of them is *incompatible* (K).
  *Amended 2026-10-09 (coordinator, under the owner's delegation, on child 1's review):* `platform` is
  `platform.system()`, `platform.machine()` and `platform.libc_ver()` — OS, architecture and libc — and
  **not** `platform.platform()`, which embeds the kernel release; in a container that release is the
  host's, so a host kernel patch would have made every checkpoint incompatible with no fallback and
  put `restart: unless-stopped` into a crash loop, while "same platform" in `plant.md` §6 is about what
  can change float arithmetic — the architecture and the libm — which the kernel release does not.
  `plant.md` §6's equivalence is "same build, same platform", so a Python or platform change ends a
  saved run exactly as an engine change does. No migration path in v1: release.md's rollback policy
  is "select a previously accepted set", not "convert".
- **What `engine` is, and the consequence the operator should see.** Two candidates: the vehicle's
  git commit, or a SHA-256 over `plant.corpus_files(root)` plus the bytes of `tools/plant.py`,
  `tools/console.py` and `tools/faults.py`. With the commit, **any vehicle patch — a README round,
  a docs-only ADR — ends every saved run**, and `--new-world` is the only exit; with the file hash,
  only a change to what the physics or the executive reads does. Recommendation: the **file hash**
  as `engine`, with the git commit recorded beside it for the manifest. Either way the consequence
  is operator-visible: a deployment that upgrades the vehicle image mid-run will refuse to resume
  and the operator must choose `--new-world`.
- **Cost**: 1.0–1.4 ms p50 per durable write of a 270–470 KB checkpoint (a lower bound: the measured
  write omits the generation rename and the directory `fsync`), p95 under 2.1 ms in both
  tool runs, with a 32 ms tail seen once; J's cadence does not rest on the p50.

Answer (I, mechanics and `engine` as the file hash with the commit recorded beside it): **as
recommended** — coordinator, under the owner's standing delegation (2026-10-09); open to the
maintainer's revision.

### J — Checkpoint cadence, the durable record, and what a frame may describe (maintainer)

| Option | Cadence |
|---|---|
| J1 — a full checkpoint every tick | 50 durable writes per mission second: 50–70 ms per mission second at the measured p50 for the write alone, plus ≈ 8 ms of `json.dumps` per tick on the full ring and, on the tail seen once, up to 1.6 s. **Excluded** by the measurement. |
| J2 — a full checkpoint every `N` ticks, plus a **durable per-cycle record** whenever a cycle produced any verdict, plus a durable **published-tick mark** at every publication | Recovery = load the snapshot, replay the records by tick (§6 rule 6), then step effect-free ticks to the published tick. Nothing the fleet has seen is lost and no mission time is lost. |
| J3 — at phase boundaries only | `plant.md` §6's full snapshots. Phases are hours long. Permitted as the *additional* full snapshot §6 asks for, not as the restart cadence. |

Three rules follow from `presentation.yaml#frame` ("`met_s` cannot jump backward") and contract §6
("nothing already submitted runs twice; nothing already executed is forgotten"):

1. **The per-cycle record is written whenever any verdict exists, not only when an effect does.**
   A refusal moves the window's `receipts` counter and may move `spend`; an accepted deferral or
   arm moves the queue and the arms with no effect this tick. If only effects were recorded, a
   restart would repeat window-local receipt numbers and forget accepted deferrals. The record
   carries, per tick: the effects (stamped), every verdict's `receipt`/`local`/`window`/`state`/
   `command`, the spend delta, the deferral and arm changes, and whether each result was written.
   This is the journal's content today plus the window-local half, so **the journal becomes the
   record**: `--journal` is retired as a separate flag and lives in `--state-dir`, one segment file
   per boot (`journal.<segment>.jsonl`), each segment naming its `boot_id`, first tick and the
   segment before it, and replay concatenates segments by tick.
2. **`k` and `N` are independent**, and the published tick is the third number. A frame or mirror
   may describe any tick the executive has reached; before it does, the window's `published_tick`
   is made durable (a few bytes, appended to the record). On restart the vehicle replays the record
   onto the snapshot and then steps forward to the greatest `published_tick`, so `met_s` never goes
   backward across a boot and F1 loses no mission time. The cost is bounded by `N × (step + hash)`
   for the replay (consequence 3) plus `(published − replayed) × (step + hash)` for the effect-free tail (the lineage needs the hash
   of every tick).
3. **A result is written only after its cycle's record is durable**, and the record notes that it
   was written. A crash between the two re-publishes from the record on restart. Under L(b) the
   result's name is a deterministic function of the trace (MET stamp, slug, command), so
   re-publication is **idempotent**: if the exact name exists, skip; the contract's "exactly one
   result per command" holds without a second durable write. Under wall stamps the name differs on
   every attempt, so "result written" must itself be durable per result before the next cycle —
   one more reason for L(b).

Consequences. *Fleet*: one frame every `k` ticks, every frame it has ever seen is a tick the vehicle
stands behind after a restart, and a restart loses nothing it saw. *Replay*: the per-cycle record
*is* the §6 replay trace; recording it for recovery and recording it for replay are the same file.
*Budget*: one `fsync` per cycle that produced a verdict (agent command rates are seconds to minutes
apart), one per publication for the mark (1.0–1.4 ms p50 every `k` ticks), one full checkpoint per
`N` ticks; recovery `≤ N × (step + hash)`. *Chassis*: none beyond H.

**Recommendation: J2**, `N` a run input required with no default (recommended `tick_hz`, one
mission second; recovery ≈ 1.1 s today on the warmed ring and ≈ 1.4 s on the full one, ≈ 0.5 s
after child 6), `k` per B.

Answer (J): **J2 with the three rules; `N = tick_hz`** — coordinator, under the owner's standing
delegation (2026-10-09); open to the maintainer's revision.

*Amended by WP08 child 4 (`#22`, 2026-10-09), where the three rules were silent; the record's format is
`vehicle.record.v1`, stated in `tools/console.py` above `RecordRefused`.* (i) **Every cycle writes a tick
row, quiet or not.** Rule 2's mark is written at every publication and a cycle always publishes (the
mirror, C2), so a row is `fsync`ed every cycle anyway; the compare-point and lineage link ride in it
for bytes, not for an `fsync`. That answers "how does replay know the tick range": a record's ticks are
contiguous by construction, a hole is a missing cycle wherever it falls (refused by name), and replay
holds every tick to the run's own compare-point. Under child 5's `k` the `k` rows of a cycle can share
its one `fsync`. (ii) **One `fsync` carries rule 1 and rule 2**: the tick row holds every publishing
window's `published` mark (the tick, and the frame number it will write, so a restart can continue `seq`
past any frame that may be on disk); a mark overstated by a publication that then failed is safe, an
understated one would not be. (iii) **The `results_written` note is its own `fsync`**, after the
results and before the next cycle, naming each result by window and window-local receipt — never by
file name, so L(b) changes nothing in it. The residual window — a crash after a result file and before
its note — is the one this record already names as "one more reason for L(b)". (iv) **The record
carries each verdict's body**, which rule 1's list omits: a re-publication (rule 3) needs the text, and
validation cannot be re-run to make it (its inputs include the agents' `variables`, which the vehicle
preserves for them and does not keep). (v) *[Superseded by (x) and (xiii) below.]* **Agent text is bounded in the record**: any agent-derived
string (a command, a body, a deferral's command) past 4 KiB is kept by its prefix with the SHA-256 and
length of the whole, as child 2 bounded the superseded root record; a resume (child 3) must treat a
deferral whose command was cut — possible only with tokens the argument parser ignores — as one it
cannot settle from the record alone. (vi) **A segment's header is written with its boot's first row**,
not at construction, so a resumed executive's `first_tick` and predecessor are the restored ones; the
boot's entry joins `segments` once the header is durable. (vii) **An explicit `--journal` without
`--state-dir` is the same record with the same durability**; every boot appends its own header to the
one file. (viii) **A record that cannot be made durable stops the run** by name (exit 3) before the cycle
is published; the commands that cycle claimed are lost with it. (ix) The checkpoint cadence `N` is not
written by child 4: its tests write checkpoints as child 2's did, and the cadence lands with the resume.

*Amended after child 4's two independent reviews (Codex `gpt-6-astra` high and Claude Opus, both
"request changes"; decided by the coordinator under the owner's delegation, 2026-10-09). The record's
format is now `vehicle.record.v2`, and a v1 segment is refused by name.* (x) **Every bound counts the
encoded bytes**, because rows are ASCII-escaped JSON (an `é` is six bytes, a lone surrogate twelve): a
receipt's command keeps 256 B and its body 4 KiB, each with the whole's SHA-256 and length; one
window's receipts in one cycle share 16 KiB, past which a receipt is fingerprint-only (at most 512 B:
numbers, state, verb prefix, the SHA-256 and length of command and body), its result file written in
full. A row is therefore at most `windows × (16 KiB + 2 × max_batch × 512 B)` plus deltas the command
schemas bound. "A refused command's echoed text beyond a small prefix" is read as the command field
(256 B): a refusal's body keeps 4 KiB because its reason (up to ≈ 1.5 KB for an interlock refusal) is
what a re-publication must carry, and the per-window budget is what bounds the volume. (xi) **No
pruning in child 4**: the record is §6's replay trace and release.md's evidence. Its size: a quiet tick
row is ≈ `365 + 27 × windows` bytes (393 B at one window, 637 B at ten), so ≈ 14–22 GB per mission at
`k = 1` and ≈ 13–15 GB at `k = 5` if every tick keeps its row, plus a few kilobytes per commanded
cycle. *[Figures corrected by (xx).]* The reader is two sequential streams (check, then replay) whose
memory does not grow with the record; recovery stays `≤ N × (step + hash)` plus that scan. (xii) **Every line carries a chain value**,
`sha256(previous chain + "\n" + the line's canonical bytes)`, seeded by the segment header from the
chain its predecessor ended on, so any edit, insertion or deletion of any field refuses by name. This
detects corruption and truncation *[mid-file truncation only: see (xx)]*, not a forger: there is no secret, and whoever can write the `0600`
state directory can re-chain — against which every field is typed and bounded, a published mark is
its row's own tick, a results note is its row's (world, boot, tick, and only that row's receipts,
once), and the physics is re-run. A line that is not JSON is accepted only as the last line of its
segment, and the next segment's header must then name the chain of its predecessor's last intact line;
anywhere else it refuses. (The review asked for "the last line of the last segment" only; a boot that
dies mid-append leaves such a line before the next boot's segment, and the successor's chain anchor is
what makes accepting it checkable.) (xiii) **A deferral is its parse**: queued, checkpointed and
recorded as its verb, its schema-checked arguments and the canonical command they spell, and settled
from that — because `parse_arguments` keeps the last value of a repeated key, a prefix of the raw line
could be another valid command, which (v) wrongly said it could not. (xiv) **The record must continue
the checkpoint's own segment history**: its first segment is one the checkpoint lists (and the
checkpoint's last segment is then in the record), or continues the checkpoint's last segment from the
snapshot's tick and chain; a checkpoint's segment entries carry the chain reached when it was taken,
and on that segment the chain at the snapshot's tick must match. (xv) **With a record, every promise
is a disk fact**: a cycle that claimed a batch `fsync`s the rewritten console and its window directory
before the tick row; each window's result files and then its `output/` directory are `fsync`ed before
its note, and the note is written right after that window's results, before its frame and mirror. A
quiet cycle is still one `fsync`; a cycle in which every one of ten windows wrote a result is 51
(measured below). Without a record nothing new is `fsync`ed. *[The cost sentence is replaced by (xix).]* (xvi) **Receipt numbers are recorded as
issued**: every tick row carries the global receipt counter and each window's, and verdicts made before
an internal fault are kept and published, so no boot reissues a number. (xvii) An existing explicit
journal this process owns is made `0600`, one another user owns is refused by name; a startup event
`fsync`s its directory; a note that cannot be made durable says the window's results are on disk.

*Amended after child 4's confirmation reviews (Codex `gpt-6-astra` high, "request changes"; Claude
Opus, "approve with findings"; both judged (xii)'s torn-line rule correct and accepted (x) and (xv);
decided by the coordinator under the owner's delegation, 2026-10-09).* (xviii) **Replay correctness.** A
deferral's canonical command is spelled from its arguments in *sorted* key order — live, in the
checkpoint and in the record — because every encoding of `arguments` sorts them: in the agent's order,
a deferral written `target=… source=…` was accepted live and failed its own schema on read-back, and
the first pass checks every row, so it poisoned the record for good. A map state's value is taken in
key order (`value_of`), live and after a restore, so a dwell's return guard does not depend on whether
the map came from the live history or from a key-sorted checkpoint. A segment the replay reaches stays
in the body's history with the chain it ended on, a header-only one too (a boot whose first cycle was
torn): otherwise the next boot names its predecessor's predecessor and the record forks. The reader
keeps each segment's byte offset, so a shared `--journal` of many boots is read twice in all, not once
per boot; and both writers of the record — the executive's and `main`'s startup events — make an owned
file `0600` and refuse another user's by name. (xix) **(xv)'s cost, restated without the cooperative
assumption (x) rejected.** A cycle `fsync`s once for its row and, for each window that wrote results,
`4 + r` times (the claim's file and directory, `output/`, the note, and one per result `r`), and an
agent controls `r` up to `--max-batch` — up to `2 × --max-batch` when a full batch of deferrals settles
beside a full new batch. So the worst case is `1 + windows × (4 + 2 × max_batch)` `fsync`s; a full refused
batch of 32 in every one of ten windows is 361, measured at ≈ 312 ms p50 on the warmed ring and ≈ 319 ms
on the full one, three cycles' budget at `m = 1, k = 5`. **`--max-batch` is the operator's throttle on
it.** Such a cycle is overload, which the operator's C answers by degrading — shedding the static files
and frames, then dilating within the lag ceiling — and only an overload sustained past the ceiling
reaches C3's recorded hold; it does not stop the vehicle by itself. The per-result `fsync` is there
because result names are wall-stamped; child 12's L(b) makes re-publication idempotent by name, after
which it can be retired. (xx) **(xi) and (xii), with their numbers.** At mission-end tick numbers
(eight digits) a quiet row is 414 B at one window and 765 B at ten, so a mission at `k = 1` is ≈ 14–26 GB.
Reading costs ≈ 25 µs per row per pass on this machine (`tools/measure_clock.py`; the reviewer measured
≈ 41 µs on another run): ≈ 4.5 s per mission hour per pass at 50 Hz (≈ 9–15 s for the check and the
replay together), and at mission end ≈ 29–47 min to read and replay the whole record, ≈ 43–70 min with
`unwritten_results`' pass — a cost of time, not memory, and the reason the checkpoint cadence and
retention (child 3) matter. (xii)'s "detects corruption and truncation" is **corruption and mid-file
truncation**: deleting complete rows at the *end* of the last segment is accepted, because there is no
end anchor — a correct filesystem cannot lose an `fsync`ed row, so a missing tail is what a crash
before the `fsync` leaves, and nothing was published from it.

*Amended by WP08 child 3 (`#21`, 2026-10-10), the resume; its design note and two independent design
reviews (Codex `gpt-6-astra` high; Claude Opus), decided by the coordinator under the owner's
delegation.* (xxi) **Rule 2's effect-free tail is empty.** Since (i) every cycle writes a row, and a
window's published tick is set only after its row is durable, so no published tick is past the last
durable row `L`: a resume replays the record from the checkpoint's tick `T` to `L` and steps nothing
more, and mission time resumes at `L` (F1). (xxii) **`N` is `tick_hz`**, recorded in the checkpoint's
clock inputs (`clock.N`) and taken from there by a resume, until child 5 makes it a run input. The
executive checkpoints at the end of every cycle whose tick is a multiple of `N` — after every window
has published, so the checkpoint's anchor is past the cycle's notes and what it owes is exact — and
also at a world's genesis (tick 0, before its first cycle), at the recovered tick before a resumed run
claims anything, and at a clean end (cycles exhausted, or an interrupt between cycles; never
mid-cycle). The genesis and the resume's checkpoint are mandatory — a failure refuses the start with
nothing bound or claimed; a cadence or clean-end failure is recorded, journaled (`checkpoint_failed`,
with the consecutive count) and on stderr, and the run goes on, because the record is what is durable.
(xxiii) **Restart cost is `O(L − T)`.** A checkpoint's segment entries carry the byte offset just after
the last line it covers (checkpoint format `vehicle.checkpoint.v2`; no v1 was ever written by a
deployed vehicle), and a resume reads the record from that anchor: the seek point is held to the file
(a regular file through the held handle, within its size, just after a newline), other segment files
are read to their first header only (past at most 64 startup events, or refused), a boot continuing
the anchor is read whole, one the checkpoint lists is skipped, and any other refuses. **What this
trusts**: the record before the anchor is attested by the checkpoint, which is verified (I) and lives
in the same private directory; it is not re-verified, so an edit below the anchor that keeps the
anchor's line boundary and everything after it is not detected by a resume, by design. The
whole-record read still verifies end to end, offline. (xxiv) **Commitment 1, as the contract
allows.** Every command whose cycle's row became durable has exactly one result; a batch claimed in a
cycle that died before its row is lost with no result (contract §2.2, "a crash mid-batch loses the
rest of that batch; it never replays it"; (viii)). A result durable in the record and not on disk — a
crash between the row and the results, or a publication that failed live — is an *obligation*,
carried by the checkpoint (`obligations`), so what a window is owed does not depend on which
generation a resume chose; a resume adds every unnoted verdict after `T`, and the window's next
publication writes each once, before its own results, after looking for it in its `output/` by its
exact receipt line. That look is bounded per window as a whole (4,096 entries examined, 256 KiB read,
candidates by name, each opened without following a link and `fstat`ed regular); past the bound the
result is written. So exactly one result per recorded command holds except in a window's own
pathological cases — its agent deleting or forging files in its own `output/`, or flooding it past the
bound — which cost at most a duplicate or a missing copy in that window alone. This closes (iii)'s
residual window under wall-stamped names, before child 12. A window owed more than 256 results gives up
the oldest, as a recorded failure. (xxv) **The root record against the recovered state** (#20's review
note 1): a record of this world agreeing on every identity key but the tick, at a tick in `[T, L]`,
is *routine* — rewritten and journaled inside the `resumed` event, with no `root_record_rewritten`
event — as is an unbound record at tick 0 beside a world with `T = L = 0`; anything else keeps H's
rules. Beside a verified checkpoint a window's legacy `pending.json` is advisory: noted in the
`resumed` event, never a refusal. (xxvi) **What the `resumed` event says**: the downtime gap — `wall_down`
(the newest journal file's last write: rows carry no wall stamp, so this is a proxy, labelled; child 10
may refine it), `wall_up`, the tick — the generation and whether it fell back, the root record's class,
the run-input changes and the advisories. (xxvii) **A fall-back never rotates a corrupt generation over
the good one**: when K2 chose the previous generation, the resume renames the refused current one to
`checkpoint.rejected.<boot>.json` before anything writes a checkpoint. (xxviii) **What the restart may
name** (G, "remembered versus named"): `--scenario`, `--seed`, `--phase`, `--ring-slots` and the slug
set either name nothing (`None`: the checkpoint's) or the checkpoint's own, else exit 3 with one
sentence naming the flag, both values and the file — a world's slug set is fixed, and adding an agent
is a new world; `--max-batch` named anew replaces the saved cap and `--closed-interlock` is *added* to
the saved trips (a restart that omits it cannot un-trip one; clearing one is a new world until WP05),
each journaled. `check_console_flags` holds `--phase`, `--max-batch` and `--closed-interlock` to the
`None` default by reading the checkpoint's `run` section (`tripped_interlocks` ↔ `--closed-interlock`).
A fresh start over a state directory that holds a record and no checkpoint refuses, naming the files to
move aside. (xxix) **Not applicable yet, and residual.** A restart at a scheduled-event tick or a phase
boundary is not exercised: `advance` applies no fault schedule and the phase is constant until child
11. The budget's wall clock (`Window.started`) restarts with the process — child 7's (E2). The agents'
`variables` are read back from their consoles without claiming them (G keeps them out of the
checkpoint); a console unreadable at a resume leaves them unknown, the gates at their defaults, and the
claim rewrites it without a `variables` key, never `{}`. The kept refusals on the root record (a
directory planted there; a readable record of another world) assume the chassis's per-slug mounts
(`adf38d6`, branch `aurora-port`); on a stack where agents mount the whole diode root they are stop
buttons an agent holds at every restart.

*Amended by #21's review round (2026-10-10): four code reviews of `06f08a3` (Codex `gpt-6-astra` high
twice; Claude Opus; Fable), its clean gate and a chassis dry run; decided by the coordinator under the
owner's delegation (rulings R1–R5).* (xxx) **One rule for an unparseable line** (R1). A line of a
record file that does not parse is a torn fragment — a write a kill cut, from which nothing was
published — only if it is the last line of its file, or it is followed only by a new boot's startup
events and then that boot's segment header (itself possibly a fragment by the same rule: a kill at the
same point of a start, repeated); anywhere else, and in particular before a complete row, it is
corruption and refuses by name. The whole-record read, the anchored read, the successor discovery and
the replay all read through one helper. Before a later boot appends to a file whose last line was cut,
it ends that tail with `#\n` rather than `\n`: a fragment that was complete JSON short of its newline
became a valid line behind the resumed boot, a permanent refusal. (xxxi) **Nothing after the anchor is
skipped.** A segment file in the state directory that is neither a verifiable continuation of the
anchor nor provably irrelevant — only startup events, a boot torn before its header (its last line),
or a segment the checkpoint lists — refuses: a damaged successor header with its rows intact was read
as "torn" and the file skipped, so a resume came back short of ticks the fleet had seen. A file holding
another boot's header than its name refuses too. And the anchor's offset is held to the line that ends
there: a record line of the anchor's boot, world and tick whose chain is the anchor's, not only a
newline in the right place. (xxxii) **One durability barrier for a result.** A result is landed — named
in its window's `results_written` note, and not owed — and an obligation leaves the list, only once the
result's file and its `output/` directory have both been `fsync`ed; a result found already on disk is
`fsync`ed, file and directory, before it counts (R2). A failure before that leaves every one of them
owed and unnoted. (xxxiii) **A checkpoint a resume cannot use is corrupt, said so.** The reader checks
every field a re-publication reads of an obligation (its text, or a fingerprint-only receipt's
fingerprints, typed) and that `clock.N` is a positive tick count; a body that fails is refused as
corrupt (so K2 falls back) by a sentence that says the body verified against its hash and what it
lacks (R4). Every `OSError` on the resume's path is a refusal by name and errno, and no handle outlives
it. (xxxiv) **The operator's view.** An unbound root record at tick 0 beside a genesis checkpoint
(`T = 0`) is routine whatever `L` is (it is what a kill after the first row and before the first
root-record write leaves); a state directory holding a record and no checkpoint is refused before
anything is said about the diode directory; a fall-back, a rewritten root record and each `pending.json`
advisory are each a line on stderr as well as in the journal; a clean-end checkpoint that cannot be
written is journaled (`checkpoint_failed`, with its occasion and the consecutive count) as (xxii) said;
`serves.json` records where the world's record is written (`journal`), and a restart naming another
journal is refused naming both; and the diode directory is resolved once, by `main`, whose handle the
executive keeps and whose canonical path `serves.json` holds. (xxxv) **Wording.** The `resumed` event's
`wall_up` is stamped after the replay, inside the new process, so `wall_down → wall_up` includes the
vehicle's own start-up and replay, not only the downtime. The agents' `variables` are lost at a resume
not only for a console that cannot be read but for one that carries no `variables` object (a
contract-legal `{"commands": [...]}`): either way the window honours the defaults until a console that
carries one arrives. A tick row's `failures` count is this boot's: it starts at zero after every
restart.

*Amended by #21's confirmation round (2026-10-10): Codex (`gpt-6-astra` high) and Claude Opus
confirmations of `0647ee2`; decided by the coordinator under the owner's delegation.* (xxxvi) **(xxx)
refined: two fragments with no complete line between them are corruption.** A boot journals its
startup events before its header, so repeated kills at a start leave complete events between their
fragments; a fragment directly followed by another is not what a kill leaves, and a file whose every
line is damaged — which (xxx) read as a run of fragments, empty, and skipped — refuses by name.
(xxxvii) **(xxxi) strengthened: the anchor's line is a record line.** It must pass the record's own
schema for its kind (a row, a note, or the header of a boot that never reached a row) and its chain
must recompute — a header's from its `previous_chain`, a row's or a note's from the chained line
before it, read backwards past startup events, bounded by `MAX_RECORD_LINE_BYTES` — not only carry the
anchor's chain, boot, world and tick. (xxxviii) **(xxxiv) bounded: a genesis root record is routine
only as far as a kill can leave it** — an unbound record at tick 0 beside `T = 0` and `L ≤ 1`; beside a
later tick it is a mismatch, with its event. And the operator's line that the record "was rewritten"
is printed once the rewrite succeeded. (xxxix) **One resolution of `--diode-dir`, everywhere.** The
checks that keep `--state-dir` and `--journal` out of the agents' directory are made against `main`'s
one resolution, which is also what the diode directory is opened from — by the walk that follows
nothing and holds each component to what the resolution saw. An obligation's `fingerprint_only`, which
every reader branches on, must be a bool; and the fresh start's scan for a record without a checkpoint
refuses an I/O error by name, as every other start path does.

*Amended by #21's third round (2026-10-10): Codex (`gpt-6-astra` high) confirmation of `d0a20dc`;
decided by the coordinator under the owner's delegation.* (xl) **The terminator rule, which supersedes
(xxx)'s position test and (xxxvi)'s adjacency rule.** An unparseable line of a record file is a torn
fragment if and only if it is its file's final, unterminated line, or it ends with `#\n` — the mark
every writer (`append_journal_line`, the executive's own record writer) puts on a cut tail before it
appends anything after it. A marked fragment may be followed only by a new boot's unchained startup
event, a segment header, another fragment, or the end of the file, never by a chained row or note; an
unparseable line ending in a plain newline is corruption wherever it is. (xxxvi)'s rule refused a real
crash loop — two resumes in a row killed half-way through their `resumed` event leave two marked
fragments with nothing complete between them — while the plain-newline rule still keeps a wholly
damaged file from reading as empty. One predicate serves every reader. A resume checkpoint's anchor
sits after the last intact line, before any marked fragment; and a window published past the last
durable tick is a refusal by name. (xli) **One boundary for the start path.** Every descriptor `main`'s
start path holds is registered, with its path, in one `ExitStack` and closed once on the way out; an
`OSError` the path did not refuse by name of its own is exit 3 naming the file (or the path in use) and
the errno. Once the first cycle claims, an `OSError` keeps its per-cycle handling. And every check about
a window directory goes through the held diode handle, never through `--diode-dir`'s spelling.

*Amended by #21's fourth round (2026-10-10): Codex (`gpt-6-astra` high) confirmation of `f12e522`;
decided by the coordinator under the owner's delegation.* (xlii) **The engine identity is computed whole
or not at all.** The corpus it hashes (I) is listed explicitly, directory by directory; an error listing
a directory or examining an entry raises with its path, and only absence is quiet. A short list was the
defect: a listing that swallowed an `EIO` made the identity a hash of the top-level files, which a world's
genesis or a resume's first checkpoint wrote and the executive kept, so one transient error made every
later checkpoint another engine's. An identity that cannot be computed at a start is a refusal naming the
file (B1, B3, (xli)); at the cadence it is a failed checkpoint, recorded and journaled, and nothing is kept.
(xliii) **Every append to a record file is whole or undone.** The writer notes the file's size, appends
and `fsync`s; on any failure it cuts the file back to that size and `fsync`s the cut, so the file ends at
its last whole line — an advisory event's failure, still not the run's, can no longer leave a fragment
for the next chained row to follow (which (xl) rightly refuses), and no terminator is written before a
chained row. A cut that fails is `RecordUnwritable`: the run stops before another cycle, from the cadence
or the clean end alike, and a resume's advisory refuses. Two consequences at the edges: a marked fragment
is allowed `len(#\n)` bytes past `MAX_RECORD_LINE_BYTES`, since the longest line cut before its newline
and marked is one byte past it; and every `close` on the start path runs whatever the others do — a
failure after another is dropped, the first when nothing else failed is named, exit 3 — and a walk holds
the child it opened before it closes the parent.

### K — A corrupt or incompatible checkpoint, and the crash loop (maintainer with chassis reviewer)

Fixed by WP08's acceptance: "corrupt/incompatible checkpoints fail explicitly". The question is what
the executive does *next*, because `restart: unless-stopped` will start it again in seconds.

| Option | Behaviour |
|---|---|
| K1 — refuse (exit 3), stay dark | Today's choice-D consequence, extended to a bad checkpoint. The container crash-loops until an operator acts. |
| K2 — fall back to the previous generation if it verifies; refuse only if both fail | With J2's record, falling back **loses nothing**: the record carries every cycle since the previous generation and the published-tick mark, so recovery from `checkpoint.prev.json` reaches the same tick as recovery from the lost one. Two bad files is a refusal. |
| K3 — start a fresh world | **Excluded**: "restarting cannot silently reset physical state". |
| K4 — `--new-world`: the operator's explicit flag | ADR 0001's rejected alternative for D, adopted here as the deliberate exit from K2's refusal: the executive starts a fresh world on the bound directory, writes a `discontinuity` record (old `world_id`, last durable and published ticks, reason, wall stamp) into the record and the new checkpoint, and the windows see a new `world_id`. **Never implied and never read from the environment**: in the deployed stack it is a one-shot `docker compose run --rm vehicle … --new-world` by the operator, after which the service is started normally and resumes the new world. |

**Recommendation: K2 with K4 as the only escape.** The refusal message names the file, the check that
failed, and the flag. An *incompatible* checkpoint (I) is never fallen back from: the previous
generation was written by the same engine, Python and platform as the current one, so it is
incompatible too.

Answer (K): **K2 — fall back to the previous verified generation, refuse if both fail; `--new-world`
the only recorded escape** — operator, 2026-10-09; the mechanics (one-shot invocation, the
discontinuity record) accepted by the coordinator under the owner's standing delegation, open to the
maintainer's revision.

### L — What agents see on a restart (L(a) maintainer; L(b) operator confirms)

Fixed: `boot_id` changes (the frame says so); `met_s` continues from the published tick (F1, J);
`world_id` is unchanged.

**L(a) — `seq` after a restart** (maintainer):

| Option | `seq` after a restart |
|---|---|
| L1 — continue from the checkpoint | The window's `seq` is checkpointed; the next frame is `seq + 1` under a new `boot_id`. |
| L2 — reset to 0 per boot | Permitted by the letter of `presentation.yaml` ("monotonic within a boot"). But `write_frame` names frames `NNN.json` by `seq` and says "a name is never reused"; `ring_frames` counts only frames `< self.seq`, so after a reset the previous boot's higher-numbered frames are neither counted, pruned nor overwritten in order — the ring's `held` and `losses` would both be wrong until `seq` passed the old high-water mark, and `000.json` would be rewritten. |

Recommendation: **L1.** The restart is still visible through `boot_id`; a gap in `seq` still means
loss; `pending.json.ticks` and `state.json.executive.tick` continue.

**L(b) — the clock in a result filename** (operator confirms; fleet-visible). Result filenames are
stamped with wall UTC (`write_result`), while `mission.yaml` says "every published timestamp is
reproducible" and the contract says the stamp "is the vehicle's clock". Options: **keep wall UTC**
(status quo; the contract's "UTC everywhere" reads naturally as wall) or **MET rendered as UTC from
`met_epoch_utc`** (reproducible; an agent learns no wall time from a filename; and, per J rule 3,
the name becomes a deterministic function of the trace so that re-publication after a restart is
idempotent and "exactly one result per command" needs no second durable write). Recommendation:
**MET rendered as UTC** — every other time the vehicle publishes is already MET, and this is the one
that is not.

Answer (L(a)): **L1** — coordinator, under the owner's standing delegation (2026-10-09); open to
the maintainer's revision. Answer (L(b)): **result stamps are MET rendered as UTC** — operator,
2026-10-09.

### M — The phase is a function of the tick (maintainer)

`Executive.phase` is `--phase`, constant for the run, and is what `mission.phase` publishes.
`plant.mission_ladder` and `plant.mission_phase(ladder, tick)` exist and no run calls them. WP08's
acceptance names "phase boundaries" as restart points, which presupposes that the executive's phase
*moves*. Recommendation: the phase is derived from the tick on every cycle; `--phase` becomes the
start of the run (the tick is set to that phase's `first_tick`), with the recorded caveat that
`initial_values` describes MET 0 and a run started at `descent` with MET-0 values is a labelled
fixture, not a mission. At a derived boundary the executive **recomputes `gate_names` and the
capability snapshot** (today both are computed once in `__init__` for the static phase), increments
`mission.phase_entry_seq`, and checkpoints both. The posture machine (`REQUEST_HOLD` and the rest)
stays WP05's.

Answer (M): **as recommended**, and child 6's encoder swap with it — coordinator, under the
owner's standing delegation (2026-10-09); open to the maintainer's revision.

## Decision

Accepted, 2026-10-09. The operator decided A (A2; `m = 1` for the first integrated run; lag
ceiling 30 wall seconds), C (shed, dilate, stop; never drop ticks), D (D1), F (F1), K (K2 with
`--new-world` the only escape) and confirmed B (`k = 5`), E (E2) and L(b) (MET rendered as UTC).
The maintainer-owned items — G, H, I, J, K's mechanics, L(a), M and the compare-point encoder —
were accepted as recommended by the implementing session's coordinator under the owner's standing
delegation, and each stays open to the maintainer's revision; the catch-up burst bound was put to the
operator afterwards and answered `k` (no burst). Two things this acceptance does **not** do: it
does not raise `m` above 1 — that is a later manifest decision after child 6's measurement — and it
does not settle the chassis side, whose reviewer is pending for H and the cross-repository list. The
answers are on each section's `Answer:` line, with who gave them.

## Acceptance and consequences

### Acceptance cases this record commits WP08 to (each becomes a test with a tick-level oracle)

- Two executives started from the same checkpoint and fed the same record produce byte-identical
  compare-points at every tick and the same `lineage_head`.
- A restart at a scheduled-event tick, a dwell boundary, a deferred command's `due_tick` and a
  phase boundary each yields the same truth, receipts and lineage as the uninterrupted run (the
  package's second acceptance bullet).
- After a kill and restart, no published `met_s` is ever followed by a smaller one across the boot
  boundary, and the first frame of the new boot describes a tick ≥ the last frame of the old.
- Every command that was claimed has exactly one result file, including commands whose cycle was
  recorded but whose result was not yet written when the process died; no window-local receipt
  number is issued twice.
- A *corrupt* checkpoint (one flipped byte, a truncated body, a link in its place, a missing `rng`
  section) is refused with a message naming the check, and the previous generation is used when it
  verifies and the identity matches, recovering to the same tick. An *incompatible* checkpoint (a
  wrong `engine`, `python`, `platform` or `format`) is refused and never fallen back from, because
  the previous generation was written by the same build; `--new-world` is the only way past both failing, and it writes the discontinuity record.
- A kill at every point of the write sequence leaves one verifiable generation; `SIGTERM` writes
  the checkpoint and exits 0.
- No file under any `/diode/<slug>/` contains a truth-only key, the lineage, the hash, the state
  directory's path or the checkpoint's contents, before or after a restart; a garbled or missing
  `.executive.json` beside a verified checkpoint is rewritten and journaled, not obeyed (the
  package's third bullet).
- With `m`, `k`, `N`, the burst bound and the lag ceiling set, the journal's per-tick lag is
  reported as p50/p95/p99/max and missed-deadline count; the scheduler sheds the static files, then
  frames, never the mirror, before dilating, and stops at the ceiling; a paused executive rewrites
  the mirror, appends no frame, claims no console.

### Consequences for existing records

- ADR 0001 choice D becomes: a bound directory *resumes* from a verified checkpoint whose
  `world_id` matches; it *refuses* without one, or with a corrupt or incompatible one (K);
  `--new-world` is the operator's recorded exit. The "restart consequence of choice D" — a dark
  container until an operator clears the windows — is withdrawn once H1 and J2 land.
- ADR 0001's clarification "the directory's record is authoritative" is amended per H: the
  checkpoint is authoritative; the record is its copy; the lock moves with the checkpoint.
- `tools/console.py`'s `main` ("an executive always starts at tick 0"), `--poll`, `--cycles` and
  `--journal` change under A2, B2 and J2; `plant.md` §6's "the clock's indifference" acquires the
  number it has been missing; `check_console_flags`' rule covers the new remembered flags.
- `docs/release.md`'s manifest rows "clock and multiplier policy" and "checkpoint
  format/compatibility identifiers" get concrete fields: `m`, `k`, `N`, burst bound, lag ceiling,
  the pause/downtime policy ids, `--max-batch`, the allowance ceiling, `format`, `engine`,
  `python`, `platform`.

### Cross-repository impact (chassis, separate package)

1. A private writable volume for the `vehicle` service alone and `--state-dir` in
   `serve_vehicle.sh` (H1). **Blocks deployment evidence** for the resume path.
2. One `console.py` process for all slugs (ADR 0001's open follow-up); per-slug processes would be
   per-slug worlds and per-slug checkpoints.
3. `DIODE_POLL_SECONDS` retired; `m`, `k`, `N`, the burst bound and the lag ceiling passed through
   (required: a stack that names none refuses to start, by design); agents mounted on
   `/diode/<slug>/` (ADR 0001).
4. The pause control (D), the `SIGTERM` contract (F), and `scripts/status.py` reading the
   checkpoint header.
5. `restart: unless-stopped` with K's exit 3: the crash loop is the designed behaviour for an
   unrecoverable checkpoint, and the operator's action is a one-shot `--new-world` run.

### Child issues WP08 should be split into

Recorded here rather than in a second planning file: the template asks this section to "name the
packages that implement and verify it", ROADMAP.md makes the GitHub issues the source of delivery
status, and CONTRIBUTING.md warns against a second editable copy of planning prose. Each child is
one reviewable PR with its own acceptance; children **1, 2, 4, 5, 6 and 11** are
**policy-independent**; 3 waits only on other children. With the operator's answers recorded,
every child may start once its dependencies have landed; the first integrated run at `m = 1` also
waited on child 6 (see A), which has landed.

| # | Child | Acceptance example | Depends on |
|---|---|---|---|
| 1 | Checkpoint format v1: writer, reader, header, integrity and compatibility checks, two generations (`G`, `I`); `spend` an opaque versioned subsection | Round trip of an executive's state is byte-identical; each corruption in the acceptance list refuses by name; a wrong `engine`/`python`/`platform` refuses with no fallback; a corrupt generation falls back to the previous one when it verifies; a kill at each write step leaves one valid generation | — |
| 2 | `--state-dir`: lock and journal moved there, inside-`--diode-dir` refusal, root record rewritten from the checkpoint and a mismatch journaled, **hidden-state isolation test** (`H`) | A `--state-dir` inside `--diode-dir` refuses; after a restart `.executive.json` equals the checkpoint's header; a garbled root record beside a verified checkpoint is rewritten, not obeyed; no window file names the state dir or carries a truth-only key | — (the chassis mount is a separate package; local tests use a scratch dir) |
| 3 | Resume mechanics: an executive constructed *from* a checkpoint plus record, continuing tick, dwell, deferrals, arms, `seq`, `receipts`, spend, lineage, `published_tick` (`G`, `L(a)`) | Kill at tick `T`, restart, truth and lineage at `T + 100` equal the uninterrupted run's; `boot_id` differs, `seq` continues, `met_s` never decreases, every claimed command has exactly one result | 1, 4; G1 for what `arms` means |
| 4 | The per-cycle durable record, the published-tick mark, journal segments, replay-from-record (`J` rules 1–3) | A run's record replayed onto its snapshot reproduces every compare-point; a record with one missing cycle refuses; a refusal-only cycle is recorded; segments concatenate by tick | — |
| 5 | The clock seam: `m`, wall epoch, due-vs-actual lag per tick, burst bound, `k` as the claim/publication cadence, `--poll` retired; `m`, `k`, `N` required with no default (`A2`, `B2` as mechanics) | With `m` and `k` set, the number of ticks between two frames is `k` regardless of machine load; a command lands within `k` ticks; the lag series is reported p50/p95/p99/max; a start naming no `m` refuses | — for the mechanism: the issue's acceptance text excludes A1, A3 and A4, so A2 is the only scheduler it admits; the *values* wait on A and B |
| 6 | The compare-point encoder: replace `_canonical` tagging with sorted compact JSON and a `default` that raises, coverage unchanged; measured with `tools/measure_clock.py` — **landed** (the evidence section's after-table) | Two runs of the same seed agree on every per-tick hash; each tagged pair (`0`/`False`, `1`/`1.0`, `0.0`/`-0.0`, `"1"`/`1`, `None`/`0`) still hashes differently; a non-string key, a non-JSON value and a NaN are refused; a one-slot change in the delay ring changes it; cost recorded | — (maintainer) |
| 7 | Budget on the chosen clock, checkpointed (`E`) | After a restart `used_this_window` and `oldest_expires_in_seconds` continue; under E2 a replay reproduces every budget refusal | 1; E |
| 8 | Overload behaviour: shed static files, then frames, never the mirror; dilate within the ceiling; stop with a recorded hold (`C`) | A deliberately slow instrument makes the lag series climb; the mirror is rewritten every cycle throughout; frames are shed before ticks; the clock stops at the ceiling and the record says why | 5, 6 (before 6 every tick is an overload); C |
| 9 | Pause and resume control (`D`) | Paused: mirror rewritten, no frame, console unclaimed, dwell unchanged; resumed: the first cycle claims what was written during the pause | 5; D |
| 10 | Downtime policy, `SIGTERM` graceful stop, the discontinuity record and the one-shot `--new-world` (`F`, `K4`) | Kill, wait a wall minute, restart: `met_s` continues from the published tick and the record carries the gap; `SIGTERM` writes a checkpoint and exits 0; `--new-world` on a bad checkpoint writes the discontinuity and the windows see a new `world_id` | 3, 5; F, K |
| 11 | Phase from the tick, `phase_entry_seq`, gate names and capability recomputed at a boundary (`M`) | A run started a few ticks before `translunar_coast → lunar_orbit` (tick 13,140,000) publishes the boundary at that tick, its capability snapshot changes with it, and a restart on that tick reports the same phase and entry count | — (maintainer) |
| 12 | Result filename stamps on the chosen clock and idempotent re-publication (`L(b)`, `J` rule 3) | Under MET-as-UTC, two replays of one trace produce identical `output/` listings and a crash between record and result yields exactly one file | 4; L(b) |

Children 1, 2, 4, 5, 6 and 11 fix no policy and could have started before the answers were in; with
the record accepted, every child is unblocked on policy and waits only on the children before it in
the table. Child 5 still ships with `m`, `k` and `N` *required* and no default: the accepted values
(`m = 1`, `k = 5`, `N = tick_hz`, burst `k`, ceiling 30 s) are what the deployed stack and the
manifest name, not what the console assumes.
