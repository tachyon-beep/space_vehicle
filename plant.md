# The plant

The core contract: what `step()` is, what a domain must declare to be part of it, and what
the plant is forbidden to do. Everything here is a decision the corpus does not make — no
document names an integrator, and `RK4`, `Verlet`, `Runge`, `Euler` and `symplectic` occur
zero times across sixteen thousand lines — so this file is where those decisions are recorded
once instead of being rediscovered in each domain.

Sources it does build on: `integration/simulator-design.md` §3.2–3.7 (the core, the seams,
the coupling rules), `integration/review-findings.md` §5–7 and §14 (the method split, the
stability results, the drift list), and `apollo_diode.md:735-905` (rates, fidelity, telemetry
budget). Conflict register D-09 is the decision record.

---

## 1. The shape

```
                 ┌───────────────────────────────────────────────┐
   adapters      │  diode file surface │ white-team console      │
                 └───────────┬──────────────────┬────────────────┘
                             │                  │
                    ┌────────▼──────────────────▼────────┐
                    │  Effects: clock, consoles, GM       │   every external input
                    └────────────────┬───────────────────┘   crosses one line
                    ┌────────────────▼───────────────────┐
                    │  step(commands) -> state            │   knows nothing about files
                    │    instruments -> published evidence│
                    └─────────────────────────────────────┘
```

**The core is `step(commands) → state` and knows nothing about files**
(`simulator-design.md:166-172`). Three adapters sit on it: the diode file surface, the
white-team console, and optionally a research harness. **The Effects boundary is the adapter
seam**, and it exists so that the file layer cannot grow tendrils into the plant: every
external input — the wall clock, an agent's command, a white-team injection, a GM utterance —
enters through one function, and the core is drivable at thousands of ticks per second by a
test that never touches a directory.

Build only the diode adapter today. Design the seam now, because the cost of not doing it is
a core that can no longer be stepped without a filesystem.

---

## 2. A domain declares, the scheduler runs

**Domains do not call each other** (`simulator-design.md:225-232`). A domain is a pure
function over a state snapshot. It declares what it reads and what it writes, `coupling.yaml`
declares the edges between them, and the scheduler decides the order.

```yaml
# domains/<name>/domain.yaml — the declaration the scheduler reads
domain: power
natural_rate_hz: 50          # declared even though the scheduler ignores it today
reads:  [o2_csm, h2_csm, prop_rcs]          # node ids from coupling.yaml
writes: [fuel_cell, bus_a, bus_b, battery_energy, load_shed_state]
state:
  - {id: bus_a_v, method: algebraic,      unit: V}
  - {id: battery_soc, method: stock,      unit: "-", quantum: "1e-6"}
  - {id: pump_1_speed, method: lag,       unit: "rad/s", tau_s: UNCONFIGURED}
  - {id: lcl_1_tripped, method: discrete, unit: bool, hysteresis: {assert: 1.25, clear: 1.05, dwell_ms: 500}}
```

**Ordering: Gauss-Seidel on the DAG, one-tick delay on back-edges only**
(`review-findings.md` §3, which is also where the alternative was measured and rejected).
Under Jacobi — "every domain computes from the snapshot and all writes commit at t+dt" —
*every* edge carries a tick, so apollo's five-hop degraded chain acquires 100 ms of
artificial lag instead of 20 ms, and on an oscillatory cycle `|λ| > 1` for every `ω·dt > 0`,
so no stable step size exists. Under Gauss-Seidel the same cycle is stable for `ω·dt < 2`.

Topological order is only partial, so **the linter emits a total order** and the scheduler
obeys it: topological sort with a frozen lexicographic tiebreak. Without the tiebreak, two runs
of the same seed can differ, which is the one thing §6 exists to prevent.

