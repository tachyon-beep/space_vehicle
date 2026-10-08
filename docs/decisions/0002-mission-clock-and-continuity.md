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
| `state_hash` (canonical, §6 today) | **19.98 ms p50**, 21.43 p95 |
| `sha256(json.dumps(sort_keys, compact, default=repr))` | **0.82 ms p50**, 0.87 p95 |
| `plant.step`, no effects | 1.54 ms p50, 1.64 p95 |
| checkpoint-shaped payload, 10 windows: bytes / `json.dumps` | 268,549 / 0.76 ms |
| durable write (`write`, `fsync`, `rename`; a lower bound on §I's sequence) / without `fsync` | 1.01 ms p50, 1.52 p95, 4.12 max / 0.25 ms p50 |
| `Executive.cycle`, 1 / 2 / 10 windows | 25.9 / 28.3 / 46.7 ms p50 (27.5 / 29.7 / 49.6 p95) |

| Quantity (`python3 tools/measure_clock.py --samples 100 --full-ring`) | ring full (52,100 of 52,100) |
|---|---|
| `canonical_state` bytes / plain sorted JSON bytes | 1,094,444 / 467,464 |
| `state_hash` (canonical, §6 today) | **25.93 ms p50**, 34.90 p95, 53.43 max |
| `sha256(json.dumps(sort_keys, compact, default=repr))` | **7.98 ms p50**, 10.94 p95 |
| `plant.step`, no effects | 1.55 ms p50, 1.96 p95 |
| checkpoint-shaped payload, 10 windows: bytes / `json.dumps` | 471,001 / 7.70 ms |
| durable write / without `fsync` | 1.36 ms p50, 2.01 p95, 2.73 max / 0.42 ms p50 |
| `Executive.cycle`, 1 / 2 / 10 windows | 33.2 / 34.0 / 51.5 ms p50 (41.6 / 38.8 / 57.8 p95) |

Mission ladder: 8 phases over `[0, 34,560,000)`; shortest phase `entry`, 180,000 ticks. An earlier
scratch measurement of the durable write on this machine saw a 32 ms p95 that neither tool run
reproduced; the tail is the filesystem's and is noted so that J does not rest on the p50 alone.

Four consequences are load-bearing for this record and are stated here rather than in the options:

1. **The per-tick compare-point costs more than the physics, and most of that is the encoder, not
   the coverage.** `plant.state_hash` tags every scalar (`{"float": repr(x)}`, `{"int": n}`, …) and
   that tagging is what costs 20–26 ms; a SHA-256 over the same truth encoded as plain sorted
   compact JSON distinguishes every pair the tags exist to separate among JSON values (`0`/`False`,
   `1`/`1.0`, `0.0`/`-0.0`, `"1"`/`1`, `None`/`0`), collides list with tuple exactly where the tags
   do, needs every key to be a string (true of the truth; the tool checks) and a `default` that
   *raises* on a non-JSON value (with `default=repr` such a value would encode as a string and collide
   with an equal `str`, which the `other`/`str` tags keep apart), and costs 0.8 ms on the
   warmed ring and 8 ms on the full one. The remaining 8 ms is the `repr` of 52,100 floats and is
   the floor for any encoding that writes the whole ring out each tick. **Coverage of §6 is
   unchanged by swapping the encoder**; only the bytes hashed change. So the live deadline
   `20 ms / m` is missed at every tick for every `m ≥ 1` *today*, and after the swap one window's
   tick alone (step + hash) sustains `m ≈ 8` on the warmed ring and `m ≈ 2` on the full one, with
   the ceiling then set by publication I/O (B) and by the ring's float encoding, not by physics.
2. The WP01 README section "Measured, not fixed here (WP12)" assigns the compare-point cost to
   WP12, which depends on WP08 — and WP08's first acceptance bullet cannot be shown at `m ≥ 1`
   until the cost falls. The circularity is resolved by making the *encoder* a WP08 child (6), the
   maintainer's, with coverage unchanged; the README's other option — hashing the ring's cursor and
   the slots the tick wrote — hashes *history* rather than *state* and is a change to §6's contract
   that only the operator can make (A recommends against it).
3. Recovery by replaying `N` ticks from a snapshot costs `N × (step + hash)`: about 1.1 s at
   `N = 50` today on the warmed ring (≈ 1.4 s on the full one), 0.5 s after the encoder swap on the full ring; `N` is bounded by the restart
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
below), with the feasibility on record: today `m < 1` is all this engine sustains; after the
encoder swap (child 6) one window's tick sustains `m ≈ 2` at steady state before publication I/O.
`m` is **not published** to the windows: it is in the journal and the manifest, and an agent that
compares `published_at` with `met_s` can infer it, which is the experiment; the frame and mirror
schemas (`presentation.yaml`) do not grow a field for it. Changing `m` between segments is
permitted and journaled (the trace is tick-keyed, so a different `m` is a different wall schedule
for the same mission).

