# 0002 — The mission clock, private checkpoints and restart continuity

- Status: proposed (choices A–M await the operator; I and M are the vehicle maintainer's and are recommended here so the operator sees the whole shape)
- Date: 2026-10-09
- Decision owner: operator, with vehicle and chassis reviewers (`docs/decisions/README.md`, row "Mission clock, multiplier, overload, pause and downtime policy")
- Related work package / evidence gaps: WP08 (#8) implements it; WP09 (#9) and WP10 (#10) wait on it; WP12 (#12) measures the budgets it names; ADR 0001 choice D and its "restart consequence" are reopened by it
- Reviewers and review evidence: none yet; this record was prepared by a design-only session against vehicle commit `bdfaf8b` and has had no independent review
- Supersedes / superseded by: amends ADR 0001's "restart consequence of choice D" and the clarification "the directory's record is authoritative" (see §Consequences); supersedes nothing

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
decisions that are genuinely the operator's, each as a concrete question with options, the
consequences for the four things this programme cares about — the validity of the fleet experiment,
determinism and replay, the performance budget, and the chassis adapter — and a recommendation
with its reasoning. The recommendations are recommendations: ROADMAP.md says "operator decides
target changes, never silent clock dilation", and nothing below is a default the implementation may
assume before the owner marks it accepted.

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
| **Boot** | `uuid4` per process | `Window.boot_id` (per window, drawn in `__init__`); `seq` ("monotonic within a boot", `presentation.yaml#frame`); arm tokens embed `boot_id`; `world_id` is per `Executive` instance and bound by the first tick |

Two of the wall-clock rows are the subject of questions below (E and the stamps in L); the rest of
the table is fixed and is only restated so the checkpoint in G knows what it is saving.

## Part (a): what accepted decisions already fix

Nothing in this list is open. Each row is a constraint the options in part (b) are checked against.

| Fixed by | What it fixes |
|---|---|
| `plant.md` §6, rule 1 | MET is an integer tick count; `t += 0.02` is forbidden. A multiplier therefore maps wall time to a *number of ticks*, never to a float mission time. |
| `plant.md` §6, rules 2–3 | One `master_seed` is a run input, never a log line; streams derive by name. The seed is a checkpoint field, not something a restart may redraw. |
| `plant.md` §6, rule 5 | Compare-point at every tick, full snapshot at phase boundaries, deltas between. A checkpoint cadence coarser than a phase contradicts this; a finer one is permitted. |
| `plant.md` §6, rule 6 | Replay drives from the trace keyed by tick index, not wall time. Whatever the live scheduler does with wall time, the recorded trace must be `(tick, effects)`, and a restart that replays a tail must replay by tick. |
| `plant.md` §6, "the clock's indifference is the mechanism" | Sim time advances on the multiplier whether or not anyone is thinking; no reasoning budget is imposed. A policy that waits for agents (a turn-based clock) is excluded. |
| `plant.md` §5 | Dwell timers run on a clock that cannot jump; effects carry integer-microsecond stamps and equal stamps keep the caller's order. Pause, downtime and dilation may not move a dwell. |
| `plant.md` §3, transport delay | The delay ring is state, indexed by tick, "part of the snapshot and part of the compare-point hash". A checkpoint must carry `<state>__delay` whole. |
| `plant.md` §4 | Stock residual accumulators (`<state>__residual`) and shortfalls are exact state. They are in the checkpoint. |
| ADR 0001 "Tick and effect timing" | Validation and effect read the same truth at the same simulated instant; dwell and queue age are simulated microseconds. The clock question is "how many ticks a wall second carries", and it is explicitly deferred to here. |
| ADR 0001 "One world per directory", choice D | A second executive on a bound directory refuses; a window bound to a different world is refused "until WP08 defines continuity". Reopened by K below. |
| ADR 0001 clarification "the directory's record is authoritative" | Identity lives in `<diode-dir>/.executive.json`, read under the lock, written at every tick. **Amended by H below**: once a private checkpoint exists, the checkpoint is authoritative and the root record is the windows' copy. |
| ADR 0001 clarification "a window that fails its pre-claim check keeps its due deferrals queued" | Deferrals survive a dark window and `EXPIRE` by simulated age. They must therefore survive a restart too (G). |
| ADR 0001 clarification "budget semantics are WP08's" | `budget.oldest_expires_in_seconds` counts the wall clock *provisionally*. Question E. |
| ADR 0001 "Bounds in memory" | The journal holds the whole lineage and is refused inside `--diode-dir`. The same rule governs where a checkpoint may live (H). |
| `docs/completion.md`, non-negotiable properties | "Physics advances independently of agent thinking and publication requests"; "pause, overload and downtime policies are decided before use; restarting cannot silently reset physical state or advance it by guessed time"; "the fleet cannot access truth, checkpoints, private fault plans". |
| `docs/completion.md`, performance | 34,560,000 ticks; live deadline `20 ms / m` for the executive plus amortised ten-window I/O; record p50/p95/p99/max, queue ages, missed deadlines and the agreed overload behaviour; replay goal ≈ 8.7 µs/tick; "changing these targets requires an operator decision". |
| `docs/release.md`, manifests | Every acceptance manifest carries "clock and multiplier policy" and "checkpoint format/compatibility identifiers"; rollback "follows its documented checkpoint compatibility policy; do not guess that a new or old engine can read an arbitrary saved world". |
| `docs/diode-contract.md` §6 (frozen, chassis) | The vehicle may restart; nothing already submitted runs twice (the destructive claim, §2.2); nothing already executed is forgotten; the operator's ceiling counters survive a restart rather than refilling; time is UTC everywhere and the stamp in a result filename is the vehicle's clock. Row at line 244: at-most-once intake covers a restart mid-batch. |
| `docs/diode-contract.md` §2.4 | `state.json` is rewritten every cycle whether or not anything was submitted; published state is never read back as input. |
| `presentation.yaml#frame` | `seq` is monotonic within a boot and, with `boot_id`, separates loss from a restart; `boot_id` exists "because six of twelve specs cannot express a restart"; `met_s` "cannot jump backward"; `sim_step` is the tick. |
| `presentation.yaml#plant_published` | `mission.phase_entry_seq` and `mission.posture_entry_seq` exist "so a fleet returning from a gap can tell a state from a snapshot" and so "a hold-and-resume cycle becomes visible". The vocabulary for making a gap or hold visible already exists; no new channel is needed. |
| `mission.yaml#met_epoch_provenance` | A fixed UTC epoch "so MET is an integer tick count from an absolute origin and every published timestamp is reproducible". |

## Evidence and measurements

All figures were measured on 2026-10-09 against vehicle commit `bdfaf8b`, Python 3.12.3 (the
session's `venv3.12`), on the implementing session's Linux machine (ext4 on NVMe), by a scratch
script outside the repository that imported `tools/plant.py` and `tools/console.py` and timed them
with `time.perf_counter`. They are measurements of *this* machine on *this* commit and are not pins:
a figure in this record that no tool re-derives will drift (AGENTS.md, "Numbers in this file"), so
each is cited with the call that produced it and should be re-measured, not copied.

| Quantity | Measured | Call |
|---|---|---|
| Truth keys at t=0 / after 500 ticks | 114 / 131 (the 17 new keys are `__residual`, `__shortfall` and one `__delay`) | `plant.initial_values`, then `plant.step` × 500 |
| `canonical_state` bytes at t=0 / t≥1 | 5,197 / ≈735,600 | the coolant transport ring `loop_transport_t__delay` is born at tick 1 with 52,100 slots (1,042 s at 50 Hz) and is fixed-size thereafter |
| `state_hash` at t=0 / t≥1 | 81 µs / **20.5 ms** p50 | `plant.state_hash(truth)`; `_canonical` tagging is the cost — `sha256(json.dumps(truth, sort_keys=True))` over the same state is 0.86 ms |
| `plant.step`, no effects | 1.6 ms p50 (1.68 p95) | at t≥1, with the ring present |
| `Executive._lineage_link` | 20.7 ms p50 | one `state_hash` plus two small SHA-256s |
| `Executive.cycle`, 1 / 2 / 10 windows | 25 / 28 / 46 ms p50 (27 / 29 / 54 p95) | fresh directories on ext4, identity instrument, no commands; publication rewrites `HELP.md` (78 KB) and `README.md` (7 KB) per window per cycle |
| Checkpoint-shaped payload, 10 windows | 269 KB; `json.dumps` 0.87 ms p50 | truth (plain JSON, `default=repr`) + tick + dwell + lineage head + receipt counter + per-window queues/arms/seq |
| Durable write: `write`, `fsync`, `rename` | 1.3 ms p50, **32.7 ms p95** | 100 writes of the 269 KB blob to ext4 |
| Same without `fsync` | 0.3 ms p50, 32 ms p95 | the p95 is the filesystem's, not the fsync's |
| Mission ladder | 8 phases, `[0, 34,560,000)`; shortest phase `entry` 180,000 ticks, `descent` 450,000 | `plant.mission_ladder`, `plant.mission_ticks` |
| **With the ring full** (every slot a float, as it is after 52,100 ticks ≈ 17 mission minutes): `canonical_state` bytes / `state_hash` / `sha256(json.dumps)` / `plant.step` | 1,094,444 B / **26.3 ms p50, 33.9 p95** / 8.7 ms / 1.5 ms | the rows above were taken with ≤ 500 of 52,100 slots filled; a `null` slot is 13 canonical bytes and a float ≈ 25, so the empty ring understates steady state by about a third |
| **With the ring full**: checkpoint-shaped payload, 10 windows; `json.dumps`; durable write | 467,659 B; 8.7 ms; 1.8 ms p50, 2.3 ms p95 | 100 writes; the 32 ms p95 in the empty-ring row did not recur in this sample, so the tail is the filesystem's and both figures stand |

The vehicle README's own record of the WP01 round (section "Measured, not fixed here (WP12)") gives
≈45 ms per cycle of which ≈42 ms is `state_hash`, on a different machine. The two agree on the
shape: **the compare-point over the delay ring is the cycle, and the physics is a rounding error on
it.** Three consequences are load-bearing for this record and are stated here rather than in the
options:

1. At the compare-point as specified, a tick costs ≥ 20 ms before any window is served (≈ 26 ms
   once the ring is full), so the live deadline `20 ms / m` is missed at every tick for every
   `m ≥ 1`, with one domain's worth of physics in the plant. WP08's first acceptance bullet ("operator-approved multiplier … is
   observable") cannot be *demonstrated* at `m = 1` on this engine until the cost of the
   compare-point is addressed — and the README assigns that question to WP12, which depends on
   WP08. The dependency is circular as written; A below says what to do about it.
2. Recovery by replaying a tail of `N` ticks from a snapshot costs `N × (1.6 + 20.7) ms` today
   (`N × 28 ms` with the ring full), because the lineage must be recomputed; `N` is bounded by the
   restart time the operator will accept, not by physics (J).
3. The replay goal of ≈ 8.7 µs/tick is three orders of magnitude from today's 1.6 ms step and four
   from the hash. `plant.md` §10 already says this "is not a Python number"; it is restated so that
   no option below is read as reaching it.

Two more observations about the deployed shape, both from `space_chassis` at the session's HEAD
and both chassis-side facts this record depends on:

- The vehicle container is `read_only: true` with `tmpfs: [/tmp]` and one bind mount,
  `./volumes/diode:/diode` (`docker-compose.yml`, service `vehicle`). **There is no private durable
  path in the deployed vehicle container today** — not for a checkpoint and not for `--journal`,
  which refuses any path inside `--diode-dir`. Every agent also mounts `./volumes/diode:/diode`
  read-write (ADR 0001's recorded unmet assumption), so the diode root is agent-writable.
- `containers/serve_vehicle.sh` still forks one `console.py` per slug, each with `--cycles 0`. ADR
  0001 names the one-process change as a chassis follow-up; until it lands, a restart design that
  assumes one executive per directory is designing for a stack that does not run that way.

## Part (b): the operator's decisions

Each question names its options, then the consequences in the four columns this programme weighs,
then a recommendation. "Fleet" means the validity of the agent experiment; "replay" means `plant.md`
§6 bit-identity and the receipt trace; "budget" means the live `20 ms / m` deadline and the restart
cost; "chassis" means what `space_chassis` must change.

### A — How wall time maps to ticks, and the multiplier `m`

Today one cycle is one tick and `--poll` is a wall sleep, so `m = dt / poll`: 0.02 at the console's
default `--poll 1`, **0.004 in the deployed compose (`DIODE_POLL_SECONDS: 5`)**, which is one
mission in about 5.5 wall years. `plant.md` §6's "sim time advances on the multiplier whether or
not anyone is thinking" is true of this loop only in the sense that it does not wait; the multiplier
it advances on was never chosen.

| Option | What it is |
|---|---|
| A1 — tick per cycle (status quo) | `m` is an accident of `--poll` and of the cycle's cost; a slow cycle dilates mission time silently. Excluded by WP08's own acceptance text ("slow thinking or I/O does not silently dilate mission time") and ROADMAP.md ("never silent clock dilation") — listed so the exclusion is on record. |
| A2 — fixed-rate scheduler with bounded catch-up | The run records a wall epoch at (re)start; tick `t` is *due* at `epoch + t·dt/m`; each cycle steps every tick that is due, up to a catch-up bound, then sleeps until the next due tick. Lag (`actual − due`) is recorded per tick. Publication is on its own cadence (B). |
| A3 — as fast as possible | No wall anchoring; the executive ticks continuously and publishes on a cadence. This is the replay/batch mode and is what `plant.py --mission` already does; as the *live* policy it makes `m` whatever the machine gives, which is A1's defect without the sleep. |
| A4 — adaptive `m` | The executive lowers `m` when it falls behind and raises it when it catches up. Mission time then depends on machine load, which is the one thing §6 rule 6 exists to prevent in the trace and which the fleet would read as physics. |

Consequences. *Fleet*: only A2 gives the fleet a known relationship between its wall-clock thinking
and the vehicle's drift; A4 makes "how long did I think" unanswerable in mission terms. *Replay*:
all four produce a tick-keyed trace, so replay is unaffected by the choice; A2 is the only one whose
*live* run can be compared against its own schedule (missed deadlines are a measurable, as
completion.md requires). *Budget*: A2 makes the deadline a checked property rather than an
aspiration — a cycle that steps `k` due ticks has `k × 20 ms / m` to do it in. *Chassis*:
`DIODE_POLL_SECONDS` stops meaning "ticks per second" and becomes B's publication cadence; a new
`VEHICLE_MULTIPLIER` (or whatever the chassis names it) is passed through `serve_vehicle.sh`; the
value is recorded in the checkpoint and the release manifest (release.md: "clock and multiplier
policy").

**Recommendation: A2**, with `m` a run input recorded beside the seed, defaulting to *nothing* —
a run with no multiplier named refuses to start, by the same rule as the seed's `None` default, so
the deployed stack cannot inherit an accidental `m` again. The *value* of `m` is the operator's and
this record does not pick it; it records the feasibility: on this engine today, `m ≤ 0.9` is the
most a single window can sustain with the compare-point as specified (`m ≤ 0.7` once the ring is
full), and `m ≈ 10` with the step alone. The circularity in the evidence section is resolved by pulling one bounded question forward
from WP12 into WP08's children: *what must the per-tick compare-point cover*, with the two options
the README already names (a rolling hash the delay ring updates incrementally; a hash over the
ring's cursor and the slots the tick wrote). That is a vehicle-maintainer decision about the
*encoding* of §6's compare-point, not a change to its contract, and without it WP08 cannot show
its own acceptance.

### B — Publication cadence, separated from the tick

If A2 steps many ticks per cycle, "publish every tick" means 50 frames per mission second per
window; at `m = 1` with ten windows that is 500 frames and ten 78 KB `HELP.md` rewrites per wall
second, and the identity instrument has no cadence of its own to thin it. WP08's outcome text is
"make physical stepping independent of agents and publication", so this is WP08's to decide.

| Option | What it is |
|---|---|
| B1 — every tick | Status quo. Frame count = tick count; the ring of 300 slots holds 6 mission seconds. |
| B2 — every `k` ticks of mission time | A frame at every `k`-th tick (e.g. `k = 50`, one frame per mission second); `state.json` and the generated files rewritten with it; results written when decided, as now. The ring then holds `300 × k` ticks. Frame *content* follows `presentation.yaml#ring.cadence_classes`: a channel appears when its own period has elapsed. |
| B3 — on a wall cadence | Publish every `DIODE_POLL_SECONDS` of wall time carrying whatever tick is current. Replayable physics, non-replayable publication: which ticks got a frame depends on the machine. |

Consequences. *Fleet*: B2 gives the fleet a telemetry rate in mission time that is the same at any
`m`, which is what makes a finding at `m = 10` comparable to one at `m = 1`. *Replay*: B2 is the
only option under which the *published evidence* is a function of the trace, which completion.md
requires ("validate that trace through command receipts and permitted evidence as well as internal
state"). *Budget*: B2 takes the ten-window I/O off the tick path and amortises it, which is how
completion.md's deadline is phrased. *Chassis*: `DIODE_POLL_SECONDS` becomes advisory or is
retired; `contract/diode_probe.py`'s expectation that "telemetry advances on its own" is met by B2
at any `k`.

**Recommendation: B2**, with `k` a run input (recommended `k = tick_hz`, one frame per mission
second, so `met_s` in consecutive frames differs by one) and the mirror rewritten at the same
cadence. This interacts with J: a frame must never describe a tick the vehicle could lose, so the
publication cadence and the checkpoint cadence are the same number or publication lags the
checkpoint. A vehicle maintainer's decision in form, but it fixes what the fleet sees, so it is
listed for the operator's confirmation.

### C — Overload: a tick misses its `20 ms / m` deadline

| Option | What it is |
|---|---|
| C1 — dilate and record | The scheduler falls behind; mission time runs slower than `m` says; every tick's lag is journaled and the run's missed-deadline count is in its manifest. Bounded by a lag ceiling. |
| C2 — shed publication first | Before dilating, skip frame and mirror writes (never results, never ticks); `seq` and the ring's `losses` make the shedding visible and countable. |
| C3 — stop and say so | Past a lag ceiling the executive stops ticking, records an operator hold with its reason, and keeps publishing the mirror (D1) until the operator resumes or ends the run. |
| C4 — drop ticks | Skip the physics of the missed ticks to catch up. **Excluded**: the trace is tick-keyed and a dropped tick is physics that never ran — "advance it by guessed time" with the guessing done by the scheduler. |

Consequences. *Fleet*: C1 within a bound is invisible to the agents in mission terms and visible in
wall terms (they can see `published_at` advancing faster than `met_s`); C3 is a visible hold. *Replay*:
C1–C3 leave the trace intact; only C4 breaks it. *Budget*: C1 is what "missed deadline" means in
completion.md's measurement list; C3 turns an unbounded overload into a recorded event rather than a
run whose `m` quietly became something else. *Chassis*: C3 needs the pause control surface (D) and a
status the operator can see (`scripts/status.py` reads records; the journal is the natural source).

**Recommendation: C2, then C1 within a recorded bound, then C3; never C4.** The bound (how many
wall seconds behind before the clock stops) is the operator's number; it belongs beside `m` in the
run inputs. Whether the fleet is *told* about dilation beyond what `published_at` versus `met_s`
already reveals is a separate sub-question: the recommendation is **no new signal** — the frame and
mirror schemas are declared in `presentation.yaml` and the existing pair of clocks is sufficient for
an agent that looks; an agent that does not look is the experiment.

### D — Pause and operator hold

Two different things share the word. `REQUEST_HOLD` is a *posture* (`mission.yaml#postures`):
in-simulation, the clock runs, `posture_entry_seq` moves, and it is WP05's. An *operator pause* is
out-of-simulation: the ticks stop and wall time passes. Only the second is this record's.

| Option | What agents see while paused |
|---|---|
| D1 — mirror continues, no new ticks, no claim | `state.json` is rewritten each cycle with `published_at` advancing and `executive.tick` frozen; no frames (a frame is a tick's); consoles are not claimed, so a command written during the pause waits in the agent's file and is claimed at the first resumed tick. |
| D2 — publication continues as if ticking | Frames appended with an unchanging `met_s`; the ring fills with identical frames. |
| D3 — the window goes dark | Nothing rewritten. Indistinguishable from a dead vehicle (F-14's frozen-value problem in `presentation.yaml#ring`). |
| D4 — no pause; only stop | The operator's only tool is to stop the process, which is F below. |

Consequences. *Fleet*: D1 is distinguishable from a dead vehicle (the mirror moves) and from a
running one (the tick does not); D2 makes the ring lie about cadence; D3 teaches the fleet that a
silent vehicle may be fine. *Replay*: a pause is a wall event with no tick, so none of these touch
the trace; D1 records the pause in the journal with wall stamps. *Budget*: none. *Chassis*: a pause
needs a control the operator reaches and the fleet cannot — a file in the private state directory
(H) or a signal to the one console process; the chassis names the mechanism.

**Recommendation: D1.** Dwell, deferral age and budgets (if E chooses mission time) do not move
during a pause, because none of them are on the wall clock. The claim is deliberately *not*
performed while paused: the contract's claim is a promise that the batch is evaluated against the
current truth, and a claimed-but-unticked command would be neither refused nor run.

### E — The command budget's time base

`budget.window_seconds: 3600` and `oldest_expires_in_seconds` are in the contract's example (§2.4)
and in the mirror today: `oldest_expires_in_seconds` counts the wall clock from `Window.started`,
and the spend is `Window.accepted`, a counter that never expires within a run and is reset to zero
by a restart — which §6 of the contract forbids ("the operator's ceiling counters should survive a
restart rather than refilling"). The surviving is G's. The *time base* is this question.

| Option | What a window-hour is |
|---|---|
| E1 — wall hour | As the fake diode (`contract/fake_diode.py`, `DIODE_HOURLY_MAX`) counts it. At `m = 10` an agent gets ten times the commands per mission hour it would get at `m = 1`. |
| E2 — mission hour | `tick_hz × 3600` ticks; spend is a list of ticks; `oldest_expires_in_seconds` is mission seconds. The budget is a property of the vehicle, like the dwell. |

Consequences. *Fleet*: E1 ties the agents' command rate to the operator's choice of `m`, so two runs
at different multipliers are not comparable; E2 makes the budget part of the vehicle the fleet is
flying. *Replay*: a refusal is a receipt, and completion.md validates replay through receipts; under
E1 a `budget` refusal depends on the wall clock and the receipt trace cannot be replayed. **This is
the discriminating constraint.** *Budget*: none. *Chassis*: the fake diode's wall semantics are a
fixture's and need not change; the contract's example gives the field names, not the clock.

**Recommendation: E2.** The counter-argument on record is that the contract's prose reads naturally
as wall time and that the fake diode counts wall time; the reply is that the contract is silent on
the clock and explicit that receipts are the vehicle's.

### F — Downtime: the process or container is down

| Option | What happens to mission time while the vehicle is down |
|---|---|
| F1 — freeze | Mission time does not advance. The vehicle resumes at the last durable tick (J); the wall gap `(wall_down, wall_up, tick)` is journaled and goes in the run manifest. |
| F2 — advance | On restart the executive steps the ticks the wall gap would have carried at `m`, with no commands (none could be claimed). The elapsed wall time is measured rather than guessed, so completion.md's letter is met; its spirit — the vehicle flew unattended with its consoles unread — is not. The gap is also measured from the last *durable* tick, not from the crash, so part of it is double-counted. |
| F3 — the run ends | A restart invalidates the run; the operator starts a new world (`--new-world`, K). |

Consequences. *Fleet*: F1 is the only option under which every tick the vehicle ever ran was a tick
some window could have commanded. *Replay*: F1 and F2 both leave a tick-keyed trace; F2 adds a
stretch with no effects whose length depends on how fast the operator restarted. *Budget*: F2's
catch-up is C's overload at its worst. *Chassis*: `restart: unless-stopped` restarts the container
automatically, so the policy runs without an operator present; F1 is the one that is safe to run
unattended.

**Recommendation: F1.** Whether the *fleet* is told about the gap: the `boot_id` changes, `met_s`
continues, and `published_at` jumped by the wall gap — the existing surface already says "the
vehicle restarted and lost no mission time", which is the true statement; **no new signal**.

### G — What the checkpoint contains

Most of this is fixed by part (a) (completion.md: "one authoritative state, event queue, mission
clock, RNG lineage and conservation ledgers"; contract §6: counters survive). What follows is the
field list the fixed decisions imply, and then the two genuine questions inside it.

Executive: `format` id; engine identity (I); `world_id`; `scenario`; `seed`; `m`, `k` and the
overload bound (A–C); `tick`; `phase` (as recorded, see M); `tripped_interlocks`; `truth` — every
key of the value map, including `<state>__delay` rings, `<state>__residual` accumulators and
`<state>__shortfall`; `dwell` (state → `{at_us, left}`); `lineage_head`; the global `receipt`
counter; the wall epoch of the current run segment and the list of segments so far (for the
manifest). Per window: `slug`, `ring_slots`, `seq`, `receipts`, the budget spend (E), `deferred`
(every entry, with `accepted_tick` and `due_tick`), and the two items below. Not in the checkpoint:
`variables` (the agent's, preserved in its own `console.json`), the generated `HELP.md`/`README.md`
(regenerated), the ring's files (the directory's), anything derived.

**RNG lineage** is, at `bdfaf8b`, the seed alone: `plant.step` is called with `rng=None`, and
`tools/faults.py` draws a fault *schedule* up front from name-keyed streams (`stream(master_seed,
fault)`), which is a function of the seed. Per-tick RNG state (instrument noise, hazard
accumulators) arrives with WP09. The format must carry a `rng` section from the first version so
that WP09 adds fields rather than a format; a checkpoint missing a field the engine needs is
incompatible (I), never defaulted.

| G1 — arm tokens across a restart | `arm_event` tokens are bound to the arming window and event (ADR 0001) and embed the window's `boot_id` as salt; nothing in the token is checked against the current boot. Options: **survive** (the token is an accepted command's outcome; `execute_event` revalidates everything at the moment of effect anyway) or **expire** with a named refusal at the first `execute_event` after a restart. Recommendation: **survive**, because contract §6 says nothing executed is forgotten and an arm *was* executed; expiring it would make a restart a safety event the crew never saw. |
| G2 — the deferral queue | Fixed by ADR 0001's clarification: deferrals keep their `accepted_tick` and `due_tick`, settle or `EXPIRE` by simulated age, and the pause/downtime policies above do not age them. Listed because the queue is per window and the obvious implementation forgets it. |

### H — Where the checkpoint lives

Fixed: not in any agent-writable path (completion.md). Today's deployed container has none that is
not agent-writable (evidence section).

| Option | Where |
|---|---|
| H1 — a private writable mount the chassis adds for the vehicle service alone, passed as `--state-dir` | The checkpoint, its previous generation and the `--journal` live there; the console refuses a `--state-dir` inside `--diode-dir` by the same `is_relative_to` rule the journal uses; agents have no mount of it. |
| H2 — the diode root beside `.executive.json` | Agent-writable in the deployed compose, so a forged checkpoint is a forged world; and `plant.md` §7's one-way boundary would have truth on the agents' volume. **Excluded** even after the mounts are fixed: a private thing should not share a volume with the adversary's directory. |
| H3 — `tmpfs` (`/tmp`) | Lost on a container restart, which is the case F exists for. Excluded. |

**Recommendation: H1.** Consequence for ADR 0001: the checkpoint becomes authoritative for identity
(`world_id`, slugs, seed, scenario, tick); `<diode-dir>/.executive.json` remains, rewritten from the
checkpoint, as the windows' side copy that the legacy refuse-only checks read — the clarification
"the directory's record is authoritative" is amended to "the checkpoint is authoritative and the
record is its copy". *Chassis*: one new volume and one new mount on one service (not the ten-edit
fleet mount list); `serve_vehicle.sh` passes `--state-dir`; the operator's `status.py` may read the
checkpoint's header (never truth) for its one line per agent. This is the one decision below that
*blocks* implementation on a chassis change rather than following it.

### I — Atomicity, integrity and compatibility (vehicle maintainer; recommended, not open)

Fixed by release.md: a format identifier and a compatibility identifier are in every manifest, and
no engine may be assumed to read an arbitrary saved world. The mechanics follow:

- **Write**: serialise; write to a temporary under a random name in `--state-dir`; `fsync` the
  file; `rename` over `checkpoint.json`; `fsync` the directory. Keep the previous generation as
  `checkpoint.prev.json` (rename before replace), so a crash in the write leaves one valid file.
- **Integrity**: a SHA-256 of the body inside a small header, and the body's byte length; a
  checkpoint whose hash or length disagrees is *corrupt* (K).
- **Compatibility**: `format: vehicle.checkpoint.v1`; `engine` = the vehicle's git commit if
  available, else a SHA-256 over `plant.corpus_files(root)` plus `tools/plant.py` and
  `tools/console.py` bytes; `tick_hz`; the schema of the truth map is implied by `engine`. A
  mismatch on `format` or `engine` is *incompatible* (K). No migration path in v1: release.md's
  rollback policy is "select a previously accepted set", not "convert".
- **Cost**: 1.3 ms p50 / 32.7 ms p95 per durable write measured with the ring empty, 1.8 / 2.3 ms
  with it full; the tail is why J does not checkpoint every tick.

### J — Checkpoint cadence, the durable tick, and what a frame may describe

| Option | Cadence |
|---|---|
| J1 — every tick | 50 durable writes per mission second: 65–90 ms p50 per mission second and, on the measured ext4 tail, up to 1.6 s, before physics. Excluded by the measurement. |
| J2 — every `N` ticks, plus a durable effects record on every tick that accepted an effect | Recovery = load snapshot, replay the logged effects by tick (§6 rule 6) to the last logged tick; the tail of effect-free ticks since then is lost *as time*, not as physics. Cost: one durable write per `N` ticks plus one per commanded tick (agent command rates are seconds to minutes apart). |
| J3 — at phase boundaries only | `plant.md` §6's full snapshots. Phases are hours long; a restart would lose hours of mission time. Permitted as the *additional* full snapshot §6 asks for, not as the restart cadence. |

The **durable tick** is the newest tick the vehicle can provably resume at. Three rules follow from
`presentation.yaml#frame` ("`met_s` cannot jump backward") and contract §6 ("nothing already
executed should be forgotten"):

1. A frame or mirror never describes a tick beyond the durable tick — so B's `k` and J's `N` are
   the same number, or publication lags the checkpoint by the difference.
2. A result for an accepted effect is written only after that effect's record is durable. A crash
   between the two re-publishes the result from the record on restart; the file is made with
   `O_EXCL` so a duplicate name takes a suffix rather than replacing (as `write_result` already
   does).
3. A refused command's result may be written at once: a refusal stages nothing and is replayable
   from the trace only as a receipt, which is what the journal already carries.

Consequences. *Fleet*: with `N = k = tick_hz` the fleet sees one frame per mission second, every
frame it has ever seen is a tick the vehicle will stand behind after a restart, and a restart loses
at most one mission second of effect-free physics. *Replay*: the per-tick effects record *is* the
§6 replay trace; recording it for recovery and recording it for replay are the same file. *Budget*:
recovery costs `N × (step + hash)` — about 1.1 s at `N = 50` today, 66 s at `N = 3,000`; the
hash dominates again (A's pulled-forward question). *Chassis*: none beyond H.

**Recommendation: J2 with `N = k`**, both run inputs, recommended `tick_hz` (one mission second).

### K — A corrupt or incompatible checkpoint, and the crash loop

Fixed by WP08's acceptance: "corrupt/incompatible checkpoints fail explicitly". The question is what
the executive does *next*, because `restart: unless-stopped` will start it again in seconds.

| Option | Behaviour |
|---|---|
| K1 — refuse (exit 3), stay dark | Today's choice-D consequence, extended to a bad checkpoint. The container crash-loops until an operator acts. |
| K2 — fall back to the previous generation if it verifies; refuse only if both fail | A bad write costs one `N` of mission time, recorded as such; two bad files is a refusal. |
| K3 — start a fresh world | **Excluded**: "restarting cannot silently reset physical state". |
| K4 — `--new-world`: the operator's explicit flag | ADR 0001's rejected alternative for D, adopted here as the deliberate exit from K1/K2's refusal: the executive starts a fresh world on the bound directory, writes a `discontinuity` record (old `world_id`, last durable tick, reason, wall stamp) into the journal and the new checkpoint, and the windows see a new `world_id`. Never implied; never read from the environment. |

**Recommendation: K2 with K4 as the only escape.** The refusal message names the file, the check
that failed, and the flag. An *incompatible* checkpoint (I) is never fallen back from: the previous
generation was written by the same engine as the current one, so it is incompatible too.

### L — What agents see on a restart

Fixed: `boot_id` changes (the frame says so); `met_s` continues from the durable tick (F1, J);
`world_id` is unchanged. The genuine question is `seq`.

| Option | `seq` after a restart |
|---|---|
| L1 — continue from the checkpoint | The window's `seq` is checkpointed; the next frame is `seq + 1` under a new `boot_id`. |
| L2 — reset to 0 per boot | Permitted by the letter of `presentation.yaml` ("monotonic within a boot"). But `write_frame` names frames `NNN.json` by `seq` and says "a name is never reused"; `ring_frames` counts only frames `< self.seq`, so after a reset the previous boot's higher-numbered frames are neither counted, pruned nor overwritten in order — the ring's `held` and `losses` would both be wrong until `seq` passed the old high-water mark, and `000.json` would be rewritten. |

**Recommendation: L1.** The restart is still visible through `boot_id`; a gap in `seq` still means
loss; `pending.json.ticks` and `state.json.executive.tick` continue. Also under this heading: result
filenames are stamped with wall UTC (`write_result`), while `mission.yaml` says "every published
timestamp is reproducible" and the contract says the stamp "is the vehicle's clock". Options:
**keep wall UTC** (status quo; the contract's "UTC everywhere" reads naturally as wall) or **MET
rendered as UTC from `met_epoch_utc`** (reproducible, and an agent learns no wall time from a
filename). Recommendation: **MET rendered as UTC**, because every other time the vehicle publishes
is already MET and the filename is the one that is not. This is a fleet-visible change and so the
operator's.

### M — The phase is a function of the tick (vehicle maintainer; recommended, not open)

`Executive.phase` is `--phase`, constant for the run, and is what `mission.phase` publishes.
`plant.mission_ladder` and `plant.mission_phase(ladder, tick)` exist and no run calls them. WP08's
acceptance names "phase boundaries" as restart points, which presupposes that the executive's phase
*moves*. Recommendation: the phase is derived from the tick on every cycle; `--phase` becomes the
start of the run (the tick is set to that phase's `first_tick`), with the recorded caveat that
`initial_values` describes MET 0 and a run started at `descent` with MET-0 values is a labelled
fixture, not a mission. `mission.phase_entry_seq` increments at each derived boundary and is
checkpointed. The posture machine (`REQUEST_HOLD` and the rest) stays WP05's.

## Decision

Proposed. Nothing here is accepted. The operator decides A–H, J–L; the vehicle maintainer decides I
and M and the compare-point encoding question A pulls forward, each with the chassis reviewer for
the adapter rows. The implementation may proceed on the policy-independent children listed below
and on nothing else.

## Acceptance and consequences

### Acceptance cases this record commits WP08 to (each becomes a test with a tick-level oracle)

- Two executives started from the same checkpoint and fed the same effects log produce
  byte-identical `state_hash` at every tick and the same `lineage_head`.
- A restart at a scheduled-event tick, a dwell boundary, a deferred command's `due_tick` and a
  phase boundary each yields the same truth, receipts and lineage as the uninterrupted run (the
  package's second acceptance bullet).
- A frame never carries a `met_s` greater than the durable tick's; after a kill and restart no
  published `met_s` is ever followed by a smaller one across the boot boundary.
- A result file exists for every accepted effect in the log, and no command ever has two results
  with different outcomes.
- A checkpoint with one flipped byte, a truncated body, a wrong `engine`, a wrong `format`, a link
  in its place, or a missing `rng` section refuses with a message naming the check; the previous
  generation is used when it verifies and the engine matches; `--new-world` is the only way past
  both failing, and it writes the discontinuity record.
- No file under any `/diode/<slug>/` contains a truth-only key, the lineage, the hash or the
  checkpoint path, before or after a restart (the package's third bullet).
- With `m` and the overload bound set, the journal's per-tick lag is reported as p50/p95/p99/max
  and missed-deadline count; the scheduler sheds publication before dilating and stops at the
  bound; a paused executive rewrites the mirror, appends no frame, claims no console.

### Consequences for existing records

- ADR 0001 choice D becomes: a bound directory *resumes* from a verified checkpoint whose
  `world_id` matches its record; it *refuses* without one, or with a corrupt or incompatible one
  (K); `--new-world` is the operator's recorded exit. The "restart consequence of choice D" — a
  dark container until an operator clears the windows — is withdrawn once H1 and J2 land.
- ADR 0001's "the directory's record is authoritative" is amended per H: the checkpoint is
  authoritative; the record is its copy.
- `tools/console.py`'s `main` ("an executive always starts at tick 0") and the meaning of
  `--cycles` and `--poll` change under A2/B2; `plant.md` §6's "the clock's indifference" acquires
  the number it has been missing.
- `docs/release.md`'s manifest rows "clock and multiplier policy" and "checkpoint
  format/compatibility identifiers" are given their concrete fields: `m`, `k`, `N`, the overload
  bound, the pause/downtime policy ids, `format`, `engine`.

### Cross-repository impact (chassis, separate package)

1. A private writable volume for the `vehicle` service alone and `--state-dir` in
   `serve_vehicle.sh` (H1). **Blocks** the resume path's deployment evidence.
2. One `console.py` process for all slugs (ADR 0001's open follow-up); per-slug processes would be
   per-slug worlds and per-slug checkpoints.
3. `DIODE_POLL_SECONDS` re-described as B's publication cadence or retired; `VEHICLE_MULTIPLIER`
   and the overload bound passed through; agents mounted on `/diode/<slug>/` (ADR 0001).
4. The pause control (D) and `scripts/status.py` reading the checkpoint header.
5. `restart: unless-stopped` with K's exit 3: the crash loop is now the designed behaviour for an
   unrecoverable checkpoint, and the operator's action is `--new-world`.

### Child issues WP08 should be split into

Recorded here rather than in a second planning file: the template asks this section to "name the
packages that implement and verify it", ROADMAP.md makes the GitHub issues the source of delivery
status, and CONTRIBUTING.md warns against a second editable copy of planning prose. Each child is
one reviewable PR with its own acceptance; the first six are **policy-independent** and may start
before the operator decides; the rest wait on the lettered choice named.

| # | Child | Acceptance example | Waits on |
|---|---|---|---|
| 1 | Checkpoint format v1: writer, reader, header, integrity and compatibility checks (`G`, `I`) | Round trip of an executive's state is byte-identical; each corruption in the acceptance list above refuses by name; a wrong `engine` refuses; the previous generation is used when it verifies | nothing |
| 2 | `--state-dir` plumbing, with the journal's `is_relative_to(diode_dir)` refusal applied to it; root record rewritten from the checkpoint (`H`) | A `--state-dir` inside `--diode-dir` refuses; `.executive.json` after a restart equals the checkpoint's header; no window file names the state dir | nothing (chassis mount is a separate package; local tests use a scratch dir) |
| 3 | Resume mechanics: an executive constructed *from* a checkpoint, continuing tick, dwell, deferrals, arms, `seq`, `receipts`, spend, lineage (`G`, `L1`) | Kill at tick `T`, restart, truth and lineage at `T + 100` equal the uninterrupted run's; `boot_id` differs, `seq` continues, `met_s` never decreases | nothing for the mechanics; G1 and E for what *spend* and *arms* mean |
| 4 | The per-tick effects record and replay-from-record (`J2`'s trace) | A run's record replayed onto its snapshot reproduces every `state_hash`; a record with one missing tick refuses | nothing |
| 5 | The clock seam: `m`, a recorded wall epoch, due-vs-actual lag per tick in the journal, publication cadence `k` as a parameter, `--poll` retired (`A2`, `B2` as mechanics) | With `m` and `k` set, the number of ticks between two frames is `k` regardless of machine load; the lag series is reported p50/p95/p99/max | nothing for the mechanism; A–C for the values and the overload action |
| 6 | The compare-point encoding (`A`'s pulled-forward question): what the per-tick hash must cover and an encoding that costs less than the step | Two runs of the same seed agree on every per-tick hash; a one-slot change in the delay ring changes it; cost measured and recorded | vehicle maintainer |
| 7 | Budget on the chosen clock, checkpointed (`E`, contract §6) | After a restart `used_this_window` and `oldest_expires_in_seconds` continue; under E2 a replay reproduces every budget refusal | E |
| 8 | Overload behaviour: shed publication, dilate within the bound, stop with a recorded hold (`C`) | A deliberately slow instrument makes the lag series climb; frames are shed before ticks; the clock stops at the bound and the journal says why | C |
| 9 | Pause and resume control (`D`) | Paused: mirror rewritten, no frame, console unclaimed, dwell unchanged; resumed: the first tick claims what was written during the pause | D |
| 10 | Downtime policy and the discontinuity record (`F`, `K4`) | Kill, wait a wall minute, restart: `met_s` continues from the durable tick and the journal carries the gap; `--new-world` on a bad checkpoint writes the discontinuity and the windows see a new `world_id` | F, K |
| 11 | Phase from the tick, `phase_entry_seq` checkpointed (`M`) | A run crossing `translunar_coast → lunar_orbit` publishes the boundary at tick 13,140,000 and a restart on that tick reports the same phase and entry count | vehicle maintainer |
| 12 | Result filename stamps on the chosen clock (`L`) | Under MET-as-UTC, two replays of one trace produce identical `output/` listings | L |

Children 1–6 can be reviewed and merged with the status of this record still `proposed`, because
none of them fixes a policy; child 5 ships with `m` *required* and no default, so nothing deployed
can run before the operator names the number.