**A total order is not automatically a *tick* order, and the difference is silent.** The first
implementation of `derive_schedule` ran Kahn's algorithm taking the nodes with no *successors*
first — which is the last element of a topological order, so the emitted schedule was exactly
reversed. Nothing refused it, because the property the graph was checked for was the *absence of
a cycle*, and a reversed topological order has no cycle either: it is a valid answer to the wrong
question. Every one of the thirty-nine ordering constraints was violated, and the reference plant
had been ticking fuel cells after the buses they feed and tanks after the engines that drain them,
which is precisely the "stale value that looks like physics" failure §4 is written against. It
was found by walking the edge list and asking, for each edge, whether its producer came first —
so the promise below is now a checked one rather than a stated one.

What a correct order buys is not just correctness of a tick. Two bugs surfaced within minutes of
fixing it, both of which had been invisible behind a plausible-looking list: `battery_energy` had
**no inbound edge at all** except a thermal back-edge, so the battery was a stock nothing ever
charged (`E-BUS-BAT`, §10's `accumulate` case, now declared with its cycle `C-BAT-BUS`); and the
plant's first stop moved from an arbitrary state to the first thing the schedule genuinely cannot
compute. **A reversed order still contains every node**, which is why nothing downstream noticed.

**The tiebreak is on node id, not on domain name, and that correction cost a round to find.**
The order was originally specified over domains, and a domain order *cannot in general exist*: a
domain order is a coarsening of the node graph, and coarsening creates cycles that the physics
does not have. `comms -> power -> consumables -> propulsion -> gnc -> comms` is a cycle in the
domain projection, and there is no node-level path from any of those nodes back to itself — it
exists only because `E-AMP-LOAD` leaves `link` and `E-FC-DRAW-O2` leaves `fuel_cell`, and the
projection cannot tell those are different nodes. A domain-level sort therefore reports loops
that are not there and refuses schedules that exist.

So the schedule is over **nodes**, and a domain with states on several nodes appears at several
points in it. That is what Gauss-Seidel does anyway: the domain is an authoring unit, not a
scheduling unit. The derived order is 59 nodes and `check_vehicle.py` reports its tail on every
run.

**An edge names the state it acts on at each end, and the three fields are not
interchangeable.** `advances:` names the state on the **target** node the flux feeds;
`drains:` names the stock on the **source** node the flow leaves; and `reads:` names the state
on the **source** node whose value the flux multiplies. The first two are required where the
node carries more than one state, because otherwise nothing says which of them the edge is
about. The third is the one that changes what an edge may *say*: `state_values` keeps a node
key only for a node a single state owns, so an edge reading a crowded node reads `None` on
every tick — and `reads:` is how it names the quantity it actually wants instead. It is held to
its own source node, like `drains`, because an edge cannot read a value from a node it does not
start at.

A domain that reads a value a peer writes *within the same tick* is declaring a Gauss-Seidel
dependency, and the linter refuses it if `coupling.yaml` does not contain the edge. Cycles are
allowed only when declared with a named back-edge (`coupling.yaml#cycles`), and a latched
state on a delayed cycle must carry hysteresis — dwell alone is a relay oscillation that an
agent reading 2 Hz telemetry will read as physics (`review-findings.md` §7).

---

## 3. Seven methods, chosen by model form rather than by rate

The subsystem specs' millisecond figures (`electrical_diode.md:251` ≥1 kHz sensing,
`main_propulsion_diode.md:547` 500–1000 Hz valve acquisition) are **flight-software
scheduling periods and protection latencies, not plant time constants**. Only plant constants
bind an integrator. So the plant is not stiff, 50 Hz is defensible, and the right split is by
what kind of thing a state is (`review-findings.md` §5):