Answer (A, scheduler): **A2** — operator, 2026-10-09. Answer (A, the value of `m`): **`m = 1`
for the first integrated run**, raised later only by an explicit manifest decision after child 6's
measurement — operator, 2026-10-09. Answer (A, lag ceiling, C3's trigger): **30 wall seconds
behind schedule** — operator, 2026-10-09. Answer (A, catch-up burst bound): **`2k` ticks per
cycle (10 at `k = 5`)**, so the scheduler catches up at no more than twice the scheduled rate and
publication I/O never more than doubles — coordinator, under the owner's delegation; open to
revision (the question was not put to the operator). It is an overload number (README row 2), so it
is listed for the operator's confirmation in the handoff; until confirmed it is the interim value.

**`m = 1` for ten windows needs child 6 first.** At `m = 1, k = 5` a cycle has `5 × 20 ms = 100 ms`
of wall time. Ten windows' publication costs about 24 ms per cycle (the ten-window cycle less step
and hash in the full-ring table). Today, with the tagged hash, a cycle is about
`5 × (1.55 + 25.9) + 24 ≈ 161 ms` on the full ring (≈ 133 ms warmed): every cycle overruns, the lag
reaches the 30 s ceiling within a minute or two of wall time and C3 holds the run — shedding frames
cannot rescue it, because the five ticks alone cost more than 100 ms. After the encoder swap it is
about `5 × (1.55 + 7.98) + 24 + 1.4 ≈ 75 ms` at p50 (≈ 90 ms at p95; a checkpointing cycle adds
≈ 9 ms), so `m = 1` is sustainable with modest headroom and the ten-window ceiling at `k = 5` is
about `m ≈ 1.35`. That figure, not the one-window `m ≈ 2`, is the baseline for any later decision to
raise `m`. **The first integrated run at `m = 1` therefore waits on child 6.**

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

**A catch-up cycle** (A2, after the scheduler fell behind) claims once, steps up to the burst bound
of `2k` ticks, and publishes one frame per `k` ticks stepped, so frames still describe every `k`-th
tick and publication I/O at most doubles; a command claimed in a catch-up cycle lands at most `2k`
ticks after it was written. While shedding (C2), frames are skipped and the skip is visible in
`seq` and the ring's `losses`. "`k` ticks between frames" holds when the executive is neither
catching up nor shedding.

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
(the ceiling answered; the burst bound set in the interim, see A).
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
not agent-writable (evidence section).

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
the ADR 0001 rules stand unchanged. (iii) The clarification "the directory's record is
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
- **Compatibility**: `format: vehicle.checkpoint.v1`; `engine`; `python` (`major.minor.micro`);
  `platform` (`platform.platform()`); `tick_hz`. A mismatch on any of them is *incompatible* (K).
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
delegation, and each stays open to the maintainer's revision; the one number nobody was asked, the
catch-up burst bound, is `2k` on the same footing. Two things this acceptance does **not** do: it
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
waits on child 6 (see A).

