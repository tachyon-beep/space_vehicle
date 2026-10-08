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
would silently reset its physics, and restart continuity is WP08's to decide.

## Acceptance and consequences

- Two windows on one executive see the same `world_id`, tick and lineage; a command from either
  changes the one truth; both rings carry the outcome in the same tick.
- Conflicting, duplicate, malformed, oversized and over-cap input, a write failure in one window,
  and a second executive all have deterministic, tested outcomes with one result each.
- No published file carries a truth-only key.
- **Chassis follow-up (separate package):** `containers/serve_vehicle.sh` must pass all slugs to one
  `console.py` process instead of one process per slug, and its compose comment changes with it.
  Until then the deployed stack still runs one world per agent. The referee-count and
  `report.refuse` pins in the chassis reconciliation README move in a chassis commit.
- **Restart consequence of choice D.** Every second process on a bound window exits 3, so a
  vehicle container restarted under `restart: unless-stopped` stays dark until the operator clears
  or renames its windows, or WP08 defines continuity. `--init` prepares a window (scenario, seed,
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