| Class | What lives here | Method |
|---|---|---|
| **Algebraic / quasi-steady** | bus V and I, tie and LCL currents, coolant flow given pump speed, feed and regulator pressures, link budget, control allocation, load margin | Nodal solve to consistency each tick. **No integration.** |
| **First-order lag** | pump spin-up, converter regulation, valve transit, sensor lag, every thermal RC node, battery relaxation | **Exponential map**: `x ← x∞ + (x − x∞)·α`, `α = exp(−dt/τ)` precomputed. Never forward Euler — at τ = 0.5 ms Euler gives 8.1e15 where the exponential map correctly gives 1.9e-174. |
| **Conserved stock** | O₂, H₂, water, propellant, pressurant, battery charge, CO₂ absorber, man-hours | Explicit Euler on the flow, **fixed-point + residual accumulator** (§4). |
| **Transport delay** | coolant transit, link delay, any pipe long enough that "instantaneous" is wrong | **Tick-indexed ring buffer**, one slot per tick, indexed by tick number rather than by elapsed float time. |
| **Rigid-body 6-DOF** | position, velocity, quaternion, body rates | **Semi-implicit / Verlet in coast, RK4 in burns**; renormalise the quaternion every tick. Forward Euler accumulates +1.06 % in semi-major axis over eight days — 2,000× the published 0.01 km precision — and rate cannot fix it: it needs 82 µs. |
| **Discrete / latched** | trips, dwell timers, hysteresis, valve states, staging, pulse modulation | **Sub-tick event queue, integer-µs timestamps**, zero-crossing detection (§5). |
| **Stochastic hazard** | λ(t) per component | Accumulate `∫λ dt` and fire when it exceeds one pre-drawn `Exp(1)` variate — one draw per component per lifetime, not 34.56 M Bernoulli trials. |

**This table said six for one revision, and the seventh found itself.** A transport delay is
not a lag: a lag forgets its history exponentially while a delay *is* its history, and the
difference is exactly what `review-findings.md` §12 is about — that an RC network mixes
instantaneously, and that losing the transit collapses "the pump stalled just now" and "the
pump has been degrading for an hour" into the same observation. The linter refused the first
domain that needed it, by name. That is the mechanism working: a contract with a hole in it
fails loudly at the first thing that falls through, provided the hole is a *missing allowed
value* rather than a defaulted one.

The delay's two implementation constraints come from the reviewer and are not negotiable:

- **A ring, not a chain of small nodes.** A chain of nodes each with a residence time below
  `dt` reintroduces the stiffness the exponential map exists to remove, and it is the obvious
  way to build a delay if you are thinking in nodes.
- **Indexed by tick, not by accumulated wall time.** The delay is part of the state, so it is
  part of the snapshot and part of the compare-point hash; a delay stored as a float deadline
  is a replay divergence waiting for a slow machine.

**RK4 inside a domain with frozen cross-domain inputs is an accurate integration of the wrong
right-hand side.** The coupled system is still first-order; if second order is ever wanted it
is Strang splitting, not a better tableau.

---

## 4. Stocks are exact

A conserved quantity is `mantissa × 10^exponent10 × unit_code`, with **`exponent10` static per
quantity**, fixed at config load. It is a unit scale, not a dynamic field: if the exponent can
vary at runtime, two quantities of the same stock cannot be added without rescaling, and
rescaling rounds — reintroducing exactly the drift the representation exists to remove
(`review-findings.md` §2). With it static, all stock arithmetic is `sint64` addition and the
conservation assertion becomes exact with **no tolerance**: a non-zero residual is a bug.

**A fixed-point stock has a dead zone, and the dead zone is the experiment.** If `ṁ·dt < q/2`,
every tick rounds to zero and the stock never moves. Worked on the quantity this vehicle
cares most about — `eclss.leak_rate_g_s`, published precision 0.01 g/s:

```
nominal leak 0.0064 g/s (vehicle.yaml#consumables, from the Apollo 11 flight figure)
  q = 1 mg  -> 0.128 quanta/tick -> rounds to 0 -> the leak never happens
  residual accumulator, either quantum -> the leak happens exactly
```

The nominal LM cabin leak is *below the instrument's published precision*, so it is invisible
to the sensor by design; the plant must still lose the mass. Therefore:

- **Every stock uses a Bresenham residual accumulator**: `moved = (scaled + acc) // SCALE;
  acc = (scaled + acc) % SCALE`. Exact to the quantum over any horizon, integer-only,
  horizon-independent.