| # | Child | Acceptance example | Depends on |
|---|---|---|---|
| 1 | Checkpoint format v1: writer, reader, header, integrity and compatibility checks, two generations (`G`, `I`); `spend` an opaque versioned subsection | Round trip of an executive's state is byte-identical; each corruption in the acceptance list refuses by name; a wrong `engine`/`python`/`platform` refuses with no fallback; a corrupt generation falls back to the previous one when it verifies; a kill at each write step leaves one valid generation | — |
| 2 | `--state-dir`: lock and journal moved there, inside-`--diode-dir` refusal, root record rewritten from the checkpoint and a mismatch journaled, **hidden-state isolation test** (`H`) | A `--state-dir` inside `--diode-dir` refuses; after a restart `.executive.json` equals the checkpoint's header; a garbled root record beside a verified checkpoint is rewritten, not obeyed; no window file names the state dir or carries a truth-only key | — (the chassis mount is a separate package; local tests use a scratch dir) |
| 3 | Resume mechanics: an executive constructed *from* a checkpoint plus record, continuing tick, dwell, deferrals, arms, `seq`, `receipts`, spend, lineage, `published_tick` (`G`, `L(a)`) | Kill at tick `T`, restart, truth and lineage at `T + 100` equal the uninterrupted run's; `boot_id` differs, `seq` continues, `met_s` never decreases, every claimed command has exactly one result | 1, 4; G1 for what `arms` means |
| 4 | The per-cycle durable record, the published-tick mark, journal segments, replay-from-record (`J` rules 1–3) | A run's record replayed onto its snapshot reproduces every compare-point; a record with one missing cycle refuses; a refusal-only cycle is recorded; segments concatenate by tick | — |
| 5 | The clock seam: `m`, wall epoch, due-vs-actual lag per tick, burst bound, `k` as the claim/publication cadence, `--poll` retired; `m`, `k`, `N` required with no default (`A2`, `B2` as mechanics) | With `m` and `k` set, the number of ticks between two frames is `k` regardless of machine load; a command lands within `k` ticks; the lag series is reported p50/p95/p99/max; a start naming no `m` refuses | — for the mechanism: the issue's acceptance text excludes A1, A3 and A4, so A2 is the only scheduler it admits; the *values* wait on A and B |
| 6 | The compare-point encoder: replace `_canonical` tagging with sorted compact JSON + `repr`, coverage unchanged; measured with `tools/measure_clock.py` | Two runs of the same seed agree on every per-tick hash; each tagged pair (`0`/`False`, `1`/`1.0`, `0.0`/`-0.0`, `"1"`/`1`, `None`/`0`) still hashes differently; a one-slot change in the delay ring changes it; cost recorded | — (maintainer) |
| 7 | Budget on the chosen clock, checkpointed (`E`) | After a restart `used_this_window` and `oldest_expires_in_seconds` continue; under E2 a replay reproduces every budget refusal | 1; E |
| 8 | Overload behaviour: shed static files, then frames, never the mirror; dilate within the ceiling; stop with a recorded hold (`C`) | A deliberately slow instrument makes the lag series climb; the mirror is rewritten every cycle throughout; frames are shed before ticks; the clock stops at the ceiling and the record says why | 5, 6 (before 6 every tick is an overload); C |
| 9 | Pause and resume control (`D`) | Paused: mirror rewritten, no frame, console unclaimed, dwell unchanged; resumed: the first cycle claims what was written during the pause | 5; D |
| 10 | Downtime policy, `SIGTERM` graceful stop, the discontinuity record and the one-shot `--new-world` (`F`, `K4`) | Kill, wait a wall minute, restart: `met_s` continues from the published tick and the record carries the gap; `SIGTERM` writes a checkpoint and exits 0; `--new-world` on a bad checkpoint writes the discontinuity and the windows see a new `world_id` | 3, 5; F, K |
| 11 | Phase from the tick, `phase_entry_seq`, gate names and capability recomputed at a boundary (`M`) | A run started a few ticks before `translunar_coast → lunar_orbit` (tick 13,140,000) publishes the boundary at that tick, its capability snapshot changes with it, and a restart on that tick reports the same phase and entry count | — (maintainer) |
| 12 | Result filename stamps on the chosen clock and idempotent re-publication (`L(b)`, `J` rule 3) | Under MET-as-UTC, two replays of one trace produce identical `output/` listings and a crash between record and result yields exactly one file | 4; L(b) |

Children 1, 2, 4, 5, 6 and 11 fix no policy and could have started before the answers were in; with
the record accepted, every child is unblocked on policy and waits only on the children before it in
the table. Child 5 still ships with `m`, `k` and `N` *required* and no default: the accepted values
(`m = 1`, `k = 5`, `N = tick_hz`, burst `2k`, ceiling 30 s) are what the deployed stack and the
manifest name, not what the console assumes.
