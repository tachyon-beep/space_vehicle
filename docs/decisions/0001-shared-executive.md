# 0001 — One executive owns the physical world; windows are ingress and evidence views

- Status: accepted (choices A–D); chassis review of the adapter consequences pending
- Date: 2026-10-09
- Decision owner: vehicle maintainer (`tachyon-beep`); chassis reviewer for the adapter/mount consequences
- Related work package / evidence gaps: WP01 (#1) first integration slice; WP08 (#8) owns clock, persistence and restart; WP05 (#5) owns protection logic
- Reviewers and review evidence: owner accepted the four recommended choices in the implementing session on 2026-10-09; independent artifact review recorded in the WP01 handoff; chassis reviewer pending
- Supersedes / superseded by: none

## Question and constraints

The reference console (`tools/console.py`) gives every window its own `initial_values(world)`,
never calls `plant.step`, counts a cycle as a "tick", times dwell and queue age on the wall clock,
and publishes its whole value map (including stock residuals) as a telemetry frame. The chassis
service starts one console process per slug, so ten agents fly ten vehicles. Completion gate I
needs one authoritative state commanded and observed through two windows.

Fixed inputs this record does not reopen:

- `docs/diode-contract.md` (frozen, chassis): one directory per principal; destructive atomic
  claim before acting; one result per command, refusals included; a malformed batch refused whole
  with one result; a command's identity is its text, so a repeated line is a repeated command;
  "consolidating the ten ingresses into the vehicle's own command queue is the vehicle's
  business"; authority re-evaluated at the moment of effect; truth never published; the result
  record may append correlation and lifecycle fields (§7.1).
- `plant.md`: `step(commands) -> state` knows nothing about files (§1); effects carry integer
  microsecond stamps and equal stamps keep the caller's order (§5); dwell runs on a clock that
  cannot jump (§5); MET is an integer tick count and replay is keyed by tick, not wall time (§6);
  the publisher turns evidence into files and the plant never reads a published value (§7).
- `apollo_diode.md:578-579` (cited by the existing console): first valid command in a conflict
  domain wins per tick, and the loser is told.

## Decision

**Ownership.** One executive process owns one `World`, one truth map, one tick counter, one
receipt sequence and one state lineage. Windows are created only by attaching a slug to that
executive; a window holds ingress, per-window gate preferences, its own results, mirror, ring and
deferral queue, and no physical state. `tools/console.py` remains the entrypoint and serves any
number of `--slug`s from one executive; one slug is the one-window case of the same path.

**Tick and effect timing.** Each executive cycle claims every window, validates, then advances
the plant exactly one tick through `plant.step` with the accepted state-staging commands as
effects stamped at `offset_us = 0` in arbitration order. Validation and effect therefore read the
same truth at the same simulated instant. Dwell and queue age are measured in simulated
microseconds (`tick × tick_us + offset`). How many ticks a wall second carries is **not** decided
here: the slice inherits one tick per cycle from the existing loop and records that the
multiplier, pause, overload and downtime policy belong to WP08.

**Arbitration.** The effective conflict key is the verb's declared `conflict_domain`, instantiated
against its arguments. Within one tick the first valid command for a key wins; a later command
with the same text is accepted as a duplicate of the winner and stages nothing further; any other
command for that key is refused `CONFLICT_SUPERSEDED` naming the winner's receipt. *Choice A:*
windows are visited in sorted-slug order rotated by the tick number, so no name holds a permanent
priority and the order is a pure function of (slugs, tick).

**Ingress bounds and malformed input.** The console read stays bounded by `MAX_READ_BYTES`. An
oversized, unparsable, non-object or half-written `console.json` produces one refusal result and is
claimed (commands emptied, last known variables preserved), so a broken writer cannot produce a
refusal every cycle. *Choice B:* a batch longer than a per-window cap is refused whole with one
result, by the same rule as a malformed batch; the cap is an operator ceiling (`--max-batch`), and
a window cannot raise it.

**Protection.** *Choice C:* an accepted command whose declared interlock the executive cannot
evaluate — because the threshold's point has no computed value, or because threshold evaluation is
not implemented yet (WP05) — is refused `INTERLOCK UNEVALUATED`, by the same rule the console
already applies to an owed dwell ("a command it cannot guard is a command it must not accept").
`set_bus_tie` reads `tie_dv_limit` on `power.dc_bus_a_v`, an algebraic state with no rule, so the
bus-tie close is refused in every window until the electrical solve (WP04) and protection (WP05)
land. The slice demonstrates the state change with an interlock-free, `apollo`-sourced command
instead. Irreversible `execute_event` declares interlocks and is therefore refused in this slice;
`arm_event` tokens are bound to the arming window and event.

**Evidence.** Frames carry `plant.emit_frame` output from the stepped truth — declared channels
only, never residuals, rings, internal sentinels or fault plans. Measurement channels are published
through an identity instrument (no noise, lag or quality model) until WP09; that substitution is
stated in the frame-producing code and in every handoff that cites the slice. Frames are identical
across windows except for the window's own `seq`; per-role restriction is not configured for
windows and is not invented here.

**Isolation.** A failure writing one window's files is recorded and does not stop the tick or any
other window. A stalled consumer only lets its own `output/` grow; its ring is bounded.

**One world per directory.** The executive takes an exclusive, non-blocking lock in the diode
directory before attaching windows, and stamps each window's `pending.json` with its `world_id`.
A second executive on the same directory refuses to start. *Choice D:* an executive refuses a window
whose `pending.json` names a different world, live or not: starting a fresh world on an old window
would silently reset its physics, and restart continuity is WP08's to decide. *Amended by WP08 child
2, per ADR 0002 H (2026-10-09):* with `--state-dir`, the exclusive lock is taken in the state
directory and the diode root's lock is not opened at all — a lock on a volume the agents can write
is a lock they can hold — and one world per directory is then held by the root record, not by the
lock; a start that finds a verified checkpoint in the state directory refuses with exit 3 naming
child 3 (#21) rather than starting over it.

## Acceptance and consequences

- Two windows on one executive see the same `world_id` and tick; a command from either changes
  the one truth; both rings carry the outcome in the same tick. The lineage is the executive's,
  in process and in its journal, never in a window file. A result's receipt carries the world,
  the tick, and the *window's own* result sequence; the executive's global sequence stays in the
  journal, and a loser is told the winner's window and the winner's window-local receipt.
- Conflicting, duplicate, malformed, oversized and over-cap input, a write failure in one window,
  and a second executive all have deterministic, tested outcomes with one result each.
- No published file carries a truth-only key.
- **Chassis follow-up (separate package):** `containers/serve_vehicle.sh` must pass all slugs to one
  `console.py` process instead of one process per slug, and its compose comment changes with it.
  Until then the deployed stack still runs one world per agent. The referee-count and
  `report.refuse` pins in the chassis reconciliation README move in a chassis commit.
- **Restart consequence of choice D.** Every second process on a bound window exits 3, so a
  vehicle container restarted under `restart: unless-stopped` stays dark until the operator clears
  or renames its windows, or WP08 defines continuity. *Amended by WP08 child 2, per ADR 0002 H
  (2026-10-09):* where `--state-dir` holds a verified checkpoint, clearing the windows is the wrong
  action and the refusal says so; the directory waits for child 3's resume. `--init` prepares a window (scenario, seed,
  ring) without binding it; the first executive to tick it binds it. The old cross-process resume of
  ticks, arm tokens, dwell and deferrals from the agent-writable `pending.json` is withdrawn: it
  restored authority (arm tokens) from a file an agent can write, and restored counters while the
  physics restarted from the initial state.
- Not established: restart, persistence, multiplier, interlock evaluation, instrument models,
  per-role views, gate I deployment evidence.

## Choices the owner decided (2026-10-09, all as recommended)

| | Recommended | Alternatives |
|---|---|---|
| A — cross-window order in a tick | sorted slugs rotated by tick | fixed sorted-slug priority (names rank); file arrival time (nondeterministic, writer-controlled) |
| B — per-window batch bound | whole-batch refusal above `--max-batch` (default 32) | refuse only the excess lines (one result each, unbounded output); no bound |
| C — interlock that cannot be evaluated | refuse `INTERLOCK UNEVALUATED` | apply and label it unevaluated (the console's current behaviour) |
| D — window already bound to another world | refuse until WP08 | explicit operator flag that starts a new world and records the discontinuity |

## Clarifications after independent review, 2026-10-09

Decided by the implementing session's coordinator on the independent reviewer's findings, under
the owner's standing instruction (2026-10-09) to close open choices by picking the best-supported
answer rather than stopping; each is implemented and tested in the slice, and each remains open to
the owner's revision. Two items filled gaps the decision text left silent; they are marked
*(gap filled)*.

- **The directory's record is authoritative, not a window's.** Binding and identity (`world_id`,
  the slugs served, scenario, seed, each window's ring bound, the tick) live in
  `<diode-dir>/.executive.json`, written atomically beside `.executive.lock` by the executive at
  `--init` (unbound) and at every tick, and read only under the lock. A record that cannot be
  read — unreadable, oversized, a link, malformed — refuses the start; it is never treated as a
  fresh directory. *Amended by WP08 child 2, per ADR 0002 H (2026-10-09):* when `--state-dir`
  holds a verified checkpoint, **the checkpoint is authoritative and the record is its copy**: a
  record that is missing, garbled, a link or disagrees is rewritten from the checkpoint and the
  mismatch journaled, never a refusal; only a readable record naming a different world refuses. The
  lock the record is read under is then the state directory's, not the root's. Without a checkpoint
  every sentence above stands. A window's `pending.json` still carries the identity keys for its reader and is
  read for nothing but the refuse-only legacy check: `ticks > 0` or a `world_id` on a slug the
  directory's record does not name refuses the start. One directory is therefore one identity.
  *(gap filled)*: a second `--init` on an unbound directory
  adds its slug to the record and inherits the recorded identity unless the caller names another,
  in which case the named pair becomes the directory's for every slug it records. **This assumes the agents are mounted on `/diode/<slug>/` and cannot write the diode root —
  and that assumption is currently unmet.** In the deployed chassis compose today every `agent_N`
  mounts `./volumes/diode:/diode` read-write (only `DIODE_DUTY_DIR` points at the slug), so an
  agent can forge or garble `.executive.json`, hold `.executive.lock`, and write another window's
  `console.json`. The record is authoritative only once the chassis mounts each agent on its own
  slug or makes the root unwritable: a chassis follow-up required before completion gate I.
  *Amended by WP08 child 2 (2026-10-09):* the assumption is met by the chassis's per-slug mounts —
  chassis commit `adf38d6`, on the chassis branch `aurora-port` and not yet on chassis `main`, binds
  each agent to its own `/diode/<slug>` and mounts the diode root on the vehicle service alone. Until
  the chassis adopts it, treat the root as agent-writable.
- **The directory's record is authoritative for identity, and a window's files may lag it.** A
  second `--init` rewrites `.executive.json` at once; a window's `pending.json` and `state.json`
  repeat the identity for their reader and are rewritten at the next tick that serves the window,
  so for one tick after a second `--init` (or a re-preparation) they may show the previous value.
- **A window that fails its pre-claim check keeps its due deferrals queued.** They are neither
  settled nor dropped while the window is dark; they settle — or `EXPIRE`, which is the one
  production path to `EXPIRED` — once it heals. Absence (`ENOENT`) heals itself: the directory or
  subdirectory is re-prepared handle-relative for the next cycle, with the dark tick recorded; a
  planted link or a wrong type stays a skip until it is cleared.
- **Adding a window to a live directory requires clearing it**, until WP08 defines continuity: a
  bound record refuses every start, `--init` included. *(gap filled)*: `--plan` and `--plan-json` bind and write nothing, so they are answered on a
  bound directory — under the lock, so a *live* directory still refuses them for the lock's reason.
- **Deferrals settle first.** At the start of each cycle every window's due deferrals are settled
  in rotation order, before any window's ingress. A deferral's *acceptance* does not claim its
  conflict domain; its *settlement* — the moment of effect — does. `apollo_diode.md:578` is "first
  valid command *received*", and a command due now was received earlier than any claimed now.
  Settlement verdicts are published whether or not the same window's claim then fails; a
  publication failure is recorded.
- **The window is an adversary's directory, and the operations on it are handle-relative.** A
  check-then-act — `is_symlink()` and then a write by path — leaves the gap between the two to an
  agent that swaps `output/` or `telemetry/` for a link inside it. So the diode directory, each
  window's directory and its two subdirectories are opened `O_RDONLY | O_DIRECTORY | O_NOFOLLOW`
  once per cycle, and every read, write, rename, unlink and listing is relative to those handles
  (`dir_fd`): a link or a file where a directory should be is refused at the open (`ENOTDIR`, or
  `ELOOP`), which is the window's recorded failure for the cycle with no write, and a link swapped
  in after the open is simply not where the handle points. The console is opened `O_NOFOLLOW |
  O_NONBLOCK`, refused unless a regular file within `MAX_READ_BYTES`, and any decoder exception is
  "not JSON"; the five files are `lstat`ed through the handle, regular where they exist and never a
  link; temporaries are created `O_CREAT | O_EXCL | O_NOFOLLOW` under random names; a result file is
  made with `os.link`, which never replaces. **Agent `variables` are preserved and selectively
  honoured, and the two are different questions.** Preserved: the claim rewrites the console with
  `variables` exactly as the agent last wrote them (replace, not merge; a file without a
  `variables` object keeps the previous map), which the contract requires ("persistent — the
  vehicle never clears it") and which is bounded by construction, the file being at most
  `MAX_READ_BYTES`. Honoured, computed from the preserved map each cycle: published (instantiated)
  gate names with bool values, and `allowance` as a non-negative integer clamped to the operator's
  ceiling (§9 check 8: lower, never raise); anything else is simply not honoured — a gate keeps its
  default, the ceiling stays in force — and no JSON value can raise. JSON is RFC 8259's on both
  sides: `NaN`, `Infinity` and `-Infinity` in a console are "not valid JSON", and the vehicle
  encodes with `allow_nan=False`. No result file is written
  about variables, because a result answers a command. The first remediation stripped the map and
  wrote a `variables_ignored` result; the chassis's contract probe caught both. A command's
  arguments are held to its `argument_schema`: an undeclared name, an enum value off its list, a
  value past its `max_length` or 128 bytes. Anything one window raises in its claim or publication
  is that window's failure, recorded (a count and the most recent entries), and the tick proceeds.
- **A refusal never prints a live value.** An interlock refusal says the point has or has no
  reading, never the number (`plant.md` §7).
- **Bounds in memory.** The executive keeps the latest lineage link and a bounded recent window;
  the journal holds the whole history. The ring is counted by frame number, and a file in
  `telemetry/` that is not a numbered regular frame is neither held, pruned nor counted.
- **Budget semantics are WP08's.** `budget.oldest_expires_in_seconds` still counts the wall
  clock; the allowance is clamped and reported, not yet enforced.
- **The mirror carries no roster.** `state.json.executive` is `{world_id, tick}`; which other
  windows exist is the directory's record's.

- **The run's identity is not published to the fleet** (2026-10-09, coordinator under the owner's
  delegation; the aurora-port spec §8 asked that vehicle-private state stay off the agent-writable
  window). The mirror carried `vehicle.scenario` and every window's `pending.json` carried
  `scenario` and `seed`. With the per-slug mounts the directory's `.executive.json` is the
  operator's alone, so the identity lives there and in the state directory; the windows carry
  neither. The seed keys the fault plan and the scenario says how hard the run is: publishing them
  would do part of the fleet's information management for it. `presentation.yaml#mirror` never
  declared `scenario`, and `check_console_flags` now reads the remembered values off
  `Executive.root_record` rather than the window's copy.