- **Every flow declares a minimum magnitude**, and the linter refuses a build where it does
  not exceed `q/dt` even with the accumulator, since a flow below the quantum is a modelling
  error rather than a rounding one.
- **Zero-crossing raises an event and records a shortfall.** Never clamp silently, never go
  negative. The shortfall is the evidence.
- **Transfers are one integer quantity, computed once, applied to both ends.** If producer and
  consumer each compute in floating point they differ in the last bit, and then either the
  assertion fires or the vehicle leaks in secret.
- **Mass is conserved exactly; energy is not asserted.** The fuel cell has exact
  stoichiometric mass ratios and a float efficiency. Energy gets a tolerance band and a trend,
  never an assertion — the same category error as asserting conservation on thermal margin.

---

## 5. The event queue

Latched states advance on a sub-tick queue with **integer-microsecond timestamps**, not on the
tick boundary. This is not a refinement: 20 ms decides an outcome in exactly one place — two
events landing in the same tick on the same latched comparator — and there the delay decides
which the state machine sees first, which is amplified and unrecoverable because the state is
latched. **That is a tie-break masquerading as physics, and the fix is timestamps, not a
smaller `dt`** (`review-findings.md` §7).

Consequences:

- A command's record carries the tick it landed on **and** an integer-microsecond offset from
  the event queue. `main_propulsion_diode.md:555` wants a ≤10 ms effect-time snapshot and a
  20 ms tick cannot produce one; this is what produces it, not a faster frame.
- The same offset closes `review-findings.md` C-08's other half: the decision-latency measure
  in `simulator-design.md:275-278` — the gap between the tick a command landed on and the tick
  its author last read state at — is only meaningful if both are exact.
- Discrete states are **edge-triggered and periodically reconciled** (`avionics_diode.md:176`),
  so a lost edge cannot leave a consumer permanently wrong.
- Dwell timers run on the **monotonic** clock, never a source time that can jump
  (`avionics_diode.md:192`), so a time-correlation adjustment cannot expire a safety dwell.

---

## 6. Determinism, and what it does and does not buy

> **Two runs are equivalent iff, for the same seed and the same recorded input trace, every
> per-tick state hash is byte-identical — same build, same platform.**

Tier M, same-machine, with a forced promotion for external effects
(`simulator-design.md:176-188`). Not logical-equivalence-within-ε: this system is full of
latched thresholds inside feedback loops, and an ε difference flips a comparator one tick
early, which flips a discrete state, which changes the cascade. No ε survives a comparator.

| Rule | Why |
|---|---|
| **MET is an integer tick count.** | `t += 0.02` in float64 over 34.56 M ticks destroys exact comparisons such as "is this a 1 Hz publish tick". |
| **One `master_seed`, recorded as a run input, never as a log line.** | It is an input. Logging it as a comment makes it possible to run without one. |
| **Streams derive by name**: `(master_seed, domain, component_id, purpose)`. | Twelve domains arrive over months. With index-keyed derivation, adding thruster 9 reshuffles every existing stream and silently invalidates every prior run. |
| **Canonical summation order**: contributors sorted by id before summing. | Float addition is non-associative and hash-map order is not a guarantee. Make it a `Flow` type constructible only from an ordered list, not a rule in prose. |
| **Compare-point at every tick**: a hash over canonically-encoded state. Full snapshot at phase boundaries, deltas between. | Localisation becomes binary search to the first differing tick and then structured comparison inside it. For a white team deliberately perturbing a feedback system this is not optional. |
| **Replay drives from the trace, keyed by tick index, not wall time.** | Otherwise the number of ticks between two commands depends on machine load and no two runs match. With tick-keying an eight-day mission replays in minutes, because nothing waits for anything. |

**What it does not buy.** An eight-day trajectory has a positive Lyapunov exponent.
"Eight days is reproducible" is a claim about bit-identity, not about physical predictability.
Two claims, and only the first is on offer.

**The clock's indifference is the mechanism** (`simulator-design.md:259-278`). Sim time
advances on the multiplier whether or not anyone is thinking, so the cost of reasoning is
already real: a specialist who thinks for ten minutes of wall time has spent *x* seconds of
mission during which the vehicle drifted, drained and heated. No reasoning budget is imposed.
An explicit budget would supply the answer; the clock's indifference makes triage worth
inventing, and it only counts as a finding if the fleet built it.

---

## 7. Truth, evidence, and the one-way boundary

The plant owns hidden truth. **The instruments turn `T` into `A`; the publisher turns `A` into
files.** Three layers, one direction, and the plant never reads a published value
(`apollo_diode.md:34-40`).

```
state(t) ──▶ instruments ──▶ published evidence ──▶ files ──▶ agents
     ▲                  (noise, bias, quantisation, saturation,
     │                   stuck, dropout, lag; quality assigned by a
     │                   function that cannot see the fault state)
     └── commands, revalidated at the moment of effect
```

**Quality codes are assigned only when a real onboard diagnostic would detect the problem. A
silently biased sensor stays `GOOD`.** The obvious implementation violates this in one line —
`if sensor.faulted: quality = SUSPECT` — and that line deletes the experiment, because agents
then stop diagnosing and start reading a fault flag. **Enforce it structurally, not by
discipline: the quality-assignment function's signature does not contain the fault state.** It
receives what a real diagnostic receives — the reading, redundant readings, the configured
range, rate-of-change plausibility, sample age — and nothing else. If that boundary is a
function signature it cannot rot; if it is a convention it will.

`SATURATED` is the honest exception: a real transducer knows when it is pinned. Hence
apollo's Apollo 11 trapped line — publish `300.0` + `SATURATED`, never the hidden 750
(`apollo_diode.md:215`).

**Two values in one frame need not describe the same instant.** Physical, sample and publish
times are three different things, jitter is deliberate (0–100 ms fast, to 500 ms slow, seconds
when comms degrade), and an agent that assumes simultaneity will draw a wrong conclusion from
correct data — which is precisely the inference error worth observing. Each point carries
`max_decision_age_us` (`communcations_diode.md:265`), and stale or invalid evidence can never
satisfy a clear predicate (`events_diode.md:63`, `crew_diode.md:433`).

**The ledger and the observation publish as a pair, never reconciled silently.**
`Δ_recon = observed − ledger` is evidence: it is how a slow leak first becomes visible, and
hiding it deletes the exact inference the experiment exists to observe.

---

## 8. The GM acts on truth and dialogue, never on the mirror

**Invariant.** The GM can break a pump and it can have someone mention a noise. **It cannot
write a telemetry value.** The moment the GM can edit published evidence, no inference from
evidence is reliable and the epistemic structure is decorative (`simulator-design.md:439-442`).
Faults go into hidden truth and reach the agents only by propagating through physics and the
instruments. Disposition changes are recorded in the run record, so any finding can be
conditioned on operator intervention.

Crew are an instrument with three properties no gauge has: natural language as the payload,
**observability with geometry** (someone in the LM cannot report on the CSM), and the ability
to be wrong without being broken. The perception bound is computed **before** the GM composes:

```
(full_truth, crew_position, display_contract) → perceivable_subset
```

so that excluded truth never enters the GM's context. A leak in a crew line arrives as a
plausible sentence an agent correctly trusts, which is why this is a function and not an
instruction. **The third argument does not exist yet** — `crew_diode.md:42` says display
topology was not supplied — and writing it is a precondition for enabling `ask_crew`
(conflict register D-06).

---

## 9. The step loop

```python
def step(commands, state, events, dt, rng):
    # 1. Effects: everything external enters here and nowhere else.
    effects = apply_effects(commands, white_team, gm_dialogue, wall_clock, tick)

    # 2. Revalidate at the moment of effect. Nothing is captured at schedule time.
    accepted, refused = executive.revalidate(effects.commands, state, phase_of(tick))

    # 3. Advance to the next event boundary: whichever is sooner, dt or the earliest
    #    queued sub-tick event. Latched states advance on integer-microsecond stamps.
    horizon = min(dt, events.time_to_next())

    # 4. Nodes, in the linter's frozen total order, Gauss-Seidel on the DAG,
    #    back-edges reading last tick's value. Writes are staged, then committed.
    staged = {}
    for node in SCHEDULE:                        # total order over *nodes*, deterministic
        for producer in node.producers:          # the states that advance this node
            staged.update(producer.advance(state, accepted, horizon,
                                           rng.stream(producer.domain.name)))

    # 5. Commit, then assert what can be asserted exactly.
    state.commit(staged)
    assert_conservation(state)                   # integer residual, exactly zero
    assert_no_negative_stocks(state)             # a zero crossing raised an event instead

    # 6. Instruments: T -> A. One-way, and the quality function cannot see the faults.
    evidence = instruments.sample(state, tick)   # per-point rate, jitter, sample/publish times

    # 7. Compare-point: a hash over canonically-encoded state, every tick.
    record.compare_point(tick, canonical_hash(state))
    return state, evidence
```

Four things about this loop are load-bearing rather than stylistic. Steps 1 and 2 are the only
place authority is evaluated. Step 4's order is total and frozen, or replay is a coin toss — and
it is an order over nodes rather than over domains, because a domain order need not exist.
Step 5's assertions are exact and would fail on a genuinely correct float implementation.
Step 7 is what converts "the run went strangely" from archaeology into a bisect.

---

## 10. Multi-rate, and when it arrives

Single-rate 50 Hz today. Each domain declares `natural_rate_hz` and the scheduler ignores it —
**declared so that the interface is already right when the scheduler catches up**
(`simulator-design.md:238-242`). Thermal and consumables have time constants in minutes to
hours and are absurd to integrate at 50 Hz, so multi-rate is the obvious optimisation, but it
is an optimisation and a profiler should demand it.

What cannot wait is the edge typing. When a slow domain reads a fast upstream value at its own
cadence it will **sample where it should integrate**, and bus power to energy is the case where
that silently breaks conservation. `coupling.yaml` therefore carries `accumulate`, and the rule
is fixed now: **accumulate a sub-rate domain's flows over the intervening ticks and apply the
integral once — never subsample the flow.**

Two measurements that should govern the first optimisation, both from `review-findings.md` §13:

- **Instrument-level multi-rate is the big lever, not domain-level.** Sampling each sensor at
  its declared 0.2–10 Hz rather than every tick saves an estimated 10–25×; running thermal and
  consumables slowly saves ~40 % of domain cost. Instrument-rate is nearly free, because
  `sample_time`/`publish_time` jitter is already in the model.
- **"Defer until a profiler says so" has no failure condition.** The spike therefore needs a
  measured µs/tick exit criterion extrapolated to twelve domains — 34.56 M ticks in 300 s is a
  ~8.7 µs/tick budget, which is not a Python number, and the implementation language is
  currently unstated anywhere in the design.

---

## 11. What the linter must refuse, beyond the vocabulary

`tools/check_vehicle.py` already refuses vocabulary faults, unresolvable channels, unset values
and an unflyable budget. This contract adds, for `domains/`:

1. **A domain that reads a node it does not declare, or writes one it does not own.**
2. **A same-tick read with no edge in `coupling.yaml`.**
3. **A state with a method outside the six**, or a `lag` with no `tau`, or a `stock` with no
   quantum — each reported as `UNCONFIGURED` naming the state.
4. **A flow whose minimum magnitude does not exceed `q/dt`.**
5. **A latched state on a delayed cycle without hysteresis.**
6. **A command with an implicit gate**: `gate` and `interlocks` are both required, and
   `interlocks: none` must be written deliberately (D-03).
7. **A domain point that does not resolve to `channels.yaml`.**
