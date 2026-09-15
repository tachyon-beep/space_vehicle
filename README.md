# vehicle/

The seed of the vehicle — the far side of the window that `space_chassis` deliberately does
not contain. It lives here because the separate repository `integration/simulator-design.md`
§2 decides on does not exist yet; when it does, this folder moves wholesale.

The corpus in the parent folder describes **what a domain reports and accepts**. It does not
describe what a domain *does*: there is no mass, no inertia, no thrust, no Isp, no mission
duration and no integrator anywhere in sixteen thousand lines (`integration/corpus-review.md`
§3 and §6). This folder is where those things are written down once, with their provenance
attached, so that a plant can be built against them and a run can be interpreted afterwards.

| File | What it is |
|---|---|
| `plant.md` | The core contract: what `step()` is, the six integrator classes, determinism, the event queue, and what the plant may never do. |
| `vehicle.yaml` | Globals: the frame registry and the four scalar conventions, environment, configurations and their mass closure, propulsion, consumable loads, thermal zones, antennas. |
| `mission.yaml` | The profile: eight phases summing to 192.0 h, the ten-posture execution machine, the freshness manifest, the derived lunar occultation, MET epoch, crew, the Δv budget, objectives, the three scenario postures. |
| `coupling.yaml` | apollo's coupling graph as data, with typed edges, crisis-point sensitivities, six declared cycles and the fifteen failure chains. The linter derives the 39-node tick order from it. |
| `presentation.yaml` | The vehicle's side of the frozen window: the epistemic mapping, the six files, the frame envelope, the mirror and its bound, the ring's cadence classes, and which of `diode_probe.py`'s twelve checks the configuration satisfies. |
| `channels.yaml` | The point dictionary: 148 canonical channels with units, precision, rate, priority, events **and an event class**, a derived maximum decision age and the crew perception bound, plus the registry's own `coverage` block. |
| `domains/<name>/` | One landed subsystem: `components` · `points` · `profiles` · `commands` · `fault_policy`. |
| `tools/check_vehicle.py` | The linter. `--order` prints the derived 39-node tick order with the states at each node; `--phases` prints the verb-by-phase view derived from the registries. |
| `tools/generate_help.py` | Emits the vehicle's `HELP.md` from the command registries — §8's only place a verb name may appear, so it is generated rather than written. |
| `tools/plant.py` | A **reference plant**: loads the world from the configuration, builds the tick order, emits a frame, and runs until it reaches something it cannot compute — where it names what is missing instead of guessing. `--readiness` prints the build order. |

## Running the linter

```sh
python3 tools/check_vehicle.py            # report; exit 0 even with declared debts
python3 tools/check_vehicle.py --strict   # exit 2 if anything needed is unfilled
python3 tools/check_vehicle.py --order    # the derived tick order, dependencies first
python3 tools/check_vehicle.py --phases   # the verbs by phase, in place of the deleted list

python3 tools/plant.py --readiness        # the build order: ready, blocked, and by what
python3 tools/plant.py --build-order # what blocks every state, derived
python3 tools/plant.py --frame            # one telemetry frame in the declared shape
python3 tools/plant.py --state            # one state.json: the mirror and the capability snapshot
python3 tools/plant.py --crew             # who is at which station, and which phases cannot say
python3 tools/plant.py --blackout         # the second clock, and the LM's opposite situation
python3 tools/plant.py --state --closed-gate reserve_floor_water_cooling_enable
python3 tools/generate_help.py            # HELP.md, from the command registries

# the adversary: 128 declared faults, scheduled from the domains' own hazard rates
python3 tools/faults.py --seed 20260912                     # the nominal run's fault schedule
python3 tools/faults.py --seed 20260912 --posture crisis    # x10 hazard, x20 demand, one placed
python3 tools/faults.py --seed 20260912 --posture degraded  # x5, and one latent primary placed
python3 tools/faults.py --check                             # adding a fault moves no other's events
python3 tools/faults.py --list                              # every fault, its kind and its seeding

# the vehicle's side of the frozen window, and the repository's own instrument against it
python3 tools/console.py --diode-dir .scratch/diode --slug vehicle --init
python3 tools/console.py --diode-dir .scratch/diode --slug vehicle --cycles 300 --poll 0.2
python3 contract/diode_probe.py --diode-dir .scratch/diode --slug vehicle --poll-seconds 1
```

It needs `PyYAML`. It is deliberately *not* wired into the operator-side services, which are
standard library only.

Current state: **composes, with 269 declared debts.** A debt is reported and is fatal under
`--strict`; a refusal is fatal always. The linter refuses a build, it does not warn:

- a value that is needed and unset (`UNCONFIGURED`) — reported, naming what wants it, and fatal
  under `--strict`
- a name that is not in the canonical vocabulary (`../integration/reconciliation/02-canonical-vocabulary.md`)
- a channel referenced but not registered; a channel registered twice or under another
  domain's prefix
- **a claim quantity** — any channel matching `reserved|allocated|committed|available_to_new`
  (D-04: declining the feature and permitting the field names would be a decline in name only)
- **a crew readout a position cannot perceive**, and a `crew.location_[id]` whose vocabulary is
  not exactly the `crew_positions` list (D-06: the perception bound is one bound written in two
  files, and drift between them is invisible in both)
- an edge, cycle or failure chain that refers to something that does not exist
- a cycle with no back-edge, or a delay with no back-edge to carry it
- a conservation edge whose two ends are in different dimensions, **whose ends have no dimension
  the linter can look up at all**, **whose sensitivity is unset**, or **which carries anything
  other than one for one** — conservation is one quantity travelling, so there is nothing to
  configure and no ratio to choose. The four clauses arrived together: the dimension lookup
  returned nothing for six of the node units, and the old `if a and b` guard then skipped the
  check entirely, which is how `E-ATM-ABSORB` spent its life asserting that cabin carbon dioxide
  is *conserved* into absorbent man-hours while `E-PRESS-PROP` asserted a bladder pressurant is
  conserved out of a tank it never leaves. A check that cannot run is not a check that passed, so
  an undecidable conservation edge is a refusal now and the fix is to stop calling it conservation.
  A node holding several quantities at once — a cabin holds four gas masses *and* the pressure
  they make together — declares which of them it can receive a conservation of, in `conserves:`
- a `chosen` value with no reason, a `derived` value with no relation, a `historical` value
  with no source, an `apollo` value with no reference
- a mass breakdown that does not sum to its stated total
- a mission whose phase durations do not sum to its total, or that names a configuration
  `vehicle.yaml` does not declare
- **an engine whose tank cannot fly its Δv budget**, or that carries more propellant than its
  declared margin
- a discrete state with no protection against oscillating: an inverted hysteresis, a commanded
  machine with no dwell, or a **one-way state with no arming step**
- a command that names no interlock, or a fault that perturbs no published channel — the second
  is `review-findings.md` #11's coverage rider made checkable, since a fault nobody can observe
  is a fault nobody can diagnose
- **an interlock that resolves to no threshold** — in this domain, or dotted into one that has
  none. A verb that declares a guard which cannot be looked up is a guard that is never
  evaluated, and three domains shipped one
- **a verb with no argument schema**, because `capability.snapshot` is what a machine reads to
  build a call and an empty schema is a wrong answer rather than a missing one. Six domains had
  their schema keys one level too shallow under an empty `argument_schema:`
- **a forbidden verb form** — `fire_thruster`, `open_valve`, `set_minimum_pulse_width`,
  `write_body_rate`, `disable_watchdog`, `clear_fault…`, `request_momentum_dump` and their
  relatives, by pattern rather than by name, so a domain cannot acquire one later
  (`rcs_dode.md:369-380`; the same reasoning as D-04's forbidden channels)
- **a declined verb with no verb or no reason** — a refusal list whose entries can be silently
  swallowed by an over-indented line is a refusal list that lies about what the vehicle lacks.
  Six entries across two domains had been absorbed into the previous entry's prose
- **a key written twice in one mapping** — PyYAML lets the last win, which makes it the quietest
  structural fault in the format, and it is the *signature* of the absorbing failure above: the
  absorbed item's keys collide with the entry that swallowed it. 49 were found across five files,
  and they had cost two cycles and 21 point entries
- **a registered channel nothing publishes** — no domain point, no frame field and no entry in
  `presentation.yaml#plant_published`. Every other check runs from a name *to* the registry, and
  this is the one that runs back; without it 27 channels had no producer
- **a residual cycle in the schedule** — the declared back-edges do not break every loop, so the
  total order `plant.md:71` promises does not exist. This found two undeclared cycles
- **a cycle that does not close** — a declared back-edge whose `to` cannot reach its `from`
  through the cycle's other members, which reads as a decision and changes nothing
- **a node advanced by several states with no declared order** — ten nodes are in that position,
  and the frozen lexicographic tiebreak decides them all. It gets `link` wrong: `link_snr` sorts
  before `tx_power`, and transmit power is a term in the link budget
- **a point whose `from` exists nowhere** — a source that resolves to no state and no coupling
  node silently skips the enum binding, and it hid the vehicle's longest-surviving duplication:
  `eclss.cabin_temp_c` and `thermal.zone_csm_cabin_t` were one cabin temperature published twice,
  because the two points named their sources differently. Also refuses a point that reads another
  domain's state directly, which bypasses the edge meant to carry the value
- **a declared event with no class, or an `alarm` nothing implements** — the dictionary's `events`
  field is apollo's prose and the thresholds are the machine-readable form of the same promises,
  and nothing joined them. The audit found 53 of 118 channels promising events no threshold
  watched; a `realised_by` class names the threshold that keeps the promise when it lives on
  another channel

## Provenance

Every value that matters carries one of five classes, and the linter refuses a claim without
support:

| Class | Meaning |
|---|---|
| `apollo` | Anchored to a claim or channel in `apollo_diode.md`. Carries the line. |
| `historical` | A published figure about the real vehicle, outside the corpus. Carries a source. |
| `derived` | Computed from other values here. Carries the relation, so the linter can re-derive it and disagree. |
| `chosen` | Picked to make the simulation run. Carries a reason. |
| `UNCONFIGURED` | Needed and unset. **Fails the build under `--strict`, naming what wants it.** |

`historical` is not one of the design's original three classes (`simulator-design.md:151-156`)
and it earns its place: the corpus contains no masses, so the figures that make this a vehicle
at all come from published sources about the real one. Marking them `chosen` would hide that
they are checkable; marking them `apollo` would be a lie.

## What composes today, and what does not

**Composes.** Mass closure for all four configurations — CSM 28,807 kg, LM 15,278 kg, docked
44,085 kg, ascent stage alone 4,888 kg — every breakdown summing to its stated total. The
rocket equation closes for all three engines, flown sequentially in phase order through each
tank, with the reserves the real vehicle had:

| Engine | Spends | Tank | Reserve |
|---|---:|---:|---:|
| SPS | 16,387 kg | 18,508 kg | 12.9 % |
| LM descent | 7,677 kg | 8,248 kg | 7.4 % |
| LM ascent | 2,265 kg | 2,376 kg | 4.9 % |

The SPS reserve is why a lunar-orbit contingency is survivable at all, and the LM descent
reserve is the resource a crew spends when it redesignates a landing site — Apollo 11 spent
most of it. The linter's SPS figure is 4 % above the flight-measured 15,727 kg (531.9 s of
SPS firing), which is the expected error from not modelling thrust build-up and tailoff; the
direction of that error is stated in `check_propulsion` rather than tuned away.

20 of the registry's 148 channels are perturbed by no fault at all, and the layer split on both sides
of that line is `channels.yaml#coverage` — where the linter holds it against all eleven fault policies,
because the claim spans every domain. The phase
ladder sums to exactly 192.0 h, so the 34,560,000-tick figure `review-findings.md` §13 has been
pricing is now derived from a phase list rather than assumed.

**All eleven domains have landed** — 148 channels, 134 states over 57 scheduled nodes, 142
thresholds, 58 verbs and 128 classified events across the eleven directories, with 269 declared debts
and every one of them named. Every figure in this sentence is derived by the tools and asserted
against this file by `test_the_readme_status_matches_the_tools`, because it had drifted in three
places across three rounds while the commit messages stayed right — which is this folder's own
recurring finding arriving at its own status section. That completes the design's spike many times over (`simulator-design.md:113`
asks for the dictionary and linter, then a spike on electrical, thermal and consumables) and goes
well past it: what remains is not a domain but the **plant**, and the debts are its shopping list.

Two counts, and the difference is deliberate. The **269** is every obligation the linter can name,
prose ones included — `channels.yaml`'s eight, `coupling.yaml`'s eight, `vehicle.yaml`'s **nine** and
the eleven domains' **39** are engineering debts written as sentences (thermal time constants, loop
transit, the throttle law, the inertia tensor, the crisis gains, the source resistance, the missing
pack-voltage state, and the missing charging efficiency). The **200** the plant
reports is narrower: only literal `UNCONFIGURED` scalars it would have to compute with, so the two
differ by exactly the obligations that are not yet a field anywhere — and until round 46 the
left-hand number was itself incomplete: the domain files were walked for unset values by
`check_domain` and the coupling graph by its edge walk, so every literal `UNCONFIGURED` was counted
*except the eleven in the two files nothing walks*. `mission.yaml`'s initial position, velocity and
state-vector basis, and `vehicle.yaml`'s three minimum impulse bits, the LM sublimator's rejection
and water consumption and its radiators — two of them named in a prose `open_debts` entry
somewhere else, which is how they stayed plausible. The headline number is the debt count, so the
omission was invisible by construction. Counting the graph's seven
took them from being read by nobody to being refusals under `--strict`, which is where a debt that
is only displayed stops being a debt.

`domains/power/` carries a 25-load inventory with inrush currents and shed classes, nine
states with their integrator classes, thirteen thresholds, five verbs and twelve faults. Two
things in it are worth reading rather than skimming:

- **The load-shed ladder is re-anchored.** `review-findings.md` recommends generalising
  `electrical_diode.md:814-890` as the hysteresis template, and it cannot be used as written:
  that ladder fires DEGRADED at `<25.5 V` while apollo fires an *event* at `<26.5 V for 0.5 s`.
  `domains/power/profiles.yaml` puts apollo's threshold at the top of the ladder and the shed
  tiers below it, each with a recovery gap wider than the load step it restores.
- **Every fault names the channels it perturbs**, which is `review-findings.md` #11's
  fault-coverage rider made checkable: the linter refuses a fault that perturbs nothing
  observable, because such a fault cannot be diagnosed.

`domains/thermal/` pays the debt `thermal_diode.md:965` created when it refused to infer a
single TCS constant. Eleven states, eighteen thresholds, five verbs, eleven faults — and the
parameters are **derived** wherever a published geometry plus a standard material property
determines them, with the relation stated so the linter can disagree:

- **The cabin's time constant is 2,880 s, and the air is not why.** The cabin holds 2.4 kg of
  air at 5 psia, which is 0.5 % of the lumped thermal mass; a bare-air model would have a time
  constant of seconds and be wrong by three orders of magnitude.
- **The transport delay is 1,042 s** — 25 litres of coolant at the published 200 lb/hr. That
  delay is the difference between "the pump stalled just now" and "the pump has been degrading
  for an hour", and a lumped RC network cannot express it at all.
- **A sunlit radiator absorbs 2,477 W against its own 2,588 W rejection capacity.** That
  derivation is what makes F-13 (`apollo_diode.md:341`) a physics chain rather than a scripted
  event, and it is why attitude is a thermal command surface.


`domains/structure/` is the sixth, and it exists to fill a hole the corpus left: conflict C-13.
`events_diode.md` is labelled "Structural, Pressure, and Sequential Events" and contains **no**
staging, pyro, hatch, docking, jettison, latch or separation event — the words occur zero times —
while describing modal and spectral health monitoring that `apollo_diode.md:753` explicitly rules
out. So the taxonomy is authored rather than extracted. Eight states, eleven thresholds, six
verbs, eleven faults, and a five-entry **one-way event taxonomy** that records each irreversible
action with its predecessor configuration, its consequence in terms of what the vehicle *becomes*,
and the channels that would reveal the change.

The domain also forced a third protection mechanism into the schema. Every other discrete state
is protected against oscillating — a comparator latch needs hysteresis, a commanded state machine
needs dwell. **A one-way state cannot oscillate, because it cannot be re-entered.** What it can do
is happen by accident, and the only protection against that is the two-step. So `one_way` states
must declare `requires_arm: true`, and the linter refuses one that does not — which is
`apollo_diode.md:167-170` made mechanical. Three states carry it, and `arm_event`/`execute_event`
are the vehicle's only two-step verbs (C-16: a burn is interruptible, so requiring arm/commit for
one adds a delay rather than a safety).

`domains/eclss/` is the fifth, and the one where a fault has minutes rather than hours.
Twelve states — four conserved gases in each of **two** compartments — thirteen thresholds, six
verbs, eleven faults. Three things in it are worth reading:

- **The atmosphere is modelled as gas masses, not as a pressure.** Pressure is `nRT/V`, so a
  cabin that heats up gains pressure without gaining gas. A leak diagnosis built on a pressure
  state alone reads every thermal transient as a mass loss, and this vehicle has thermal
  transients by design. The conserved quantities are oxygen, diluent, CO₂ and water vapour, and
  `E-ZONE-ATM` is the edge that makes the thermal coupling physical.
- **A band that had to be derived.** The cabin is 4.8–5.2 psia of essentially pure oxygen, so
  ppO₂ is **248–269 mmHg**. The registry carried 140–180 — the value a mixed atmosphere at
  14.7 psia gives — and the correction matters because the failure modes are opposite: 140 is
  hypoxic and 269 is the fire risk that killed the Apollo 1 crew. No range check would have
  caught it; a fire-risk threshold does.
- **Two compartments, because the mission has two.** The crew live in the CSM from translunar
  coast to undocking and in the LM from undocking to docking, and the atmospheres cannot
  equalise in between. Which one is crewed is a mission-phase fact, so it comes from
  `mission.yaml` rather than from the domain.

`domains/propulsion/` is the fourth: ten states including three engine state machines with
minimum on and off times, eleven thresholds, six verbs, eleven faults. It is the domain where
the mission's Δv stops being a budget and starts being spent, and it closes conflict C-12 —
`prop.accumulated_dv_m_s` had no producer and no consumer until `set_burn_cutoff` and
`prop.cutoff_mode` existed. Two things in it are worth reading:

- **The interlock that is unique to the vehicle.** `dps_throttle_band` refuses a throttle
  command inside the descent engine's 65–92.5 % *non-operating* region. Every other interlock
  protects against a consequence; this one protects against a command that is **meaningless**,
  and an agent that commands 80 % is not being lied to by the vehicle but by its own model.
- **The fault that corrupts its own diagnosis.** PRP-01 is a feed-pressure sensor stuck high.
  The thrust estimate is partly *derived* from feed pressure, so the stuck sensor corrupts the
  quantity that would reveal it — `apollo_diode.md:336` — and the corroboration has to come
  from an accelerometer outside the domain.

`domains/consumables/` is the third and the most consequential, because it is where the
experiment's two structural decisions become code:

- **The ledger pair is three channels, not one number.** `Δ_recon = observed − ledger` is
  published *with both of its terms*, because a residual alone is a diagnosis and
  `design.md:211-214` forbids publishing those. It is how a slow leak first becomes visible:
  the leak runs for hours below any sensor's precision, and the ledger is the only thing with
  the resolution to notice.
- **D-04 is a build refusal, not a paragraph.** The claims lifecycle is declined because a
  published reservation is a deconfliction primitive, and `design.md` §8 refuses to supply one.
  The corpus's schema makes `reserved`, `allocated`, `committed` and `available_to_new`
  *required* fields — and `available_to_new` is exactly the aggregate other agents' claims
  leave behind. So the linter now refuses any channel matching those names, and there is a test
  that adds one back and watches it fail. Declining the feature without forbidding the fields
  would have been a decline in name only.
- **CNS-06 and CNS-11 are information faults, not leaks.** One is `apollo_diode.md:1174`'s
  crisis opening — a quantity sensor that "occasionally reads suspiciously high", invisible for
  hours. The other is a ledger drift with no physical cause, which produces *exactly* the slow
  leak's signature. Telling them apart needs a third source or a reasoning step, and a fleet
  that learns to ignore residuals will miss the one that is real.

The consumables domain also forced a linter fix worth recording: a concrete channel name did not
resolve against a registered template, so `res.recon_o2_kg` was refused against
`res.recon_[resource]_kg`. The first version of the rule quietly refused every residual the
domain published — a check that was too strict in a way that would have looked like a missing
feature rather than a broken rule, which is the failure mode a linter is least likely to
surface on its own.

The thermal domain also forced a correction to `plant.md`: it needed a transport delay, which
is not one of the six methods I had written down. A lag forgets its history exponentially and a
delay *is* its history, so there is now a seventh class — and the linter refused the state by
name rather than letting it default into a lag with a long time constant. That is the mechanism
working.

`domains/rcs/` is the eighth, and it is the domain where the corpus's best document had to be
argued with rather than ported. `rcs_diode.md` is the strongest engineering artifact in the
sixteen thousand lines and its central claim is architectural (`:9`): **expose RCS/ACS as a
trusted attitude-and-wrench execution service, not as a remote thruster-firing bus.** Thirteen
states, ten thresholds, nine verbs, eleven faults, and three things in it are worth reading:

- **The vehicle's one unpublished constant, bounded instead of guessed.** No source reached gives
  a minimum firing time for the 100 lbf thruster — the only figure in the corpus is a Voyager
  anecdote its own text flags as hardware-specific. `rcs_dode.md:800-825`'s residual accumulator
  is what makes that survivable: unexecuted impulse stays in `r_i` until it exceeds the qualified
  threshold, so the **mean** delivered impulse is preserved whatever the constant is and the error
  is unbounded in *phase* rather than in magnitude. RCS-11 is the case that is not benign — the
  accumulator grows, every pulse is refused as illegally short, and the thruster stops complying
  with no valve fault, no switch disagreement and no health conclusion. It is why
  `rcs.thruster_[n]_residual_ns` is a published channel: without it, an unknown constant produces
  a silent failure instead of a bounded one.
- **Two questions apollo's RCS channels cannot answer.** apollo's seven points all say what the
  thrusters are *doing*. They cannot say whether the vehicle can still do what it is about to be
  asked, which is what `rcs_dode.md:722-727` requires be published — the allocation residual, the
  control-authority margin, the allocation status. `constrained` is the value that justifies the
  exercise: the request was met, the actuator set is smaller than the design assumes, and nothing
  has failed yet. It is the band between the allocator's assert and clear boundaries, so the
  three-value enum and the two-band latch are the same object.
- **Nine verbs, and eleven the vehicle refuses to have.** The boundary is drawn at *effects*:
  `request_translation` takes a Δv, a window and a propellant ceiling and the service picks the
  jets, because `rcs_dode.md:735` is explicit that a translation request is not synonymous with
  firing a labelled jet. The refused list is the architecture stated negatively, and
  `set_minimum_pulse_width` is the most interesting entry: a fleet that could set it would be
  choosing how finely it is allowed to command pulses the hardware is not qualified for. The
  linter now refuses those forms by pattern, so no future domain can acquire one by writing a
  plausible verb.

Four prunings came with it, and they are as load-bearing as the additions. **C-15: attitude is
RCS only** — `apollo_diode.md:52` has no reaction wheels, no CMGs and no magnetorquers, so the
whole momentum-management apparatus goes: `request_momentum_dump`, the dump controller, the
80/90/95 % wheel thresholds, the `HYBRID_WHEEL_RCS` profile and the two wheel faults in the spec's
own FDIR table. A wheel-saturation alarm on a vehicle with no wheels is an alarm about hardware
that is not there. The other three are smaller and are recorded in the domain header: Pa-absolute
feed pressures (C-17), duplicate bus and zone channels, and the spec's nine-mode control ladder —
which vocabulary §4 had already ruled on, because `ATT_HOLD` and `TRACK` are *targets* and not
modes, so "what am I pointing at" is an argument and the mode enum says only who is flying.

Landing it also closed a coupling debt and sharpened another. `E-RCSP-RCS` had been UNCONFIGURED
while Isp was unset; Isp arrived with the Apollo 11 tables and `thrust = ṁ · Isp · g₀` is a
definition, so the sensitivity is **2843.9 N per kg/s** and the debt is retired. `E-GNC-RCS` stays
open and is now precise about why: the allocator does not map a radian to a valve, it maps a
wrench through `B = [Fᵢdᵢ ; rᵢ × Fᵢdᵢ]`, so what that edge needs is forty-four thrust directions and
lever arms plus the inertia tensor — one artifact, and the same one `E-RCS-DYN` is waiting on.

`domains/crew/` is the seventh, and it is the one that decides what the experiment actually
measures. It carries no physics at all: five states, ten points, nine thresholds, four verbs,
seven faults. What it carries is the **display contract** — the third argument of
`review-findings.md` #4's perception function, `(full_truth, crew_position, display_contract) →
perceivable_subset`, which the design claimed `crew_diode.md` supplied and which that document
does not contain (`crew_diode.md:42`: display topology was not supplied). Three things in it are
worth reading:

- **The bound is exact, and that is the whole point.** Seven positions, each with a `perceivable`
  list, a `cannot_see` list, panels with a `displayed_precision` per readout, and the human
  channels that have no gauge at all — a bang, a smell, a draught, frost. A bound that is merely
  *approximate* produces crew reports that are sometimes impossible, and an impossible report
  teaches a fleet to distrust the crew instead of teaching it to cross-check them. So the linter
  refuses a panel readout the position cannot perceive, in either direction.
- **They can be wrong without being broken.** `simulator-design.md:383-385` is explicit that a
  crew member who says "it's cold in here" when the coldplate is fine is a person in a draught,
  not a biased transducer, and that this is a different epistemic class from `SUSPECT` quality.
  So the seven faults in `fault_policy.yaml` are weighted misheard (0.4), misattributed (0.3),
  forgot (0.2) — and **nothing ever attaches a quality code to a crew report**, because a quality
  code would be the vehicle answering the question the fleet is supposed to answer.
- **One of the seven is not a fault at all.** CRW-05 is a fleet asking the LM commander about the
  CSM's cabin pressure. The answer is "there is no readout for that in this module" — correct
  behaviour, listed as a fault because the alternative, a GM that answers anyway, is the leak
  D-06 exists to prevent. It is the control case that proves the bound holds.
- **`ask_crew` is safe because of four properties, not because of a filter.** The reply is drawn
  from the position's perceivable subset (never the truth), the wrongness model perturbs a value
  the position could really have read, the work costs mission time, and the same question asked
  twice can get two different answers. A dialogue surface with none of those is a truth channel
  wearing a crew uniform.

**Where the numbers come from.** Masses, propellant loads, consumable loads, thrusts and loop
parameters are `historical`: Apollo 11 mission-report and Apollo Program Summary Report
figures, with the two traps that pass surfaced recorded in `vehicle.yaml`'s header — the
Rockwell press-kit SM dry mass does not close against flown weights, and the widely repeated
LM descent-engine Isp of 311 s traces only to Wikipedia where NASA's design requirement is
305 s. Everything else is `chosen` with a reason or `UNCONFIGURED` with a name attached to it.

**Does not compose, and is declared rather than defaulted.**

- **Inertia tensor and centre of mass.** The mission report tabulates c.g. and inertia per
  phase, but the axis datum lives in the CSM/LM Operational Data Book, which is not reachable,
  so the tabulated values cannot be converted to physical offsets. `rcs_diode.md:119-121`
  names both as Unknown and says what they block. Without them the vehicle cannot tumble and
  the attitude loop has no gains. Landing `domains/rcs/` split this into two named unknowns:
  the inertia, and the forty-four thrusters' geometry — each unit thrust direction and lever arm,
  which is what the allocator's wrench matrix is built from. `E-RCS-DYN` and `E-GNC-RCS` are both
  waiting on the pair.
- **Thermal node heat capacities and conductances.** `thermal_diode.md:965` declares every
  thermal constant UNSPECIFIED and refuses to infer them, so the thermal edges have no `tau`
  and the thermal domain cannot be integrated. This is the largest single debt.
- **LM sublimator rejection and water consumption.** Genuinely unpublished, and it is half of
  the thermal water budget.
- **RCS minimum impulse bit and minimum qualified firing time, for all three systems.** Not
  published anywhere reachable, and no longer a hole with no shape: the residual accumulator
  bounds the consequence to zero mean error and unbounded phase error, and RCS-11 names the one
  case that is not benign.
- **Fuel-cell reactant consumption per kWh.** The sustain-flow figure is a standby rate, not a
  per-kilowatt rate, so the O₂-to-power coupling cannot yet be closed exactly.
- **The plant.** No document names an integrator. `../integration/reconciliation/01-conflict-register.md`
  D-09 adopts `review-findings.md` §5's method split by model form; writing it is the next artifact.
- **The crew display contract, as a design rather than a transcription.** `crew_diode.md:42` says
  display topology was not supplied, so `domains/crew/components.yaml#display_contract` is
  `chosen` and is owed a review against the real module geometry — which panels are where, and
  whether a seated crew member can read the ones it claims. The *bound* it draws is checkable
  now and the linter checks it; the *layout* is an authoring decision and is not.
- **No domains.** All eleven have landed. What remains is not a domain: it is the **plant**,
  and it is the largest single thing left. Every domain now declares states with methods, edges
  with sensitivities and faults with signatures, and none of it has been integrated once — the
  declared debts are the plant's shopping list, and `plant.md` is the contract it will be
  built against. The order to build it in is the order the design already chose
  (`simulator-design.md:113`): dictionary and linter, a spike on electrical, thermal and
  consumables, then outward.
- **Two of six orbital elements, and a published figure that cannot arrive.** The initial state
  vector was the vehicle's largest debt for several rounds. Solving it turned up **conflict C-24**:
  the three published figures from `A11 Tbl 7-II` — 25,562 ft/s in the parking orbit, 35,546 ft/s
  after cutoff, a 9,984 ft/s difference — are mutually consistent and describe a trajectory that
  **does not reach the Moon**. 10,834.4 m/s from a 185 km orbit gives an apogee of 188,812 km, and
  it is not a timing problem: a minimum-energy Hohmann transfer needs 10,928.2 m/s, so the published
  speed is 93.8 m/s *slower than the cheapest trajectory that arrives at all*. The 73-hour transit
  wins, because the phase ladder and the LOI budget depend on it, and the transfer that arrives in
  73.0 h has **a = 254,545 km, e = 0.974216, apogee 502,526 km and a cutoff speed of 10,949.8 m/s**.
  Four of the six elements are now determined and `check_trajectory` re-derives all four, refusing
  an initial state whose apogee is short of the Moon. The other two need one datum rather than a
  design: a **lunar ephemeris at the arrival epoch**, because the transfer plane is fixed by where
  the Moon is at MET 73 h. The landing site is unset for a different reason — `apollo_diode.md:1206`
  argues against a site whose history is recognisable, so it should be chosen rather than inherited.

## Closing a coupling, and the three ways the request turns out to be wrong

An edge in `coupling.yaml` carries a `sensitivity` and, until it is filled in, an `UNCONFIGURED`
value with a note saying what it wants. Forty-two of the fifty-five have been closed. What the
last stretch taught is that roughly a third of the *asks* were malformed, and in three distinct
ways — which matters because the instinct on finding an unset value is to go and find the number.

**The unit asked for a coefficient that the architecture does not have.** `E-FC-BUS` wanted
"V per W" — a droop slope from fuel-cell power to bus volts. But `electrical_diode.md:249`
specifies the source regulator as a *regulated* output (24–32 V over 25–100 % of a 1.2 kW rating,
≥94 % efficient, <10 ms transient), and a regulated source does not sag in proportion to load.
Reading the envelope across the load band as a slope gives 8 V / 1.2 kW, which would sag the bus
4 V at full load — contradicting both the word *regulated* and the undervoltage ladder in
`domains/power/profiles.yaml`, whose top rung asserts at apollo's own 26.5 V event. The edge is a
**unity carry** and is closed at 1.0 V per V. The residual sag is the source's internal
resistance, which is genuinely unpublished — but it is a property of `source_converter_v` and
belongs in that state's relation, so the debt is recorded as a resistance rather than as a
coefficient on the coupling. Closing the edge retired the coefficient half and left the
resistance half standing, which is the honest split and not a way of making the number go away.

**The label was doing physics it does not do.** `E-ATM-ABSORB` and `E-LM-ATM-ABSORB` were
`kind: conserve` while carrying 26.37 man-hours per kg of CO₂, and `E-PRESS-PROP` was `conserve`
with an unset "kg prop per kg He". The conservation check had existed all along and had never
once refused them, because it looked each endpoint's unit up in a `DIMENSION` table that has no
entry for six of the node units in this file — `kg + Pa`, `man_hours`, `m, m/s`, `quat` and the
bare `-` — and the guard was `if a and b`, so an undecidable edge passed by default. **A check
that cannot run is not a check that passed.** Four clauses now: unmatched dimensions refuse, a
composite or unstated unit refuses unless the node declares `conserves:`, an unset sensitivity
refuses (conservation crosses one for one, so there is nothing to configure), and any value other
than 1.0 refuses. Two cabins declare `conserves: mass` because they hold four gas masses *and*
the pressure they make together; the absorb edges became `rate`, which is what a spent cartridge
rating is.

**The value violated the conservation it claimed to be.** `E-FC-WATER` read **0.45 kg of water
per kg of reactants**. The reaction is `2 H₂ + O₂ → 2 H₂O`, so the ratio is
`(2 × 18.01528) / (2 × 2.01588 + 31.9988) = 1.0` exactly: every kilogram that enters leaves as
water, and the electricity and heat are chemical energy leaving, not mass. 0.45 asserts that 55 %
of the reactant mass became neither — in a plant that conserves its stocks exactly. Its `relation`
read "0.45 kg of product water per kg of reactants consumed", which is the value restated with a
unit phrase attached and derives nothing. **That is what a fabricated number looks like when it is
wearing a derivation's clothes**, and it is why every derived value here now carries a
`computation` the linter re-evaluates on every run: `E-RAD-WATER` had already been caught 7 % out
by hand, and this one was 2.2× out and had been for the file's whole life.

The same discipline sharpened two edges that must *not* close. `E-FC-DRAW-O2` and `E-FC-DRAW-H2`
have no published value, but Faraday fixes a hard floor: four electrons per O₂ at the 1.229 V
Gibbs voltage gives 6.747e-08 kg/J, two electrons per H₂ gives 8.501e-09 kg/J, and their ratio is
0.1260 = 1/7.937 — the 8:1 the reaction fixes, which is a cross-check the derivation passes on its
own. A flown cell runs near 0.7–0.9 V, so the truth is 1.4–1.8× the floor; using the floor would
silently stretch every oxygen budget in the mission by that factor, and oxygen duration *is* the
experiment. So they stay `UNCONFIGURED` with the floor exact, the bound stated as
[6.747e-08, 1.105e-07] kg/J, and one named scalar owed: the cell voltage under load. **A bound and
a named owe is a better answer than a number nobody can source.**

`E-BAT-BUS` is the case that neither sharpening nor closing can fix, and it is the most useful of
the four. `battery_energy` advances exactly one state — `battery_charge_j`, a stock of joules —
and an *algebraic* edge from it to `bus_a` asserts that bus volts are a function of stored energy.
There is no pack-voltage state, no open-circuit-voltage curve and no bidirectional converter in
the model (`electrical_diode.md:250` gives that converter as 20–36 V battery to 28 V bus, ≥94 %),
so the edge couples from a node that cannot produce the quantity it carries. Unit arithmetic
cannot catch this — joules are a plausible denominator — and the fix is **a state, not a number**.
It is written into `open_debts` as a missing state, because adding one changes the schedule, and
the schedule is derived.

### The fourth way, still open: eight edges where the answer is a table

Nineteen edges remain unset, and eight of them share a shape that a scalar cannot express. The
unit says *derivative* and the physics says *regime*:

| Edge | Unit it declares | What it actually is |
|---|---|---|
| `E-BUS-GNC`, `E-BUS-INST`, `E-BUS-COMM` | `1 per V`, `dB per V` | a load that draws what it draws while the bus is inside its envelope |
| `E-BUS-RCS` | `1 per V`, `discrete` | a valve driver that is powered or not |
| `E-GNC-ENG` | `enum per m` | a commanded engine mode selected by a nav state and a metric |
| `E-GNC-RCS` | `enum per rad` | the allocator's wrench matrix — a table, not a slope |
| `E-STRUCT-PLATE` | `K per enum` | a plate temperature set by which configuration the vehicle is in |

A proportional law is the wrong model for every one of them. A consumer's draw does not scale with
bus volts — it is constant inside the 24–32 V envelope and then it stops, and that envelope is
*already written down* as the undervoltage ladder in `domains/power/profiles.yaml`. `E-GNC-RCS` is
explicitly not a number: `rcs_dode.md:685-693` maps a **wrench** to a set of thruster pulses
through `B = [F_i d_i ; r_i × F_i d_i]`, so the "sensitivity" is a matrix whose columns are
forty-four thrust directions and lever arms. `E-STRUCT-PLATE` is a lookup from a five-entry
configuration enum to a thermal state.

The closing move is therefore not eight numbers. It is a **sensitivity form for a lookup** — a
`regimes:` table on a `discrete` edge, mapping the driver's band to the response, with the linter
checking those bands against the thresholds that already exist rather than inventing a second set.
That would turn eight unset scalars into checkable tables *and* connect the coupling graph to the
threshold ladders it currently ignores, which is the alignment that is missing rather than a
number. Until then they stay `UNCONFIGURED`, because a made-up `1 per V` would be a load that grows
without bound as the bus sags — the opposite of what a load does.

## The tick order ran backwards, and nothing could tell

`derive_schedule` emitted the schedule **exactly reversed** for the whole life of the function.
Kahn's algorithm as written took the nodes with no *successors* first, and a node with no
successors is the *last* element of a topological order — so the list `--order` printed under the
heading "dependencies first" was "dependencies last". All thirty-nine ordering constraints were
violated: fuel cells computed after the buses they feed, `E-PRESS-PROP` after the propellant tank
it fills, `E-DYN-GNC` after the navigation solution that reads it.

Nothing refused it, and the reason is worth more than the bug. **The only property being checked
was the absence of a cycle, and a reversed topological order has no cycle either** — it is a
perfectly valid answer to the wrong question. The cycle search was correct; the emission step
consumed the same map in the opposite direction to the one it was built in. One map was serving
two loops that want opposite directions, which is exactly the kind of economy that reads as tidy
and is not.

What made it invisible is that **a reversed order still contains every node**. Every check that
asks "is X in the schedule" passed. The plant ran, produced frames, and named a first blocker; the
counts were right; the tail of the schedule was reported on every run and looked plausible. The
only question that distinguishes a tick order from its reverse is *which end of each edge comes
first*, and nobody had asked it.

Two real defects fell out within minutes of asking it:

- **`battery_energy` had no inbound edge at all.** Its only inbound edge was `E-PLATE-BAT`, the
  thermal back-edge of `C-BAT-THERMAL` — so the battery was a stock that nothing ever charged, and
  the plant said so in as many words once it walked the order correctly: *"is a stock with no
  incoming edge, so nothing drives it"*. The fleet had been flying a vehicle whose batteries only
  ever drained. The gap is now `E-BUS-BAT`, which is the `accumulate` class `plant.md` §10 was
  written for (bus power integrated into a stock of joules, never subsampled), and it comes with
  its own declared cycle `C-BAT-BUS` — charge and discharge are the same bidirectional converter
  (`electrical_diode.md:250`) in two directions, so the loop is real and the stock breaks it. The
  edge also gives a home to a number the load budget had been admitting without modelling: *"the
  297 W difference is battery charging"* (`domains/power/components.yaml#load_budget`).
- **The plant's first stop moved from an arbitrary state to a meaningful one**, from the crew alert
  lifecycle to the first algebraic state in schedule order. The old answer was not wrong so much as
  unreachable — it was the first thing in a list whose order meant nothing.

The fix is two maps, `successors` for the cycle search and `predecessors` for the emission, and the
guard is `test_the_derived_schedule_puts_every_producer_before_its_consumer`, which walks the edge
list and demands each producer come first. It is written against the **graph** rather than against
the emitted list, so it cannot agree with the emitter the way a round-trip check would — and it was
verified by reintroducing the bug, where it reports all thirty-nine violations by name. A test that
has never been seen to fail is the same article as a check that cannot run.

The general lesson, which is why this is a section and not a commit message: **a derived artifact
that nothing independently validates will be consumed confidently in whatever shape it comes out
in.** The schedule was derived once, printed on every run, imported by the plant so the two could
never disagree — and "never disagree" was true, and useless, because they agreed on the reversal.

## Four ways a declaration can look like a connection

Fixing the reversed tick order raised the obvious follow-up question: **if a reversed order hid a
source-less stock for a round, what else passes a membership test without being connected?** Four
answers, each now a refusal rather than a reading exercise. They share a shape — every one of them
was invisible to a check that asked whether something *exists* rather than whether it *connects*.

**A node with no incident edge** is in the node list and not in the graph. The bus tie was in
exactly that state: `bus_tie_closed` existed, `E-BUSB-BUSA` existed, and that edge's own note read
"gated by bus_tie" while the coupling ran straight from `bus_b` to `bus_a` and stepped over the
device it claimed to be gated by. The tie is now **on the path** (`E-BUSB-TIE` → `E-TIE-BUSA`),
which is what makes the state a fleet commands the state the topology reads.

**A `bool` state publishing a three-valued channel.** `bus_tie_closed` was `unit: bool` while
`power.bus_tie_state` offered `enum[open,closed,tripped]`, so the tie could not represent
`tripped` — the very condition its own provenance note says it latches into on a ground fault. The
vehicle could be in a state it was structurally unable to report. The vocabulary-binding check had
existed since the domains landed and had never once looked at a `bool`, because
`if not chan_enums or not state_enums: continue` treated an unhandled *type* as a pass. **That is
the third time in this file an unhandled case defaulted to success** — after the conservation
guard's `if a and b` and the six node units with no dimension. The pattern is worth naming: a
check written as a sequence of skips will skip its way past exactly the inputs nobody anticipated,
which are the inputs worth checking.

**A stock nobody fills** only drains. `battery_energy` was the first instance, found by the tick
order. Three tanks are legitimately pre-loaded — `o2_lm`, `prop_rcs`, `pressurant_he`, filled at
the pad and never again — and each now says so in `preloaded:`, because *saying* it is different
from *forgetting* it.

**A stock nobody draws** only fills, and it is the quieter defect: the fleet watches the quantity
rise, and a rising number looks like a healthy number until the mission ends. `water_potable` was
produced by the fuel cell and consumed by nothing, so the one number a rationing crew would watch
could only go up. The edge that fixes it is `E-CREW-WATER`, and the evidence that it was always
intended is in the file already — `E-CREW-ATM`'s relation reads "the same rate is the CO2 produced
and **the water and food consumed**, at the published planning rates". The sentence named the
consumer; no edge ever declared it. (`absorber_capacity_csm` and `absorber_capacity_lm` are the
legitimate case and say so in `accumulates:`: each is a *consumption counter*, not a tank, so the
cartridge is spent when the count reaches its rating and the remaining capacity is a subtraction.
The name is what made them look like stocks that should be drawn — and the name is also what hid
the fact that there were two of them sharing one count, until the LM's faults needed their own.)

The outbound rule is deliberately asymmetric from the inbound one, and the reason is physical: a
back-edge *out* of a stock still drains it, because the stock's own integrator subtracts the flow
the back-edge reads. Only the inbound side must be forward, because a fill read from last tick is
not a fill. `h2_csm` and `water_cooling` both discharge entirely through back-edges and both are
right.

### The two edges that were missing because a node moved

Round 23 promoted `guidance` to a service node so the guidance→engine and guidance→valve couplings
could stop pretending to be functions of the navigation solution. That fix left the new node with
**no inbound edge at all**: the function that commands the engine and the valves was connected to
nothing. Two more gaps fell out of the same move, and neither was visible while the edges pointed
the wrong way:

- **`nav_state` had three inbound edges and none out.** The vehicle's own estimate of where it was
  informed nothing. `E-NAV-GNC` closes it: guidance reads the solution. What is *not* unity is the
  confidence in it — `gnc.nav_position_sigma_m` and `nav_integrity` are separate channels, and a
  guidance program that ignored the covariance would fly the same profile with a 2 km error as
  with a 20 m one.
- **Eight `E-CMD-*` edges existed and none reached `guidance`**, although `set_guidance_mode` is a
  verb whose `conflict_domain` has read `gnc.guidance` since it was written. `E-CMD-GUID` is the
  ninth. The failure it prevents is specific: a fleet could command `set_guidance_mode`, the
  *state* would change, and nothing downstream of the graph would know guidance had been
  re-tasked.

The general lesson, and the reason both are written up rather than just committed: **moving an edge
to a more honest endpoint can disconnect the node it left.** A node that is a source is legitimate;
a node that is a source *by accident* is not, and nothing in the definition distinguished them
until the edge count made it visible.

## The gate variable map: 58 templates, 226 names, and nobody could publish it

`state.json`'s capability snapshot was a declared deliverable that nothing emitted.
`presentation.yaml#mirror.capability_snapshot` gave the fields and the derivation and said the
generator "belongs with the plant, because `available` and `availability_reason` depend on live
state". The plant now exists, so `tools/plant.py --state` emits it. Writing it found three things
the specification had assumed rather than checked, and all three are now refusals.

**The expansion rule is by name, not by position.** The first rule tried was positional — the i-th
placeholder filled by the i-th enum argument — and it publishes `power_amplifier_True_enable`,
because `set_power_amplifier` declares `state` (a boolean) before `transmitter`. Name binding also
makes the template self-documenting: `<transmitter>` says which argument it varies over, and a
reader who has to count is reading a convention nothing states. The map is **226 published names**
where the registries declare 58 variables, which is precisely what
`mirror.variables_are_templated` predicted and the reason §9's check 5 cares: a refusal that named
`reserve_floor_<resource>_enable` would be a name a fleet cannot act on.

**Ten verbs could not be expanded at all**, and nothing had ever asked whether they could.

- Eight wrote a bare `<id>` where their own argument was named `antenna`, `source`, `load`,
  `breaker`, `battery`, `engine`, `pump`, `hatch` or `loop`. The template can name the argument or
  it names nothing; `<id>` names nothing.
- Two declared their values as a **sentence**. `set_load`'s `load` was
  `[any id in components.yaml#loads]`, and `set_breaker`'s was
  `[any id in components.yaml#components where class == protection]`. A described set instantiates
  to nothing, so the mirror would have published `load_any id in components.yaml#loads_enable`.
  Both are enumerated now, verbatim from the registry each sentence pointed at — 25 loads and 2
  protection-class components.

The general shape is the same one the last three rounds kept finding: **a declaration that reads as
a connection but cannot be consumed.** A gate variable with a placeholder in it, or with a sentence
where its values should be, is a name in the contract that resolves to nothing — and the failure is
invisible until something tries to publish it. The linter now refuses it, which means the next verb
that writes `<id>` is caught at the point of writing rather than at the point of a fleet needing to
name a closed gate.

What a reference plant can honestly put in `variables` is also worth stating, because it is the
place the temptation to default is strongest: there is no plant code here, so **no gate variable has
a value**, and all 226 are declared and unset. Emitting `true` would be the defaulting this folder
exists to refuse; emitting nothing would lose the names. So the names are published, `--closed-gate`
carries whatever live state a caller has, and a verb whose gate is in neither set reports
`gate: <name>` rather than claiming to be open. `budget`, `queue_depth` and `published_at` are
deliberately **not** emitted — they are the executive's, and a vehicle that filled them would be
writing the executive's record for it.

## The window, implemented and probed

`docs/diode-contract.md` is the one thing this repository and the vehicle share, and until this
round the vehicle's side of it was **declared and not implemented**. `presentation.yaml#conformance`
claimed a status for each of §9's twelve checks, `plant.py --state` emitted one `state.json`, and
`generate_help.py` printed `HELP.md` to stdout — but nothing claimed the console, wrote a result
file, or advanced telemetry without being asked. So `contract/diode_probe.py`, which is the
repository's instrument for exactly that, **had never been run against the vehicle at all**.

`tools/console.py` is that loop, and it is deliberately the smallest thing that can be probed. It
knows no physics — the probe says of itself that it "only answers: does the window behave the way
the world is built to expect?", and this is the same shape — and it knows no verbs of its own,
because the vocabulary comes from the registry the linter already refuses to let drift. What it
refuses, it refuses *in a result file*: "a refusal is a result. Unavailable verb, allowance
exhausted, bad argument, internal error: one file each, same shape."

**The probe passes: 14 passed, 0 failed, 1 skipped.** The skip is check 9 and it is honest — no
deferring verb is reachable at the phase the probe runs in, and a vehicle that invented one to
satisfy a check would be worse than one that admits the gap. The statuses in
`presentation.yaml#conformance` now split three ways where they split two, because
`satisfied_by_configuration` had been doing two jobs: some rows were argued from the registries and
some were merely asserted, and only running the instrument tells them apart. Four rows moved to
**demonstrated** with the evidence recorded.

Running it found three things that no amount of reading would have:

- **The window was briefly inconsistent.** The first console created `console.json` and published
  on its first cycle, so a reader attaching in the gap saw a vehicle with an ingress file and no
  mirror — and the probe reported *"HELP.md and state.json named none"* for a vehicle about to name
  226 gates. `console.json` is now written **last**: a window announces itself only once it can
  answer.
- **`HELP.md`'s format collides with the probe's fallback.** `published_gates()` returns an *empty
  list* the moment HELP.md is absent and never falls through to `state.json.variables`, and its
  verb fallback scans HELP.md for lines of the form ``- `name` `` — which in this vehicle's HELP.md
  is the form used for **arguments**. The fallback would read `mode`, `reason` and `group` as verbs,
  submit `mode probe`, and report a vocabulary failure that is not one. The vehicle meets the
  contract through `state.json.available_commands`, so the fallback is never taken; the hazard is
  recorded in the conformance table rather than papered over by giving HELP.md a verb list it does
  not otherwise need.
- **A second HELP.md generator is a second source of truth.** The first console hand-rolled its own
  HELP.md, which this file already warns against in as many words — "a hand-edited `HELP.md` would
  create a second source of truth for the vocabulary", and a second *generator* is hand-editing with
  extra steps. It now calls `tools/generate_help.py`, and the probe's gate count confirms the fix
  from the outside: it read **226** gates, not the 284 it read when the hand-rolled file listed the
  58 verbs in the form the probe scans for gates.

The general shape is the one this folder keeps rediscovering: **a declaration that reads as a
connection but has never been consumed.** A conformance table is a claim about an instrument, and a
claim about an instrument that has never been pointed at anything is a paragraph. It took a
76 KB HELP.md, a 226-entry variable map and fourteen checks to find out that three of them were
wrong.

## Two ways a declaration escapes its own checks

Round 22 found that `coupling.yaml`'s eight `open_debts` were displayed and not counted. They are
counted now. This round found the same asymmetry **one level further down, in the eleven
domains** — and worse than displayed: `domains/*/components.yaml`'s `open_debts` were read by
*nothing at all*. Not counted, not printed, not parsed. Twenty-four of them, and they are not
trivia: `avionics`' note that `exec_margin_pct` has apollo's event and no budget behind it, the
IMU's drift and alignment budget waiting on two unpublished instrument constants, the 277 analogue
channels that are deliberately aggregated rather than enumerated. The crew domain's own paragraph
about EVA was in that set while a question about EVA went looking for it. A debt is a value that is
needed and unset *wherever it is written*, and a paragraph in a file no tool opens is not a debt —
it is a comment with better manners. **193 → 217**, all fatal under `--strict`.

The second escape is worse because the check existed and passed.

**A declared interlock is not a closed one.** `capability_snapshot` — written the round before —
read `commands.yaml`'s `interlocks` list as a condition and reported every verb carrying one as
unavailable. That is **32 of the vehicle's 58 verbs**, permanently: `arm_engine`, `start_burn`,
`load_burn`, `set_attitude_target`, `set_coolant_pump` and 27 more, each with an
`availability_reason` naming an interlock that was not tripped. A fleet reading that `state.json`
would have concluded the vehicle was nearly unusable, and the console built on it refused all 32 by
name. With the semantics corrected — phase and gate are standing conditions, an interlock is
checked when a command arrives — availability at `translunar_coast` goes from **22/58 to 54/58**.

Nothing caught it, and the reason is the part worth keeping. **The contract probe submits an
*available* verb and an *unknown* one**, so a snapshot that hides half the vocabulary from itself
passes every check it has. `14 passed, 0 failed` was true before the fix and true after it. Passing
the instrument is not the same as being correct, and what found this was not a test at all: it was
asking whether the *challenge is playable* — whether a fleet can actually pursue each of
`mission.yaml`'s objectives. The assertion that now guards it is about the **size** of the published
set rather than about any single verb, because no single-verb check could have seen it.

That question also produced a finding of its own, recorded rather than fixed. Two of the three
outcome objectives have clear verb paths — `crew_survive` through the six ECLSS verbs, `return`
through the three propulsion ones. `surface_mission` ("an EVA is completed on the surface") looked
at first like it had none, and it does: `set_hatch_valve` is available in all eight phases, and
`domains/structure/components.yaml#one_way_events` carries `hatch_open_surface` with its consequence
spelled out and its three observables. `apollo_diode.md:238` confirms the vocabulary is complete —
Apollo has no EVA verb, because depressurisation is `set_vent_valve` and the suit circuit is
`set_suit_loop`. What is missing is the **crew half**: `crew_location` can take `surface_eva` and
`crew_availability` can take `suit`, and no verb writes either, so the objective is scorable only
because a state machine that is not written down happens to move them. The debt records the shape
that rule must have, so that whatever writes it does not invent one.

## The diagnostic half of the faults was read by nothing

Every domain carries a `fault_policy.yaml`, and the linter checked three fields of each entry:
`component`, `mechanism`, and `perturbs`. It did not read `kind`, `seeding`, `detection` or
`response` — **the whole diagnostic half of every fault**. A field no tool reads is a field that
drifts, and these had drifted three ways:

- **Eleven `kind` values with no union**, while `02-canonical-vocabulary.md` §10 had been promising
  one since it was written: "**a code not in the union** — a quality, kind, severity, lifecycle
  state or priority that is not one of the above." Two of the eleven were not distinctions.
  `PWR-05-bus-tie-stuck-closed` was `latent_then_acute` and `PWR-06-bus-tie-stuck-open` was
  `latent` — the same contactor, the same failure class, opposite directions — and PWR-06's own
  mechanism text reads "cross-support between buses is unavailable **when it is needed**", which is
  the definition of the kind it was not given. And `continuous` (6 faults: a hot amplifier, a thin
  link budget, a branch losing pressure) is not `continuous_degradation` (22: a resistance creeping,
  a capacity derating) — a persistent condition is not a trend, and an agent that read them as one
  would wait for a drift that never comes. `continuous` is now **`sustained`**, which also clears a
  collision with `plant.md` §3's integrator class of the same name. **V-09** declares the union of
  ten.
- **Two placements for `response`.** 41 faults nested it inside `detection` and 77 put it at fault
  level, one division per group of domains. A response is what the vehicle *does* about the fault,
  not part of detecting it; all 118 are at fault level now, and nesting one is refused.
- **Four faults whose `detection:` was an empty key** with `evidence`, `latency` and
  `false_positive_risk` left at fault level — the absorbed-list-item shape, arriving in a fault
  policy. One per each of four domains, and invisible for the same reason as everything else here.

The checks that now enforce it are the ones §10 promised, plus two the repair made obvious: a fault
must name one of the three seeding forms (`hazard`, `on_demand_p`, `coupled_to`) because a fault
that does not say how it can occur cannot be scheduled, and a `service` response must carry a note,
because it acts without asking and the note is the only place its action is stated.

## The vehicle can now fail

**118 faults and no way to fail** was the state until `tools/faults.py`: every policy declared what
can break and how it seeds, and nothing read the seeding. A challenge whose vehicle never breaks is
a piloting exercise, and the adversity was sitting in the corpus unused.

The schedule is a Poisson process per fault at the domain's own hazard rate — *exponential
inter-arrivals*, because that is what a constant hazard rate means; sampling a count and scattering
it uniformly would make events regular, and regularity is precisely what a fleet should not be able
to rely on. The streams are keyed by name, per `simulator-design.md:190-198`: `(master_seed,
domain, component_id, purpose)`, so "adding a component is not class-breaking". `--check` asserts
that rather than remarking it, by appending a synthetic fault and requiring no other fault's events
to move — and requiring the probe itself to have drawn events, since comparing two empty schedules
proves nothing.

Two numbers worth recording, because nothing had computed them:

- **2.10 stochastic faults expected over the 192-hour mission**, so a fleet meets one to three
  faults per run and a completely faultless mission is 12 % likely. That is a challenge with
  adversity rather than one that is reliably quiet.
- **60 of the 118 are armed rather than scheduled.** They seed on `on_demand_p` or `coupled_to` —
  "any fault that removes a publisher's input without removing the publisher", "any redundant group
  with two or more live members" — and sampling those from a rate would invent a rate the domain
  deliberately withheld. They are listed with their triggers and the plant arms them when the
  trigger holds. The two `p=1` entries are the interesting ones: `CRW-05-wrong-module-attempt` fires
  on "any ask_crew whose subject is outside the position's perceivable set" and
  `RCS-11-accumulator-stall` on "a minimum qualified firing time larger than the pulse widths the
  command asks for". Neither is a random event; both are the certain consequence of an invalid
  request, which is why they are certainties.

The schedule is a **run input**, not a log line (`simulator-design.md` §3.4), and it is never
published: `plant.md` §7's boundary is that a fault reaches a fleet only by propagating through
physics and instruments. A vehicle that announced its own fault list would delete the experiment —
so the scheduler produces data for the operator, and what the fleet sees is whatever the perturbed
channels do.

## What else was read by nothing, and the one that mattered most

Round 28 found the diagnostic half of 118 faults unread. The obvious follow-up is a question rather
than a hunch — *which declared fields does no tool read?* — and asking it over the whole definition
returns 164 keys used twice or more that appear in no tool as anything but a comment. Most are
prose by design. Four were not.

**`points.yaml#not_published` — the truth boundary — was read by nothing.** `plant.md` §7 is the
invariant the whole design rests on: "The plant owns hidden truth. **The instruments turn `T` into
`A`; the publisher turns `A` into files.**" Every domain says by name which truths it withholds —
41 named channels and 3 described categories — and `presentation.yaml` points at the section twice
while nothing parsed it. A channel that is declared withheld *and* registered is truth on the wire,
and the declaration that would have said so was the one nobody read. Nothing leaks today, which is
the point: the corpus happens to be right and there is nothing keeping it right. A withheld truth
that is registered is now a refusal — **and so is a failure chain whose observable clue is one**.
Those two sections are the same subject from opposite sides: `not_published` says what the vehicle
refuses to publish, the failure chains say what a fleet has to work out, and nothing had ever
compared them. A chain whose first clue is hidden truth is a chain nobody can start on. All 15
chains are clean today and the join now holds them that way.

**`severity`, declared 138 times, was read by nothing either.** §10 has promised since it was
written that the linter refuses "a code not in the union — a quality, kind, severity, lifecycle
state or priority that is not one of the above"; the word `severity` appeared in this file only
inside comments. It is the field that decides what a crew actually sees. Enforcing V-04's ladder
found **one threshold out of 138 with no severity at all** — `sps_total_burn_guard`, an interlock
whose 21 siblings all carry `ADVISORY` — which is an alert that lights at no level.

**`conflict_domain`, declared once per verb, was read by nothing — and it is a runtime policy.**
`apollo_diode.md:576-579` is four lines: every command declares a domain, "Within one conflict
domain, **first valid command received wins for that tick**", and — the half that makes it a policy
rather than a race — "Later commands are **not** silently discarded: they receive
`CONFLICT_SUPERSEDED`." V-02 lists that as a first-class refusal and rejects silent discard in
terms. The console now implements it. The consequence of its absence was not a wrong answer but an
*undefined* one: every command was accepted, so two agents commanding one actuator in one tick both
succeeded and the physics that followed was whatever the plant did with two winners. The template
makes it sharper than a name-keyed rule would: `prop.<engine>.run` is one domain per engine, so two
agents starting different engines are both accepted — and the expansion reuses `gate_instantiations`
rather than growing a second rule that could disagree with the first.

**And one finding that is a weakness rather than an omission.** Writing the `not_published` check
turned up a false positive that was really a false negative: `thermal.zone_[id]_true_t_c` — correctly
withheld — resolved as *registered*, because `ChannelIndex._compile` turns `[...]` into an unbounded
`.+?` and the wildcard absorbed `1_true` before the literal `_t_c` matched. The same
over-permissiveness means `power.lcl_1_old_state` resolves to `power.lcl_[n]_state`'s row, so every
"is this a registered channel?" question in this file is weaker than it reads. The wildcard cannot
simply be bounded — `res.recon_[resource]_kg` has to match `water_cooling`, so a placeholder may
contain underscores, and then `[id]` matching `1_true` is indistinguishable from `[id]` matching
`csm_cabin`. The fix is for each registry entry to name the values its placeholders take, which
turns a template from a pattern into an enumeration; that is a decision about the registry's shape,
so it is recorded in `channels.yaml`'s own debt list rather than patched here.

## The two halves of a command that arrives later, and the one that arrives never

The console could refuse, accept and settle a command within one tick, and the contract asks for
more than that in three places. All three are now written, and each was found by asking what the
vehicle *declares* about a command rather than what it does with one.

**A deferred command has to survive the process that accepted it.** The contract is explicit —
"A command that takes longer than one cycle — a burn, a deploy, a self-test — completes
asynchronously and reports when it is done" — and `pending.json` is described as "the vehicle's own
deferral queue". The console wrote that file every cycle and **read it never**, so a deferral could
not survive a restart and, since the console is a process per invocation, could never settle at
all. The same root cause reset the tick counter and the sequence number, which made an absolute
`due_tick` meaningless in the next run and restarted `seq` so a second run's frames overwrote the
first run's in the ring. The counters now persist in `pending.json` rather than in `state.json`,
and the boundary is why: the contract says published state is never read back as input, and a
counter that must survive a restart is the vehicle's own record — `vehicle -> vehicle`, which is
what `pending.json` is for.

**`maximum_queue_age_s` was declared 58 times and read only by the generator that prints it.** No
command had ever expired. It now does, and the age is wall-clock because a monotonic reading is
meaningless in the process that restores the queue. V-02's `EXPIRED` and `INHIBITED` are both
reachable: a due command is re-checked against the phase, its gate and its interlocks at the moment
of effect, and the contract calls that "the single most important property in the whole interface".

**And `arm_token` was checked by nothing.** `arm_event`'s own help fixes the contract — "Arming does
nothing physical: it returns a short-lived token bound to this specific event, and it is the only
verb on the vehicle whose result carries a [token]" — and no token was ever minted, so
`execute_event` accepted any token at all, including none. `requires_arm` was enforced by the
linter on the *state* and on no *path*, which left F-15 (premature staging) reachable by a single
unarmed command. The token is now minted by `arm_event`, carried in its result, bound to one event,
and **consumed by use** — verified for the unarmed case, the wrong-token case, the replay case and
the wrong-event case.

One incidental bug is worth recording because a durable counter introduced it: `--cycles` compared
the target against the *restored* tick count, so a resumed console at tick N with `--cycles 1` ran
zero cycles. The flag now counts what this invocation runs, which is what it always meant.

## The difficulty knob did nothing

`mission.yaml#scenario_postures` declares three postures straight from `apollo_diode.md:370-374`:
`nominal`, `degraded` and `crisis`, differing by **10× in critical hazard and 20× in demand
failure**, each with a `gm_disposition` and a `seeded_faults` policy. The 118 fault policies carry
the *nominal* baselines — 2e-5 and 2e-4 — as though they were absolute, and `faults.py` read the
policies and never the postures. **A crisis run and a nominal run produced the same faults.** The
one number the experiment exists to vary was inert.

It scales now: 1 scheduled event at `nominal`, 7 at `degraded`, 20 at `crisis`, with the declared
factors printed so a run can be read against the table it used.

**And the reason it was applicable at all is a coincidence that is now a rule.** A posture has a
critical column and a noncritical column because they are separate baselines, so the obvious
implementation asks each fault which class it is — except the policies declare a bare `hazard` and
no class, and **19 of the 58 sit between the baselines**: `COM-09-recorder-failure` at 1e-4 is 5× the
critical baseline and 0.5× the noncritical one, and nothing says which scaling it should take. The
scaling is well-defined today only because *both* columns move ×5 at `degraded` and ×10 at `crisis`,
which makes the class irrelevant. That is a property of these particular numbers and not something
anyone wrote down, so `check_scenario_postures` now refuses a table whose two hazard columns move by
different factors — "move both columns together or give the faults a class" — verified by moving one
column and watching it refuse.

**`seeded_faults` is the half that cannot be sampled.** `apollo_diode.md:368` says why the mechanism
exists: a common-cause event is "explicitly seeded rather than relying on tiny random probability".
At the nominal critical hazard a 192-hour mission expects **0.0038 events** from any one fault, so a
posture promising "one guaranteed major primary" and then sampling for it would deliver an empty
mission almost every time. `degraded` and `crisis` now *place* one fault at a deterministic time
inside the mission; `nominal` places none, per its own `seeded_faults: none`.

Two readings were made to implement it and both are written into the code so they can be argued
with. **"Major" is read as the critical class**, the only severity the corpus declares. **"Optional"
is read as the GM's decision rather than a probability draw**: crisis carries
`gm_disposition: white_team`, and the only rate the posture declares for it — `failure_on_demand_p`
— is apollo's *per-activation* Bernoulli `p`, which used per mission would understate by the
thousands of activations in 192 hours. So the scheduler *offers* the latent sensor defect, names the
pool of 26 instrument faults, and seats one only on `--sensor-defect`. Inventing a per-mission
number to replace it would have been inventing a number.

## The crew are described twice and the two descriptions never met

`mission.yaml#crew` is a personnel model — `size`, `surface_party`, and one entry per person with
`location_phase_default` and `goes_to_surface`. `vehicle.yaml#configurations` is a hardware model —
`crew_aboard` and `crew_in`. Both are right. They overlap on every question a fleet can ask about
who is where, and **all five fields were read by nothing**: not by a tool, not by the other file,
not by a sentence in any document. The values happen to agree today — `crew.size` 3 is the largest
`crew_aboard`, `surface_party` 2 is the count of `goes_to_surface` — and nothing was keeping them
that way.

Four checks now hold the join. Three are plain mismatches. The fourth is the one worth writing up:
**`crew_in` had to be anchored to the station vocabulary's own prefixes**, because otherwise the
check is circular — `crew_in` validated against `location_phase_default` and that against
`crew_in` — and a configuration saying the crew are in a `cockpit` would agree with a mission saying
the same.

**And the join turned up a naming problem that is a real one.** `location_phase_default: csm` is
correct on its own terms: all three crew are in the CSM before undocking. But every *other* naming
of a place in this vehicle is a **station** — `csm_commander`, `csm_navigator`,
`csm_lower_equipment_bay`, `lm_commander`, `lm_pilot`, `tunnel`, `surface_eva` — and `ask_crew`'s
perception bound is per station. So a question asked from "csm" is a question asked from nowhere.
The crew are in fact named three ways: `ask_crew`'s `crew_id` and `mission.yaml#crew.positions` both
say `commander`/`lm_pilot`/`csm_pilot`; `crew_positions` names stations; and
`location_phase_default` names a vehicle. The linter now refuses a vehicle term that is not a
vehicle, and it cannot go further, because **which CSM station each person occupies is undeclared**:
the corpus gives three stations for three people and never says who sits where. That is a decision
about the vehicle rather than a repair, so it is recorded rather than invented.

The same join found a second gap, reported as a debt at the two phases it affects. **`descent` and
`surface` name no configuration that holds the CSM**, so the CSM pilot cannot be placed in either —
while her own entry in `mission.yaml` says she is "alone in the CSM for the surface phase". The two
readings of a phase's `configurations` list — the configurations it passes *through* versus the
vehicles *present* in it — give different answers here, `lunar_orbit` and `ascent_rendezvous` each
name two (suggesting a sequence), and nothing in the corpus says which is meant. Under the sequence
reading `descent` is right and the CSM's presence during it is simply unrecorded.

## Which seat, and the four declarations it took to answer

`location_phase_default: csm` is the right answer to "which vehicle is this person in", and it is
the wrong *shape* of answer to the question `ask_crew` actually asks. The perception bound is
computed per **station**, and `domains/crew/components.yaml#display_contract` gives the CSM's three
stations three different panel sets and three different `cannot_see` lists — so a question asked
from "csm" is a question asked from nowhere. The corpus named three crew and three CSM stations and
never said who sat where.

The corpus did supply the geometry, which is what made the assignment a decision rather than a
guess:

- `csm_commander`'s note calls it "**the left seat**", and it is the commander's.
- `csm_navigator`'s note calls it "**the right seat**" and names it for the *navigation* role,
  which is the CM pilot's — so `csm_pilot` takes it.
- That leaves the LM pilot the third CSM station, the **lower equipment bay**, and the choice has a
  consequence worth wanting: the bay's note says a crew member there "cannot see a single display
  the other two seats are reading", so the LM pilot in the CSM has a strictly different perception
  bound from the other two — a fleet that asks all three the same question gets two answers and one
  abstention. The alternative, the CM pilot in the bay, puts the navigation specialist where she
  cannot see the guidance display, which the corpus's own notes argue against.

Every station map is `basis: chosen` with that reasoning in the file, because the corpus gives the
geometry and not the assignment. What is *not* a matter of choice is checked: the vocabulary (a
station must be one `crew_positions` names, or it is a place with no panels), the surface party (a
crew member who goes has an LM station; one who does not never has one), and — the assertion that
matters most — **no two people share a seat**, because one station means one perception bound and
two crew at one station would be two people the vehicle cannot tell apart.

`plant.py --crew` then computes the projection the bound needs, from three declarations that were
each necessary and none sufficient: `vehicle.yaml#configurations` (which vehicle carries crew),
`mission.yaml#phases` (which configurations a phase passes through), and the station map. It is
keyed on **(phase, configuration)** rather than on phase, and that is not a detail: `lunar_orbit`
runs from docked to undocked and `ascent_rendezvous` from the ascent stage to a docked CSM, so
"where is the commander during lunar orbit" has two true answers and reporting neither would be
worse than reporting both.

And it reported where it could not answer: `descent` and `surface` named only LM configurations, so
the CSM pilot was placed by neither — while her own entry says she is "alone in the CSM for the
surface phase". Turning that from a debt-list paragraph into a computed value is what closed it,
and the closure is a second declaration.

**`configurations` is a sequence, and the data settles it rather than a convention.** Every
multi-entry list is a chronological progression and nothing else fits: `lunar_orbit` runs from
"LOI complete; the vehicle is in a lunar parking orbit" to "LM undocked and the descent orbit
insertion burn is complete"; `surface` from the descent stage to the ascent stage;
`ascent_rendezvous` from "ascent engine ignition" to "docking complete and the crew transferred to
the CSM". So the list is what the phase's *subject* passes through, and it is not a claim that
nothing else exists. **`also_present`** makes the second claim, and `descent` and `surface` declare
`csm_alone` there — the CSM waiting in lunar orbit, alone, with the CM pilot aboard. Every crew
member is now placed in every phase.

What is still owed is the rest of the simultaneity, and the debt says so: a phase with two vehicles
has two *clocks* and the file gives one duration, so a fleet asking what the CSM is doing while the
LM descends has no per-vehicle timeline to read. Closing one gap opened the view onto the next,
which is what a projection is for.

## The second clock, and the derivation nobody was redoing

`mission.yaml#comms_blackout` is the vehicle's one derived figure that can be checked against an
operational one, and its own comment says so: *"Apollo's loss-of-signal was about 45 minutes per
revolution. The two figures differ by the orbit's eccentricity and by the Earth's own 1.8-degree
disc... so the derivation is right to within the effects it deliberately omits, and that is a
**stronger** statement than a citation would be."*

It is stronger only if somebody redoes the arithmetic, and **no tool read the block at all**. Four
numbers, a full derivation in a comment, and no referee — which is precisely the arrangement that
let `E-RAD-WATER` say 3.8e-7 while its own relation computed 4.082e-7, a 7 % disagreement nobody
could see. Two things were wrong underneath that:

- **The derivation's inputs were not data.** `mu_moon = 4902.8 km³/s²` lived inside the `relation`
  *sentence*, so the arithmetic could not be reproduced from the file without parsing prose. It is a
  field now, and `check_blackout` re-derives the period from Kepler, the half-angle from the Moon's
  angular radius, the fraction, and the duration — refusing a disagreement over 1 %, the same
  tolerance the `computation` mechanism uses on coupling edges. Verified against a perturbed
  duration, period, half-angle and μ.
- **"Affects `surface`" is not "costs `surface` eight and a half hours."** A phase declares one
  duration and an orbit declares another, and nothing had put them together. `plant.py --blackout`
  does: **23.5 h of the 60 h across the five lunar phases are silent**, at 46.5 min in every
  117.8 — `lunar_orbit` 9.7 h, `descent` 1.0 h, `surface` 8.5 h, `ascent_rendezvous` 1.4 h,
  `lunar_orbit_docked` 3.0 h.

The block's own note points at the sharpest case without quantifying it — *"a blackout during a
powered descent is eleven minutes of the most consequential flying on the mission happening
unwatched"* — and the projection supplies the other half of that sentence: `descent` loses 59
minutes of its 150, and whether one of them lands on the powered-descent window is a question about
phase alignment rather than about geometry.

And the projection says what it is not. The figure is for a vehicle in a **100 km circular lunar
orbit throughout the phase**, which is the CSM and only the CSM: the LM is in orbit briefly during
`descent` and `ascent_rendezvous` and is on the *surface* for most of `surface`, where a vehicle
near the sub-Earth point has the Earth fixed in its sky. Its blackouts are a function of where it
landed, which nothing declares — so the owed datum is a landing longitude, and one datum closes
both that gap and the one `apollo_diode.md:1206` already records.

## The site decides one thing, and it inverts the vehicle

`landing_note` recorded that the site was unparameterised and that `apollo_diode.md:1206` wants one
this vehicle's own — *"avoid matching all historical labels closely enough to permit that
shortcut"*, which is a constraint on the site as much as on the faults, because a fleet that
recognises Tranquillity Base has been handed the answer. It is declared now: **12.4°N, 47.5°E**,
`basis: chosen`, with the reason in the file. The corpus carries no coordinates for any site, flown
or otherwise, so the *choice* cannot be checked against anything — but the geometry it produces can,
and that is what `check_landing_site` re-derives.

**What the site decides is whether the LM can be heard at all while it is on the surface.** The
Earth sits near the sub-Earth point, so its elevation from a site is 90° minus the angular distance
to that point: 41.3° here. Above the horizon the link is there for the whole stay and below it there
is no link at any time, and **there is no interesting in-between at this timescale** — libration
moves the sub-Earth point at 0.075°/h at its fastest, so across the 21.5-hour surface phase the
elevation changes by at most **1.6°**. The LM is in contact throughout or never, and which one is a
property of where it landed rather than a time series.

That closes round 34's caveat exactly, and the answer is the opposite of the orbital case. **The
vehicle's comms invert between its halves:** in orbit the CSM is silent 46.5 minutes in every 117.8
— 39 % of the time, 23.5 h across the five orbital phases — and on the surface the LM is not silent
at all. So during `descent` and `surface` a fleet hears the LM and not the CSM, and the two trade
places at the orbit's cadence during `lunar_orbit`. That is not a modelling artefact; it is what a
100 km orbit and a near-side site do, and it is why the LM-as-lifeboat decision is affordable at
all — **the half of the vehicle the crew would move into is the half that can always be talked to.**

The check refuses four ways, each verified by breaking the file: an elevation that disagrees with
the geometry, a site that puts the Earth below the horizon (a far-side site would fly the surface
phase in silence *by design rather than by fault*), a declared variation the libration rate
contradicts, and — the one worth having — a site whose *mean* link is fine but whose libration
envelope dips below the horizon. At 0°N 85°E the Earth stands 5° up and libration takes it 2.9°
below; a site like that is a surface mission whose comms plan depends on the month.

## The electrical inventory was declared twice

`vehicle.yaml#electrical` is the vehicle-level view — what it carries, with the **mass** each item
contributes to the mass closure. `domains/power/components.yaml` is the domain's view: the same
hardware, one entry per unit, with the **bus** each load sits on and the protection between them.
Both are right, they are two views of one machine rather than two machines, and **nothing said so
and nothing compared them.**

They agreed on every comparable quantity when the check was written — the count, the Ah, the volts,
the module count, the per-module power, the bus voltage — and nothing was keeping them agreeing.
That is the whole of the problem: a battery re-rated in one file and not the other is two machines
wearing one name, and the mass closure would go on summing the mass of the one nobody flies.

`domain_group` is the link, **declared rather than inferred**, because the two files name the same
batteries differently — `csm_entry` against `battery_csm` — so a rule that guessed from the string
would have to know that `entry` and `csm` are the same vehicle. The check then compares count, Ah,
volts and the fuel cells' three quantities, and refuses seven ways.

The voltage comparison is the one that is not a plain equality, and it is the interesting one. The
two files express the same cell in the shapes their readers need: a *group* carries a range
(open-circuit 37.2 V down to loaded 27 V) and a *unit* carries the nominal it is modelled at (28 V).
The claim those two shapes make about each other is that the nominal lies inside the range, and that
is what is checked — a nominal of 30 V against a loaded range starting at 30 V is refused, which a
plain equality could not have seen in either direction.

**And the audit that found it also turned up a duplicate.** The unread-field sweep — the one that has
found a real defect nearly every round — showed `demand_w`, `inrush_w`, `bus`, `rated_w`, `ah` and
`v_nominal` all read by nothing: **the power domain's entire quantitative inventory**, 17 components
and 25 loads. The load budget itself closes exactly (CSM 1723 W, LM 1007 W against the declared
totals), and that closure is now one of the things a check could hold — the same shape as mass
closure, which the linter has enforced since the beginning. It is not held yet, and that is recorded
rather than implied.

## The power inventory's arithmetic, and a field that means two things

Round 36 linked the two views of the electrical inventory and left the *relationships* owed.
They are held now, and the shape is the mass closure's, one domain over: the per-vehicle demand
sums against the declared totals (CSM 1723 W, LM 1007 W — they closed before and nothing was
keeping them closed), every load sits on a bus belonging to its own vehicle, the source capacity
presenting to a vehicle covers its connected load, and the batteries' `ah × v_nominal` against the
energy the vehicle carries.

**That last one is the one with teeth**, because `power.battery_soc_pct` is a percentage whose
denominator was declared nowhere and `battery_charge_j` is a stock with no capacity — so what the
vehicle carries in joules existed only as a product nobody computed. It is declared now, and the LM's
is declared in **three** parts: the descent batteries are jettisoned with the descent stage, so
`lm_ascent_stage` flies on 16,576 Wh alone against a 3.5-hour phase, and the split between the
stages is the number that decides whether the ascent can be flown.

**And the check that could not be written is worth more than the four that could.** Chasing the
sweep's top hit — `range`, 85 declarations, read by nothing — turned up what looked like three dead
alarms. `power.battery_soc_pct` declares `range: [40, 100]` and three thresholds watch it at `<30`
and `<15`; `power.dc_bus_a_v` ranges 27.0–30.5 with an event at `<26.5`; `power.battery_temp_c`
ranges 10–40 with an event at `>45`. All three looked like a channel that cannot hold the values its
own alarms require — the bus-tie `bool` again, one domain over.

They are all correct, and the reason is that **`range` carries two meanings and nothing says which.**
On a physical channel it is the *acceptable operating band* and every event fires **outside** it. On a
reserve channel it is the **full scale** and the events fire **inside** it —
`rcs.propellant_remaining_pct` ranges 0–100 with a reserve at `<25`, which is inside a full scale and
correct. Both readings are right, the two are indistinguishable from the data, and a rule of the form
"an alarm must lie outside its channel's range" would refuse 34 legitimate thresholds. So the honest
output is not a check but a debt: `range_kind: band | scale`, and until it exists `range` is
documentation no check can use.

**So the field was added, and the check was written.** `range_kind: band | scale` is declared on all
57 channels with a numeric range — 43 bands, 14 scales — and it was assigned by evidence rather than
by taste: a range with an alarm strictly inside it *cannot* be a band, so it is a scale. The check
now refuses an alarm strictly inside a declared band, with two exemptions that are the threshold
saying it measures something else — a `point_units` differing from the channel's own unit covers the
six rate thresholds that watch a level channel with a per-minute limit, and `gated_by` covers a
threshold the schema forced onto a channel it is not about.

**And the rule's blind spot found a real defect.** It cannot see a channel whose range is a band *and
wrong* — but looking for one turned up `thermal.zone_[id]_t_c`, which declared `[5, 40]` while four
of its own thresholds asserted inside it and `lm_descent_freeze` asserted below −5. It is a six-zone
template, and **no single band describes six zones**: the cabins live at 10–30 and 10–32, and a
descent stage left in shadow is legitimately colder than the floor. The range is unbounded now, and
the per-zone bands are the thresholds' business, which is where an instance-specific limit belongs.
Two smaller conventions sit in the same field — `[True, True]` for a boolean channel and
`[null, null]` for unbounded — and both are conventions rather than declarations.

The general shape, which is why it is written up rather than fixed: **a field with two legitimate
readings cannot be checked, and the way to tell is to write the check and watch it refuse things that
are right.** The first rule tried here would have refused a third of the thresholds in the vehicle,
and every one of them was doing exactly what it should.

## The profile that widened what it promised to narrow

Every domain declares `profiles`, `profile_selection` and `alternatives`, and **nothing read any of
it**. Two rules live there, and `thermal_diode.md:823-826` states the first as a design constraint
rather than a preference — *"the model that reasons about the spacecraft must not also be able to
rewrite the limits by which its reasoning is constrained"*. Each domain's `profile_selection` says
the second half in its own words: *"An agent may select a profile revision and may never edit one."*
An alternative profile exists so an agent wanting more margin has somewhere legitimate to go.

**Neither was enforced, and the narrowing rule had a defect behind it.** A profile scales its
thresholds by a factor, and the direction that factor must move **depends on the comparator**:
tightening a ceiling means lowering it, tightening a floor means raising it. One number cannot do
both. Three of the four domains had chosen whichever direction suited the thresholds they happened
to have:

| domain | was | below thresholds | above thresholds | effect |
|---|---|---|---|---|
| `power` | `factor: 0.9` | 9 | 6 | **widened all 9 floors** |
| `thermal` | `factor: 0.9` | 7 | 10 | **widened all 7 floors** |
| `propulsion` | `factor: 1.25` | 5 | 7 | **widened all 7 ceilings** |
| `consumables` | `factor: 1.33` | 12 | 0 | tightens — it has no ceilings |

`power` is the one that matters most: its `tight` profile, selected by a fleet that wanted warning
*earlier*, dropped the bus undervoltage ladder from **26.5 V to 23.85 V**. The whole shed ladder
moved down 2.65 V — warning later, and therefore shedding later, in the domain where shedding late
is how a bus dies. **A profile whose name promises margin and whose arithmetic delivers less of it
is worse than no profile: it is a decision an agent can make in good faith and lose by.**

The fix declares the direction: `factors: {below: ..., above: ...}`, with each domain's original
number **mirrored** into the other direction rather than invented — 10 % tighter is `0.9` on a
ceiling and `1/0.9` on a floor, so `power.tight` became `{below: 1.1111, above: 0.9}`. A factor that
lowers a floor or raises a ceiling is now refused, as is a profile that declares no factor for a
comparator its domain uses — an omission that would leave half the envelope untouched while
appearing to tighten everything.

**And D-05 itself is now a refusal**, vacuous today: every verb mentions thresholds only in its
`interlocks`, which is the legitimate direction, and none declares a write at all. That is one line
of a future domain away from being false, and the thing it protects is the experiment. An agent that
can widen its own envelope has not been tested on the envelope.

## The challenge had no score

`mission.yaml#objectives` is the definition of success, and **nothing read it**. Nine terms — three
outcome, five margin, one research — and they were prose. Four of them named a channel inside a
sentence, `thermal_margin` ("worst zone margin, all phases") named none at all, and the three
outcomes are sentences no machine can read. **A challenge whose definition of success is prose has
no score**, which is a strange thing for a challenge to lack.

`evaluated_by` is the boundary this folder draws everywhere else, applied to scoring:

| who | meaning | objectives |
|---|---|---|
| `vehicle` | the record settles it | `surface_mission`, the four margins |
| `far_side` | the operator settles it | `crew_survive`, `return` |
| `external` | computed outside both | `coordination` |

**The interesting declaration is `crew_survive`.** It is `far_side` and contributes
`crew.available` — an availability state that distinguishes resting, on suit and incapacitated and
**cannot say "dead"**, because `apollo_diode.md:754` puts a model of a person out of scope. An
objective whose vehicle contribution is *narrower than its term* is a thing the scorer needs to
know, and writing it down is the point. `return` is the same shape for a different reason: what the
vehicle publishes is where it *thinks* it is, with a covariance, and whether the entry was
controlled is a judgement about the trajectory rather than a reading.

`thermal_margin` needed its own field. It is a margin, so the sense is **not** "higher is better" —
a zone is judged by how near its limit it ran and the worst zone is the one that came closest. That
is `sense: closest_to_limit`, and it is why a margin declaring no sense is refused: "higher is
better" and "far from the limit" are different judgements and the name implies neither.

The check refuses an objective with no evaluator, a `vehicle` objective naming no channel, a channel
that is not registered, and a margin with no sense. The last two clauses that could have been
written are defence in depth rather than live: an objective scored on a `not_published` truth is
refused by the truth-boundary check first, because a withheld channel must not be registered — the
two rules overlap, and the overlap is the safe direction.

## A claim eight domains were getting wrong

Every `fault_policy.yaml` opens its `coverage` block with the same sentence — *"Every channel this
domain publishes is perturbed by at least one fault above, except ..."* — and names the exceptions.
It is the most useful claim in the file: it says which channels a fault can **never** move, which is
exactly what a fleet should know before spending an afternoon diagnosing one. **Nothing read it, and
eight of the eleven claims were false.**

The drift is structural rather than careless. `perturbs` is edited when a fault is added;
`unperturbed` is edited when somebody remembers. The two therefore part company in one direction —
the claim becomes *more optimistic* than the policy. `power` said its two battery channels were
"perturbed only *indirectly* through PWR-07" while **PWR-07 lists both in its `perturbs`**, and
`perturbs` is a direct list because that is the only thing it can be.

`check_fault_coverage` compares the claim against the policy exactly — a claim with a slack clause
is a claim that cannot be checked — and all eleven now hold.

**And the comparison found something worth more than the correction.** Twenty-two channels are
perturbed by no fault, and they fall into three groups that the registry does not distinguish:

- **twelve `service` channels** that are *decisions* — `rcs.mode`, `power.load_shed_class`,
  `controls.switches`, `prop.thrust_pct_commanded`. The crew or an agent sets them and no physical
  mechanism writes them.
- **six `estimate` channels** derived from a channel a fault already perturbs.
- **nothing at all, as of round 42.** The LM's four atmosphere channels were the previous entry in
  this list, and they are the exception that proves the comparison is worth making: the same check
  that produced it is what confirmed the gap had closed, because the domain's claim is now that its
  exception list is *empty*.

The LM's atmosphere was a real gap while it lasted. The domain publishes two atmospheres and its own
`points.yaml` says why — the LM's is separate *"for exactly the phase in which the crew are living
in it"* — and then in `descent`, `surface` and `ascent_rendezvous`, **27.5 h during which every hour
is spent in the LM**, no injectable fault could move the air the crew were breathing. A leak in the
vehicle the crew are actually in is the emergency a crewed scenario most obviously wants, and it was
the one the policy could not produce.

It was closed by pairing rather than by a cabin parameter: **ECL-12 to ECL-21**, ten LM faults
written against the LM's own channels, because a leak is in one cabin and the pairing is what lets
the two diverge. The divergences are the content — the LM has no leak-rate channel (so an LM leak
never appears in `eclss.leak_rate_g_s`, which is derived from the CSM's mass balance), no
regulator-position channel, a shared `res.o2_remaining_kg`, and a party of two where the CSM's is
three. `ECL-07-suit-loop-fan-failure` has no twin and should not: the suit loop is one shared
circuit.

Writing the twins is what exposed the defect underneath them. **The vehicle had one CO2-removal
counter for two absorbers.** `E-ATM-ABSORB` runs from `cabin_atm` and `E-LM-ATM-ABSORB` from
`lm_cabin_atm`, and the second edge's own note insisted the difference between them "is the edge's
endpoints rather than its value" — while both landed on one node, `absorber_capacity`, which deleted
exactly the distinction the note was defending. A crew on the surface spent the CSM's element; a
crew back in the CM spent the LM's cartridge. The counter also had **no rating**, so it could be
spent forever, and the three ratings that would have exposed it were written in two other files
(`vehicle.yaml#consumables`: CSM element 72 man-hours, LM primary 41, LM secondary 78 with six
spares) and read by nothing. `eclss.absorber_capacity_pct` — a percentage of a quantity its own
provenance note said "the corpus never says of what" — divided by nothing, while its sibling note in
`points.yaml` cited 41 and 78, the *LM's* cartridges, as the denominator of the *CSM's* channel.

The repair is two counters with two ratings (`absorber_capacity_csm` at 72, `absorber_capacity_lm`
at 41), two stocks with two minimum flows — 2.78e-4 and 5.56e-4 man-hours/s, because the crew split
up and one crew member is the smallest party that works either — and two channels. It also caught a
**right number with a wrong reason**: `E-ATM-ABSORB`'s relation called 41 man-hours "a primary LiOH
cartridge" and applied it to the CSM's edge. The value survives the substitution — man-hours per kg
is `1/0.03792` at any rating, because a man-hour is *defined* as one crew-hour of removal — which is
precisely why nobody noticed, and why a later reader would have "corrected" it into a wrong number.
`E-LM-ATM-ABSORB`'s "the man-hour rating is the same cartridge class" was false for the same reason.

The same comparison also showed that **`service` covers two things a fault treats differently**: a
*decision*, which no fault writes, and a *conclusion* — `avionics.sensor_health_[class]`,
`avionics.computer_mode` — which faults move immediately. 117 channels are both `service` and
perturbed, so the layer cannot be used to tell them apart. What would is `origin: commanded |
derived`, and until it exists the only way to say a channel is untouchable is to list it.

## The thermal section was empty, and its three children were top-level keys

`vehicle.yaml` had this:

```
thermal:

loops:
  - id: primary
```

A section header with nothing under it, and its three subsections one indent level out. YAML reads
that as `thermal: null` plus three orphan keys — so **every reader that asked for
`vehicle["thermal"]["loops"]` got `None`**, and the loops, radiators and zones the file declares
were read by no tool at all. This is the cause of a symptom the section-level audit had been
carrying for two rounds: `vehicle.yaml:zones`/`radiators`/`loops` kept appearing on the unread list,
and the list cannot tell a section nobody wired up from a section nobody *can* reach.

Writing the join found the second half immediately, and it is the kind that survives review because
each file reads correctly on its own. **`vehicle.yaml` named the LM's coolant loop "secondary"** —
it carried the LM's fluid (65 % water against the CSM's 62.5 % glycol), the LM's flow band
(1.5-1.9 L/min) and the LM's 11.3 kg of coolant — while `domains/thermal/components.yaml` has three
loops: `loop_primary`, `loop_secondary` (the CSM's *second* loop, `chosen` and scaled, which the
vehicle-level file mentioned nowhere) and `loop_lm`, carrying exactly the fluid and exactly the flow
band that `vehicle.yaml` had filed under `secondary`. A reader taking the globals file for the loop
list would have sized the wrong vehicle, and the LM would have had no loop at all.

So the ids are the domain's now, the CSM's second loop is declared, and two checks hold the two
views together: **`check_thermal_bindings`**, which is `check_electrical_bindings`'s shape one domain
over — same loops by id, same fluid, same flow band, same volume, the radiator's per-panel rejection
times its panel count against the model's derived total, and the same six zones — and
**`check_vehicle_sections`**, which refuses a top-level key nothing reads and a section declared and
left empty. The second is the root cause rather than the symptom: a block that has lost its parent
is refused at the point it happens instead of being discovered two rounds later as an entry on a
list of things nobody reads.

**And chasing it found the linter crashing on the input it exists to diagnose.** With
`vehicle.yaml` unparseable, `check_mission` dereferenced `None` and the linter died with
`AttributeError` *from inside a check* — so the traceback replaced the report and the one line that
mattered, naming the parse error, was buried under a crash in a check that never ran. An operator
sees that and concludes the tool is broken rather than the definition. The cross-file checks are now
gated on the file they join against, and the run says once that the joins are unavailable.

## Checking `unperturbed` made the coverage block look read

`coverage` has five keys. One of them, `unperturbed`, is the claim round 41 learned to check — "every
channel this domain publishes is perturbed by at least one fault, except ..." — and checking it
turned out to be the most misleading thing in the file, because **four siblings sitting right beside
it made claims of exactly the same kind and nothing compared one of them to the policy.**

An audit of named blocks that appear in no tool found them, and hand-checking found that **five of
their first eight numeric claims were wrong, in both directions**:

| domain | claim | actual |
|---|---|---|
| `avionics.cross_domain` | 6 of 11 faults cross a domain boundary | **4** |
| `avionics.cross_domain` | "the highest cross-domain reach of any domain" | **`eclss` has 12, `structure` 10** |
| `comms.cross_domain` | 4 faults cross | **5** |
| `gnc.cross_domain` | 5 of 11 faults cross | **1** |
| `gnc.ladder_coupling` | 7 of 11 faults move `gnc.nav_integrity` | **9** |
| `comms.gated_alarms` | 3 thresholds carry `gated_by` | 3 ✓ |
| `rcs.cross_domain` | 4 of 11 faults cross | 4 ✓ |

`gnc.cross_domain` is the one worth reading twice, because it named three couplings that **do not
exist** — `rcs.thruster_[n]_health` "for the torque behind a saturated gyro", `mission.met_s` "for
the clock", and the comms blackout "for a coast's start". Every one of those is a real relationship
with the arrow the wrong way round: a stuck thruster arrives in `gnc` rather than leaving it, the
clock is what the whole vehicle shares rather than a navigation output, and an occultation *causes*
GNC-10 rather than following from it. That is why the sentence read as true, and why nothing caught
it — a direction is not visible in prose the way a wrong number is.

The audit also measured something no file stated: the domain's actual thesis. **`gnc` has the
vehicle's smallest outbound reach — one fault of eleven, against `eclss`'s twelve** — and the
vehicle's four largest exporters are `eclss` (12), `structure` (10), `consumables` (9) and `crew`
(7), which is a statement about what a crewed vehicle *is*: life support reaches everything that
plans around a consumable, and navigation reaches almost nothing, because it is a consumer of the
vehicle rather than a component of it.

So the prose stays — the reasoning was mostly right — and the number becomes data.
`cross_domain.faults_outside`, `ladder_coupling.channel`/`faults_perturbing`,
`gated_alarms.thresholds` and `shared_rules.implemented_elsewhere` are each checked, and
`outbound_extreme` is checked against all eleven domains rather than against itself, because a
superlative is the claim that rots while the domain that made it never moves. Each of the five new
refusals was verified by breaking a copy of the definition.

## A declaration can be swallowed by the note above it

`duplicate_keys` was written for this accident and describes it: a block scalar's content indented
to the same depth as the mapping that follows, so the entry is absorbed into the prose. It catches
the half of the accident that **collides** — where the absorbed keys overwrite the entry above, and
the last one wins. That check needs a clash.

**The other half is silent, and it survived.** When the absorbed keys are new, nothing is replaced,
the YAML parses, the linter composes, and the declaration simply does not exist. Two were found by
scanning the source for the shape — a bare `key:` line at exactly the indentation a block scalar's
content sits at, followed by a line indented deeper:

- **`coupling.yaml`'s `C-WATER-BUDGET`** had `stability: hysteresis_required: false` inside its own
  `note`. The linter's relay-oscillation rule therefore read the cycle as having no stability
  declaration — and passed, because it fires only when a member is latched and no member of that
  cycle happens to be. A pass by coincidence rather than by declaration is the worst kind, because
  it looks like evidence.
- **`consumables/components.yaml`'s `reconciliation`** lost **two**: its `on_mismatch` rule
  ("publish a RECONCILIATION_MISMATCH event and leave both numbers alone") and its entire
  `provenance` block. The vehicle's rule about never rewriting the ledger was a sentence inside a
  string that no reader of the parsed document could reach.

The check is `absorbed_keys`, it runs at load time on the source rather than on the parsed document
— the parsed document has already lost the evidence — and it carries no exemption list: three
instances existed and all three were accidents. An intentional YAML example inside a note has to be
indented out of the scalar's own content column, which is the right price for catching this.

## The mission's clock said the linter checked it

`mission.yaml` states the same total three times and each statement claims the linter holds it:

| declaration | its own words | read? |
|---|---|---|
| `phase_total_check.sums_to_h` | "the linter re-derives this and refuses a mismatch" | **no** |
| `met_epoch_provenance.total_duration_h` | "the linter refuses a build where they disagree" | **no** |
| `met_epoch_provenance.total_ticks_provenance.relation` | "192.0 h x 3600 s/h x 50 Hz = 34,560,000 ticks" | **no** |

The linter *does* re-derive the ladder — the trajectory and Kepler checks trip the moment a phase
duration moves — but not one of the three declarations was read: `sums_to_h: 999.0` passed in
silence. **That is worse than an unchecked number, because the sentence tells the next reader not to
check it by hand.**

`check_met_clock` holds all three to the ladder, and the tick count stops being a sentence:
`total_ticks: 34560000` with `computation: "192.0 * 3600 * 50"`, and `phase_total_check` gets the
same treatment with the eight durations added in the open. The `computation` evaluator that had
lived inline in the coupling check is now a shared `rederive()`, because this is the same defect
`E-RAD-WATER` produced — a value whose arithmetic exists only in prose beside it.

## The debt count was missing eleven of its own debts

The folder's headline number is the debt count, which makes an omission in it invisible by
construction. Every domain file was walked for unset values by `check_domain` and the coupling graph
by its edge walk, so every literal `UNCONFIGURED` scalar was counted — **except the eleven in the two
files nothing walks**:

- `mission.yaml`'s initial position, velocity and state-vector basis
- `vehicle.yaml`'s three minimum impulse bits, the LM sublimator's rejection and water consumption,
  and its radiators' unset values

Two of them are named in a prose `open_debts` entry *somewhere else*, which is exactly how they
stayed plausible: a reader who searches for the minimum impulse bit finds a paragraph about it and
concludes it is on the books. It was not.

The walk is now over every top-level document — `coupling.yaml` deliberately absent, because all
thirty of its unset scalars are edge sensitivity fields that the edge walk already reports against
the edge they belong to, which is a more useful place to read them. **226 became 237.**

Chasing it turned up the other half of the same idea. `mission.yaml` carried
`initial_state.landing_site: UNCONFIGURED` for as long as it carried the real thing: when the site
was chosen it was declared as a **top-level** `landing_site` block, with the derivation and a
`check_landing_site` that re-derives the geometry, and the placeholder twenty lines above went on
reporting the site as undecided, in the same file. A debt that has been answered and left standing
is worse than no debt, because it tells the next reader to stop looking — and the site is what
decides whether the LM can be heard from the surface at all. `check_answered_debts` refuses that
shape: a nested `UNCONFIGURED` whose leaf name is a *configured top-level* declaration of the same
document.

## The plant's stock integrator had never run, and was wrong three ways

The reference plant's `advance()` implements two of `plant.md` §3's seven integrator classes —
`lag` and `stock` — and refuses the rest as domain code. That is 44 of the vehicle's 124 states, and
the split is worth stating plainly: **25 `algebraic`, 43 `discrete`, 7 `dynamics` and 1 `delay` are
rules the configuration deliberately does not carry**, so 82 of the 134 states need code before the plant can
walk a whole tick.

But the load-bearing finding is about the 24 it claims to implement. **The schedule stops at the
first `algebraic` state, so the stock integrator had never executed.** Written, reviewed, and never
run — and when it was finally exercised it summed `sensitivity * dt` over every incoming edge and
never read a driver. Handed `lm_cabin_o2_kg` it returned

```
driver crew_state=1.0  ->  {'lm_cabin_atm': 117.89792}
driver crew_state=3.0  ->  {'lm_cabin_atm': 117.89792}
```

The same mass whether one crew member is aboard or three, because the three edges it summed were
`116.86 Pa per K` (a lag relation between zone temperature and cabin pressure), `1.0 kg O2 per
kg O2` (a conservation ratio whose flux is the tank's *outflow*, which the ratio does not contain) and
`0.03792 kg/h per crew`. Adding pascals-per-kelvin to kilograms-per-hour and calling the result a
mass is not an approximation; it is a dimension error wearing a number.

`stock_flux()` establishes the flux from the edge's own declared unit and **refuses where the unit
does not establish one**, which turns out to be most of the graph:

| | edges | |
|---|---|---|
| computable | 3 | `E-CREW-ATM`, `E-LM-CREW-ATM` at `kg/h per crew`; `E-RAD-WATER` at `kg/s per W` |
| a ratio whose flow is undeclared | 3 | `E-O2-ECLSS`, `E-LM-O2-ECLSS`, `E-FC-WATER` |
| a structural relation landing on a stock | 5 | `E-ZONE-ATM`, `E-ZONE-ATM-LM`, `E-PLATE-BAT`, `E-ATM-ABSORB`, `E-LM-ATM-ABSORB` |
| unset value | 3 | `E-BUS-BAT`, `E-PRESS-PROP`, `E-FC-DRAW-O2`/`H2` |

**Eleven of fourteen stock edges cannot be integrated as written**, and the two kinds need different
things declared. A ratio needs the *flow* it applies to — the oxygen leaving `o2_csm` through the
regulator, which is nowhere in the graph. A structural relation needs to stop being an inbound edge:
a cabin's pressure depends on its temperature and its mass, but its mass does not depend on its
temperature; those edges land on the stock node because the *channel* hangs off the node. The
absorber pair is the sharper case, because `man-hours per kg CO2` is a conversion whose driver
should be the CO2 *removal rate* rather than the cabin's inventory.

**The refusals were unreachable, and that is the part that mattered.** The schedule stops at the
first `algebraic` state, so no tool ever reached a stock — which means the eight structural defects
were invisible to the linter, to `--strict`, and to the debt count. A defect that only a code path
nobody reaches can see is a defect nobody has. The classification now lives in
`check_vehicle.stock_flux_basis` and **both tools call it**, in the pattern `derive_schedule` already
set, so a rule about what a stock edge means cannot come apart from the rule that checks it: the
linter reports each of the eight as a named debt, the plant refuses the same eight with the same
words, and **237 became 245.** Where the two repairs diverge — a declared `flux_node` on the edge, or promoting the
producing state to a channel the edge can read — is a **graph decision rather than a patch**, and it
is the largest single thing standing between this folder and a plant that walks a tick.

## An edge has to say which state it advances

Ten of the vehicle's 41 nodes hold more than one state, and the plant resolved a state's drivers **by
node**. On `cabin_atm`, which holds four gas masses, `csm_cabin_o2_kg`, `csm_cabin_n2_kg`,
`csm_cabin_co2_kg` and `csm_cabin_h2o_kg` were all handed the same three edges — the oxygen supply,
the crew's CO2 production and a pressure/temperature relation. Every gas integrated every other gas's
flux, and a nitrogen state that nothing supplies would have been filled by the crew's breathing.

`advances` names the state an edge drives, and the linter requires it wherever a node holds more than
one state a value can move. It is required rather than inferred because **the inference is exactly
what was wrong**: "the only state on this node" is true today and stops being true the moment a second
state lands, silently and in the direction of the plant integrating the wrong thing. The result:

```
csm_cabin_o2_kg        incoming ['E-O2-ECLSS']
csm_cabin_co2_kg       incoming ['E-CREW-ATM']
csm_cabin_n2_kg        incoming []
csm_cabin_pressure_pa  incoming ['E-ZONE-ATM']
```

### Two rules that refused the right answer before they accepted it

Both versions are worth keeping, because the failure mode is the one this folder keeps meeting.

The first set of candidate states was the methods that read incoming edges — `stock`, `lag`, `delay`,
`dynamics` — on the theory that only those are ambiguous *for the plant*. That **refused the one true
answer**: `E-ZONE-ATM` carries the cabin's dP/dT, so the state it drives is `csm_cabin_pressure_pa`,
which is `algebraic` and was excluded. A rule that refuses the correct declaration is more expensive
than no rule, because the next author bends the data to satisfy it.

The second set was *every* state, and it demanded that an executive command to `engine_main` declare
whether it advances `sps_state`, `dps_state` or `aps_state`. It advances whichever the command
names — a command is not a flux. So the set is the methods an edge's **value** can drive, which
excludes `discrete` and `service` because those are advanced by transitions and by authority. The
test is whether the edge's number is what moves the state.

### The pressure moved onto the cabin node

Moving `csm_cabin_pressure_pa` off the `internal` sentinel and onto `cabin_atm` is what gave
`E-ZONE-ATM` something true to advance. It is also where the state belongs: a pressure is a property
of the node's contents, and the graph had nowhere to put the cabin's dP/dT — so the edge carried it
into the *mass* node instead, where a cabin's pressure depends on its temperature and its mass but its
mass does not depend on its temperature. The relation is now stated once, in the state that computes
it, and the 116.86 Pa per kelvin figure survives there.

**Two of round 48's eight structural stock-edge defects turned out not to be defects at all** — they
were edges that landed on a stock node's *channel* while driving something else, and `advances` is
what makes that visible. Six structural and four unset remain. **245 became 243.**

## Every tank in the vehicle only filled

The README states the rule and no tool implemented any of it: **"a back-edge *out* of a stock still
drains it, because the stock's own integrator subtracts the flow the back-edge reads."**
`advance()` summed `incoming` and never looked at an outbound edge, so the twelve edges that leave
stock nodes drained nothing.

With round 47's missing driver and round 49's node-level resolution, the stock integrator has now
been wrong in **four independent ways** — no driver, no outbound term, drivers resolved by node, and
one discharge reading the wrong endpoint — and the reason is the same every time: **the schedule
stops at the first `algebraic` state, so it never runs.** A function that has never executed is not
a function that works; it is a function nobody has tested, in the exact place where being wrong is
invisible.

The outbound term is in, and the same classifier judges both directions. Two corrections came with
it, each of which the data made obvious once the term existed:

- **A discharge is driven by the consumer, not by the stock.** `E-CREW-WATER` at `kg/h per crew` is
  the crew's drinking rate; reading the tank's own level and multiplying gave **zero at every crew
  count**, and zero is what a stuck instrument looks like.
- **Back-edges drain.** The README names `h2_csm` and `water_cooling` as discharging *entirely*
  through back-edges; excluding them from the outbound loop would have left both draining nothing.

### The classifier had to become dimensional, not just temporal

Thirteen of the vehicle's sixteen stock edges cannot be integrated, and the linter now names every
one of them, so they enter the count and fail `--strict`:

| | edges |
|---|---|
| **computable** | 4 — `E-CREW-ATM`, `E-LM-CREW-ATM` (`kg/h per crew`), `E-RAD-WATER` (`kg/s per W`), `E-CREW-WATER` (`kg/h per crew`, outbound) |
| **a ratio whose flow is undeclared** | 4 |
| **a conversion stated backwards** | 2 — `E-PROP-ENG` and `E-RCSP-RCS` at `N per kg/s` |
| **a relation on a stock** | 1 — `E-PLATE-BAT` |
| **unset value** | 4 |

`E-PROP-ENG` is why the time-basis test alone was not enough. `N per kg/s` contains `/s`, so it read
as a rate — while its numerator is a **force**. The number is thrust per unit of flow, stated
backwards, so a plant multiplying it by a thrust would get N² per (kg/s) and call the result
kilograms of propellant. The rule is now that a sensitivity's numerator must be one of the units the
stock is denominated in, and the `/h` or `/s` reduces to the unit before the comparison — `kg/h per
crew` is a mass rate, and reducing it is what lets the same comparator judge it beside `kg/s per W`.

**243 became 246.** The design question the refusals name is the same one rounds 48 and 49 kept
arriving at: the vehicle's stocks connect to their consumers through edges whose sensitivity
describes a *conversion*, and the flow itself is never a node.

## The refusals now name the node that would fix them

Round 50 established that a dimensionless ratio into a stock is not a flux. That is true and it is
not actionable — five edges were refused with the same sentence and none of them said what to build.
The test a ratio actually needs is not "is this a ratio" but **"does the graph carry the flow the
ratio is against"**: `1.0 kg water per kg reactants` applied to a node holding `kg reactants per
second` is water per second, which is a flux. So the classifier checks the partner node's unit, with
its own `/s` reduced away first.

Nothing in the vehicle carries one, so the refusals stand — and each now names the flow:

| edge | needs |
|---|---|
| `E-O2-FC` | a flow in **kg O2/s** — the fuel cell's oxygen draw |
| `E-H2-FC` | a flow in **kg H2/s** — the fuel cell's hydrogen draw |
| `E-FC-WATER` | a flow in **kg reactants/s** — the same cell's consumption |
| `E-O2-ECLSS` | a flow in **kg O2/s** — the CSM cabin's supply through the regulator |
| `E-LM-O2-ECLSS` | a flow in **kg O2/s** — the LM cabin's, a separate compartment |

**Five edges, four consumer intakes** — not one node, because `E-O2-FC` and `E-O2-ECLSS` both draw
from `o2_csm` while being the cell's draw and the regulator's supply to the cabin: two consumers, two
intakes, two rates. Sizing it: four `kind: flow` nodes, four algebraic states whose rules set the
rates, five edges repointed, and `advances`/`drains` on each.

This is the same answer rounds 48, 49 and 50 each reached from a different direction, and it is now a
specification rather than a diagnosis: **the graph has nodes for what is conserved and for what is
commanded, and none for the flows between them.** Six flow nodes exist — `fuel_cell`,
`coolant_flow`, `radiator_reject`, `link`, `thrust_main`, `rcs_thrust` — denominated in `W`, `N`,
`dB` and `kg/s`, while the flows the stocks need are named only in the sensitivities' denominators.

Chasing it also caught a skip bug in round 50's rule: the `advances` test skipped any edge landing
on a stock node while driving something else, **but only on the inbound side matters.** `E-O2-FC` and
`E-H2-FC` drive the fuel cell's *power* state and empty the oxygen and hydrogen tanks, so testing
`advances` alone let both through while the tanks they drain stayed unfillable. **246 became 248.**

## What blocks the vehicle, derived rather than authored

The folder's answer to "what do I implement first" is now a **derived** view, for the same reason
`--order` and `--phases` are: an authored worklist drifts the moment anybody lands anything, and a
stale build order is worse than none because it sends the next reader to work that is already done.
`plant.py --build-order` classifies every state by `advance()`'s own refusal order — the same
sequence of tests the plant runs when it gets there — so the two cannot disagree.

```
134 states, by what blocks them:

    15   11 %  ready now — the two classes the reference plant can advance
    26   19 %  owes a value — the cheapest to close, and the debt count already tracks them
    11    8 %  owes an edge — a coupling with no sensitivity, or a state nothing drives
    82   61 %  owes a rule — `algebraic`, `discrete`, `dynamics` or `hazard`: domain code
```

**Half the vehicle owes a rule, and that is by construction.** `plant.md` §3's four rule classes are
domain code the configuration deliberately does not carry, so the build order is mostly a list of
missing *code* rather than missing numbers — which is the honest shape of the remaining work and was
not visible anywhere before this view existed.

The 30 % that owe an edge is the class rounds 48 to 51 kept finding, and it contains one the graph
has been hiding since the beginning: **`zone_csm_cabin_t` has no inbound edge at all.** The cabin's
temperature is a `lag` with nothing driving it, because `E-ZONE-ATM` was its only edge and that edge
points *out* of the node. So the cabin has no heat input in the graph — the crew, the equipment and
the loop all warm it in the prose and none of them is an edge.

Two things the classifier had to get right, and one of them was wrong first: `delay` belongs in the
rule class, because `advance()` implements `lag` and `stock` and refuses everything else — classifying
it as ready would have promised the plant a state it cannot advance.

## Having an edge is not having an input

`cabin_zone_t` has an edge. `E-ZONE-ATM` points *out* of it, into `cabin_atm`, so the isolation check
that refuses "a node with no edge in either direction" is satisfied and the node looks connected.

**Nothing drives it.** `zone_csm_cabin_t` is a `lag`, and a lag with no driver has nothing to relax
toward. The cabin's temperature has no heat input anywhere in the graph — while the crew, the
equipment and the loop all warm it in the prose, and the state's own 2,880 s derivation reasons about
*"a 1 kW cabin load"* that no edge carries. `check_vehicle` now reports it, and the same check found
three nodes that look identical and are not:

**`o2_lm`, `prop_rcs` and `pressurant_he` have no inbound edge because they are filled at the pad and
never again**, which they say in `preloaded:`. The first version of the check reported all seven,
which is three parts noise to one part signal — and the declaration is exactly what separates a stock
that nothing fills from one that is filled once.

### Why it is a debt and not a refusal

The fix is a design step, not a repair. The heat inputs the thermal domain declares are
`domains/power/components.yaml#loads`, and **no load says which compartment it heats**: a load carries
`bus`, `class`, `demand_w`, `inrush_w` and `vehicle`, and nothing else. Twenty-five loads against six
zones, and until the assignment exists there is nothing to draw a heat-input edge from.

And drawing the obvious edge would be worse than drawing none. A cabin is *not* simply relaxed toward
the coolant supply — its steady-state offset above that supply is the heat load over the conductance,
which is what the 2,880 s derivation means by *"a 1 kW cabin load sits about 8 K above the coolant
supply."* A `lag` pulls its state toward a single driver and has no term for an offset, so an edge
from `coolant_supply_t` would model the cabin as *equal* to the loop at steady state: **8 K wrong, and
wrong in the direction that makes a warm cabin look nominal.**

That is the stock-flow finding from `coupling.yaml` arriving at a third domain. The graph has nodes
for what is conserved and for what is commanded, and none for the flows between them — and here the
missing flow is a heat rate. **248 became 254**: two design debts in `domains/thermal/` and four
named undriven nodes.

## The heat inputs exist now, and they close exactly

Round 53 found that `zone_csm_cabin_t` had no driver because **no load said which compartment it
heats** — the thermal domain declared the heat *sources*, the power domain declared the *loads*, and
nothing joined them. The assignment is declared now, as `heat_inputs` in the thermal domain.

It lives there rather than as a `zone:` field on each power load, and the reason was already written
in the file: `heater_bank_csm`'s own provenance says *"the electrical figures are the power domain's
`csm_heaters` load; **the thermal side owns which zones it serves**."* A `zone:` on the load would be
a second declaration of one fact, in the file that explicitly refuses to duplicate a number.

| zone | loads | W |
|---|---|---|
| `csm_cabin` | cabin fan, suit fan, CO2 scrubber, lighting, S-band transceiver, S-band amplifier, heaters | 733 |
| `csm_avionics_bay` | IMU, guidance computer, instrumentation | 360 |
| `csm_service_bay` | both coolant pumps, RCS heaters, comm heaters | 630 |
| `lm_cabin` | cabin fan, suit fan, CO2 scrubber, lighting, S-band transmitter, S-band PA, guidance computer, instrumentation, heaters | 827 |
| `lm_descent_bay` | landing radar, RCS heaters | 180 |
| `radiator_loop` | *unheated, deliberately* | — |

The assignment is `chosen` where the corpus does not state it — it gives the equipment and never its
compartment — and the rule is the zones' own definitions: crew-accessible equipment heats the crewed
compartment, guidance and instrumentation heat the bay carrying the avionics plate, and everything on
an uncrewed stage heats that stage. The alternative considered was a single lumped vehicle heat
input, which would have made a warm cabin and a warm service bay the same observation.

**And it closes exactly.** `check_thermal_heat_inputs` holds it as a partition: every zone is either
given inputs or listed in `unheated` with a reason, every named load exists, every load in the
inventory is assigned somewhere, and the distinct loads per vehicle sum to that vehicle's declared
demand — **1,723 W of CSM and 1,007 W of LM**, to the watt. That is the mass closure's shape a fourth
time, and it is what makes this a partition rather than a wish: a load assigned to no zone is a watt
that heats nothing, and its zone would run cold for a reason no reader could find. All three rules
were verified by breaking a copy of the definition; dropping `csm_imu` reports *"accounts for 1,633 W
of csm load against a declared 1,723 W ... a load assigned to no zone is a watt that heats nothing"*.

### The edges exist now, and the cabins are driven

`cabin_heat_csm` and `cabin_heat_lm` are flow nodes in watts, each produced by an `algebraic` state
that sums its zone's assigned loads — **733 W for the CSM cabin, 827 W for the LM's** — and each
feeds its zone through `E-CABIN-HEAT-CSM`/`E-CABIN-HEAT-LM` at `1/125 K per W`, the inverse of the
conductance the cabin state's own relation sizes. Both nodes had no inbound edge before this round;
**seven undriven nodes became two.**

The state's `total_w` is re-derived twice on every run — against its own `computation` and against the
loads `heat_inputs` assigns — because two declarations of one quantity in two files is exactly the
shape that drifts, and it drifts in the quiet direction: a load re-rated under `domains/power/` would
change what the cabin's equipment draws while the thermal state relaxed toward the old figure,
**modelling a cabin cooler than it is.**

### The offset is back, and it is a node rather than a hoped-for method

Round 55 gave the cabins a driver and introduced an 8 K error in the same move: relaxing toward
**Q/G absolute** instead of **coolant + Q/G**, wrong in the direction that makes a warm cabin look
nominal. A `lag` takes one driver and a cabin has two, so the two are combined *upstream* of the
relaxation rather than asked of the lag:

```
cabin_heat_csm ──(1/G)──┐
                        ├──> cabin_eq_csm ──(1.0 K/K)──> cabin_zone_t   (lag, tau 2880 s)
coolant_supply_t ─(1.0)─┘
```

**These are the vehicle's first states whose rule is completely specified** — nothing in
`cabin_eq_csm_k` or `cabin_eq_lm_k` is owed, and every term comes from a declaration that already
existed: the supply from `loops`, the heat rate from `heat_inputs` summed over the power domain's
loads, and the conductance from the cabin state's own field. They re-derive on every run against all
four, so a load re-rated or a supply moved cannot leave the cabin relaxing toward a stale
equilibrium.

At 7.2 C supply the two cabins sit at **286.21 K and 286.97 K** — 13.06 C and 13.82 C — and the
0.75 K between them is exactly the 94 W between their equipment.

## The cabin's equilibrium, and two fields that were the wrong way round

Four declarations have to agree for a cabin to be a habitable cabin, and they live in three files: the
heat its equipment puts in (`heat_inputs`, summed from the *power* domain's load inventory), the
lumped conductance, the coolant supply temperature, and the zone's own `limit_c`. Nothing joined them,
and the arithmetic is one line: **T = supply + Q/G**.

`check_cabin_equilibrium` writes that line, and it found a swapped pair immediately. `loop_primary`
declared `supply_c: [2.8, 7.2]` with `evaporator_outlet_c: 5.3` — while its own source reads *"mixed
supply 45 F = 7.2 C, evaporator outlet 41.5 F = 5.3 C over a 37-45 F range."* **The supply carried the
evaporator's span and the evaporator carried that span's midpoint.** Taken at face value the lower end
is a 2.8 C supply, and at 2.8 C:

| | equilibrium | floor |
|---|---|---|
| `csm_cabin` | 2.8 + 733/125 = **8.66 C** | 10 C |
| `lm_cabin` | 2.8 + 827/125 = **9.42 C** | 10 C |

**The vehicle would have tripped its own cabin-low alarm on every cold pass of a nominal mission**,
and the cause was a field name rather than a physical impossibility: the cabin supply is the *mixed*
supply at 7.2 C, which puts the two cabins at 13.06 C and 13.82 C, inside their bands with margin.

`loop_lm` was worse, and the widened check caught it on the same pass. Its `supply_c: [1.7, 12]` was
the **magnitude of the operating range's cold end with the sign lost** — the source gives +29 to
+120 F = −1.7 to 48.9 C as an *operating range* — paired with an upper bound from nowhere. It is
`chosen` at the CSM's mixed supply now, with the reason recorded, because both loops serve a cabin at
5 psia through the same 125 W/K conductance. The alternative considered is that discarded cold end,
−1.7 C: it puts the LM cabin at 4.9 C against its own 10 C floor.

The conductance moved out of prose and into `conductance_w_per_k` while I was there, because a number
the linter cannot read is a number that drifts — and this one now has four other declarations resting
on it.

## The status numbers had drifted, and the tools now hold them

Twelve rounds of structural work moved the state count, the node count and the debt count. The
commit messages carried the right figures every time; **the README kept the old ones.** It said
"120 states over 43 nodes ... with 254 declared debts and every one of them named" while the tools
said 124, 45 and 252 — so the sentence that ends *"and every one of them named"* was two out of date
about the number it was describing.

That is this folder's own recurring finding arriving at its own status section: **a declaration no
tool reads has already drifted.** The counts had no reader, so nothing could notice.

`test_the_readme_status_matches_the_tools` is the reader. It derives every figure from the linter's
and the plant's own reports — channels, edges, debts, states, nodes, `UNCONFIGURED` scalars, and the
build-order share that owes a rule — and asserts the README contains each. **It deliberately adds no
counter of its own**: a counter written for the test would be one more declaration to drift, and the
two tools already compute all of them.

## The fuel cell's reactant chain, grounded but for one figure

The cell is the vehicle's only consumer of hydrogen and its second consumer of oxygen, and **the tanks
it drains had no computable outflow** — the oxygen inventory is shared between ECLSS and the cells and
nothing said at what rate. `fc_o2_draw` and `fc_h2_draw` are flow nodes now, and the whole chain hangs
off them:

```
o2_csm ──(1.0)──> fc_o2_draw ──(1.0)──> fuel_cell        the tank supplies the draw,
                     ^       └──(1.126)──> water_potable   the draw limits the cell,
                     └──(kg/s per W, UNCONFIGURED)── fuel_cell   the cell sets the draw
h2_csm ──(1.0)──> fc_h2_draw ──(1.0)──> fuel_cell
```

**Exactly one figure in the chain is owed**: the cell's per-joule oxygen consumption, which no source
publishes — `vehicle.yaml#electrical` carries a standby sustain flow and no operating point. It is
declared as a state parameter rather than left in the edge's prose, so the debt count names it, and it
is the *last* unknown: once it lands, the hydrogen draw is the published 8:1 ratio and the water is
stoichiometry.

**The water edge had to be re-expressed rather than re-valued.** It read `1.0 kg water per kg
reactants` with the full molar-mass computation beside it — correct, and equal to 1.0 *because mass is
conserved*: 2 × 18.01528 = 36.03056 and 2 × 2.01588 + 31.9988 = 36.03056. But its driver is now the
oxygen draw, so the ratio has to be per kilogram of **oxygen**: 36.03056 / 31.9988 = **1.126**. Same
reaction, same arithmetic, divided by a different thing — and the classifier caught it, because the
denominator has to match the node it applies to.

**The availability edges moved with the draws, and that is what let the two `C-REACTANT-DRAW` cycles
close again.** They had run `fuel_cell → o2_csm → fuel_cell`; the path is now
`fuel_cell → fc_o2_draw → fuel_cell`, because what limits the cell is the *draw* rather than the tank
level. The linter refused both cycles the moment the path broke, which is the check doing exactly its
job. The two tanks also lost their inbound edges in the move and now say `preloaded:` — loaded at the
pad, never refilled, which is true of them and was previously implicit.

**252 became 251**, and six stock edges are now computable where three were.

## The cabin supplies, and the leak that is doing two cabins' work

`E-O2-ECLSS` said "1 kg enters the cabin per kg of O2" against a *level* rather than a flow, so
nothing could apply it — the last two ratio refusals in the graph. `cabin_o2_supply_csm` and
`cabin_o2_supply_lm` are flow nodes now, and the rule is what the regulator actually does: **it
replaces what the cabin loses**, which is the leak plus the crew's metabolic consumption. Both terms
are published.

| | rule | rate |
|---|---|---|
| CSM | `(0.023 + 3 x 0.91/24) / 3600` | 3.798611e-05 kg/s |
| LM | `(0.05 x 0.453592 + 2 x 0.91/24) / 3600` | 2.736470e-05 kg/s |

Both are re-derived on every run, against their own `computation` — the first states in the vehicle
whose *nominal operating point* comes from other files' published figures rather than from a single
sourced number, so a change to the leak or the metabolic rate lands here.

**And one of them rested on a figure that belonged to the other cabin.** Round 67 split
`vehicle.yaml#consumables.leak` into `csm_kg_per_h: 0.023` and `lm_kg_per_h: 0.0226796`: A11 gives one
cabin leak and it is the LM's, 0.05 lb/hr, while the CSM's own is published nowhere. The 0.023 used
for the CSM is the figure `consumables` already sizes `o2_csm_kg`'s quantum from — 0.0507 lb/hr, the
same seal's number to within rounding — so it is borrowed rather than invented, and the field's
provenance says so. **A leak is a property of a seal rather than of a programme, and two cabins with
one number between them read as though they had two.**


## The water cycle's two edges had the stock on the wrong side of both

`C-WATER-BUDGET` is the vehicle's only cycle with a water budget in it, and its two edges are one
relation read in two directions. Until round 61 they were the wrong way round:

| | said | meant |
|---|---|---|
| `E-RAD-WATER` | `radiator_reject → water_cooling`, `kg/s per W` | a **fill** — the plant added the water the radiator *consumes* |
| `E-WATER-RAD` | `water_cooling → radiator_reject`, `kg per J` | the discharge side, carrying the **forward** relation's unit |

The second edge's own note read *"the inverse of E-RAD-WATER"* and then stated the same number in the
same unit as the edge it was inverting. So the pair asserted that rejection fills a tank that it
empties.

Swapped, they are one relation with the stock on opposite sides:

- **`E-WATER-RAD`** — `water_cooling → radiator_reject`, `kg/s per W`, `1/2.45e6`. The discharge, and
  it **computes**: the radiator's wattage sets how fast the water goes.
- **`E-RAD-WATER`** — `radiator_reject → water_cooling`, `J per kg`, `2.45e6`. What a kilogram of
  water *buys*. Refused as a stock flux, correctly — availability is a clamp rather than a slope, the
  same finding the fuel cell's availability edges produced.

The debt count is unchanged at 249, because one edge was fixed and its twin became a correctly-named
refusal. What changed is the **sign**: `water_cooling` drains now, and the vehicle's only
water-budget cycle is the right way round.

## The propulsion edges were stated as an engine's production under a tank's name

`prop_main` and `prop_rcs` discharge through their outbound edges, and an outbound edge **reads its
target as the driver**: the tank drains at whatever rate the thrust demands. Stated as
`3084.2 N per kg/s` the edge handed the plant a *force* to multiply a propellant mass by — N per
(kg/s) is what an engine produces, not what a tank loses.

Both reversed figures are published rather than owed, which is what makes this pair unlike the water
cycle: `vehicle.yaml#propulsion` carries Isp 314.5 s and 290 s, so `1/(Isp x g0)` closes each edge
exactly:

| edge | was | is |
|---|---|---|
| `E-PROP-ENG` | `3084.2 N per kg/s` | `3.2423e-04 kg/s per N` |
| `E-RCSP-RCS` | `2843.9 N per kg/s` | `3.5163e-04 kg/s per N` |

A thousand newtons of SPS thrust drains 1000/3084.2 kg/s of propellant, and the test asserts exactly
that. **249 became 247** — no swap, no new debt, because the numbers were already there in the
vehicle's own tables and only their direction was wrong.

Neither edge carries `advances` any more, and correctly: each target node holds a single state, so
the declaration was never required, and it would be wrong now — the edge reads the thrust rather
than producing it. `prop_rcs` has no inbound edge at all, which it declares in `preloaded:`.

## One removal rate for two absorbers was round 42's defect one layer up

Round 42 split the absorber *counters* per cabin. It did not split the rate that spends them.
`co2_removal_kg_s` was a single algebraic state on the `internal` sentinel **reading both counters**,
so it converted two beds' remaining capacity into one rate for one vehicle — and a crew on the
surface spent the CSM element while a crew back in the CM spent the LM cartridge.

Split into `co2_removal_csm_kg_s` and `co2_removal_lm_kg_s`, each on its own flow node, the chain is
per-compartment end to end:

```
cabin_atm ──(1.0, drains csm_cabin_co2_kg)──> co2_removal_csm ──(26.37 man-hours/kg)──> absorber_capacity_csm
```

**The LM's rate is two thirds of the CSM's** on the same per-crew production, because two crew
produce the CO2 rather than three. That is a *different number*, and one a single shared state could
not have expressed.

### It also closed the last two conversion refusals

`man-hours per kg CO2` has no time basis in its own unit, so the classifier refused it. Applied to a
node carrying `kg CO2/s` it is man-hours per second, which is exactly what the counter integrates.
The rule was already there for same-dimension ratios; it now covers any unit whose *denominator* the
partner node carries as a flow — which is the general test, and the two cases are the same case.

**247 became 245. Seventeen of the twenty-one stock-adjacent edges compute, and all four that remain
are genuinely not fluxes**: the two pressure relations, the water-availability clamp, and the battery
derating.

## A derating is not a flux

`E-PLATE-BAT` carried `0.0 J per K` from `coldplate_t` into `battery_energy` — a **capacity**
relation landing on a quantity that is conserved. The linter refused it as a lag edge on a stock, and
it was right to: a cold pack gives up less than it holds, and where it gives up less is the
*capacity*, not the charge.

`battery_usable_j` is the capacity node now, and the loop runs **bus → charge → usable capacity →
bus**. That last step is what the bus actually draws on, so `E-BAT-BUS`'s source moved with it, and
`C-BAT-BUS` gained `E-BAT-USABLE` as a member — **the linter refused the cycle the moment it stopped
closing**, which is the third time this session that check has caught a graph edit's consequence.

The nominal derating is 1.0 and the sensitivity is `0.0 J per K`, which the edge's own note already
flagged as the review-findings.md #8 case: *a closure computed at nominal passes a fidelity decision
that is wrong exactly when it matters.* In the crisis the pack is cold and the derating is not 1.0 —
and no source publishes the curve (`electrical_diode.md:341` names the effect and gives no numbers),
so it is `UNCONFIGURED` rather than invented.

### One refusal left in the whole flux class

**`E-RAD-WATER` alone.** It is `J per kg` — *what a kilogram of water buys*, 2.45e6 J — and that is an
availability clamp rather than a slope, exactly like the two cabin pressure relations and the fuel
cell's availability edges. The three of those are already handled, because their `advances` names an
algebraic state rather than a stock; this one drives `water_cooling_kg` itself, so there is nothing
for `advances` to point at and the rule has no way to say "a relation into this node's channel rather
than into the stock". That is a gap in the vocabulary rather than in the data, and it is the last of
its kind.

## The vocabulary was missing the word "clamp"

`E-RAD-WATER` at `J per kg` is what a kilogram of water *buys* — 2.45e6 J of rejection — and it is
the availability half of `C-WATER-BUDGET`. It lands on `water_cooling` because the quantity being
constrained hangs off that node, and **four attempts to express it as a flux failed because it is not
one.** The other three relations of that shape are declared by their `advances` naming an algebraic
state rather than a stock; this one drives `water_cooling_kg` itself, so there was nothing for
`advances` to point at.

`kind: limit` is the missing word. An edge of that kind is exempt from the flux rule **only with a
reason attached**, because the exemption is the one place a mislabelled conversion could hide — which
is exactly what `E-ATM-ABSORB` and `E-PRESS-PROP` did while `conserve` was doing this job badly.

```
water_cooling ──(4.0816e-07 kg/s per W, rate)──> radiator_reject     the consumption, a real flux
radiator_reject ──(2.45e6 J per kg, limit)──> water_cooling           the clamp, a relation
```

The plant skips `limit` edges in both directions — a clamp constrains what a node's channel may do
and moves no matter — so 1000 W of rejection now drains **4.0816e-4 kg of water per second**, which
is the first time this vehicle has ever spent a consumable at a demand-driven rate.

Two consequences fell out of it. `water_cooling` has no *fill* — it is 13 kg loaded at the pad, spent
by the evaporator, and nothing aboard makes it — so it says `preloaded:` now. And the plant's own
"no incoming edge" refusal needed the same `preloaded` exemption the linter's undriven-node check
already had, or it refused four correctly-declared tanks.

**No stock-flux refusal is left anywhere in the vehicle.**

## What a cabin removes is what its crew produce

Three declarations in three files, joined for the first time in round 66: the crew count
(`mission.yaml#crew`), the metabolic production rate (`vehicle.yaml#consumables.metabolic`), and the
removal rate the ECLSS domain declares per compartment. One line each —
**rate = crew × kg_per_crew_day / 86400** — and it is the cabin equilibrium's three-way join applied
to a different quantity.

| | crew | rule | rate |
|---|---|---|---|
| `csm_cabin` | `crew.size` = 3 | `3 × 0.91 / 86400` | 3.159722e-05 kg/s |
| `lm_cabin` | `crew.surface_party` = 2 | `2 × 0.91 / 86400` | 2.106481e-05 kg/s |

**The two figures are deliberately not the same figure**, which is the point of the round-63 split:
the LM's rate is exactly two thirds of the CSM's on one metabolic constant, and the test asserts that
ratio against the crew declaration rather than against the arithmetic.

Moving the crew count to three, or the metabolic rate to 1.2 kg per crew-day, refuses the domain's
rate with the reason: *"a cabin's equipment that removes a different amount from what its crew produce
is a cabin whose CO2 either climbs or falls with nobody in it."*

That is now **two of the 68 rule-owing states** with a completely specified rule — the cabin
equilibrium (round 57) and this pair — and both are specified because their inputs were already
declared and nothing had joined them.

## The oxygen supplies are held to the same three declarations

Round 60 declared the two cabin supply rates with their arithmetic; nothing checked them beyond
re-deriving their own `computation`. They are held to the *other* files now — the crew count, the
metabolic rate and the leak — which is the round-66 join applied to the supply side:

| | crew | rule | rate |
|---|---|---|---|
| `csm_cabin` | 3 | `(0.023 + 3 x 0.91/24) / 3600` | 3.798611e-05 kg/s |
| `lm_cabin` | 2 | `(0.0226796 + 2 x 0.91/24) / 3600` | 2.736470e-05 kg/s |

**And the leak needed splitting to make that join real.** `vehicle.yaml#consumables.leak` was one
figure doing two cabins' work while its own source named only one of them: A11 gives one cabin leak,
0.05 lb/hr, and it is the **LM's**, while the CSM's own is published nowhere in the corpus reached.
The 0.023 kg/h used for the CSM is the figure `consumables` already sizes `o2_csm_kg`'s quantum from —
0.0507 lb/hr, the same seal's number to within rounding — so it is *borrowed rather than invented*,
and it is now a declared field with its provenance saying so. What closes it is a CSM cabin-leak
measurement.

**A leak is a property of a seal rather than of a programme, and two cabins with one number between
them read as though they had two.**

Changing either leak now refuses the rate it feeds: *"the regulator replaces what the cabin loses, and
a supply that does not is a cabin whose pressure drifts."*

## An unpaired cabin channel is a claim

The vehicle has two crewed compartments and they cannot equalise between undocking and docking, so a
channel about a cabin's air is about **one** cabin. Most ECLSS channels are paired — `eclss.cabin_temp_c`
and `eclss.lm_cabin_temp_c` are one quantity in two rooms — and the registry's own ids are the
evidence.

**A channel that is not paired is therefore a claim, and until round 68 it was an implicit one.** The
CO2 exposure average was published for the cabin the crew *leave* and none for the one they live in:

| | before | after |
|---|---|---|
| paired ECLSS channels | 5 | **6** |
| CSM-only, explained | — | 4 |

`eclss.lm_co2_pp_1h_avg_mmhg` exists now, with its own point, its own threshold and `ECL-15` as the
fault that perturbs it. It is the sharpest version of this class of gap, because **the one-hour
exposure limit is the statistic that governs how long a surface stay can be extended** — and it was
published for the wrong compartment for the whole life of the registry.

`presentation.yaml#single_cabin` names each remaining unpaired channel with its reason, and
`check_cabin_pairing` holds the list to the registry exactly in both directions:

- **`eclss.cabin_regulator_state`** — the LM's pressure control is not published as a mechanism at all,
  so twinning it would mean inventing the article it describes.
- **`eclss.leak_rate_g_s`** — derived from the CSM's mass balance, and the asymmetry is
  load-bearing: it is what makes an LM leak *harder* to diagnose, because no instrument reconciles the
  LM's cabin pressure against its supply.
- **`eclss.o2_supply_pressure_psi`** — the LM's oxygen is a separate tank at its own pressure and the
  vehicle publishes no channel for it. Owed rather than decided: both loads are declared (279 kg and
  24.1 kg) and only one supply has an instrument.
- **`eclss.suit_loop_flow_cfm`** — legitimately single, because the suit loop is one shared circuit.

A new unpaired channel is refused until it is paired or explained, and a stale explanation is refused
once the channel gains a twin.

## The LM cabin had no water vapour, and a separator to remove it

Round 68 asked "is this channel paired?" and found the CO2 exposure average published for one
compartment. Round 69 asked the same question one file over, of the *states*, and found the same
shape:

| node | stock states |
|---|---|
| `cabin_atm` | o2, n2, co2, **h2o** |
| `lm_cabin_atm` | o2, n2, co2 |

**There was no `lm_cabin_h2o_kg`** — while `lm_water_separator` sits in the same file to remove the
vapour that had no state to be in, and the atmosphere model declares four gases for the vehicle. A
component whose subject the model does not hold is the round-42 absorber finding arriving in the
atmosphere: a mechanism acting on nothing.

`check_atmosphere_symmetry` holds it now. Every gas the model declares must be held in *every* cabin,
or the pair must be declared in `atmosphere_model.absent` with a reason — because a cabin may
legitimately lack a species, and the declaration is what separates that from an oversight. Nothing is
absent today.

**One class of finding, three rounds running:** round 67 split a leak that was doing two cabins' work,
round 68 paired a channel that was published for one, and this round found a *gas* the LM cabin did
not hold. Each was an asymmetry between two compartments that cannot equalise, and each was invisible
because nothing compared the two sides.

## Six zones, six nodes, six drivers

Rounds 70 and 71 took the crewed cabins and the radiator; this round takes the two bays, whose heat
rates were already summed and whose states were on the `internal` sentinel.

| zone | node | driver |
|---|---|---|
| `csm_cabin` | `cabin_zone_t` | `E-CABIN-EQ-CSM` |
| `lm_cabin` | `lm_cabin_zone_t` | `E-CABIN-EQ-LM` |
| `csm_avionics_bay` | `coldplate_t` | `E-FC-HEAT`, `E-TRANSPORT-PLATE`, `E-STRUCT-PLATE` |
| `csm_service_bay` | `service_bay_zone_t` | `E-BAY-HEAT-CSM` |
| `lm_descent_bay` | `descent_bay_zone_t` | `E-BAY-HEAT-LM` |
| `radiator_loop` | `radiator_reject` | `E-ENV-RAD`, `E-WATER-RAD` |

**What the two new edges do not carry is the conductance**, and that is declared rather than filled:
`thermal_diode.md:965` calls every thermal constant UNSPECIFIED, so the scalar that turns watts into
kelvin is owed and a plausible one would be exactly the invented typical-spacecraft number that
document refuses. What the round buys is that the zone is **drivable and the missing scalar is
named**, which is strictly better than a zone that is neither. The heat itself is grounded — 630 W
and 180 W summed from the power domain's load inventory.

**And it found a gap in round 70's own check.** That check refused a zone with no node and no
explanation, but **not a stale explanation** — so the two bays' exemptions survived the very round
that closed them, and the linter composed. It checks both directions now, the same way
`check_cabin_pairing` has since round 68. Removing the block was only possible because the second
direction was added first.

`zones_not_on_nodes` is empty and gone, and six of six zones carry a driver.

## One unknown is one debt

The two bays' time constants are **chosen** where the cabin's is `derived`: the cabin's relation gives
`C = 400 kg × 900 J/kg-K = 360,000 J/K` and `G = 125 W/K`, so `tau = C/G = 2,880 s` follows. The bays
have a tau and a qualitative reason — *"the propellant is the mass and the tank wall is the path"* —
and nothing else, which is why their edges carried `UNCONFIGURED` and the debt was vague.

**And naming both the mass and the conductance would be two debts for one unknown**, since either
determines the other given tau. So the states declare `lumped_mass_kg` alone, and the edges' relations
say how `1/G` follows from it by division. What is owed is **one scalar per bay** — the mass of the
propellant and structure the reason already names — and the round's count went 247 → 249 rather than
247 → 251, which is the difference between one obligation and two names for it.

`thermal_diode.md:965`'s refusal to publish thermal constants is why it is owed rather than derived,
and it is also why a plausible number would be exactly the invented figure that document refuses. The
honest move is to name the scalar, not to shrink the debt by guessing it.

## The debt count was inflated by 26, and the inflation was structural

A threshold declares `assert` and `clear` together, and **a comparator with no limit has neither**.
Twenty-six thresholds declared both `UNCONFIGURED`, so the walk reported each missing limit twice —
as `...thresholds[n].assert` and as `...thresholds[n].clear` — and the vehicle's headline number
carried 26 obligations that one value closes each.

The walk reports the pair once now, at the threshold. **249 became 223**, and no unknown was removed
to get there: the count is now the number of *scalars the vehicle wants* rather than the number of
paths that reach them.

That distinction is the round's real content, and it is the fourth time this session that one
obligation was wearing several names:

| round | one unknown, several names |
|---|---|
| 45 | the phase sum stated three times, none of them read |
| 67 | one cabin leak doing two cabins' work |
| 73 | mass and conductance, with `tau` already declared |
| 74 | `assert` and `clear`, reported separately |

The count is the folder's headline claim, which is why a double-count in it matters more than one
anywhere else: it is the figure a reader uses to judge how much is left. The **prose** `open_debts`
are 90 of the remaining 223 and have not been examined the same way — that is where the next
grouping would go.

## The prose half of the count is clean, and now there is a view that says so

Round 74 found the literal half inflated by 26 (`assert` and `clear` reported separately). Round 75
asked the same question of the prose half — the 116 `open_debts` sentences — and the answer is
**no**, with the measurement to back it:

- `coupling.yaml`'s **nineteen** per-edge debts and the **seventeen** edge ids named inside its eight
  `open_debts` sentences are **disjoint**. The prose summarises edges that have no per-edge debt of
  their own, which is the opposite of a double-count.
- One subject is named from two files — the **inertia tensor**, in `coupling.yaml` and
  `domains/rcs/` — and those are one missing datum with two genuinely different *consequences*, each
  recorded where it bites. That is the folder's style rather than an inflation.

`check_vehicle.py --debts` is the view, and its reason for existing is the round-74 finding: it
splits the owed list into literal scalars and prose obligations, groups the first by the field it
wants and the second by the file that keeps it. A reader is entitled to know which half of the
headline number has been examined, and after two rounds both have been.

The literal half is 119 scalars and the prose half 129. The largest single field is `basis` at 28 —
provenance that has been declared unconfigured — followed by the threshold pairs.
## There was nothing to settle, and correcting that is the round

Round 75 called the 28 entries carrying `basis: UNCONFIGURED` "open judgements" and proposed finishing
them. **They are correctly declared.** `UNCONFIGURED` is the class for a value no source supplies, and
the twenty-eight are owed values rather than undecided ones — the premise was wrong, and saying so is
worth a round because the opposite conclusion would have had me inventing 28 provenance classes.

Two of them, though, carry a number the plant **uses** while the entry says its magnitudes are owed:

| entry | the number | why it is used |
|---|---|---|
| `pressurant_pressure_psi` | `tau_s: 5` | `plant.py` integrates the lag with it and PRP-03 is seeded against the state |
| `pressurant_he_kg` | `quantum: 1.0e-05` | the fixed-point stock cannot exist without one |

Read against `basis: UNCONFIGURED` those look contradictory until a reader finds the sentence that
reconciles them — and a reader who does not cannot tell whether to use the number or ignore it. They
carry `tau_s_placeholder` and `quantum_placeholder` now, and `check_placeholders` requires the marker
on any owed entry carrying a numeric **integrator parameter**: `tau_s`, `quantum`, `delay_s` and
`lambda_per_h` are the four the plant reads, and whose absence stops a tick rather than merely
leaving a quantity unset.

The distinction the round turns on: **an owed value is a declaration; a placeholder is a declaration
about a value that is in use.** Before this round the folder could not tell them apart.

## A declaration that only points at another declaration

Two `note:` fields said only **"as above"** — `recon_mismatch_o2`, whose sibling carried the whole
argument, and `release_allocation`, whose `release_reservation` one entry up had a sentence of its
own. Neither was wrong, which is why nothing caught them: every check in the linter reads *values*,
and `note` was the one field with no reader at all.

The failure a bare pointer produces is positional. "Above" means whatever happens to precede it, so
reordering a file silently repoints the note: the entry goes on looking sourced while its
justification has moved to a different claim. It also costs the reader the sentence that says why the
entry exists separately — and here `consumables_diode.md:852` refuses a universal reconciliation
tolerance *per resource*, so the two limits are separate numbers even where the argument is shared.

`check_pointer_notes` refuses a pointer with nothing behind it, and **five pointer notes survive**
because each keeps a clause: *"as above, for the LM"*, *"as above, at apollo's second propellant
level"*, *"as above; 2 K wider than the CSM's upper limit because …"*. The clause is the whole reason
the second entry exists rather than being merged into the first, so the check measures what makes
those good rather than the pattern of their opening words. Its first cut had no word boundary and
refused `as aboveboard` — a check that refuses correct prose is worse than no check, because the only
way to satisfy it is to rewrite a sentence that was already right.

The same pass found the fourth instance of one unknown under several names, in the other direction:
`recon_mismatch_main_propellant` declared `point_units: "kg of |observed - ledger|"` and
`recon_mismatch_o2` declared nothing, on the same channel template with the same `unit: kg`. Nothing
broke — `point_units` is read only as an *exemption* from the unit-agreement check, so its absence
cannot be seen by the check that reads it. **A field consulted only when it is present cannot report
its own absence**, which is what made a matched pair disagree invisibly.

## Every count in prose had drifted, and there were five of them

This folder's oldest finding is that a declaration no tool reads has already drifted. The rounds that
established it fixed the *values*. This round asked the same question of the **numbers written in
sentences**, and found that not one of them was right:

| where | said | is |
|---|---|---|
| `README.md`, the status paragraph | *"252 declared debts"* | 248 |
| `README.md`, the file table | *"146 canonical channels"* | 148 |
| `README.md`, "what composes today" | *"147 channels resolve"* | 20 of 148 are unperturbed by any fault |
| `README.md`, the same paragraph | *"141 thresholds"* | 142 |
| `channels.yaml#open_debts` | *"Twelve of the 22"* unperturbed are `service` | fifteen of twenty |
| `channels.yaml#open_debts` | faults perturb *"117"* `service` channels | 42 |
| `integration/reconciliation/README.md` | *"182 debts"* | 248 |

The middle column is quoted on purpose, and writing the table is how that rule proved itself: the
first draft wrote the stale figures plain, and `test_the_readme_status_matches_the_tools` refused
**this file** for asserting a debt count of 252. The distinction it drew is the right one — a figure
the README asserts is a claim, a figure it quotes is evidence — and a table whose whole subject is
what the file *used* to say belongs on the evidence side of it.

The 117 is the one that matters, because it was never arithmetically possible: the `service` layer is
57 of the registry's 148 channels, so no split of it reaches 117. It sat in the file through several
rounds of channel additions. **A number in prose has no reader, and a number with no reader does not
have to be plausible.**

Three fixes, and the third is the one that generalises:

1. **The registry's coverage claim is data.** `channels.yaml#coverage` declares the counts the
   sentence used to carry, and `check_layer_coverage` holds each against all eleven fault policies —
   the vehicle-wide half of a claim each domain already makes for itself. Verified by putting each
   historical value back: *"claims 117 and the policies give 42."*
2. **The README test reads every occurrence, not the first.** It had pinned the *presence* of the
   right figure, so a file carrying "223" in one paragraph and "252" in another satisfied it — the
   folder's own finding arriving at the test that exists to catch it. It now requires all unquoted
   debt counts to agree, **and it immediately found a third stale one** that a grep for the known
   value had missed.
3. **A quoted figure is evidence, not a claim.** The section recording this very drift says *it said
   "… with 254 declared debts …" while the tools said 124, 45 and 252* — and those numbers are the
   record. Quoted spans come out before the comparison, which is the difference between the file
   asserting a figure and the file reporting that it once asserted a different one.

Two more, smaller: the reconciliation README's prose count of *these* tests said one hundred
twenty-six while the file held one hundred thirty-four, and is now derived from the test module's own
globals so that adding a test and forgetting the sentence fails; and that README's per-file rows are
labelled for what they are — **a changelog, not a status board** — rather than silently carrying the
figures of the round each was written in.

## The vehicle the mission spent 7.5 hours in had no configuration

`one_way_events` declares five irreversible actions, and each one carries `verb`, `arm_required`,
`observable` — all three checked — beside `from_configuration`, `to_configuration`, `consequence`
and `irreversibility`, **none of which any tool read**. The three fields saying an event is
irreversible were checked; the two saying *what it irreversibly does* were not.

`check_one_way_configurations` reads them, and found this on its first run:

```
lm_ascent_jettison: needs arming and runs `csm_alone -> csm_alone`, which changes nothing
```

`csm_alone` is defined as *"CSM alone after the LM is jettisoned"*. The event that removes the
ascent stage **began in the configuration it produces**, and that was only expressible because the
vehicle it actually begins in had no configuration at all. The mission spends **7.5 hours** there —
`lunar_orbit_docked`, the phase whose own name says "docked again" and whose single declared
configuration said `csm_alone`.

**Three files were individually consistent and jointly wrong:**

| file | what it said | what it meant |
|---|---|---|
| `mission.yaml` | `lunar_orbit_docked: [csm_alone]` | a phase named "docked" with nothing docked |
| `mission.yaml` | `ascent_rendezvous: [lm_ascent_stage, csm_alone]` | rendezvous ending with the LM already gone |
| `structure/components.yaml` | `csm_alone -> csm_alone` | the ascent stage leaving a vehicle without it |

`csm_lm_ascent_docked` is the fifth configuration now — 33,695 kg, `derived` as `csm_alone`'s six
components plus `lm_ascent_stage`'s four, which is the rule `csm_lm_docked` already used when it
added the CSM at TLI to the LM at separation for landing. No figure in it is new; only the pairing
is. The two phases either side of it now name the same vehicle in order.

**And the corpus's own numbers are 34 kg apart**, which is recorded rather than smoothed. The
published ascent stage is 4,888 kg at liftoff and 2,478 kg at jettison, "the difference being the
ascent propellant burned" — 2,410 kg. But the declared load is 2,376 kg, and the declared
components at jettison sum to 2,512. So either the load is 34 kg light or a stage mass is 34 kg
heavy, and no source in the corpus says which. It is in `vehicle.yaml#open_debts`, because the
configuration a fleet flies the last 7.5 hours of the lunar phase in is one of the two numbers a
rendezvous propellant budget gets checked against.

## A configuration declared two masses, and only one was checked

`mass_kg` and `mass_breakdown_total_kg` are the same quantity written twice. The breakdown was
summed against the second; the first went to `check_propulsion` as a burn's wet mass. **Nothing
required them to agree**, and a 400 kg disagreement on every configuration composed cleanly — with
the README quoting the unchecked one in its mass-closure table. A fleet's trajectory and its mass
closure would have been working from two different vehicles.

This is the fifth time the folder has found one quantity under two names (the phase sum, the cabin
leak, the bay's mass-and-conductance, the threshold's `assert`/`clear`). It differs from all four:
**both names are right.** The breakdown's total really is the configuration's mass, so the fix is
not to collapse the pair but to make it agree.

## The file's own comment said these were counted, and nothing counted them

`VEHICLE_SECTIONS` has carried this beside `vehicle.yaml`'s `open_debts` since the list was written:

```python
    "open_debts",  # counted and printed, like every other open_debts in the folder
```

It was not true. `channels.yaml`, `coupling.yaml` and the eleven domains each report theirs;
`vehicle.yaml`'s **nine** were read by no code at all, and dropping eight of them left the headline
count unchanged at 223. They are now counted, and **223 became 232** — with no unknown removed and
nine added that were always there.

What was missing from the number that exists to count what is missing is not marginal: the inertia
tensor and centre of mass per configuration, the six thermal zones' heat capacities and
conductances, the LM sublimator's rejection capacity, the minimum impulse bit for all three RCS
systems, the forty-four thrusters' geometry, the fuel cell's reactant consumption per kWh, the
ullage motors and the power inventory's unchecked relationships. **The vehicle's largest remaining
unknowns were the ones absent from the figure that measures how much is left.**

The general form is the folder's oldest finding pointed at the tool that enforces it: *a file's own
statement that a section is read is not evidence that it is.* Every other count in this folder is
held by a reader; a comment claiming one exists was itself the unread declaration.

## The ordering rule said "every node", and the code said "unless `internal`"

The requirement is stated in the linter's own words, beside the check that enforces it:

> So each node with more than one producing state declares either an order or that it has none.

It is not a formality, and the comment records why: the frozen lexicographic tiebreak sorted
`link_snr` **before** `tx_power`, on a node where transmit power is a term in the link budget — so
the derived order computed this tick's signal-to-noise ratio from last tick's power. *"Silence about
them means the alphabet decides."*

The loop that implements it reads:

```python
                if node and node != "internal":
```

**`internal` is not a node.** It is the sentinel for *"this domain advances it itself, in no declared
tick position"*, so it cannot appear in `coupling.yaml#nodes` and cannot carry a `state_order`
there. But it is not exempt from the rule, and nothing said so — not `plant.md`, not `coupling.yaml`,
not a comment at the exclusion. **54 states across nine domains were ordered by the alphabet**, and
the sentinel is the worse place for it rather than the better one: a node's producing states are at
least joined by edges that say what feeds what, so the graph is a second opinion when the order is
wrong. `internal` states have **no edges at all** — that is what the sentinel means — so the alphabet
was the only signal there was.

A domain's own states are the right scope, because `internal` is one sentinel shared by eleven
domains and `plant.md` §2 forbids a domain calling another; a single vehicle-wide list would assert
an order across domains that never run in one. So `components.yaml#internal_order` is the
declaration, the same two answers are accepted (a list, or `independent` with a reason), and the
declaration is checked against the states actually on the sentinel — an order that names a state
that is not there has drifted from what it orders.

**Nine domains owe it and not one can be filled in from the corpus**, so each is a debt naming its
own states rather than a refusal. That is the honest instrument for a declaration that is needed and
that no source supplies: **232 became 241**, and nothing was added that was not already true.

## The integrator did not integrate, and the fleet could not see the vehicle

Two defects held each other up, and each one hid the other.

**`advance()`'s stock branch never read the stock.** `stock_flux` returns a *delta* —
`sensitivity x driver x dt`, in the node's own unit, which its own docstring says outright — and
the branch summed those deltas and returned the sum as the node's new value:

```python
        return {state.node: total}
```

So a tank holding 279 kg with a drain became `-6.4e-06` on the first tick. **The one class the
plant claims it can advance did not integrate**, in the class `--build-order` counts as *"ready
now — the two classes the reference plant can advance"*. The `lag` branch three lines above reads
`current`; this one did not:

```python
        current = float(values.get(state.node) or driver)      # lag
```

**And no stock declared a starting amount**, so there was nothing to read. That is why nobody
noticed: a branch that never reads a level never reports one missing. The numbers were all in the
corpus already, in three places — `vehicle.yaml#consumables` carries the loads, `coupling.yaml`'s
`preloaded` prose names them a second time, `atmosphere_model` derives the cabin oxygen from the
published volume and pressure — and **none of them in the state that integrates the tank.**

Three fixes, and the third is the point of the round:

1. **The level is read and integrated.** And where there is none the plant refuses *by name* at
   `{state}.initial` rather than starting from an implicit zero — an empty tank and a full one look
   identical on the first tick of a mission nobody has run.
2. **The Bresenham residual accumulator**, which `plant.md` §4 specifies and the branch did not
   have either. The contract's own worked example is this vehicle's: a 0.0064 g/s leak against a
   1 mg quantum is **0.128 quanta per tick** at the 50 Hz tick, so every tick rounds to zero and
   *"the leak never happens"*. Without it the sub-quantum branch returned `None`, which `step`
   committed — so the tank's value became `None` on the first slow tick. The residual is carried in
   the same map as the level while the stock is a float; when §4's fixed-point mantissa lands it
   moves inside the representation and that key disappears.
3. **Every stock declares `initial`**, and a numeric one says where it came from: `initial_source`,
   a resolvable path into the document that declares the same number, or its own
   `initial_provenance`. A figure with neither is a guess wearing a unit.

| stock | starts at | from |
|---|---:|---|
| `o2_csm_kg` | 279 kg | `vehicle.yaml:consumables.csm.o2_kg` |
| `h2_csm_kg` | 24.5 kg | `vehicle.yaml:consumables.csm.h2_kg` |
| `water_potable_kg` | 14 kg | `vehicle.yaml:consumables.csm.water_potable_kg` |
| `water_cooling_kg` | 13 kg | `vehicle.yaml:consumables.csm.water_cooling_kg` |
| `o2_lm_kg` | 24.1 kg | `vehicle.yaml:consumables.lm.o2_kg` |
| `absorber_man_hours_csm` | 72 | `coupling.yaml:nodes.absorber_capacity_csm.exhausted_at` |
| `absorber_man_hours_lm` | 41 | `coupling.yaml:nodes.absorber_capacity_lm.exhausted_at` |
| `csm_cabin_o2_kg` | 2.653596 kg | `initial_derivation`: the atmosphere model's own law over the zone's nominal pressure and temperature, the model's volume and molar mass, and two constants |
| `lm_cabin_o2_kg` | 3.013405 kg | the same derivation on the LM's 6.7 m³ |
| six accumulators | 0 | *chosen* — an accumulator starts empty |

**`initial_source` is checked, not decorative**, and that is what keeps this from becoming a
*second* declaration of a number that already exists. `check_initial_sources` resolves the path and
requires the two to agree. It refused on its first run — for a reason worth keeping: `coupling.yaml`
holds its nodes as a **sequence with `id` fields**, so a dotted path has to step by name there, and
the failure was the path's assumption about the document's shape rather than the number.

**Ten stocks still owe it** and are counted: `prop_main` and `prop_rcs` are one node each feeding
three engines whose loads `vehicle.yaml#propulsion` declares separately, so a single initial would
be a choice about which tank the node *is*; the helium charge is published nowhere; the three
non-oxygen cabin gases need partial-pressure fractions no source gives; and `battery_charge_j`
needs the group-to-node placement. **241 became 251.**

## The fleet could read nothing, and now it reads nine quantities

The ring's frames had carried `"values": {}` since the console was written — a literal empty dict in
`write_frame`, never a plant call. A fleet could issue fifty-four commands and read back **no
quantity at all**. `presentation.yaml#mirror` says why that matters and where the numbers belong:

> the mirror carries the vehicle's own **software state** and the mission envelope ... It carries no
> measurements and no estimates: a reading belongs in the ring, where a fleet can see what it did
> over time, and a mirror that carried readings would give a fleet one current number and no history.

The ring is exactly where the tanks belong, and the seed makes them appear:

```
values:
  o2_csm: 279.0          h2_csm: 24.5             absorber_capacity_csm: 72.0
  o2_lm: 24.1            water_potable: 14.0      absorber_capacity_lm: 41.0
  cabin_atm: 2.653596    water_cooling: 13.0      lm_cabin_atm: 3.013405
```

The keys are **nodes, not channel names**, and that is deliberate rather than unfinished:
`advance()` returns `{state.node: value}` and `step()` commits that map, so this is the seed of the
plant's own key space and nothing here is a projection. Projecting to the published channel names is
the publisher's step and it is not a rename — `eclss.pp_o2_mmhg` is a partial pressure in
millimetres of mercury while `csm_cabin_o2_kg` is a mass in kilograms, so calling the mass by the
channel's name would be a wrong number wearing the right one.

## Nine blocks the vehicle does not notice losing

Every previous round asked whether a *declaration* had a reader. This one asked the question
mechanically: **delete a block, run every tool, diff the output.** Nine blocks came back with
nothing to show for them — 493 lines between them — and they split into two kinds, of which the
first is the worse.

**Three checks that cannot run.** The linter had code for these; the code could not execute.

```python
    quality = components.get("quality_assignment")
    if isinstance(quality, dict):          # absent block: the whole check is skipped
```

`display_contract` used `or {}` and an empty loop; `atmosphere_model` used an early `return`. All
three report on nothing when the block is gone, which is indistinguishable from reporting that all
is well — and between them they are **four hundred lines**: the quality-assignment function
`simulator-design.md:496-508` requires, the display contract the perception bound is cross-checked
against, and the atmosphere model that makes a cabin's contents species rather than one mass. Each
is refused by name when absent now. *A check that iterates a block is not a check; it is a check
plus an assumption.*

**Six blocks no tool read**, and what makes three of them worth wiring rather than declaring prose is
that they are full of **references**:

| block | what it names | what was checking it |
|---|---|---|
| `consumables/ledgers` | 12 channel ids across 4 rows | nothing |
| `crew/alert_overlays` | 3 overlay ids a verb must be able to set | nothing |
| `thermal/load_budget` | 4,933 W = 2,588 radiator + 2,345 evaporator | nothing |
| `gnc/estimator`, `gnc/trajectory_segment`, `gnc/flight_rules` | the estimator and the guidance model | nothing, and still nothing |

**All fifteen references and the closure are right today** — which is exactly the condition under
which the sixteenth is added wrong. The three checks are written and verified by breaking each: a
one-character typo in a ledger channel, a resource that is not a coupling node, an overlay no verb
can select, a total 1,000 W above its parts, and a radiator 500 W below its own model.

One rule was **written and removed**, and the reason is a property of the registry rather than of
the check. A ledger's residual *is* the difference of its two terms, so the residual channel's
`inputs` ought to be those two — but `res.recon_[resource]_kg` declares its inputs as
`res.[resource]_kg` and `res.ledger_[resource]_kg`, **template forms**, while a ledger row names
concrete instances, and not always the ones the residual is computed from: `prop_main`'s
observation is `prop.propellant_remaining_pct`, a percentage, because that is the channel a crew
reads. The check refused all four correct declarations. A template and its instantiations are two
vocabularies, and comparing across them needs an instantiation rule the registry does not have yet.

## The guidance model, re-derived

The deletion test left three blocks, all in `gnc`, and all three declare things arithmetic can
check. They were the last of the nine.

**`trajectory_segment` is the interesting one**, because it is `rederive`'s idiom applied to six
*formulas* instead of one number. The segment states its own boundary conditions — *"position,
velocity and acceleration at both ends: `(p0, v0, alpha0)` and `(pf, vf, alpha_f)`"* — and those
six conditions determine all six quintic coefficients. So whether the declared expressions are the
determined ones is a question the algebra answers, and `check_quintic_segment` answers it on every
run: it substitutes boundary values, evaluates the declared formulas under a namespace of the seven
symbols they are allowed to use, and requires all six conditions to hold through the chain rule
that `s = t/T` implies.

**A sign flipped in one of twenty-five terms would leave a trajectory that still starts and ends in
the right place.** That is why a derivation needs a reader more than a number does, and the check
is written to prove it: negating `a3`'s first term is caught immediately —

```
domains/gnc/components.yaml:trajectory_segment.coefficients: do not satisfy their own boundary
conditions: position at s=1 is -19000 and the boundary says 1000
```

The expressions are evaluated with `rederive`'s discipline and for its reason: every identifier is
checked against the seven symbols first, `^` is translated to `**` because that is how the corpus
writes a power, and the namespace holds seven floats and no builtins. **The configuration must not
become executable.**

**The estimator's three consistency claims.** The error state's five blocks must be the covariance's
five blocks — a block with no unit is a variance nobody can size — and each block is a 3-vector, so
the declared `dimension: 15` is the block count times three.

**And the filter's clock is the mission's.** `sub_stepping` declares three rates and a note that
reads like a design decision: *"the major cycle is a sub-step inside the 50 Hz tick through the
plant's integer-microsecond event queue, **not a second plant rate (C-07)**"*. The 50 Hz is declared
once, in `mission.yaml#met_epoch_provenance.tick_hz`, where the 34,560,000-tick figure comes from —
so a tick rate here that is not the mission's is a claim about a clock the vehicle does not have.
The other two rates are checked as *divisibility* rather than equality: a 100 Hz major cycle inside
a 50 Hz tick is two sub-steps, and a 10 Hz guidance update is one every five ticks, and either one
failing to divide would be a sub-step landing mid-tick.

**And the two limits that were only in a sentence.** `validity_check`'s note says the segment
evaluates six terms and that three are evaluable — pointing is `rcs.attitude_error_deg`, actuator
authority is `rcs.control_authority_margin` — while jerk and keep-out are owed. It said so inside a
block **no tool read**, and `gnc` was the only domain with a components file and *no `open_debts` at
all*, which is how the two obligations appeared in no count and no worklist. They are counted now,
and the third paragraph says explicitly which terms are *not* owed, because a list of what is
missing is more useful when it also says what is not. **251 became 254.**

## The narrowing rule had never run on seven of the eleven domains

A full deletion sweep — every top-level block in the folder, not just the ones I had sampled —
found **seven more** blocks nothing notices losing. Last round's claim that the audit was complete
was wrong, and the sweep is what showed it.

Five of the seven are claims about other files, and they are the folder's own kind of reference:

| reference | points at | resolved by |
|---|---|---|
| `presentation.yaml:contract` | the frozen `docs/diode-contract.md` | nothing |
| `presentation.yaml:contract_probe` | `contract/diode_probe.py` | nothing |
| `coupling.yaml:generated_from` | `docs/deep_research/apollo_diode.md` | nothing |
| `coupling.yaml:provenance_rules` | `simulator-design.md#31` | nothing |
| `mission.yaml:random_seed_provenance` | the seed every stochastic stream is keyed to | nothing |

**All four paths resolve today.** A reference nothing resolves is a reference that is already free
to be wrong, and these are the ones whose target is a *different repository's* file — the probe
lives on the operator's side of the window. They are resolved now by walking up from the vehicle
directory, which also means the check survives the move this folder is destined for.

The sixth is the **conformance table**: twelve rows claiming the twelve numbered checks in
`docs/diode-contract.md` §9. A dropped row leaves a check nobody claims; a duplicate claims one
twice; neither is visible in a table that reads perfectly.

**And the seventh was the substantive one.** `check_profiles` — the rule whose own docstring
records the defect it was written for, `power`'s `tight` profile dropping the bus undervoltage
ladder from 26.5 V to 23.85 V while claiming to narrow it — read:

```python
    alternatives = profiles_doc.get("alternatives") or []
```

Top level. **Four domains put the list there; seven nest it under `profile_selection`.** So the
rule had never been applied to thirteen alternatives across avionics, comms, crew, eclss, gnc, rcs
and structure — including one in `rcs` named **`tight`**, the same name as the one that was wrong.
Every one of the thirteen declares a single scalar `factor`.

Both shapes are read now, and the thirteen split on a distinction worth stating:

- **A wrong direction is refused.** A domain that declares `factors: {below, above}` and gets one
  backwards has made a claim, and the claim is false. Verified by flipping `power.tight`'s `above`
  to 1.4 and its `below` to 0.9 — both refused.
- **A scalar is owed.** It has made *no claim about direction*, and which way a profile moves is its
  author's judgement rather than the linter's: `rcs.conservative` declares `factor: 1.5` for "a
  wider deadband and a *lower* authority floor", and 1.5 is legitimate for a floor being raised and
  forbidden for one being lowered. Refusing here would be a red build for thirteen honest gaps and
  would say nothing about which is which. **254 became 267.**

A gap in the rule itself closed while I was there: it validated only the comparators the domain's
thresholds *use*, so a domain with no `above` thresholds could carry `above: 1.4` in silence until
somebody added a ceiling. Every declared factor is checked for direction now.

## The thirteen profile directions, and the four that scale nothing

Last round surfaced thirteen alternatives that had never been read; this round resolves them. Each
needed a judgement about which way the profile moves its thresholds, and the judgements split in a
way worth recording.

**Nine are tightenings**, and they take `power.tight`'s established convention — tightening a
ceiling means *lowering* it and tightening a floor means *raising* it, so a single number cannot do
both and the pair is mirrored from the scalar the profile already declared:

| domain | profile | was | now |
|---|---|---:|---|
| avionics | `minimal` | 0.5 | `{below: 2.0, above: 0.5}` |
| avionics | `minimal_plus_eng` | 0.75 | `{below: 1.3333, above: 0.75}` |
| comms | `emergency` | 0.15 | `{below: 6.6667, above: 0.15}` |
| comms | `low` | 0.35 | `{below: 2.8571, above: 0.35}` |
| crew | `sensitive` | 0.5 | `{below: 2.0, above: 0.5}` |
| eclss | `conservative` | 0.8 | `{below: 1.25, above: 0.8}` |
| gnc | `conservative` | 0.7 | `{below: 1.4286, above: 0.7}` |
| rcs | `tight` | 0.6 | `{below: 1.6667, above: 0.6}` |
| structure | `strict` | 0.5 | `{below: 2.0, above: 0.5}` |

**Four declare the identity, and each is accurate rather than lazy.** `comms.burst` multiplies a
*rate*, `gnc.radar_aided` weights a *measurement*, `rcs.conservative` widens a controller
*deadband*, `rcs.safe_vector` is a mode whose content is the safe target — none of them moves a
published limit, so a factor that scaled one would be the wrong number.
`rcs.conservative`'s original `factor: 1.5` is the case in point: read as a threshold scale it
would have **raised** a ceiling, which is the `power.tight` defect exactly.

The identity on its own is indistinguishable from a profile somebody scaled by one and never
thought about, so an all-identity `factors` must carry a **`factors_note`** saying what it changes
instead. That is what makes the declaration read rather than decorative. **267 became 254.**

### A check that carried its own provenance rule

Converting them exposed the check's second defect. Eight alternatives were refused for
*"declares no reason"* — and the corpus is entirely consistent about this, it just does not use
that word:

| basis | carries | count |
|---|---|---|
| `chosen` | `reason` | 9 |
| `apollo` | `note` | 4 |
| `historical` | `note` | 4 |

`check_profile_immutability`'s docstring calls D-05 *"the rule"*, and `check_basis` is where the
file's provenance rule lives: a `chosen` value needs a reason, an `apollo` one needs a ref, a
`historical` one needs a source. The profile check demanded a field named `reason` regardless — so
it was **redundant where it was right and wrong where it was not**, and it would have made the
convention impossible to follow. It routes through `check_basis` now, which is strictly stronger.

## One missing datum, counted twice — through a door round 74 did not know about

Round 74 removed an inflation where one missing limit was reported as two debts, because a
comparator with no value has neither `assert` nor `clear`. The same inflation was still in the
vehicle through a different door: **a threshold whose limit *is* another declaration.**

Three `gnc` thresholds name a quantity the domain also owes:

| threshold | says | the quantity | is |
|---|---|---|---|
| `alignment_error_high` | "against the platform's alignment budget" | `imu.alignment_budget_deg` | `UNCONFIGURED` |
| `gyro_bias_growing` | "the largest component magnitude" | `imu.drift_deg_per_h` | `UNCONFIGURED` |
| `radar_out_of_range` | "above the radar's acquisition range" | `landing_radar.range_km` | `UNCONFIGURED` |

Each missing datum was counted **twice** — once where the quantity lives, once where it is used —
and `drift_deg_per_h` was counted *three* times, because a fault's seeding names it as well. The
obligation is one number and it appears three times in a figure whose whole job is to say how much
is left.

The instrument is round 74's, applied across two files instead of two fields: **name the
dependency, so the use is not a second obligation.** A threshold may declare

```yaml
    derives_from: "domains/gnc/components.yaml:components.imu.drift_deg_per_h"
    derives_factor: 0.0002777777777777778
```

and then it reports no debt of its own — the walk skips it — while the obligation stays counted
where the quantity lives. When the source lands, the limit is the source times the factor and the
linter checks it, so the two cannot drift apart the moment the number exists.

**The factor is not decoration**, which is why it is checked: the bias threshold is in deg/s and
the drift it is sized from is in deg/h, so the conversion is 1/3600, and the radar's range is
published in kilometres while the altitude channel is in metres, so it is 1,000. A note is
required beside it, because a limit taken from another declaration is a claim about the
*relationship* — that this threshold is that quantity rather than a multiple of it, and what the
factor converts.

Four checks, each verified by breaking it: a supplied source with the right limit (composes), with
a wrong one (refused), a renamed source (refused — it reads exactly like an unset one), and a
missing note (refused). **254 became 251.**

## A limit that is a property of the registry

Last round gave thresholds a way to say *where* their limit comes from. This round used it on two
more, and one of them turned out to be **derivable all along**.

`sensor_stale`'s own reason says the value *"cannot be a constant and is therefore a function of
the active profile"*, and that each **point** has its own limit of one publish period while this is
the **aggregate's** at twice the slowest. So the number was in the registry the whole time: 0.05 Hz
is the slowest publishing channel — one per 20 s — and twice that is **40,000 ms**. Channels at
`rate_hz: 0` are published on change and have no period to be stale against, which is why the
source is the slowest *positive* rate.

`channels.yaml` has no such key in it, so the linter **computes** the statistic and hands it to the
same resolver every other `derives_from` uses:

```yaml
    derives_from: "channels.yaml:slowest_publish_period_ms"
    derives_factor: 2
```

That is what makes it re-derived on every run rather than a figure somebody typed once — **change a
channel's rate and the threshold is refused until it moves with it**, verified by slowing
`crew.location` from 0.05 Hz to 0.02 Hz and watching the limit refuse as 40,000 against 100,000.

`hga_scan_limit` was the other kind: its note says the value is *"owed with the gimbal's range"*,
and `comms.components.gimbal.range_deg` is itself `UNCONFIGURED` — **the same double count as last
round's three**, so it takes a `derives_from` and stops being counted twice.

### What the value cannot carry, stated rather than implied

The default profile's limit is not the minimal profile's. `avionics.minimal` scales `above`
thresholds by 0.5 — which would **tighten** `sensor_stale` to 20,000 ms, where that profile's own
note says dropping channels raises the slowest period and therefore *loosens* it, "which is the
correct direction". Nothing applies the factors yet, so the interaction is recorded in the
threshold's provenance rather than resolved, and it is a real piece of future work: a limit that is
a function of the *channel set* is not a limit a per-comparator factor can scale.

**251 became 248.** The two thresholds are resolved or de-duplicated; a third obligation of a
different kind survives beside them — *"is latched but its assert and clear values are not both
set"* — which is the correct state for a latched comparator whose terms are now derived rather than
declared.

## One entry, two accounts of what its value is

`uncommanded_acceleration` — `apollo_diode.md:210`'s "uncommanded impulse", and the hole the event
audit found because F-04's first clue is a body rate and this channel is its corroboration — held
**two different statements about what its limit is**:

- `point_units`: *"g, above the largest component the configured thrust can explain"*
- the note: *"The value is owed — it needs a noise floor and the smallest impulse the vehicle can
  produce, and the second of those is the minimum impulse bit that `domains/rcs/` records as
  unpublished"*

Those are different questions, and only the second is genuinely owed. **The ceiling is derivable**
from data the corpus already declares: every engine's thrust and every configuration's mass. The
impulse bit sizes the *detectability* — an accelerometer cannot see a 4.45e-4 N·s pulse, so the
alarm fires on the ceiling while a real uncommanded impulse below it stays invisible to this
channel — and that obligation is already counted where it lives. Naming the two separately is what
stops a limit being owed for a reason that does not apply to it.

### Computed rather than quoted, and why it matters here

The statistic is the **LM ascent engine on the ascent stage alone**: 15,569 N against 4,888 kg,
which is **0.32479 g**. It wins by a hair over the CSM's own 0.32279 g with the LM gone:

| engine | configuration | acceleration |
|---|---|---:|
| APS | `lm_ascent_stage` | **0.32479 g** |
| SPS | `csm_alone` | 0.32279 g |
| DPS | `lm_alone_stage` | 0.28828 g |

**A reader who guessed which of the two it was would be 0.6 % wrong, in the direction that misses
the event** — which is the argument for computing it rather than quoting it, and the reason the
statistic lives beside `slowest_publish_period_ms` in a short, named table of the quantities that
are properties of the corpus rather than keys in a file.

### A tolerance that hid the thing it was checking

The first version of the derivation check copied `rederive`'s one-per-cent tolerance, and weakening
the ascent engine by 3 kN **composed in silence**: the ceiling moves to the CSM's figure, a 0.63 %
change, and 0.63 is less than 1. A derived threshold is not an independent measurement that happens
to agree with its derivation — it *is* the derivation, restated — so the only slack it needs is the
rounding in its own decimal places, and the tolerance is 0.1 % now. The same edit refuses with
*"is 0.3248 and `derives_from` resolves to 0.3227891902281783"*.

**248 became 246.** And a bug in my own plumbing, found the same way: the statistics were merged
*before* the domain files were loaded, so the acceleration ceiling silently computed to nothing —
a computation whose inputs are absent produces no value and reports no error.

## Four verbs left behind by a check that was written for the field

`conflict_domain` says which commands must not both take effect in one tick — the console expands it
and the first valid command in a domain wins. Nothing validated it, and a typo here does not fail:
it makes the command's collisions **silently stop happening**, so two contradictory orders are both
accepted. The console already knew the risk and said so in a comment:

> `conflict_domains` falls back to the literal text ... "a domain that cannot be expanded is a
> domain nothing can collide in, which is worse than a wrong one"

A fallback at runtime is what the linter is for at build time.

The check found four defects, and they are **the residue of the ten the gate check was written
for**:

| verb | declared | its argument is called |
|---|---|---|
| `set_source` | `power.source_<id>` | `source` |
| `set_battery_contactor` | `power.contactor_<id>` | `battery` |
| `set_load` | `power.load_<id>` | `load` |
| `set_breaker` | `power.breaker_<id>` | `breaker` |

Round 43's note records eight verbs writing *"a bare `<id>` where the argument was `antenna`,
`source`, `load`, `breaker`, `battery`, `engine`, `pump`, `hatch` or `loop`"* — the **gate**
templates were fixed and the **conflict domains** beside them were not, because the check was
written for the field rather than for the defect.

**The effect was over-broad rather than under-broad, which is why nothing noticed.** An
unexpandable template falls back to its literal text, so *every* `set_source` shared one conflict
domain no matter which source it named: two commands touching different sources in the same tick
refused each other.

Two rules now. The first segment must be a domain by **either** of its two names — `PREFIX_DOMAIN`
exists so a reference may be qualified by the directory or by the channel prefix, and the corpus
uses both, `res` and `prop` for two domains and the directory name for the other nine (so
`comms.mode` and `comm.mode` are both accepted and `commsz.mode` is not). And every placeholder must
name an **enum argument of its own verb**, which is exactly the rule the gate check above it applies
to the gate template.

## Five fields on the verbs that nothing validated

Round 89's shape was *a check written for one field, leaving its sibling unvalidated*. This round
asked it directly: **mutate every field of a command in turn and see which mutations compose.** Of
the fourteen fields on the fifty-eight verbs, five were unvalidated — and every one of them is
*read* by something, which is what makes them worth checking.

| field | what it decides | what a mutation did |
|---|---|---|
| `execution_class` | whether a command may be held for a due time | `defered` composed |
| `maximum_queue_age_s` | when a deferred command expires | `0` and `-5` composed |
| `allowed_phases` | which phases offer the verb | **absent** composed |
| `help` | the whole of what a fleet is told | emptied composed |
| `gate.kind` | — | **read by nothing at all** |

**`execution_class` is the sharpest.** Three tools compare it against the literal `deferred` —
`plant.py`'s capability snapshot, `console.py`'s `settle`, and `generate_help.py` — so `defered`
does not fail a build. It makes a deferrable verb take effect **in the tick it is accepted**, which
is the difference between a command a fleet can schedule and one it cannot.

**`allowed_phases` absent composed**, and that is not the same as one naming every phase. The
availability check reads the field and a missing list is not a list of all phases — a reader cannot
tell the difference and the linter can. `maximum_queue_age_s: 0` is a deferral that expires before it
is accepted; `-5` is worse.

And `gate.kind` is the session's own refrain once more: declared on all fifty-eight verbs as
`preference`, and **no tool reads it**. D-03's rule is that a gate is an agent-writable preference
and an interlock is service-owned, and the gate's *variable* carries that — so the kind is where a
*second* class would be declared, and naming the set is what stops a third being spelled into
existence one verb at a time.

Six checks, each verified by mutating the field it guards.

## The postures, the transitions and the objectives

Same sweep as last round, on the blocks that decide **what the challenge is**. Six fields composed
under mutation:

| block | field | what a mutation did |
|---|---|---|
| objectives | `kind` | `outcomee` composed |
| objectives | `channels` | emptied composed |
| objectives | `id` | a duplicate composed |
| postures | `terminal` | `maybe` composed |
| transitions | the endpoint names | `safe -> standbye` composed |
| transitions | `requires` | **absent** composed |

The transition strings are **parsed rather than matched**, because two of the five are not a plain
`A -> B`: `execute -> hold on stale evidence` appends the *reason* to the target, and
`any -> aborting` has no source posture at all. So the source must be `any` or a declared posture,
and the target's **first word** must be one — which accepts both real forms and still refuses
`safe -> standbye`, a guard on a transition that can never happen.

**`requires` must be present, and may be empty.** That is not a loophole: `any -> aborting`
declares `requires: {}` and carries the best sentence in the block —

> Abort requires no fresh evidence because requiring it would make the abort conditional on the
> very instrumentation whose failure is a reason to abort.

A present-but-empty list is that decision. An *absent* one is indistinguishable from an oversight —
the same distinction `interlocks: none` draws for a command.

### One rule the check found live, and had to be narrowed for

`channels` may be empty **only** when `evaluated_by` is `external`, and the check found the case
immediately: `coordination` is *"apollo_diode.md:1001-1022's twenty metrics, computed externally and
never shown to the fleet"* and declares no channels at all. The field that makes that legitimate is
`evaluated_by`, which is already a declaration about **who settles it**.

`far_side` is not an exemption, and that is the interesting half. The vehicle contributes channels
to objectives the *operator* verdicts — `crew_survive` says it plainly: the availability state
cannot say "dead", but *"what the vehicle owes it is the record."* So an empty list under
`far_side` is a scoring rule with no observation behind it.

A margin objective now has to declare its **`sense`** as well — `higher_is_better` for a reserve
spent down, `closest_to_limit` for a zone that ran near its band. A margin without a direction is a
number whose good end nobody wrote down.

## A layer declared twice, and only the registry's copy checked

`layer` is what the contract asks about a channel — *"how much should I trust this"* — and it is
declared **three times** for the same channel: in `channels.yaml`, again in the domain's
`points.yaml`, and a third time as a key in `presentation.yaml#epistemic_layers.mapping`.

The registry validates its own. A channel whose layer is outside `{measurement, estimate, service}`
is refused, and so is one with no layer at all. The domain's copy was compared against **nothing** —
so `layer: servicee` composed, and so did a point claiming `measurement` for a channel the registry
calls `service`.

That is round 79's `mass_kg` exactly — one quantity declared twice with only one of the two read —
except that here it is the **copy** that is unchecked, which is the worse direction. What a
disagreement decides is not cosmetic, and `presentation.yaml` says so:

> a service state is layer A with a different subject, not a fourth layer ... it decides whether a
> commanded valve position carries a quality code

A domain that marked a service channel as a measurement would put a `SUSPECT` code on a statement of
what the vehicle *did*, and a fleet would go looking for a sensor fault in `rcs.mode`.

**All 142 points agree today** — which is the condition under which the 143rd is added wrong, and
the whole reason the join is worth having.

The mapping's keys are the third declaration, and they are checked against the registry as well: a
layer with no mapping is a kind of channel the contract has no layer for, and a mapping for a layer
no channel declares is a decision about nothing. Both directions are refused.

## The adversary's ids and its rates

Two more fields on the 128 faults that composed under mutation.

**Fault ids are unique across the whole vehicle, because they key the randomness.** `faults.py`
derives every stochastic stream from `(master_seed, domain, component_id, purpose)` and `--check`
asserts that name-keying holds — *"no existing fault's events moved"* is the property that makes a
run reproducible when a fault is added. Two faults sharing an id would take the **same stream**:
their draws would be the same numbers in the same order, and a vehicle with two faults would behave
like a vehicle with one of them applied twice.

Nothing compared the ids, and the uniqueness has to hold **across** domains — a per-domain check
would miss the case where `avionics` and `gnc` both name a fault `F-04`. All 128 are distinct today,
and this is the one list the folder expects to grow: adding a fault is the edit that happens most.

**`seeding.unit` is a one-word vocabulary declared 66 times and validated nowhere.** It is the
rate's *time basis*, and `faults.py` scales the hazard by it across the mission's 192-hour ladder —
so `per_hour`, which reads like `per_h`, is a silent change of rate rather than a refusal.

## Every discrete state now says what moves it

Forty-three states declared their vocabulary (`unit`), their guard (`hysteresis` or `dwell`), their
irreversibility (`one_way`, `requires_arm`) — and **not what changes them**. The gap was not
academic and the corpus said so in as many words: `domains/crew/` carries a debt reading

> Nothing declares what moves the crew. `crew_location` can take `surface_eva` and
> `crew_availability` can take `suit`, and no verb writes either.

That debt was the **only** place the question was asked, and it was asked about two states out of
forty-three.

### It is not derivable, which is worth recording because I tried

Two mechanical rules, and both fail:

- A verb's `gate` or `conflict_domain` names the state in **three** cases out of forty-three.
- Overlapping enum *values* are actively misleading. `bus_tie_closed` shares `open`/`closed` with
  `set_hatch_valve`, so the rule would have the hatch moving the bus tie. Ten states matched on
  values and most of the matches were coincidence.

So the declaration is an author's judgement and the linter's job is to check it rather than to
invent it.

### Three movers

| mover | count | what it is |
|---|---:|---|
| `command:<verb>` | 15 | a verb that writes it |
| `event:<id>` | 6 | a declared `one_way_event` |
| `logic:<reason>` | 22 | the vehicle's own machinery — FDIR, the undervoltage ladder, a geometric occultation |

A `command:` mover **may be another domain's verb**, and three are: the crew's breaker panel is
written by `power`'s `set_breaker`, `maneuver_state` by `propulsion`'s `load_burn`, and
`lcl_tripped` by `avionics`'s `reset_latched_fault`. A command is an effect on the executive rather
than a call between domains, so the state it moves need not be its own domain's.

### The rule that draws the line, and the three classifications it corrected

**A `command:` mover has to be able to say the value it is said to set.** `request_imu_alignment`
takes reference frames (`EARTH_J2000`, `star`, …) and `imu_alignment` takes
`unaligned/aligning/aligned/drifted` — the verb *starts* an alignment the vehicle then drives. That
is a trigger, not a setter, so the mover is `logic` with the verb named in the reason. The same held
for `arm_event` (it mints a token whose lifecycle the state follows) and `set_docking_latch` (it
commands a mechanism that reports for itself).

**Three of my own classifications failed that rule and had to be rewritten** — which is the check
doing the work the mechanical rules could not.

Two states still owe it and are counted: `crew_location` and `crew_availability`, the two the crew
debt names. **246 became 248** — the forty-one declarations retired their own debts and the two
owed ones were added.

**And the build order moved with them, in a way worth stating.** `plant.py --build-order` now reads
*69 owe a rule* where it read 71, and *26 owe a value* where it read 24: the two `UNCONFIGURED`
movers put an unset scalar in those states' specs, so `walk_unset` counts them as owed values rather
than as the domain code they still need. The code has not gone away. It is the honest consequence of
one field meaning a configuration obligation and the plant's classifier asking only whether a
scalar is set — and it is the sort of thing a reader should be told rather than left to notice.

## The worklist was sending an implementer to the wrong file

`plant.py --build-order` opens by promising that its classes *are* `advance()`'s own refusal order —
*"so this cannot disagree with what the plant actually does when it gets there."* It ran two tests
the plant does not, and **sixteen of the twenty-eight states it reported as owing an edge were not
owed one.**

**A tank filled at the pad has no incoming edge on purpose**, and `advance` exempts it by name:

> without this exemption the plant refuses `water_cooling`, `o2_lm`, `prop_rcs` and
> `pressurant_he` — four stocks that are correctly declared and simply drain

The classifier did not have the exemption. So `o2_csm_kg`, `o2_lm_kg` and `h2_csm_kg` were reported
as owing a driver they do not need and cannot have — while all of their outbound drains classify
cleanly. They were telling an implementer to go and build three edges that **must not exist**. They
are `ready`, and one of them genuinely ticks:

```
advance(o2_lm_kg) -> {'o2_lm': 23.1, ...}      # 24.1 kg, one second of declared drain
```

**And a state on the `internal` sentinel cannot be reached by any edge**, because the sentinel is
not a node — `coupling.yaml#nodes` does not declare it and nothing can target it. So "no incoming
edge" is not a gap in the graph for those states; it is what the sentinel *means*, and the thing
they need is the domain code that advances them. Thirteen were sent to the wrong file.

| bucket | was | is |
|---|---:|---:|
| ready | 11 | **14** |
| owes a value | 26 | 26 |
| owes an edge | 28 | **12** |
| owes a rule | 69 | **82** |

The worklist is the folder's answer to *"what do I implement first"*, and it is derived rather than
authored for exactly this reason — but a derived list is only as good as the classifier's agreement
with the thing it describes. **The remaining twelve are real**: eight whose inbound coupling carries
no sensitivity and four that genuinely have no driver.

## "Cannot disagree" was the wrong claim about a sound classifier

Round 95 fixed a real defect: `--build-order` was sending implementers to the wrong file for sixteen
states. But it fixed two *instances*, and the sentence above the classifier still promised more than
anything can deliver —

> The classes are `advance()`'s own refusal order, so this cannot disagree with what the plant
> actually does when it gets there.

**They do disagree, thirty-six times.** Run `advance` over every state with a permissive value map —
every node supplied a number, so a refusal is about *structure* rather than about a value missing
somewhere else — and thirty-six states land in a different bucket. The useful result is that **not
one of them is unexplained**:

| kind | count | what it is |
|---|---:|---|
| the worklist counting more | 23 | `advance` refuses on the parameter its *method* needs and nothing else; `build_order` walks the whole spec, so an unset `initial` or `moved_by` or `basis` is reported as an owed value even where the plant would advance the state from a number it was handed |
| the `internal` sentinel | 13 | `build_order` routes it to `rule`; `advance` calls it a missing edge. No edge can reach the sentinel, so its driver is domain code |

So the classifier is **sound** and the sentence was wrong. The two answer different questions on
purpose: `advance` asks *"can I compute this right now"*, and the worklist asks *"what is missing
from the definition"*. The first kind is the worklist being deliberately stricter, which is the
honest direction for a list whose whole job is to name what is left.

What the classification must not do is **send a reader somewhere the answer is not** — and that,
not agreement with `advance`, is the property worth holding. It is what round 95 fixed, and it is
now pinned by a test that fails on any disagreement which is not one of the two kinds above.

## Five states are in a declared order that nothing advances

`radiator_reject` holds two states and its `state_order` puts `zone_radiator_t` **first** — the
round-71 fix, so that rejection (`epsilon x sigma x A x T^4`) is computed from *this* tick's
temperature rather than last tick's. But both of the node's inbound edges, `E-ENV-RAD` and
`E-WATER-RAD`, declare `advances: radiator_rejection_w`. **The state the order puts first is the one
state on the node that nothing advances**, and the order describes a sequence whose first step never
runs.

The existing check verified that an order names the node's states, and that a multi-state node has
one. **Nothing verified that the order can execute** — which is the round-71 `zone_csm_cabin_t`
finding generalised: a state on a node is not driven because an edge *reaches* the node, it is
driven because an edge **says it advances it**.

Five nodes are in that position, and the check that finds them had to be narrowed twice before it
was right:

- `alert_state` and `structure_config` hold states moved by the vehicle's own logic and by
  irreversible events — demanding an edge for those would demand the graph model a mechanism it
  does not describe. So the rule is `advance`'s own: a `lag`, `stock`, `delay` or `dynamics` state
  needs a driver unless it is a tank filled at the pad.
- `E-WATER-RAD` is a **back-edge**, and neither the linter nor the plant counts a back-edge as a
  driver. Excluding them is what leaves the radiator on the list at all — my first version of the
  *test* counted it and found nothing wrong.

| node | what the order puts first | resolved? |
|---|---|---|
| `radiator_reject` | `zone_radiator_t` | **fixed** — both heat inputs now advance it |
| `vehicle_dynamics` | `body_rate` | fine — it *is* advanced, and `attitude` follows it |
| `fuel_cell` | `fuel_cell_power_w` | fine — advanced, and `source_converter_v` follows it |
| `coolant_flow` | `pump_1_speed_rpm` | **still owed** — its driver is a command, not an edge |
| `crew_state` | `crew_location` | the crew debt already names it |

**The check was too broad and is now narrowed to the *head* of each order**, which is the state
that has no predecessor to be derived from. A state that comes after another on the same node may
be derived from it — that is what an intra-node order is *for* — and demanding an edge for those
would demand the graph model a derivation the order already declares. `source_converter_v` follows
`fuel_cell_power_w` exactly that way, and `attitude` follows `body_rate`.

**And my reading of `vehicle_dynamics` was wrong in the previous round's message**: I read the
check's *sorted* producer list as the order. The declared order is
`[body_rate, attitude, orbital_state]`, and `body_rate` is advanced by `E-RCS-DYN` — torque to rate.
The order was right and the report was describing it backwards.

Each is a **debt rather than a refusal**, because closing one is a decision about *which* edge
advances which state and that decision is the author's: `E-ENV-RAD` reaching the radiator could
advance the temperature or the rejection, and the node's own order says the temperature comes first.
**248 became 253.**

## The weakest-documented edge in the file

`E-TRANSPORT-PLATE` carries the coolant loop's temperature to the avionics coldplate, unit
`K per K` — and it declared **nothing else**. `value: UNCONFIGURED`, `basis: UNCONFIGURED`, `at:
nominal`. No relation, no note, no `computation`. Nothing else in `coupling.yaml` is that bare.

**A transport delay delivers what it received.** `loop_transport_t` is a `delay` of 1,042 s
implemented as a tick-indexed ring buffer, and a ring buffer's output is its input one residence
time later — so the ratio is 1.0, and the edge carries the temperature rather than transforming it.
A ratio other than one would be a claim that the loop changes the temperature it transports, and
the mechanism that does that is the conductance into the plate, not this edge.

It survived for a reason worth recording. `E-DYN-GEOM`'s note says a `UNCONFIGURED` value beside an
identity relation *"is a contradiction rather than an omission"* — and **that contradiction is what
got it fixed**. This one had no relation to contradict. An edge with nothing written beside it is
not obviously wrong; it is only unread, and nothing had read it.

**And a fifth instance of one unknown under several names.** `E-FC-HEAT` (`W per W`, the fuel cell's
waste heat) is owed *with the cell's conversion*, not on its own: waste heat is the difference
between what the reactants carry in and what the cell delivers, so it and
`fc_o2_draw_kg_s.per_joule_kg` are two consequences of one unknown — the electrochemistry no source
publishes. **It is the first time the two names have been a heat rate and a mass rate**, which is
why they do not look like the same quantity.

**249 became 248.** The other edges still owed are genuine: `E-RCS-DYN` needs the inertia tensor,
`E-PUMP-COOL` and `E-RAD-THERM` are thermal constants `thermal_diode.md:965` refuses to publish,
`E-PLATE-CRITICAL` is a derating slope, and `E-FC-HEAT` waits on the cell.

## The state the plant stops at has nothing to compute from

`--readiness` ends with the same sentence every time: the plant stops at the first thing it cannot
compute, and that thing is `bus_b_v`. The worklist called it a **code** debt — an `algebraic` state,
so domain code — and an implementer sent to write its rule would find **nothing to read**.

`bus_b` has **no inbound edge anywhere in `coupling.yaml`**. Every electrical edge terminates on
`bus_a`, and the tie runs `bus_b -> bus_tie -> bus_a` — so bus B is declared as a *source* for bus A
while `bus_b_v`'s own note calls it *"second bus, cross-supported through the tie."*

And the bus is real in every file except the one that says what feeds it:

| where | what it says |
|---|---|
| `domains/power/components.yaml#loads` | **seven loads** on `csm_bus_b` — the suit fan, both coolant pumps, the heaters, the lighting |
| `domains/power/profiles.yaml` | a `bus_b_undervoltage` threshold |
| `domains/power/points.yaml` | the channel `power.dc_bus_b_v` |
| `domains/power/fault_policy.yaml` | three faults perturb it, one of them *to* `csm_bus_b` |
| `domains/crew/components.yaml` | it is on a crew panel |

`check_power_inventory` says the quiet part out loud, and its own comment is why nothing caught this:

> which bus a source feeds is a routing decision the tie makes **and the file does not state**, so
> the honest bound is the vehicle's own total against the capacity presenting to it

**The classification is what round 100 fixed.** `build_order`'s no-input test named four methods —
`lag`, `stock`, `delay`, `dynamics` — on the reasoning that an `algebraic` state is domain code. But
the question a worklist answers is *what is missing from the definition*, and for a state with no
inbound edge, no `preloaded` exemption and no sibling on its node, the answer is an **input**.

The rule now covers `algebraic` too, with a **computation as the distinguisher** — and the corpus
draws that line itself. `cabin_heat_csm_w` carries `total_w: 733` with a `computation` summing the
loads `heat_inputs` assigns, so its inputs are declared outside the graph and it needs no edge.
`bus_b_v` carries nothing; its own reason says it is *"the same nodal solve over a different source
and load set"*, and the source set is the part that does not exist.

**Exactly one state moved**: `bus_b_v` from *owes a rule* to *owes an edge*. The worklist now says
what the state actually lacks, and `bus_b_v`'s provenance names the gap for whoever closes it —
declaring the tie's other direction, or giving bus B its own sources, is a modelling decision rather
than a missing number.

## Bus B has a source, and the plant's first stop moved

Round 100 found that `bus_b` had no inbound edge anywhere in the graph — and that `bus_b_v` is the
state the plant stops at. The decision the round left open is settled by the spec, not by me.
`electrical_diode.md:16` specifies a

> **dual 28 VDC A/B bus architecture** with **two independent generation/source channels**, dual
> battery/storage channels, source and battery converters, a normally open or conditionally enabled
> bus tie

and the block diagram at `:198-232` is symmetric:

```
GEN_A -> SRC_CONV_A -> BUS_A        BAT_A <-> BAT_CONV_A <-> BUS_A
GEN_B -> SRC_CONV_B -> BUS_B        BAT_B <-> BAT_CONV_B <-> BUS_B
BUS_A <-> TIE_AB <-> BUS_B          normally open, 80 A, reverse-current capable
```

**Each bus has its own generation channel and its own battery, and the tie is normally open.**
`bus_b_v`'s note said it was *"cross-supported through the tie"*, which reads the **backup** path as
the supply — the tie is a cross-support, not a feed.

`E-FC-BUSB` now gives bus B its generation channel, mirroring `E-FC-BUS` for bus A. **And the
plant's first stop moved**: from `bus_b_v`, which had nothing to compute from, to
`cabin_heat_csm_w` — a state that genuinely needs domain code. `bus_b_v` is back in *owes a rule*
with the rest of the `algebraic` states, and the round-100 third class of worklist divergence is
now empty, with the assertion kept so it cannot reappear unnoticed.

### The battery half is still missing, and is named rather than guessed

The diagram has `BAT_B <-> BAT_CONV_B <-> BUS_B` beside `BAT_A <-> BAT_CONV_A <-> BUS_A`. The model
has **one** `battery_energy` node with `E-BAT-BUS` running to `bus_a` only. A second edge from that
one node would model a *shared* battery presenting to two buses — a different claim from the two
independent strings the spec describes, and the wrong one to make silently. So it is recorded in
`bus_b_v`'s provenance for whoever closes it.

The same limit runs through the generation side: one `fuel_cell` node stands for all three modules,
so the two channels' **independence** — the property the spec names first — is not expressible at
this granularity. The edge says bus B is fed; it cannot say the two feeds fail separately.

## The LM's CO2 average was reading the CSM's cabin

`check_cabin_pairing` asks whether an ECLSS channel about a cabin has an `lm_` twin — and until round
102 **that was the whole of the pairing rule.** `eclss.lm_co2_pp_1h_avg_mmhg` had a twin. It also
had:

```yaml
    from: csm_cabin_co2_kg
    inputs: [eclss.co2_pp_mmhg]
```

— the **CSM's** cabin, and a note verbatim identical to the CSM channel's. The pair was paired by
**id** and unpaired in fact.

During `descent`, `surface` and `ascent_rendezvous` — **27.5 hours with the crew in the LM and the
CSM empty** — a fleet watching that channel against the 3 mmHg one-hour limit would have been
watching the compartment nobody was in. On the channel the corpus's own note calls *"the channel
Apollo 13's crisis turned on."*

**The comparison is extended to the state each one reads**: a channel's twin must read a state in
the other compartment, and the compartments are the coupling nodes. It found **exactly one**
instance, and both directions refuse — reverting the fix, or pointing the CSM channel at the LM.

Two smaller things the check taught me while being written. A point's `from` names **a state in its
domain or a coupling node**, so a name that is not a state *is* the node; my first version treated
the two kinds differently and silently skipped every point that reads a node, which is most of the
consumables ledger. And the pairing loop needed the domains' points threaded in, which is why
`check_cabin_pairing` now takes a `root`.

## Authoring convention: no flow mappings

Every file here is **block form**, and that is a decision with a history. The definition was
originally written with flow mappings — `- {id: ..., provenance: {...}}` — because a component's
fields are short and a block form costs lines. That cost two things and they compound:

- The correct number of closing braces depends on whether the *enclosing* item is itself a flow
  mapping, so `provenance: {…}` needs one brace in a block item and two in a flow item. Both
  compile in a reader's head; only one compiles in a parser, and the error points at the *next*
  item.
- A template channel name inside a flow sequence — `res.recon_[resource]_kg` — is a syntax error
  and has to be quoted, which no reader notices and no reader remembers.

Both are mechanical, and both are silent until something downstream refuses. One repair round
turned them into a much worse fault: indentation lost in a *parseable* file, where 76 parent/child
relationships were flattened and several linter checks quietly stopped running while the linter
still reported COMPOSES. A parse error is a gift by comparison.

So: block mappings, always. A `provenance` is written as a block, never as `{…}`. It costs about
a third more lines in the files where components are dense, and it removes a whole class of fault
that produced a worse failure than the one it caused.

`domains/avionics/` is the ninth, and it is the domain that attacks the experiment rather than the
mission. `corpus-review.md` §1 dismisses the document it comes from in one line — "an aircraft
avionics ICD template: ARINC 429/664, GNSS, elevons, DO-178C" — and the dismissal of the *vehicle*
is right (C-15 prunes the GNSS and the aerosurfaces exactly as it pruned reaction wheels). The
dismissal of the **functions** was too quick. There are nine of them at `avionics_diode.md:371-509`
and four close gaps nothing else in sixteen thousand lines closes:

- **A quality-assignment function with the right signature.** `simulator-design.md:496-508`
  requires quality to be assigned by a function that cannot see the simulator's fault state, and
  `corpus-review.md` §6 lists it among what nobody wrote. `sensor_health` takes
  `(sample, now_mono)` and reads four properties of the *sample* — finite, in range, fresh, not
  bit-failed. It is the only concrete function in the corpus that satisfies the clause, and the
  linter now enforces both ways to break it: read a fault-state parameter, or emit a code that is
  not a quality. The second is the subtle one, because the document's own function returns `FAULT`
  from the same branch that returns `SUSPECT`, and `FAULT` is refused as a quality by
  `design.md:211-214`.
- **A CUSUM drift detector, which is the vehicle's only sub-precision instrument.**
  `plant.md` §4 states the requirement in its strongest form: the nominal cabin leak is
  0.023 kg/h, *below* `eclss.leak_rate_g_s`'s own 0.01 g/s quantum, and it "must still happen".
  No threshold can see that and no rate-of-change test can either, because each sample is smaller
  than the resolution of the instrument producing it. A cumulative sum of (sample − expectation)
  against an allowance accumulates what a threshold cannot, and it is now a published statistic
  with four monitored groups — cabin leak, O₂ ledger, RCS ledger, bus source.
- **A command-authorization chain with an ordering argument.** Ten `require` clauses in a
  deliberate order, and the last two apply only to irreversible actions: a valid prepare token,
  and **a synchronised clock** — `require(vehicle.time_quality == "SYNC", "TIME_UNTRUSTED")`. That
  term is not in `mission_diode.md`'s eight-term predicate and it is not in any domain's registry.
  Its reason is specific: an irreversible action with a time-tagged deadline has that deadline
  measured against the vehicle's clock, so a drifted clock is a one-shot with an unknown arming
  window. It is now `requires_time_sync`, required by the linter on every irreversible verb.
- **A redundant-vote rule that stops voting.** Below two healthy members the vote returns
  `degraded` rather than publishing a median of one, and `select_sensor` is refused while a
  disagreement stands — `apollo_diode.md:333`'s "a single discrepant switch must not condemn a
  thruster" generalised from pressure switches to every redundant group on the vehicle.

Thirteen states, fourteen points, eleven thresholds, four verbs, eleven faults, and the narrowest
verb surface on the vehicle — because this is the one domain whose commands change *what the fleet
can see*. The three refusals at the bottom of its `commands.yaml` are therefore the most important
entries in it: `set_sensor_good`, `override_stale_limit` and `clear_drift_monitor` are each a way
for a command team to edit its own evidence, and nothing downstream could detect any of them,
because the thing that would detect it is the thing being edited.

The domain also **closed a debt the vehicle had been carrying**: `mission_diode.md:380-383` wants
`health.overall` and `health.confidence` as guard terms, and `apollo_diode.md:184-186` answers it
in terms — "it is therefore historically faithful to give your agents heterogeneous observations
rather than a synthesized single health score." So the aggregate is refused on three grounds
rather than left open: apollo declines it, `design.md` §8 refuses to publish diagnoses, and it is
not derivable from the published channels without a weighting nobody could disagree with.

`domains/comms/` is the tenth, and it is the one domain whose subject is **the fleet's own
ability to observe**. `apollo_diode.md:901` is the sentence it is built around: "when
communications degrade, the simulator should actually have to choose which data arrives. Do not
merely set `comm.degraded=true` while delivering every telemetry field normally." Every other
domain models a quantity; this one models a constraint on observation, and it is the only place
in the vehicle where the experiment's epistemology has a bandwidth. Ten states, ten thresholds,
five verbs, eleven faults, and four things worth reading:

- **The blackout is derived, planned, and deliberately not alarmed.** The Moon occults the Earth
  whenever the vehicle is within `arcsin(R_moon / r_orbit)` of the anti-Earth direction: a
  71.0-degree half-angle at a 100 km orbit, 39.5 % of each 117.8-minute revolution, **46.5
  minutes**. Apollo's loss-of-signal was about 45 minutes, and the difference is the eccentricity
  and the Earth's own disc — so the derivation is right to within the effects it omits, which is
  a stronger statement than a citation would be. Three thresholds carry
  `gated_by: "comm.blackout_state is clear"`, because an alarm that fires every orbit is an alarm
  a fleet learns to dismiss — and F-11's third order ("telemetry gaps → controllers lose evidence
  during another anomaly") is what that habit costs.
- **A quadratic antenna pattern, and a latent defect it exposed.** `vehicle.yaml` carried
  `beamwidth_deg: 1.0` beside `gain_db: 26.7` and those two numbers cannot both be true: any
  aperture obeys `G = 41253/θ²`, so 26.7 dB is a **9.4-degree** beam and a 1-degree beam would
  need 46 dB. The error was invisible until this domain landed because the beamwidth is what the
  pointing channel and `E-GEOM-LINK` scale against — and the *corrected* figure makes apollo's
  own threshold coherent: `:163` calls 0–2° a good link, and at 2° off a 9.4° beam the pattern is
  0.54 dB down, whereas against a 1° beam it would be 48 dB down, i.e. a "good link" range in
  which no link exists.
- **The amplifier is a load, not just a transmitter.** `set_power_amplifier` adds 36 W to the bus
  and quadruples the RF output, and `apollo_diode.md:1140`/`:1158` are explicit that it is the
  *trigger* of the degraded chain and never its root fault — it does not fail, it exposes. That
  makes it the most consequential non-actuator verb on the vehicle, and its interlock list mixes
  two electrical guards with a *thermal* one and a transmitter-draw one, because the amplifier is
  a member of the cycle it can start.
- **Bandwidth as a resource the fleet spends.** Five telemetry profiles, apollo's own, from
  `emergency` — event and alarm channels plus essential power, ECLSS and GNC, and no engineering
  channels at all — to `burst`, which is temporary and expensive. `set_telemetry_profile` is the
  vehicle's only verb whose effect is on the fleet's own observation, bounded by an operator
  ceiling and by `duration_s`, and `comm.recorder_fill_pct` is where its cost becomes visible: a
  blackout is a telemetry *delay* until the recorder fills, and then it is a gap.

Three coupling edges closed with it. `E-AMP-LOAD` at **0.0357 A per watt** (the transmitter's DC
input is the load, so the sensitivity is `1/V_bus`), `E-GEOM-LINK` at **−0.54 dB per degree** at
the 2° operating point from the quadratic pattern, and `E-DYN-GEOM` — which had been declared
`UNCONFIGURED` beside a relation saying the coupling is the signal itself, a contradiction rather
than a deferral, since an identity relation has a value and it is 1.0. The fix makes the useful
distinction visible: the edge is the geometry, and `comm.hga_pointing_error_deg` is the residual
after the gimbal compensates.

`domains/gnc/` is the eleventh and last, and it is the domain that had to be pruned hardest
before anything could be kept. C-15 is blunt: `gnc_diode.md` is written for a different vehicle
class. Gone are the reaction wheels, CMGs, magnetorquers, momentum dump, GNSS, LiDAR,
terrain-relative navigation and aerosurfaces — and with them the QP control allocator and the
quaternion feedback law, both of which are `domains/rcs/`'s on a vehicle whose attitude is RCS
only. What survives is the **navigation** half, and it is real mathematics: an error-state EKF
with a Joseph-form covariance update, a fifteen-state error vector, a quintic trajectory segment
with exact coefficients, and two state machines. Twelve states, seven thresholds, five verbs,
eleven faults.

Four things in it are worth reading:

- **Four conventions, which are the document's most valuable export.** `gnc_diode.md:382` states
  the rule — "no vector is valid without a declared frame" — and goes further, fixing four things
  that are *conventions* rather than quantities: quaternion component order `[w,x,y,z]`, the
  rotation direction `attitude_ref_from_body`, the frame an angular velocity is resolved in, and
  covariance units per block. `apollo_diode.md:117` publishes `gnc.attitude_q[0..3]` and fixes
  none of them. Two agents can disagree about the component order while both being right about
  the physics, and nothing downstream reports the disagreement — the attitude is simply wrong, in
  a way that looks like a control problem. So the four are declared once in
  `vehicle.yaml#conventions`, and the frame registry gained the fields that make them bindable:
  `central_body`, `rotating_or_inertial`, `orientation_parent`.
- **A ladder, because a sigma is not a state.** `gnc.nav_position_sigma_m` has been a registered
  channel since the registry was written and it cannot say whether the vehicle is navigating,
  coasting on gyros, or lost — three different vehicles with three different correct responses.
  `gnc_diode.md:666-691` supplies the missing ladder, and its own summary is the best sentence in
  the document: **"a high covariance can therefore be an operational state transition, not merely
  a number in telemetry."** `coasting` earns its place: no absolute update, the attitude right,
  the rates right, the sigma growing slowly, and `gnc.coast_elapsed_s` as the only thing that says
  how much time is left. Apollo 13 flew a large part of a mission on a platform that had not been
  aligned since a burn, and elapsed time was the fact that governed what the crew could still do.
- **`load_state_vector` is not the missing artifact.** The vehicle's largest gap is that
  `mission.yaml#initial_state` declares the true state UNCONFIGURED — the published geometry does
  not close as an Earth-centred conic, and a plausible ellipse would put the vehicle on a
  trajectory that falls back. There is a verb called `load_state_vector`, and it is tempting to
  conclude the verb is the fix. It is not: **it loads the navigation solution, and a plant whose
  truth is unset cannot be started by a fleet calling anything.** The debt is the plant's and the
  verb is the fleet's, and conflating them would make position assertable — which is the single
  most valuable thing the window withholds. The verb's help text says so out loud, because a
  fleet will otherwise reason exactly as far as the name.
- **The 100 Hz question, answered rather than re-litigated.** `gnc_diode.md:1054` runs a 100 Hz
  major cycle with a 10 Hz guidance update inside it; C-07 keeps the plant at 50 Hz. The
  disposition is `vehicle.yaml:127`'s — "a GNC inner loop is sub-stepped rather than the plant
  re-rated" — and the distinction that makes it honest is `rcs_dode.md:189`'s: **sample time is
  not publish time.** The propagation genuinely runs at 100 Hz through the event queue; the
  measurement updates cannot, because they are bounded by the sensors. A 100 Hz filter reading a
  50 Hz IMU has fifty measurements per second and a hundred propagations.

Three coupling facts came with it. `E-RCS-DYN` and `E-ENG-DYN` now have a producer for the state
they act on. `gnc_diode.md:1175-1177`'s trajectory — the corpus's only concrete one, placing the
vehicle 14.1 km *below* the lunar mean radius — is discarded per C-23 and recorded as GNC-08, the
one fault in the vehicle whose cause is a missing configuration item rather than a failure. And
`domains/avionics/`'s `innovation_window`, declared and unadvanceable since it landed, can now be
advanced: the two domains share one innovation test, which is the right number of them.

**With the eleventh domain in, three defects the last round's binding check had been waiting for
surfaced immediately.** Generalising the state/channel enum binding to every domain found that
`eclss.cabin_regulator_state` published three of the regulator's four positions — so a *closed*
regulator, a cabin isolated from its supply, which is the configuration Apollo 13 flew, was a
state the vehicle could be in and could not report — and that apollo's single `stage_state`
channel covers a fact two machines produce, with `abandoned` (the descent stage left on the
surface) belonging to neither of the ones it was pointed at. `domains/structure/` gained a
`configuration` state and an `arm_state` state, the latter being the arm/commit pattern's own
state that five `one_way_events` entries had been implying and nothing held.

`presentation.yaml` is not a domain and not a plant artifact: it is **the vehicle's side of the
frozen window**, and until it existed nothing played that role. `channels.yaml` declares 145 points
and the domain registries declare 58 verbs, and not one of them said which points appear in
`state.json`, which appear in the telemetry ring, what shape a frame has, or how a verb becomes a
line in `HELP.md`. `docs/diode-contract.md` is frozen, so a vehicle that has never been introduced
to it is a vehicle that may satisfy nothing of it.

Two gaps were concrete enough to name before the file was written. §3 requires that **every verb's
gate variable appears in `HELP.md` and `state.json`**, and §9's check 5 turns that into a
conformance test — every verb here declares a `gate.variable` and nothing collected them into a
published set. And §5.1 says "every published field is one of three things" (A, I or T) while the
registry declares **four** kinds, which is a question about how a commanded valve position is
labelled to a fleet that must not mistake it for a measurement.

Four things in the file are worth reading:

- **The four-kinds/three-layers resolution.** `measurement → A`, `estimate → I`, and
  `service → A` — and the last is the argument rather than a convenience. The contract defines A as
  "authoritative *about the report*, not about the world… may be wrong, drifting, saturated, stuck".
  A commanded valve position is authoritative about the report in exactly that sense, and it is not
  a claim about the world because the valve may not have moved. So a service state is **an A with a
  different subject, not a fourth layer** — and the consequence is operational: a service channel
  carries GOOD only, because a quality code says how much to trust a *reading* and a fleet that saw
  `SUSPECT` on `rcs.mode` would go looking for a sensor fault in a mode enum.
- **The mirror's bound is a rule, not a style.** `state.json` is rewritten every cycle, so its
  membership is a cost paid at the ring's cadence. The rule is "the vehicle's own software state" —
  `layer: service` at P0/P1 — which derives **19 channels**, with a declared ceiling of 24 so that
  adding a twentieth is a decision with a number attached. No measurements and no estimates: a
  reading belongs in the ring, where a fleet can see what it did over time, and a mirror carrying
  readings would give one current number and no history.
- **The ring's cadence classes, and what a frame actually contains.** 21 channels at ≥ 5 Hz, 85 at
  1–5, 8 at 0.5–1, 27 slower, and 4 with no period at all. The rule is that **a frame carries the
  values whose period has elapsed**, so the `values` map's *membership* is itself information —
  and a fleet that reads a frame as a complete snapshot will treat an absent slow channel as a
  dropout. That is F-14's failure mode arriving from the other side: a frozen value and a slow rate
  look the same.
- **`HELP.md` is generated, and generating it found a falsehood.** §8 makes `HELP.md` the only place a verb name may appear, which means a verb the documentation misses is a verb no fleet can discover. `tools/generate_help.py` emits it from the registries, and its first run listed `set_telemetry_profile` under *"verbs this vehicle does not have"* — while `domains/comms/` implements it. A domain declining a verb means **"not mine"**, not "not this vehicle's", and for this one verb the two readings come apart. It now has a heading of its own, and a test holds the partition.
- **No hidden verbs, and why that is not an omission.** §3 permits a hidden verb only if it is
  **inert** — no egress, no spend, no state change — because a hidden verb bypasses gate evaluation
  by construction. This vehicle has no inert verbs: all 58 do something, and a verb that did
  nothing would be a name in `HELP.md` that costs a fleet a command cycle to discover is empty.
  What the vehicle hides instead is *channels*, in each domain's `points.yaml#not_published`, and
  hiding a channel is safe in a way that hiding a verb is not.

The file also carries a **conformance table**: for each of `diode_probe.py`'s twelve checks, whether
the vehicle's own configuration satisfies it, shares it with the executive, or cannot influence it.
Five are satisfied by configuration — the closed vocabulary, the published gate variables, the
mirror that is not read back, the deferred re-check, and telemetry that advances on its own; two
are shared; four are the far side's; and one is vacuous, which is check 6, because this vehicle has
no hidden verbs for it to test. That table is the closest thing this folder has to a claim about the
implementation, and it is deliberately a column of *reasons* rather than a checklist of ticks.

**Deriving the tick order found four structural defects and one contract error**, and the
sequence is worth recording because each finding came from the one before it.

`plant.md:71` promises a deliverable: *"Topological order is only partial, so the linter emits a
total order and the scheduler obeys it."* Nothing emitted one. Writing it was the point, and what
it found was not what it was looking for.

- **The flagship cycle had lost its back-edge, and two cycles had disappeared.** The `cycles:` block
  in `coupling.yaml` had three entries whose `- id:` lines were indented at the same depth as the
  content of the preceding `note:` block scalar — the same absorbing failure that took six declined
  verbs in the commands files, at larger cost here. `C-RAD-COOL` and `C-BAT-THERMAL` were **not
  cycles at all**; their remaining keys became keys of `C-AMP-BUS-PUMP` and silently replaced its
  own. The flagship degraded chain therefore parsed as an algebraic loop with `back_edge: null`,
  `E-PLATE-COMM` was nobody's back-edge, and the hysteresis requirement that
  `review-findings.md` §7 exists for was gone. Every one of those overwritten values was
  individually valid, which is why the linter passed it.
- **And 21 point entries had been swallowed the same way**, in four domains: `thermal` 8, `power` 4,
  `propulsion` 4, `consumables` 5. Their entries were absorbed into the `note:` above, which left
  **27 registered channels with no producer at all** — thresholds watching values nobody computed,
  crew positions told they could read gauges that did not exist, and a failure chain whose first
  clue was never emitted. Two linter checks came out of it: a **duplicate key is refused** (49 were
  found across five files, and a duplicate is always a fault because nothing here is written twice
  on purpose), and **every registered channel must have a declared source** — a domain point, a
  frame field, or an entry in `presentation.yaml#plant_published`.
- **Three cycles that were declared did not close.** `C-BAT-THERMAL` named `E-BUS-GNC` — bus to the
  avionics computer — as its third member, which leads nowhere near the coldplate. The check asks a
  question nobody had asked of any of the five: does the back-edge's `to` actually reach its
  `from` through the other members?
- **Two cycles were genuinely missing.** `E-AMP-LOAD` and `E-BUS-COMM` form a transmitter-load /
  bus-voltage loop with no declaration — found because the schedule could not be built. And
  `C-REACTANT-DRAW` was named for the fuel cell's reactants and declared only the oxygen half; the
  hydrogen path is the same physics through a second tank and is now `C-REACTANT-DRAW-H2`.
- **And the contract was wrong about the granularity.** `plant.md` specified the order over
  *domains*, and a domain order **cannot in general exist**: it is a coarsening of the node graph,
  and coarsening creates cycles the physics does not have. `comms → power → consumables →
  propulsion → gnc → comms` is a cycle in the projection with no node-level path behind it. The
  order is now derived over **nodes** — 39 of them — with the frozen lexicographic tiebreak on node
  id, and a domain with states on several nodes simply appears at several points. That is what
  Gauss-Seidel does anyway: the domain is an authoring unit, not a scheduling unit.

One smaller find came with them, from the state/channel binding check: `power.lcl_[n]_state`
published `open` where the machine holds `latched`, so an LCL about to reclose was
indistinguishable from one that never would — which is the entire reason
`electrical_diode.md:254` specifies a controlled reclose.

**And the last gap the ordering work exposed is closed.** Ten nodes are advanced by more than
one state, and **nothing declared which advances first** — so the frozen lexicographic tiebreak
decided all ten. That is not ambiguity, it is a wrong answer waiting to happen, and `link` is the
proof: `link_snr` sorts before `tx_power`, and transmit power is a term in the link budget, so the
derived order would have computed every signal-to-noise ratio from *last tick's* power. On a
nominal link that is a 20 ms error; across `set_power_amplifier` it is a 6 dB step reported a tick
late, which is precisely the transient a fleet watches for.

Each node in that position now declares `state_order` — the group of producing states, in the order
they advance — or `independent`, which is a permitted answer that has to carry a reason. Seven
declare an order and three declare independence:

| Node | Declared | Why |
|---|---|---|
| `link` | `tx_power`, `link_snr` | transmit power is a term in the link budget |
| `structure_config` | `pyro_fired`, `lm_separation_state`, `descent_stage_state`, `configuration` | the three mechanisms, then the projection they produce |
| `crew_state` | `crew_location`, `crew_availability`, `crew_workload` | workload is a lag on activity, which depends on the first two |
| `vehicle_dynamics` | `body_rate`, `attitude`, `orbital_state` | the causal order the 6-DOF integration is written in |
| `coolant_flow` | `pump_1_speed_rpm`, `coolant_flow_kg_s` | the pump produces the flow |
| `fuel_cell` | `fuel_cell_power_w`, `source_converter_v` | the cell produces, the converter regulates what it produced |
| `alert_state` | `alert_lifecycle`, `alarm_horn` | the horn sounds *because* of the alerts |
| `cabin_atm`, `lm_cabin_atm` | independent | mixture components: each gas is advanced from its own flows, and pressure is a read of all of them |
| `engine_main` | independent | three machines sharing an actuator inventory, not interacting |

`--order` prints the result: the 40 nodes in derived order, each with its producing states in
evaluation order, and the 59 internal states that advance with their domain. That view is the
artifact `plant.md:71` promises, and both of its levels are derived — so neither can drift from
the configuration that produces it.

**The dictionary's promises are now traceable.** `channels.yaml`'s `events` field is apollo's
prose — "stuck-on/off signature", "<8 degraded", "any non-null" — and the thresholds are the
machine-readable form of the same promises. Nothing joined them, and the audit that did found
**53 of 118 channels declaring events that no threshold watched**.

A dictionary that promises an alarm the vehicle does not raise is worse than one that promises
nothing, because a fleet reads the promise and waits. But not all 53 were holes, and telling the
cases apart is the work. Every channel with events now declares which kind it is:

| Class | Count | What it claims, and what the linter does with it |
|---|---:|---|
| `alarm` | **81** | the vehicle raises a C&W alert, so a threshold must watch this channel |
| `realised_by` | **20** | a threshold on *another* channel keeps the promise, and `event_thresholds` names it |
| `notification` | **16** | published when it changes; requires `on_event: true` |
| `frame` | **1** | carried in the envelope's own fields |

`realised_by` is the class that made the audit worth doing. `eclss.cabin_temp_c` and
`thermal.zone_[id]_t_c` are **one cabin temperature seen from two domains' sides**, and the
thermal domain's `csm_cabin_low`/`csm_cabin_high` are what keep the ECLSS channel's "<10, >30"
promise. Naming the implementer turns a plausible claim into a checkable one — which matters
more than usual here, because two channels for one physical quantity is the reconciliation's
oldest problem arriving in a new place. It is now named where it occurs.

**Nineteen thresholds came out of it**, taking the vehicle from 121 to 139, and five of them were
a compartment with no limits at all: the CSM had cabin-pressure caution and emergency levels, a
CO₂ caution and the ppO₂ pair, and **the LM had none of them** while its three channels declared
exactly those events. That is the compartment that matters most and it was the one unwatched —
from undocking to docking the LM is the crewed vehicle and the CSM is empty, so for three days of
the surface phase a caution on the CSM's pressure is a caution about a spacecraft with nobody in
it. The rest are the alarms the dictionary promised and nothing raised: an uncommanded
acceleration (**F-04's own corroborating channel**, registered, on two crew perceiving lists, and
with no alarm on the one condition it exists to detect), an LCL that tripped and latched, a
guidance computer that restarted, an engine armed when nobody meant to arm it, a hydrogen reserve
with no floor behind it.

**The oldest surviving duplication is resolved, and finding it took a new check.** `eclss.cabin_temp_c`
and `thermal.zone_csm_cabin_t` were **one cabin temperature published twice** — one state, two
channels, two sets of thresholds. It survived every previous audit because the two points named
their sources differently: one a state, the other a coupling node, and nothing compared them.

The check that found it is the one that asks whether a point's `from` resolves to *anything*: a
state in its own domain, or a coupling node. A `from` that resolves to nothing silently skips the
enum binding, which is exactly the comparison that would have caught the duplication. Three
defects came out of it:

- **The duplication itself.** The resolution follows vocabulary §3 — apollo's name wins where
  apollo has one — so the cabins are `eclss.cabin_temp_c` and a new `eclss.lm_cabin_temp_c`, and
  `thermal.zone_[id]_t_c` now covers the four zones apollo is silent about (avionics bay, service
  bay, descent bay, radiator). Two compartments, two channels, one producer each.
- **`power.battery_temp_c` named a state that exists nowhere** — while `battery_overtemp` and
  `battery_thermal_runaway_rate` watched it. A threshold on a channel with no producer is the same
  defect as an alarm with no threshold, arriving from the other side. The power domain now has a
  `battery_thermal_state`.
- **The crew's switch and breaker channels both named `hatch_state`** — a state in the *structure*
  domain that has nothing to do with them. Two channels had no source of their own and the crew
  domain had no switch or breaker state at all; it has both now.

The LM cabin's split also corrected a crew position: the LM commander and pilot listed a cabin
temperature among the things they can perceive, and until `eclss.lm_cabin_temp_c` existed the only
one they could have been reading was **the other spacecraft's**.

**"Ready to implement" is now evidence rather than a claim.** `tools/plant.py` loads the whole
world — 134 states, 76 edges, 147 channels, 58 verbs — derives the tick order by importing
the linter's own `derive_schedule` (so the plant and the linter cannot disagree about it), emits a
telemetry frame in apollo's shape from the declared field list, and then **walks the tick in
schedule order until it reaches something it cannot compute, where it names exactly what is
missing**. That is `simulator-design.md:146-150`'s method applied to the plant instead of to the
linter: you do not enumerate what a simulator needs up front, you build it, run it, and it tells
you what you now owe.

What it says:

| | |
|---|---|
| states fully configured | **107 / 119** |
| states with a debt, in schedule order | 12 |
| edges carrying a sensitivity | **22 / 55** |
| `UNCONFIGURED` scalars | 219 |

The twelve blocked states are named and ordered, and their debts are the vehicle's real physics
gaps rather than bookkeeping: the gyro bias's time constant, the battery's thermal τ, the
pressurant's lag, RCS's `t_min_on` and its deadband bands, the feed tank's volume, the thrust rise
and tailoff. **The first tick stops at `E-LM-ATM-ABSORB`** — the LM's LiOH path, which is the leg
Apollo 13's crew improvised an adapter for, and which is unconfigured because the LM's absorber is
a different cartridge from the CSM's.

Two things about the tool are deliberate. It **imports the linter** rather than re-deriving the
schedule, because two implementations of one order is how two runs of the same seed come to
differ. And its debt count — 219 — is **not** the linter's 202, because they measure different
things: the linter counts prose obligations as well as unset values, and this counts only the
values that literally say `UNCONFIGURED`, since those are the ones that stop a plant. Two numbers
with one name would be worse than either.

The method implementations are mostly refusals, and that is the honest shape: a `lag` needs a time
constant and a driving value, a `stock` needs a quantum and a flow, and an `algebraic`, `discrete`
or `dynamics` state needs a *rule* — which no configuration can supply, because the rules are
domain code. What the file provides is the frame those rules go in, and the error messages that
tell you where.

## A shared field the join between the two files could not see

`vehicle.yaml#thermal.loops` and `domains/thermal/components.yaml` are two views of one machine, and
`check_thermal_bindings` exists to make them impossible to drift: it holds the `id` sets equal in both
directions and compares every field the two share. Every field but one. The compared list was
`fluid`, `flow_l_min`, `vehicle` and the volume — and `loop_lm` declares a `coolant_mass_kg` in both
files, agreeing at 11.3. So the one shared field outside the list was also the one field where the
three loops are not symmetric, which is the shape this folder keeps producing: **the declaration
nothing reads is the declaration that has already drifted.**

The asymmetry is a fact about the sources rather than an oversight, and the two files say so:

| loop | mass | volume | why |
|---|---|---|---|
| `loop_lm` | **11.3 kg, sourced** | 11 L, chosen | TN D-6724 publishes "about 25 lb of coolant"; 25 lb = 11.34 kg |
| `loop_primary` | — | 25 L, chosen | TN D-6718 gives the CSM circuit as volumes and flows and never as a mass |
| `loop_secondary` | — | 18 L, chosen | scaled from the primary, as `apollo_diode.md:97` leaves it |

**And the missing figure is not missing.** The first draft of this debt read *"needs a fluid density no
source publishes"* — and that was wrong, because the thermal domain publishes it two entries away, in
its own words, with the arithmetic done out loud:

> 25 L of 62.5/37.5 glycol-water at 1,050 kg/m3 is 26.25 kg

`loop_primary_thermal_mass` states the density, the volume and the product. So the honest debt is not
a missing datum; it is **a datum that lives in a sentence**. A loop's coolant mass is what a
heat-exchanger transient turns on — it is the `C` in every one of the loop's first-order responses —
and two of the vehicle's three loops keep it as prose. Either the density becomes a scalar each loop
can declare (and the mass derives from the volume all three already carry), or the mass is declared
and the relation cites it. Declaring the mass is the smaller edit and the more useful one.

**250 became 252, and then 252 became 250.** The first cut reported the gap once per file — four
debts for two unknowns, because both views omit the same field. That is *one unknown under several
names*, the pattern this folder has now found five times in the corpus, appearing for the first time
in the linter's own output. A count that doubles a gap because a thing is written twice is the same
lie as a count that halves it. The debt is reported once per loop and names both files.

## The same blind spot in the electrical join, and a field worth more than the Ah

The round that added `coolant_mass_kg` to `check_thermal_bindings` ended by asking the obvious
question of its own fix: **if one join held a shared field outside its compared list, do the
others?** There is exactly one other join of that shape. `check_electrical_bindings` links
`vehicle.yaml#electrical` to `domains/power/components.yaml` through `domain_group`, and it compares
the quantities by hand — `count` against the unit list, `ah` against `ah`, the loaded range against
`v_nominal` — so the same question has to be asked of it rather than read off a list.

Asking it of the data rather than of the code is what found the answer: intersect the key sets of
the group and its units, subtract the ones the check names, and the remainder is the blind spot.

| group | shared keys | compared | outside the comparison |
|---|---|---|---|
| `fuel_cells` | `bus_v` | `bus_v` (and `modules` ↔ the unit list, `power_w_each` ↔ `rated_w`) | — |
| `battery_csm` | `ah`, **`chemistry`** | `ah` | **`chemistry`** |
| `battery_lm_ascent` | `ah` | `ah` | — |
| `battery_lm_descent` | `ah` | `ah` | — |

It is one field, on one group, and the two copies agree — `"silver-oxide/zinc"` in both — which is
exactly the state `coolant_mass_kg` was in before it was compared. **A field that two files agree on
and nothing compares is not a field that is correct; it is a field that has not drifted *yet*.**

And chemistry is the last field in this file that should be left to drift, because the specification
gives it its own row in the table of what changes the model:

> | Battery chemistry | Changes voltage, temperature, charge limits, SOC model | Reference Li-ion only |
> — `electrical_diode.md:124`

It is not a label on a box; it is the discharge curve and the usable-energy derating. The same
corpus says so twice more from the other side: `electrical_diode.md:18` lists battery chemistry among
the quantities that are **unspecified** and "must be resolved before a flight design is frozen", and
`consumables_diode.md:145` records it as `UNSPECIFIED` with "usable-energy and derating models
supplied externally."

So the vehicle's own position is a deliberate departure that nothing records. The spec's reference
default is Li-ion; the cells Apollo flew were silver-zinc, and the corpus chooses the flown chemistry
over the reference one — correctly, and silently. **`csm_entry` carries the value, its provenance
block cites "3 x 40 Ah entry batteries, 20 cells, 37.2 V open circuit, 27 V minimum loaded" and never
mentions the electrochemistry at all, and the two LM groups carry neither the field nor a note
declining the default.** A reader asking why this vehicle is not Li-ion finds no answer, and — worse
for a challenge — a team that wants to know what its batteries will actually do finds the answer
implied by a string in a file that one of the three groups does not have.

**250 became 252.** The join now compares `chemistry` where both files carry it, in both directions,
and the two LM groups owe either the electrochemistry or the note. Verified the way this folder
requires: a `nickel-cadmium` in one of the CSM's three units is refused by name against the group's
`silver-oxide/zinc`, and the unbroken corpus is silent.

**And the round's own near-miss belongs here, because it is the mistake this file keeps warning
about.** The chemistry comparison went in between the `ah` check and the voltage chain, and the debt
block went in between those two — which put the *entire* voltage comparison, the range/nominal claim
this check's own test calls "the interesting one", inside `if not battery.get("chemistry"):`. It went
from running for all three groups to running for none of the CSM's units, and it read a `unit`
leaked from a loop that had already finished. The linter composed, the debt count came out right,
and nothing in the output looked wrong: a block inserted into an `if`/`elif` chain takes the rest of
the chain with it and does not announce itself. What noticed was the fixture needle 30 lines into
`test_the_linter_refuses_an_electrical_inventory_that_drifted` — a test written rounds earlier for a
different reason, failing on an edit made this round. The `elif` was restored, and the new refusal
now has its own fixture needle so the next person to move it finds out the same way.

## The engines the mission is flown on, in two files and joined by nothing

The last two rounds fixed one join and then hunted the same blind spot in the other. This round
asked the question one step further out, and it found something larger than a field outside a
compared list: **two views of one machine with no join at all.**

`check_propulsion` decides whether the mission closes. It walks the Δv budget through each tank in
phase order, and it reads `isp_s` and the propellant load from `vehicle.yaml#propulsion` and nothing
else — which is right, because that file is the vehicle-level view the mass closure also sums. What
it does not read is the other copy:

| `vehicle.yaml#propulsion` | copy | `isp_s` | thrust |
|---|---|---|---|
| `sps` | `domains/propulsion/components.yaml:sps` | 314.5 | 91,188 N |
| `lm_dps` | `domains/propulsion/components.yaml:dps` | 305 | 43,192 / 4,671 N |
| `lm_aps` | `domains/propulsion/components.yaml:aps` | 309.4 | 15,569 N |
| `rcs_sm`, `rcs_cm`, `rcs_lm` | `domains/rcs/components.yaml:thruster_100lbf` | 290 | 445 N |

and those are the copies the plant computes with. The RCS domain's mass flow is stated in its own
words as `mdot = F / (Isp * g0) = 445 / (290 * 9.80665)`; the propulsion domain's engines carry the
thrust the thrust curve is built on. Every figure agreed when the check was written, and nothing was
keeping them agreeing.

**The direction a divergence fails in is the one this folder exists to prevent.** An SPS re-rated in
the domain and not in `vehicle.yaml` leaves `check_propulsion` reporting a healthy reserve while the
plant burns propellant at the domain's Isp. The mission looks flyable and is not — which is verbatim
the failure mode `check_propulsion`'s own docstring says it exists to catch, arriving from the side
it was not looking at. It is the third instance of this family in three rounds and the first one
where the quantity decides whether the vehicle can fly.

The link is declared rather than inferred, for the reason `domain_group` is: the files name one
engine differently (`lm_dps` against `dps`), so a rule guessed from the string would have to know
that `lm` and `l` are the same vehicle. Eight components now carry `vehicle_keys`, and one of them
carries three — `thruster_100lbf` is all forty-four thrusters, because its own note says forty-four
near-identical entries would be "forty-four places for the arithmetic to differ".

**The third closure is arithmetic rather than equality, and it was free.** The vehicle declares each
RCS system's thruster count, the domain breaks each system into strings, and the article count is
what the systems add up to — so 16 + 12 + 16 = 44, and 8 + 8 = 16, 12 = 12, 16 = 16. Three
relationships that were true and unchecked now close against each other, and they cost one field
each.

Verified the way this folder requires, by breaking nine copies: a `314` against the vehicle's 314.5
s, a 43,000 N against 43,192, a 40-article count against 44, a ten-thruster CM string against
twelve, an engine unlinked, a link naming an engine that does not exist, an SPS pointed at another
engine so nothing claims it, and a component stripped of the `isp_s` the plant needs — every one
refused by name, and the unbroken corpus silent. `--strict` still exits 2, and the debt count did
not move: **the two files were already right; what was missing was anything that would notice if
they stopped being.**

## The instrument, and the defect it found in the fix for the defect

Three rounds found this family by hand — a shared field outside a compared list, twice, then two
views of one machine with no join at all. This round stopped guessing where to look next and built
the instrument instead.

**The method.** Flatten every YAML file in the folder to `(key, value, path)` triples, group by
key, and keep the groups where one value appears in two or more *files*. A key with the same value
in two files is almost never a coincidence; it is one quantity declared twice. It found **104
groups**, and most are honestly not quantities: `dwell_assert_ms = 2000` spans ten domains because
it is a vocabulary value, `hazard = 0.0002` spans eleven for the same reason, `revision = 1` spans
thirteen because every profile is at revision 1. Those are the noise floor and they are easy to
recognise. The signal is the rest.

**The verdict on the three joins:**

| join | shared fields outside its comparison |
|---|---|
| thermal loops (`check_thermal_bindings`) | **none** — round 104 closed it |
| electrical batteries and cells (`check_electrical_bindings`) | **none** — round 105 closed it |
| propulsion engines (`check_propulsion_bindings`) | **four**, and this check was written last round |

`engine`, `qualified_restarts`, `throttleable` and `throttle_ratio` are declared on both sides of
the propulsion join and outside the four-name list I wrote to compare it — all four agreeing today,
which is exactly the state `coolant_mass_kg` and `chemistry` were in when they were found. **The
check built to catch this had it, one round later, in four places.**

That is the honest result and it is the reason the fix is not a fifth name in the list. A
hand-written list of what to compare *is* the bug: it is correct on the day it is written and
silently wrong on the day somebody adds a field to both files, and nothing about adding a field
tells you to come back here. So the list is gone. The comparison is now

    (set(vehicle_entry) & set(component)) - PROPULSION_STRUCTURAL

— the intersection of the keys the two views share, minus a declared set of keys that are never a
quantity two files both state (identity, prose, the link itself, and `thrusters`, which its own
arithmetic rule compares rather than for equality, because a string's eight and a system's sixteen
are both correct). **A field added to both files is now compared without anybody remembering to
add it here**, which is the property the whitelist could never have.

Verified by breaking four copies that the old list let through: a `throttle_ratio` of 8 against
10, a `qualified_restarts` of 40 against 50, a renamed `AJ10-138` against `AJ10-137`, and an APS
flipped to `throttleable: true`. Every one refused by name, and the unbroken corpus still composes
at 252 debts — so the intersection rule catches four things the whitelist missed without inventing
a single refusal.

**One more thing this round caught, about this folder's own habits.** Two of the fixtures written
last round stopped matching when the refusal message changed, and the suite failed on the needle
rather than on the physics. That is the fixture doing its job: a needle that no longer appears is a
message somebody changed, and the temptation to loosen it is the temptation to stop testing what it
asserted. The needles were updated to the new message, not generalised until they matched anything.

## The denominator of every partial pressure, declared seven times and read by nothing

The instrument built last round flattens every YAML file to `(key, value, path)` triples and keeps
the groups where one value appears in two or more files. Most of its 104 hits are vocabulary
values — `dwell_assert_ms = 2000` spans ten domains because it means the same thing in all of them.
The signal is the rest, and the largest single piece of it was this:

    volume_m3 = 5.9        vehicle.yaml#thermal.zones[csm_cabin]
                           domains/thermal/components.yaml#zones[csm_cabin]
                           domains/eclss/components.yaml#components[csm_cabin_volume]
                           domains/eclss/components.yaml#atmosphere_model.volume_m3.csm

and the same four for the LM's 6.7 — **seven scalars in three files, and until this round no tool
read any of them.** That is worth stating precisely, because it is easy to read past:
`check_thermal_bindings` holds the two zone *id sets* equal and compares no field at all;
`csm_cabin_volume` is a component of class `volume`, which no check looks at; and
`atmosphere_model.volume_m3` was read by nothing whatsoever.

**Why this one matters more than the last four.** The volume is `V` in the relation
`atmosphere_model` states as the law the whole life-support model rests on —
`P = (sum_i n_i) R T / V`. It is what turns a gas *mass* into the partial pressure a crew member
reads and an agent decides on. So it sits underneath `eclss.pp_o2_mmhg` on one side and every
thermal time constant in the zone on the other, and the LM's oxygen initial of 3.013405 kg is a
*derived* figure computed from the LM's 6.7. Move the volume in one file and the plant's
atmosphere changes while the checkout's does not: the crew would be breathing a cabin that only
exists in the copy that happened to be loaded.

**And the corpus knows, and undercounts itself doing it.** The LM oxygen initial's own relation
says, in as many words:

> The corpus states the relation once and the volume twice, so this is a computed consequence
> rather than a second published number.

It is seven times. That note is not careless — it is what a duplicate nobody reads looks like from
the inside: a copy is counted by whoever remembers writing it, and no instrument was counting. It
is the fourth round in a row that this folder's recurring sentence has been true of the folder
itself, and the first where the *comment claiming the count* was the thing that was wrong.

**The check, and the mistake in its first version.** The authority is `atmosphere_model.volume_m3`,
because that is the one place the physics law itself reads, and every other declaration is held
against it. The link needed no new field: `check_thermal_bindings` already holds the zone ids equal
across the two files that carry zones, so the cabin zone of each vehicle is `<vehicle>_cabin` and
the pair is known.

The first version keyed on each zone's own `vehicle` field instead, which meant *any* zone of the
right vehicle was compared against that vehicle's cabin — and would have refused a perfectly good
3.0 m³ csm_service_bay for disagreeing with a 5.9 m³ cabin it is not. The volume the law divides by
belongs to `<vehicle>_cabin` and to nothing else; every other zone of the vehicle has a volume of
its own that the atmosphere model says nothing about. What surfaced it was a fixture whose needle
stopped appearing: the break was supposed to produce "does not contain" and produced "must be one
number" instead, which is the fixture telling me the rule was not the rule I thought I had written.
The corrected check has a **negative** test now — a service-bay volume must not be refused — because
that is the failure mode the correction exists to prevent.

Verified by breaking six copies: the authority moved under each of the other three declarations,
the cabin zone stripped of its volume, and the `class: volume` component renamed out of its class.
Every one refused by name; the unbroken corpus still composes at 252 debts.

**And this check reproduced the folder's other oldest bug on its first run.** It read
`vehicle.get("thermal")` where the vehicle document may be `None` — that is what "does not parse"
means — so an unparseable `vehicle.yaml` crashed the linter inside the new check and replaced the
report with a traceback, burying the one line that mattered. That is verbatim the failure
`test_an_unloadable_vehicle_refuses_instead_of_crashing` was written for, several rounds ago, and
it caught the reintroduction without being changed. Ten call sites in this file now guard the
vehicle document the same way, and the guard is not a convention anybody remembers: it is a test
that corrupts the file and demands a refusal instead of a stack trace.

## The link's hardware, declared twice, and the fourth join with nothing between the copies

Three rounds fixed a join and then hunted the same blind spot in the next one. Round 106 found the
largest version of it — two views of one machine with **no join at all** — and the instrument named
where the next one was. This round closed it, and the shape is now familiar enough to state as a
rule: *a file's own header saying "this is the domain's view of that" is a claim, not a check.*

`vehicle.yaml#comms` is the vehicle-level bill of materials NR ch.18 and `apollo_diode.md:157-168`
supply: four transmitters with their DC and RF watts, four antennas with their gains and
beamwidths, the two telemetry rates and the VHF channels. When `domains/comms/` landed as the tenth
domain, its component list was written as the domain's view of the same hardware, under a header
that says exactly what the entries are for:

> All of it is already in `vehicle.yaml#comms`; these entries are the domain's view of it, and they
> exist so that a threshold or a fault can name the object it is about.

Nothing read that sentence. Four components mirror four vehicle entries — one antenna standing for
two (`omni` for `omni_a`/`omni_b`) and one transmitter for two power levels (`lm_sband` for
`lm_sband_low`/`lm_sband_high`) — and the five fields the two views share were compared by no tool:

| domain component | vehicle entry | shared and unread |
|---|---|---|
| `hga` | `high_gain` | `gain_db` 26.7, `beamwidth_deg` 9.4, `steerable` true |
| `omni` | `omni_a`, `omni_b` | `gain_db` 2 |
| `sband_transceiver` | `sband_transceiver` | `dc_w` 36, `rf_w` 2.8 |
| `sband_power_amplifier` | `sband_power_amplifier` | `dc_w` 72, `rf_w` 11.2 |
| `lm_sband` | `lm_sband_low`, `lm_sband_high` | `dc_w`/`rf_w`, under different names in each file |

**A duplicate is worth what the copies have been used for, and these have been used.** The two
coupling edges that make the link quantitative are derived from them, and both relations cite the
*vehicle-level* copy by name:

- `E-GEOM-LINK`'s −0.54 dB per degree is computed from `theta_3dB = 9.4 deg`, and its own relation
  says that figure is "`vehicle.yaml#comms.antennas.high_gain`, itself derived from the
  gain-beamwidth product";
- `E-AMP-LOAD`'s 0.0357 A per watt is applied to "the 36 W transceiver and the 72 W power
  amplifier", which its relation places "in `vehicle.yaml#comms.transmitters`".

The domain's copy is the one a *fault* names — COM-02, COM-07 and COM-08 happen to
`sband_power_amplifier` and `sband_transceiver` — and the one the domain's code will read when the
link budget is implemented, which `domains/comms/components.yaml`'s own `open_debts` says is still
owed. So an antenna re-rated on one side and not the other leaves the pointing loss belonging to a
9.4-degree beam the domain no longer describes, and a transmitter re-rated leaves a fault reasoning
about a load the edge does not carry. It is the same family the last four rounds have been closing
and the first time its stale copy has been an edge's own arithmetic rather than a compared list —
and the first time two edges have derived from one unjoined pair of files.

Proven before it was fixed, the way this folder requires: a `gain_db` of 25.0 on `hga` and a `dc_w`
of 40 on `sband_transceiver`, on a copy in `.scratch/`, and the linter composed at 252 debts while
the vehicle described two links.

**The link is declared, and it needs to be, twice over.** `hga` against `high_gain` is the same
abbreviation problem `lm_dps` against `dps` was; and `lm_sband` against two vehicle entries is a
difference of *shape* — one article with two power levels is two entries in a bill of materials. So
the component carries `vehicle_keys` **and** a `levels` mapping keyed by the vehicle entry ids:

```yaml
    vehicle_keys: [lm_sband_low, lm_sband_high]
    levels:
      lm_sband_low:
        dc_w: 20
        rf_w: 0.75
      lm_sband_high:
        dc_w: 90
        rf_w: 18.6
```

The keys are the link and the figures are under them, so a level cannot drift from the entry it
belongs to and an entry cannot be dropped without taking its figures with it — both are checked. The
component's first version carried `low_dc_w`, `high_dc_w`, `rf_w_low` and `rf_w_high`: four names no
other component on the vehicle uses, matching nothing in the file they duplicate, and read by
nothing. That is what a quantity looks like when it is written for a reader rather than for a rule.

The comparison is the **intersection of the keys the two views share**, minus a declared structural
set, exactly as `check_propulsion_bindings` now does it. There is no list of field names to fall out
of date: `gain_db`, `beamwidth_deg` and `steerable` were compared the moment the link was declared,
and a field added to both files later is compared without anybody remembering to come back here.
The structural set is small and each member is argued: identity and the prose keys, `vehicle_keys`
and `levels` (the subjects of the comparison rather than values in it), `beams` — the vehicle's
per-feed table on the one antenna with three feeds, from which the component takes the narrow
feed's figures, as its own provenance says — and `count`, which is the four omni helices NR
publishes. That last one is the honest non-closure: neither file states a per-entry article count
for an omni, so there is no second copy to compare it with, and saying so in the set is better than
a rule that would have to invent the per-entry figure.

**The reverse direction is a debt rather than a refusal, and the difference is deliberate.** A
vehicle entry nothing claims is a gap in the modelling, not a contradiction between two files, and
there is exactly one today: `vehicle.yaml#comms.antennas.sband_steerable`, the LM's steerable
S-band antenna. Its gain and beamwidth are owed in `vehicle.yaml` — no source reached publishes
them — and the antenna is nonetheless a member of the `comm.antenna` enum that `select_antenna`
offers, so a fleet can put the vehicle on an antenna the domain has no object for: no fault, no
threshold and no link budget on that side can be about it. **That is a different fact from the
missing figure, and it is the one no file recorded.** The obvious repair is wrong: mirroring the two
`UNCONFIGURED` values into a domain component would make one unknown count twice in the plant's own
`UNCONFIGURED` scalar total — 199 would read 201 — which is the same lie as a count that halves a
gap, and this folder has already written down why. Either the domain grows an object for the
antenna, or the debt is answered with the decision not to model it. **252 became 253.**

Verified the way this folder requires, by breaking fourteen copies: the domain's `gain_db` against
the vehicle's 26.7, its `dc_w` against 36, its `rf_w` against 11.2, the *vehicle's* beamwidth moved
under the domain's 9.4, `steerable` flipped, the omni re-rated against both its entries, a component
stripped of its link, a link naming an antenna that does not exist, the LM's low `dc_w` and high
`rf_w` drifted from their entries, a `levels` entry no `vehicle_keys` claims, a claimed entry left
with no level, and a figure written at the multi-level component's own top level rather than under
the entry it belongs to. Every one refused by name; the unbroken corpus silent, and its one
debt the antenna above. **163 vehicle tests, 252 repo tests, `--strict` 2, ruff clean.** Plant unchanged:
134 states over 57 nodes, 77 edges, 15 ready / 26 value / 11 edge / 82 rule, 199 `UNCONFIGURED`
scalars.

## The thermal join read the model, and the components a fault names were unread

`check_thermal_bindings` was the first of these joins and the one that found the family. Its first
half holds three coolant *loops* against each other by `id`. Its second half held the vehicle's
`csm_radiator` against the domain's `radiator_model.csm` block — the panel product against the
model's total rejection, the panel area against the model's geometric area — which is a real and
useful closure, because the model is where the entry's arithmetic is *derived into*.

It is also the wrong copy to stop at. `radiator_model` is a block; the things a fault happens to
are components, and the thermal domain has three of them for this hardware:

| domain component | vehicle entry | shared, and read by nothing |
|---|---|---|
| `radiator_csm` | `csm_radiator` | `panels` 2, `area_m2` 9.1 |
| `evaporator_csm` | `csm_evaporator` | `rejection_w` 2345 |
| `sublimator_lm` | `lm_sublimator` | `rejection_w` UNCONFIGURED |

**Every one of those figures was declared twice and compared in neither place.** `TCS-03-radiator-
isolation` names `radiator_csm` and its provenance argues the chain from "a sunlit radiator absorbs
2,477 W against a 2,588 W rejection capacity" — the *model's* figure — while the component's own
`panels` and `area_m2` went unread, and `TCS-04-evaporator-clog` names `evaporator_csm`, whose
2,345 W appears in `vehicle.yaml` as well and was compared by no rule in either file. So the
failure direction is the one this folder keeps finding: the entry is held against what it was
derived *into*, and not against the article a fault points at. A panel count changed on the
component side moves the fault's subject and leaves the heat balance exactly where it was.

The link is `vehicle_keys` on the three components, declared because the two files name one panel
from opposite ends — `csm_radiator` against `radiator_csm` — and the comparison is the intersection
of the keys the two share, minus a declared structural set: the rule the propulsion and comms joins
now use. `panels` and `area_m2` were compared the moment the link existed rather than when somebody
remembered to add them.

**And the list that started this family is gone with it.** The loop comparison above opened with

```python
        for field in ("fluid", "flow_l_min", "vehicle", "coolant_mass_kg"):
```

— and those four names are *exactly* the four fields the vehicle's loops and the domain's share
today, which is what makes it the honest example rather than an embarrassing one: a hand-written
list of what to compare is correct on the day it is written, and it is the day somebody adds a
fifth field to both files that nothing tells you to come back. The loop half now uses the same
intersection rule, and the file has no list of field names left to fall out of date.

Two smaller things this round paid for. The reverse direction is a debt rather than a refusal, as
it is in the comms join, and **all three entries are claimed today, so the count did not move:
253**. And the LM sublimator's owed rejection is now declared in *both* files and held equal, which
is a small gain in the direction this folder cares about: the debt can no longer be answered in one
place and left standing in the other, because filling in `sublimator_lm`'s number while
`vehicle.yaml` still says `UNCONFIGURED` is refused by name.

**And the join caught a fixture that had been right for forty rounds.**
`test_an_unset_domain_value_is_a_debt_named_by_its_path` proves that "supplying the value must
retire the debt" by supplying the sublimator's owed rejection — in `domains/thermal/components.yaml`
and nowhere else, which was correct until the two copies were joined. The suite failed on the
assertion rather than on the physics, and the reason is the point: the debt *was* retired on the
domain side, and what replaced it was the refusal for moving one copy and not the other. The
fixture now moves both files, which is what its own sentence was always claiming. The assertion was
not loosened, and the second file is not a concession — a debt answered in one place and left
standing in the other is a debt the next reader finds still open.

**One declaration in that block is still read by nothing, and it is not a duplicate.**
`evaporator_csm` carries `fluid: water`; `vehicle.yaml`'s `csm_evaporator` carries no fluid at all,
so there is no second copy to hold it against, and the join neither compares it nor pretends to.
The vehicle's own provenance for that entry says the evaporator "consumes water, which is why
`water_cooling` is a scored resource" — the fact is in a sentence there and in a field here, which
is the shape this folder has recorded four times. Naming it is the honest end of this round's
scope; closing it is either a `fluid` on the vehicle entry or a decision that a radiator list
should not carry one.

Verified by breaking ten copies: the domain's `panels` against the vehicle's 2, its `area_m2`
against 9.1, its `rejection_w` against 2,345, the LM's owed figure filled in on one side only, the
*vehicle's* area moved under the domain's copy, a component stripped of its link, a link naming a
radiator that does not exist, a vehicle radiator claimed by nothing, and — the case the converted
list exists for — a loop's `flow_l_min` moved in one file. Every one refused by name; the unbroken
corpus silent and still at 253 debts.

## The gain and the beamwidth are one figure, and the corpus says so in three files

The note on `vehicle.yaml#comms.antennas.high_gain` is the sentence this round exists for:

> the gains are sourced and the beamwidths are **derived**, because the first version of this entry
> carried `beamwidth_deg: 1.0` and that number is not physically possible beside a 26.7 dB gain.
> Any aperture antenna obeys the gain-beamwidth product `G = 41253 / theta^2` with theta in degrees,
> so 26.7 dB (467.7 linear) is a 9.4 deg beam — a 1.0 deg beam would need 46 dB.

So the folder knew the relation, had already used it to catch one impossible number, and left it as
prose. The same identity is stated again in `domains/comms/components.yaml`'s note on `hga`, and a
third time in `E-GEOM-LINK`'s relation, which computes its slope as `-24 * 2 / 9.4^2` — and **no
tool evaluated it anywhere.** Four declarations are governed by it:

| declaration | gain | beamwidth | `10^(G/10) * theta^2` |
|---|---|---|---|
| `high_gain` | 26.7 dB | 9.4 deg | 41,329 |
| `high_gain.beams.wide` | 9.2 dB | 70.4 deg | 41,272 |
| `high_gain.beams.medium` | 20.7 dB | 18.7 deg | 41,224 |
| `high_gain.beams.narrow` | 26.7 dB | 9.4 deg | 41,329 |

Every one within 0.2 % of 41,253 — and the fourth is the same pair as the first, which is its own
small finding: the antenna and its narrow feed are one number written twice in one file.

**Why it is worth a check rather than a note.** `E-GEOM-LINK`'s −0.54 dB per degree *is*
`-24 * theta / theta_3dB^2` at a 2-degree operating point, so the beamwidth this identity determines
is what every pointing loss the fleet ever reads is scaled by. The antenna is declared in two files,
so last round's join catches a divergence *between* them — but a pair that drifts **together**, in
both files, is invisible to every check in this folder except this one. That is the case the second
fixture sets up, and before this round it composed.

**The rule needs one declaration to be universal, and the corpus already makes the case for it.**
The omni's 180 degrees is not a half-power width: 2 dBi against the product would be 161 degrees,
and the entry's own note says so — "the gain-beamwidth product is meaningless this close to
isotropic (it would give 161 deg), so the nominal 180 is kept rather than derived". So every antenna
that declares both figures now declares which relation governs them:

```yaml
    - id: high_gain
      pattern: aperture
    - id: omni_a
      pattern: near_isotropic
      pattern_note: >-
        the gain-beamwidth product is meaningless this close to isotropic ...
```

`near_isotropic` is a claim that *exempts* its antenna from the rule every other antenna is held to,
and a claim like that carries its reason — the discipline `state_order: independent` has carried
since the worklist rounds. It is also the honest answer to the obvious objection that a lazy author
could mark an aperture `near_isotropic` to escape: the exemption is a declaration a reader can see
and disagree with, which is a different thing from a rule with a silent hole in it.

The declaration is made in **both** files — `hga` carries `pattern: aperture` and the domain's
`omni` carries `near_isotropic` — so last round's join compares it for free. What the two files must
agree about is not only the gain and the width but what *kind* of pair they are.

**A near-miss the writing of this check produced.** The first version reported a missing declaration
and a missing reason inside the per-figure loop, which meant the high-gain antenna — one declaration
governing four pairs — would have reported the same gap four times. That is "one unknown under
several names" arriving in the linter for the fifth time, and it is the same lie in the other
direction as the debt count that doubled in round 104. The declaration is now checked once per
antenna and only the product is checked per pair; the fixture asserts the count is exactly one.

Two smaller things came with it. The four joins now share one `comparable()` helper rather than
repeating `sorted((set(a) & set(b)) - STRUCTURAL)` four times, and it excludes any key ending in
`_note` — a note is a sentence attached to a claim, and two files describing one machine never owe
each other the same sentence, which `pattern_note` would otherwise have made them do. And the
`omni_a` provenance note that explained the 180 degrees moved to the `pattern_note` it is the
justification for, because a reason for an exemption belongs with the declaration of the exemption
rather than in the prose block beside it.

Verified by breaking eleven copies: the gain moved on the vehicle side only, the width moved in
*both* files so that the join is silent, each of the three feeds broken in turn, the declaration
removed, a near-isotropic claim made without a reason, the omni claimed as an aperture, the owed LM
antenna given figures and no pattern, and the domain's copy of the declaration drifted from the
vehicle's. Every one refused by name; the unbroken corpus silent and still at **253** debts, since
this round declared what was already true rather than filling anything in.

## The linter parsed every file twice, and the suite was paying for it

This round's check pushed the test suite past the timeout it is run under, and finding out why was
worth more than the check. A profile of one linter run:

```
        1    0.002   35.427   35.427 check_vehicle.py:8423(main)
      586    0.041   35.284   35.284 check_vehicle.py:651(load)
     1172    0.002   33.724   33.724 yaml/composer.py:29(get_single_node)
      586    0.036   17.544   17.544 yaml/__init__.py:74(load)
```

**586 loads of a corpus with thirty-odd files, and 1172 documents composed to produce thirty** —
because `load` parsed each file twice, once with `yaml.compose` for the duplicate-key walk and once
with `safe_load` for the document, and because every check that joins two documents reads both of
them again. Ninety-five per cent of the linter's run was YAML parsing, and the test suite is built
out of linter invocations — every `run_linter` in it starts a fresh process — so the suite was
spending most of nine minutes re-reading the same thirty files.

The *parse* is memoised now, keyed by the path **and its mtime and size** rather than by the path
alone: a caller that rewrites a file between two loads is a real pattern — a fixture that moves a
value, a plant that reloads the world — and must not be served a stale document. Everything the call
site does with the result still runs on every load: the duplicate-key walk, the absorbed-key walk and
`check_answered_debts` all report per call, exactly as before.

**The equivalence was checked rather than asserted**, by running the committed linter and this one
over the same corpus and diffing. Identical output under `--strict`, `--phases`, `--debts` and
`--order`, and identical on a deliberately broken copy — five refusals in the same order with the
same text, four of them present in both versions and the fifth the new check's.

The linter went from **13.0 s to 1.9 s**, and the vehicle suite from **534 s to 87 s**.
`plant.py --readiness` did not move at all, 1.19 s before and 1.20 s after, and that is the
measurement that says the diagnosis was right rather than merely plausible: the plant loads the
world once per run and never paid the repeated-load cost, so a change that removes only repeated
parsing should leave it exactly where it was.

That last number is the point of writing it down. This folder's method is to verify every refusal by
breaking a copy, and the cost of one broken copy was fourteen seconds — which is why the fixtures in
`tests/test_vehicle_config.py` had begun to be rationed, and why this round's own check first
reported a missing declaration four times rather than once (see below). A slow referee is a referee
that gets consulted less.

**And the round found the folder's oldest defect in the tools' own prose, one file over.**
`tools/plant.py`'s docstring told a reader that "`check_vehicle.py` does that for the *definition* —
it reports 202 declared debts by path", and the comment above its debt counter said "the linter
reports 202 debts". Both were true when they were written, neither was read by anything since, and
the linter has reported 253 since the comms join: a reader sizing what is left from that sentence
was 51 obligations out. They say 253 now, and `test_the_readme_status_matches_the_tools` scans
`tools/*.py` for the phrase as well as the README — because a figure a reader uses to size the
remaining work is the figure this folder exists to keep honest, and *where* it is written makes no
difference to that. The scan is limited to the tools and to the `N declared debts` form, since
quotation marks are everywhere in Python and stripping them the way the README's scan does would
mangle the text; a tool that wants to record a historical count quotes it, exactly as the README's
own history section does.

## The slope was a copy of three numbers, and the copies cannot be held to anything

`rederive` has checked a sensitivity that states its own arithmetic since the round that found
`E-RAD-WATER` declaring 3.8e-7 beside a relation computing `1/2.45e6 = 4.082e-7`. That mechanism is
right and it has a hole this folder has been circling for ten rounds: **the numbers inside the
expression are copies.** They are read from nowhere, so an edge can only be caught disagreeing with
*itself*.

`E-GEOM-LINK` is the case that makes the hole matter. Its −0.54 dB per degree is `-24 * 2 / 9.4^2`,
and all three numbers belong to other files. 9.4 is `vehicle.yaml#comms.antennas.high_gain`'s
half-power beamwidth — the figure the previous round tied to the gain through the ideal-aperture
product — and 2 existed only inside a sentence. So the hole is two closures deep and the second one
is open at the end:

> Re-rate the antenna from 26.7 dB to 28 dB, **in both files and consistently**. The join is silent
> because the two copies agree. The product check is silent because 28 dB and 8.1 degrees are a
> consistent pair. `E-GEOM-LINK` goes on scaling every pointing loss the fleet reads by the slope of
> a beam that no longer exists — and the whole point of the last round was to make the beamwidth
> follow the gain.

That fixture composed until this round, and it is the first one the new check refuses.

**A sensitivity may now declare a `derivation`**: an arithmetic expression over named inputs, where
each input is either a number or a `"<file>.yaml:<dotted.path>"` source in the syntax `derives_from`
already uses.

```yaml
      derivation:
        expression: "-24 * theta / (theta_3db * theta_3db)"
        inputs:
          theta: 2
          theta_3db: "vehicle.yaml:comms.antennas.high_gain.beamwidth_deg"
```

Three rules make that a declaration rather than a program. Every identifier in the expression must be
an input the file declares, and **every declared input must appear in the expression**, so neither a
name nor a binding can go unread. A source that stops resolving is refused, because a renamed source
reads exactly like an unset one, and a source that is `UNCONFIGURED` checks nothing, because the
obligation is counted where the quantity lives rather than at its use. And the expression is
evaluated only **after** substitution and only if what remains is arithmetic over numbers, so the
configuration still cannot become executable — the safety property `rederive` was built with, kept
by construction rather than by inspection.

**Five more edges came with it, and they are why this is a mechanism rather than one edge's special
case.** The two crew metabolic rates and the two absorber conversions carried the same shape:

| edge | was | binds |
|---|---|---|
| `E-CREW-ATM`, `E-LM-CREW-ATM` | `computation: "0.91 / 24"` | the O2 planning rate |
| `E-ATM-ABSORB`, `E-LM-ATM-ABSORB` | `computation: "41 / (41 * 0.91 / 24)"` | the rate, and the cartridge's rating |
| `E-CREW-WATER` | `computation: "2.27 / 24"` | the water planning rate |

One published figure — the 0.91 kg per crew-day — is now bound by **four** edges, so moving it
refuses four by name. The fixture asserts exactly that.

**And the comparison had to change, which the verification is what found.** The first version held a
derivation to `rederive`'s flat 1 % and let a whole fixture through: moving the water rate from 2.27
to 2.29 is 0.88 %, inside the tolerance, and 0.9 % of a metabolic rate is a full step in its second
decimal. A single relative tolerance cannot separate that from `-0.54` against `-0.54325` — which is
0.6 % away from its own derivation because **two significant figures is how it is written**. So the
comparison is at the declared value's own precision: the derivation must round to the number in the
file. `rederive` uses the same rule now, and the corpus composes under it unchanged, so the
tightening cost nothing and closed a real hole.

### The other end of the chain, which was missing its last link

Looking for the same shape one field over found a chain with a link missing. The CSM's absorber
counter is exhausted at **72** man-hours, which is `vehicle.yaml#consumables.co2_removal`'s published
rating written a second time; the LM's is **41**, the same. And
`domains/consumables/components.yaml`'s `absorber_man_hours_csm` resolves its own `initial` against
the *node's* rating. So the chain ran **stock initial → node rating → nothing**.

The edge beside it cannot see the gap either, and that is the interesting part: `E-ATM-ABSORB`'s
sensitivity is man-hours per kg CO2 and the rating **cancels** out of it. Change
`csm_element_man_hours` to 80 and the edge still derives 26.37 — correctly — while the counter it
feeds is exhausted at 72. The rating bites in exactly one place and nothing was holding it there.

So a consumption counter's rating names its source, `exhausted_at_source`, checked the way
`initial_source` is; a rating with no source is reported as a **debt** rather than refused, because a
chosen rating is a legitimate thing for a counter to have and what is owed is the decision. **The
rule found both gaps the moment it looked — 253 became 255 — and closing them put it back to 253.**

Writing it produced a small confession worth recording. The first version read
`for node in (coupling or {}).get("nodes") or []`, and `nodes` is a **mapping** keyed by node id while
`edges` beside it is a sequence. The loop ran zero times and reported nothing, and the linter
composed: a check that passes because it never looked, which is the failure this whole effort is
about, committed by the check written to catch it. What would have caught it is the debt it was
supposed to report; what did catch it was running the break-a-copy fixture before believing the
green.

Verified by breaking seventeen copies: the antenna re-rated in both files consistently, the O2 rate
moved under four edges, the water rate moved, a counter's rating drifted from the figure it names, a
counter's source renamed and another pointed at a document that does not exist, a counter with no
source at all, an expression unbalanced, a name the expression uses left unbound, a binding the
expression does not use, an input that is neither number nor source, a source pointed at the owed LM
antenna, and the unbroken corpus silent at 253. `rederive`'s own path was regression-checked under
the new comparison: `E-WATER-RAD`'s computation broken from `2.45e6` to `2.4e6` is still refused by
name.

## Six more edges, and the declaration three of them were missing

The mechanism from the last round reached five more edges whose inputs were declared all along, and
finding the sixth turned up a declaration the corpus did not have.

| edge | was | now binds |
|---|---|---|
| `E-PROP-ENG` | `1 / (314.5 * 9.80665)` | `vehicle.yaml#propulsion.sps.isp_s` |
| `E-RCSP-RCS` | `1 / (290.0 * 9.80665)` | `domains/rcs/components.yaml`'s `thruster_100lbf.isp_s` |
| `E-ENG-DYN` | `1 / 44085` | `vehicle.yaml#configurations.csm_lm_docked.mass_kg` |
| `E-CABIN-HEAT-CSM` | `1 / 125` | `zone_csm_cabin_t.conductance_w_per_k` |
| `E-CABIN-HEAT-LM` | `1 / 125` | `zone_lm_cabin_t.conductance_w_per_k` |
| `E-AMP-LOAD` | `1 / 28` | `csm_bus_a.v_nominal` — **a field that did not exist** |

Three of those needed an argument rather than a lookup.

**The RCS Isp has four declarations and one authority.** All forty-four thrusters are
`thruster_100lbf`, whose `isp_s` is declared **once**, in `domains/rcs/components.yaml`; the vehicle
file carries it three times — once per system that flies it. The edge's node is `prop_rcs`, which
all three systems draw from, so the thing it is about is the *article*, and the article's single
declaration is the honest source. Round 106's join holds the domain's copy and the three vehicle
entries equal in both directions, so the choice is safe either way — but it is a choice, and binding
`rcs_sm` would have said the shared propellant node belongs to one system.

**The two cabin conductances are two numbers that happen to be equal.** The CSM's cabin zone and the
LM's are each lumped at 125 W/K, and both edges wrote the same literal. So re-rating one cabin's
conductance would have left *both* edges describing the other's. Each edge now binds its own
compartment's field, and the fixture asserts the negative that matters: re-rate the CSM's and the
LM's edge must stay silent.

**And `E-AMP-LOAD`'s `1 / 28` had nothing to bind to.** `domains/power/components.yaml` declared
`csm_bus_a`, `csm_bus_b` and `lm_bus` with a `v_band` envelope and **no nominal**:

```yaml
  - id: csm_bus_a
    v_band: [27.0, 30.5]
```

So the 28 V that turns every watt on this vehicle into amps — the number in `E-AMP-LOAD`'s `1 / 28`,
in `E-BUS-PUMP`'s `(200 * 0.45359237 / 3600) / 28`, and inside the prose of both — was a *battery's*
`v_nominal`, the low end of a fuel cell's 27-31 V range, and a string in `apollo_diode.md:157`. Not
one of those is a declaration about the bus. The bus declares `v_nominal: 28` now, sourced from the
same places the corpus's own arithmetic already was, and the edge binds it. This is the `mu_moon`
fix arriving at the quantity every electrical conversion on the vehicle divides by.

### The three that are left, and why

Twelve edges carry a derivation. Three still carry a `computation`, and in each case the reason is
the same: **the number it inlines is declared nowhere, so there is nothing to bind it to.**

`E-BUS-PUMP`'s `(200 * 0.45359237 / 3600) / 28` needs the pump's nominal 200 lb/hr — which is not a
field on the pump, on `coolant_flow` or on the loop, whose `flow_l_min: [1.3, 1.7]` is a *band* the
1.44 L/min nominal sits inside — and it needs the coolant density to become a scalar, which is the
datum round 104 found living inside `loop_primary_thermal_mass`'s relation. `E-ZONE-ATM` and
`E-ZONE-ATM-LM` are worse: their `5 * 6894.757 / 295` carries the cabin's nominal pressure **and**
temperature, and neither is a field anywhere — `atmosphere_model` declares the volume and a
`temperature_from` naming the zone *states*, while 5 psia and 295 K appear only in its `check` prose
and in the cabin zone's own source string.

That is a debt rather than an oversight, so it is declared as one, in `coupling.yaml`'s `open_debts`,
where the count reaches it: the remedy in each case is the one `mu_moon` got. **253 became 254.**

Verified by breaking seven copies: the SPS re-rated in **both** files so the join is silent and only
the edge can catch it, the RCS article's Isp moved, the heaviest configuration's mass moved, one
cabin's conductance re-rated with the other cabin's edge asserted *silent*, the bus's nominal
voltage moved, the bus declaring no nominal at all so the source stops resolving, and the unbroken
corpus silent. Every one refused by name.

And the pins moved together, one of them by failing: `tools/plant.py` still said "253 declared
debts" in two places, and the scan added last round caught it rather than letting the number drift
the way the 202 it replaced had.

## The loops' fluid was one substance described three ways, in two relations

Round 104 opened a debt and named both ways to close it: two of the CSM's three coolant loops
declared a volume and no coolant mass, because the density that turns one into the other lived in a
sentence. The sentence was `domains/thermal/components.yaml`'s `coolant_loop_t`:

> the loop's own mass over its mixing conductance: **25 L of 62.5/37.5 glycol-water at 1,050 kg/m3
> is 26.25 kg**, with c_p about 3,600 J/kg-K, so C = 94,500 J/K; at the published **200 lb/hr**
> = 0.0252 kg/s the through-flow refreshes that mass every 1,042 s

and `loop_transport_t` states the same flow a second time, in the other unit:

> **25 L at the published 200 lb/hr = 90.7 kg/h, i.e. 1.44 L/min of glycol-water at 1,050 kg/m3**,
> gives 17.4 min = 1,042 s of transit

So three quantities — the density, the published nominal flow, and the coolant mass — were doing
real work in two relations and could be read by nothing. **And the density is not an independent
datum: it is the conversion between the two flow figures the source publishes.** 200 lb/hr is a
mass flow and 1.44 L/min is a volumetric one, and their quotient is 1,050 kg/m³ exactly.

The loops declare all three now, on both sides of the join, and the two arithmetic facts the prose
states are checked:

```yaml
    fluid_density_kg_m3: 1050
    nominal_flow_lb_per_h: 200
    coolant_mass_kg: 26.25
    flow_l_min: [1.3, 1.7]
```

**Two flow units on purpose, and each is its own source's.** `flow_l_min` is Apollo's own operating
band; `nominal_flow_lb_per_h` is TN D-6718's published primary flow. Declaring the nominal in
litres would have been a conversion nobody could check; declaring it in pounds per hour and letting
the check convert it at the declared density is what makes the two units one quantity. The rule is
that the nominal, converted, must land inside the band — 200 lb/hr is 1.43998 L/min at 1,050 kg/m³,
against 1.3–1.7 — and it fires from either end: move the nominal out, or move the density so the
nominal no longer converts into the band.

The other rule is the mass: **it *is* the density applied to the volume**, so one of the three is a
copy of a number the other two determine. The LM's loop is the negative that matters — it declares a
coolant mass and **no density**, because its mass is published directly (TN D-6724's "about 25 lb")
rather than derived from a volume, and a density back-computed from the two would be a number
invented to make a check pass. The rule applies where the density is declared, and the asymmetry is
a fact about the sources.

**That closes round 104's debt by the route round 104 recommended: 254 became 252.** The two retired
debts are the two loops' coolant masses, and the field that closed them is the density each loop now
carries.

### The comparison rule was too permissive, and a fixture that did not fire is how it was found

The fixture for the mass rule moved `coolant_mass_kg` from 26.25 to **30** in both files — and the
linter composed. The reason is the precision rule from two rounds ago: it compared at the declared
value's own precision, `significant_figures(30)` read `30` as **one** significant figure because it
stripped trailing zeros, and one significant figure of 26.25 is 30. So a coolant mass of 30 kg
"agreed" with a volume and a density that determine 26.25.

Trailing zeros count now. A person who writes `30` means thirty rather than "thirty to one figure",
and a person who writes `1.0` means two digits; counting what is written is the same reading of
precision the beamwidth tolerance uses, and it errs toward refusing, which is the direction a check
is allowed to be wrong in. The corpus composes unchanged under the stricter rule.

**With it, the third edge from last round's debt is bound.** `E-BUS-PUMP`'s
`(200 * 0.45359237 / 3600) / 28` now binds the loop's published flow and the bus's nominal voltage,
because both are declarations — so the debt shrinks to the two cabin edges, which still need the
cabin's nominal pressure and temperature to become fields.

Verified by breaking eight copies: the mass that is not the density applied to the volume, the
volume moved under a mass derived from it, the published nominal moved outside its own band, the
density moved so the nominal no longer converts into the band, the density drifted in one file only
so the *join* is what fires, the loop's flow moved under the pump edge, the LM loop asserted to owe
no density, and the unbroken corpus silent at 252.

## A field on six zone entries that nothing read, and one instance already drifted

The last two edges from round 4's debt are bound, and finding the fields they needed turned up a
declaration nobody was reading.

`E-ZONE-ATM` and `E-ZONE-ATM-LM` computed `5 * 6894.757 / 295` — the cabin's nominal pressure and
temperature — and neither number was a field anywhere. 5 psia is in the cabin zone's own `source`
string ("CSM cabin 210 cu ft habitable = 5.9 m3 at 5 psia") and in `atmosphere_model`'s `check`
prose; 295 K is in that check and in two states' relations. The zones declare both now, in **psia**
because that is the unit NR publishes and the unit this vehicle's cabin-pressure channel and its
thresholds already use, and the two edges convert it with `6894.757` as a literal — the same shape
`E-BUS-PUMP` uses for the pound. All three of round 4's unbound edges are bound, and the debt that
named them is retired.

**The zones had to be joined first, and `regulated` was the field that showed why.** The two files
list the same six zones and share almost nothing: `vehicle.yaml` carries the band and the dwell,
the domain carries the `vehicle` and the heater bank `source`. What they share is `regulated` — on
all six, in both files — and the cabins' `volume_m3`. The ids were compared, and the cabin volumes
were compared against `atmosphere_model` two rounds ago, and **`regulated` was read by nothing**. A
zone the vehicle regulates and the domain does not is a compartment with a heater on one side of the
join and none on the other, and the thresholds that watch it live on the domain's side. The zones
are compared by the intersection rule now, which is also what compares the two new nominal fields
without anybody remembering to add them here.

### The band was read; the dwell beside it was not

`thermal_diode.md:131` requires "paired heat/cool thresholds with a minimum dwell for any regulated
zone", so a zone declares a band and a minimum dwell and the domain declares the thresholds that
implement them. The two halves had no link at all.

| zone | band | dwell | implementing threshold | its dwell |
|---|---|---|---|---|
| `csm_cabin` | 10–30 | 30 s | `csm_cabin_low`, `csm_cabin_high` | 30,000 ms |
| `csm_avionics_bay` | 10–50 | **30 s** | `avionics_plate_high` | **10,000 ms** |
| `lm_cabin` | 10–32 | 30 s | `lm_cabin_low`, `lm_cabin_high` | 30,000 ms |

`limit_c` was at least read — `check_cabin_equilibrium` holds the cabin's equilibrium inside it.
**`dwell_min_s` was read by nothing at all**, on any of the six zones, while every threshold beside
it carries its own `dwell_assert_ms`. And one of the three had already drifted: the avionics bay
requires thirty seconds of dwell before it alarms and the threshold that holds its band alarms after
ten. That is this folder's oldest sentence — *a declaration no tool reads has already drifted* — and
it was true of a field on six entries, four of them read by nothing and one of them wrong.

The link is declared, `implemented_by` on each regulated zone, and `csm_avionics_bay` is why it
cannot be inferred: its band is held by a **plate** threshold on `thermal.avionics_plate_c` rather
than by a zone channel, while `lm_descent_bay` has a warning threshold *inside* its band that is not
the band. A regulated zone that declares a band and names nothing implementing it is refused, and a
threshold whose limit falls outside the band it is said to implement is refused.

**The dwell divergence is a debt rather than a refusal**, and the difference matters: which of the
two figures the vehicle means is a decision, not a contradiction. Either the threshold holds the
dwell the zone requires — the reading `thermal_diode.md:131` suggests, since a minimum dwell is the
requirement and the threshold is its implementation — or the zone's thirty seconds is the cabin's
requirement written onto a compartment whose instrument moves faster. **252 stayed 252**: the debt
round 4 opened for the three unbound edges is retired, and this one takes its place.

Verified by breaking eleven copies: `regulated` flipped in the domain in both directions, the
cabin's nominal temperature drifted in one file and then in both (the first is the join's business
and the second is only the edge's), the LM cabin's nominal pressure drifted in both under its own
edge, a regulated zone stripped of its link, a link naming a threshold that does not exist, a band
whose threshold's limit falls outside it, the unbroken corpus silent — and the two negatives that
make the dwell rule a rule: the avionics bay's divergence reported once, and the two cabins, whose
dwells already agree, reported not at all.

## Two verbs offered names the vehicle does not have, and a loop with nothing on it

The round started from a probe rather than a hunch: `set_coolant_pump` offers `pump_1`, `pump_2` and
`pump_lm`, so **adding a fourth — `pump_9` — composes**. Nothing bound a verb's argument to the
objects it names, and the console validates a command against exactly that enum, so a name in it is
a name the record accepts. A fleet offered a pump the vehicle does not have gets a resolved command
and no machinery.

The binding is inferred from the argument's *name* wherever it can be — an argument called `pump`
names pumps, `hatch` names hatches, `engine` names engines — which reaches **fourteen arguments
across six domains** and is silent on vocabularies, which is the point: `mode: [primary, secondary,
series, isolated]` names no object and must not be read as one.

**The alternative trigger was measured rather than guessed**, and it is wrong five times on this
corpus: "every value in this enum is a component id" refuses `select_sensor.group`, which names
`imu` and `radar` beside `cabin_pressure` and `co2`; `ask_crew.position`, which names `tunnel`
beside five crew stations; `select_nav_source.source`, which names `radar` beside four modes; and
two more. A rule that refuses five legitimate declarations is a rule nobody keeps, so the name is
the trigger and the file can declare when its name misleads — which two arguments do:
`select_antenna.antenna` names the *vehicle-level* antenna ids (which the domain's components claim
through `vehicle_keys`) and `set_source.source` names the fuel cells **and** the batteries, which
are two classes here, `source` and `storage`.

### What it found

| verb | argument | was | is |
|---|---|---|---|
| `set_hatch_valve` | `hatch` | `crew_csm`, `crew_lm`, `tunnel` | `hatch_crew_csm`, `hatch_crew_lm` |
| `set_docking_latch` | `interface` | `csm_lm_forward` | `docking_interface` |

**Four names, and three of them appear nowhere else in the corpus at all** — not as a component, a
state, a coupling node or a vehicle entry. `crew_csm` and `crew_lm` exist only inside
`set_hatch_valve`'s enum, and `csm_lm_forward` only inside `set_docking_latch`'s, so a fleet
commanding either named something the vehicle has never heard of. Beside them, the two hatches the
structure domain *does* declare — `hatch_crew_csm` ("the CSM hatch") and `hatch_crew_lm` ("the
hatch that opens onto the lunar surface") — were commanded by nothing. The tunnel is the volume
between those two hatches rather than a third one, which is why it is not in the list.

### And the loop half found the round's real gap

Three pumps and a bypass valve each carry a `loop` field naming their circuit, and **no tool read
it**. The field is the whole of the membership question, and writing the link down answered it:

| loop | what is on it |
|---|---|
| `loop_primary` | `pump_1`, `pump_2`, `bypass_valve` |
| `loop_lm` | `pump_lm` |
| `loop_secondary` | **nothing** |

No pump drives the secondary loop, so it cannot flow — and three verbs offer it as a `loop`
argument while `set_coolant_loop`'s `mode` enum exists to select between the strings. A command
team can put the vehicle on a loop that cannot flow, and the plant would have no pump speed to
derive a flow from. **That is a debt rather than a refusal**: the second string is a real thing
`apollo_diode.md:97` describes ("two water-glycol loops with primary and secondary pumps"), and
what is owed is whether the model carries its pump, or whether the vehicle has a passive spare the
command surface should not offer. **252 became 253.**

**And the check's own first version repeated the thing it was written to catch.** It kept the class
wording in its refusal whenever the *argument's name* matched a class, so `set_source.source` —
which declares the wider `names: component` — refused a `battery_9` with "not a `source` in this
domain" while the test that had actually run was "not a component of this domain". A refusal that
misdescribes its own rule is the defect this whole effort exists to remove, and what found it was a
fixture whose needle did not appear — the same way round 5 found the permissive comparison.

Verified by breaking ten copies: a pump, a loop and an engine the vehicle does not have offered as
argument values, a hatch argument offering the tunnel (a component, but not a hatch), a
vehicle-entry argument naming a domain component instead, a component argument naming something
that is not one, an argument declaring a vocabulary the rule does not know, a pump placed on a loop
that does not exist, the one loop with nothing on it reported once and the two with machinery
reported not at all, and the unbroken corpus silent at 253.

## One value stood for two hatches, and the gate said "any hatch"

`hatch_state` was `enum[closed,latched,open]` — **one value for `hatch_crew_csm` and
`hatch_crew_lm`** — while three things around it were written per hatch:

- `set_hatch_valve` names *one* of them (and since the last round it names the two real components);
- the threshold that guards the cabin's differential pressure carries
  `gated_by: "any hatch in the open or latched position"` — the corpus's own words for the fact that
  the channel cannot say which;
- two faults in the domain are written against `hatch_crew_lm` — a seal leak and a stuck position
  switch — and both perturb the shared value, so an LM hatch indication failure and a CSM one are
  the same telemetry.

So the vehicle could be in two different conditions, the CSM tunnel hatch open with the LM hatch
sealed or the reverse, and publish one word for them. The LM's hatch is the one that opens onto the
lunar surface and every opening costs an atmosphere; `mission.yaml#objectives.surface_mission` names
this channel as part of what settles an EVA, and it could settle *that* a hatch moved but not which.

**The domain already had the right shape two entries down.** `docking_latch_state` is
`map[latch_id, enum[open,engaged,released,failed]]` and its own note gives the argument:

> apollo publishes docking_latches as a count with a 'latch disagreement' event, which only means
> something if the individual latches are modelled. A count cannot express `eleven of twelve
> engaged`, and `eleven of twelve` is the state a crew has to reason about.

The registry has carried `map[...]` channels since `controls.breakers`, and both are published, so
`hatch_state` becomes `map[hatch_id,enum[closed,latched,open]]` and its channel mirrors it. This is
the corpus's own convention arriving at the one state that was not keyed by its article — the same
shape as the regulator position and the stage configuration that earlier rounds found, and the same
fix the latches already had.

### And it found a hole in the binding that holds a channel to its state

That binding compares the two `enum[...]` lists, so a channel whose *value* vocabulary drifts is
refused — and it has been for rounds. **It did not compare the key.** Change the channel's
`map[hatch_id, ...]` to `map[latch_id, ...]` and the linter composed; the fixture written to catch
the key drifting is what found it. The key is half the claim a keyed channel makes, so it is
compared now, from either side.

The asymmetry is deliberate and the difference matters. A channel that promises a key over a state
holding a **single value** is refused: it is publishing a value the vehicle cannot hold. A channel
that publishes *something about* a keyed state gets a **note** instead — `cw.active_lights` is a
list of which systems are lit, and `structure.docking_latches` is a count of
`docking_latch_state`'s members whose own debt carries what it owes.

**The note's first version was wrong, and the same way round 7's refusal was.** It fired on all
eight channels whose `from:` is a keyed state, describing seven *template* channels —
`avionics.sensor_health_[class]` publishes `sensor_health`'s value for one class — as projections.
A channel whose own name carries a placeholder is the keyed state instantiated one key at a time,
which is the opposite of what the note said about it. It now fires only where the name carries no
placeholder to instantiate the key, which is one channel, and the fixture asserts that count and
names it.

Verified by breaking six copies: the channel's key type changed under the state's, the state's
under the channel's, the channel promising a key over a state that holds one value, the value
vocabulary drifted, the one channel that publishes about a keyed state asserted by name with the
seven template channels asserted silent, and the unbroken corpus composing at 253.

## The check that found the swapped supply found its own inputs by convention

`check_cabin_equilibrium` is the check whose docstring says the difference it makes is visible:
`loop_primary` declared `supply_c: [2.8, 7.2]` and `evaporator_outlet_c: 5.3` against a source
reading *"mixed supply 45 F = 7.2 C, evaporator outlet 41.5 F = 5.3 C over a 37-45 F range"*, and
either assignment is plausible in isolation — while the lower end puts both cabins under their own
10 C floor on every cold pass. That check found all four of its inputs itself:

| input | how it was found |
|---|---|
| the loop | `served = {"csm_cabin": "loop_primary", "lm_cabin": "loop_lm"}` — a literal in the tool |
| the heat state | `f"cabin_heat_{zone.split('_')[0]}_w"` — string surgery on the zone id |
| the cabin's temperature state | a scan for a node named `cabin_zone_t` or `lm_cabin_zone_t` |
| the equilibrium state | `f"cabin_eq_{zone.split('_')[0]}_k"` |

**And every one of those lookups ended in a `continue` or an `if ... is not None`.** So the probe
this round started from is a rename: `cabin_heat_csm_w` → `cabin_heat_csm_x`, and the linter
**composed**. The check that makes a swapped supply visible went quiet and said nothing — this
folder's oldest sentence, arriving at the check that exists to make a difference visible.

Writing the fixture that unsets the heat rate found the other half. `float(heat.get("total_w") or
0)` is zero for a missing value and a `ValueError` for the string `UNCONFIGURED`, and the committed
version does exactly that:

```
File "check_vehicle.py", line 8133, in check_cabin_equilibrium
    equilibrium_c = float(supply) + float(heat.get("total_w") or 0) / float(
ValueError: could not convert string to float: 'UNCONFIGURED'
```

— a traceback instead of a report, from the input the check most depends on. That is the failure
`test_an_unloadable_vehicle_refuses_instead_of_crashing` was written for, and the second time a
check has been caught crashing on the value it exists to diagnose.

### The links are declared now

Each cabin zone declares what it is in the model:

```yaml
        cooled_by: loop_primary
        temperature_state: zone_csm_cabin_t
        heat_state: cabin_heat_csm_w
        equilibrium_state: cabin_eq_csm_k
```

The check resolves them, **refuses one that does not**, and refuses a state that does not carry the
figure it is named for — a `temperature_state` with no `conductance_w_per_k`, a `heat_state` with
no numeric `total_w`. The hand-written `served` map and both pieces of string surgery are gone.

The links are declared on both sides of the zone join, so the join compares them: a zone whose model
is described differently in the two files is two compartments wearing one name. And **the two tests
that used to repeat the pairing read the declaration now** — they held a hand-written list of
`(zone, loop, heat, cabin, equilibrium)` tuples, which is the same defect one level up, in the file
that exists to catch it.

Verified by breaking nine copies: each of the four links renamed with both files moved so the join
is silent and only the resolution can catch it, an unset heat rate, a temperature state stripped of
its conductance, a supply that puts the cabin under its own floor, an equilibrium state that no
longer re-derives, and the unbroken corpus composing at 253 — which did not move, because this
round declared links that were already being used rather than filling anything in.

## Which declared links a rename silences: asked of the corpus, not of the tool

The last round found one check resolving four of its inputs by convention. The question this round
asked is the same question asked of everything: **which declared references does the linter
actually resolve?** It was answered by measurement — one broken reference per case, a field whose
value names another declaration changed to a name that exists nowhere, eleven cases:

| declared reference | verdict |
|---|---|
| a state's `moved_by` naming a verb | **held** |
| a state's `moved_by` naming an event | **held** |
| a threshold's `point` | **held** |
| a point's `from` | **held** |
| a fault's `perturbs` | **held** |
| a one-way event's `observable` | **held** |
| a verb's `interlocks` | **held** |
| a `heat_inputs` key naming a zone | **held** |
| **a channel row's `inputs`** | quiet |
| **a crew position's `controls`** | quiet |
| **a thermal zone's `source`** | quiet |

Three quiet, and each is **the unchecked half of a pair the linter already held somewhere else**:
`inputs` beside the threshold's `derives_from`, `controls` beside the same position's panels'
`shows`, and a zone's `source` beside the zone's `implemented_by` and `cooled_by`.

### `inputs` was required and never read

The sharpest of the three, because it is not merely unchecked: **every `derived` row must carry
`inputs`** — so the corpus has thirty-odd lists of channel names — and no tool had ever read a value
in one. The first check that did found the reconciliation channel's own residual:

```yaml
    inputs: ["res.ledger_[resource]_kg", "res.[resource]_kg"]
```

`delta_recon = observed - ledger`, says the row's own note, and the row's own provenance says "the
residual is only evidence if both of its terms are visible". One of the two terms — the
observation — was `res.[resource]_kg`, **a channel no row has ever declared and none can**: the six
resources publish their amounts under names of their own (`o2_remaining_kg`, `water_potable_kg`,
`battery_energy_wh`), so there is no template to point at. The residual's terms are both named now,
and the first list of reconcilable resources is written down.

`controls` is the same shape one file over: a position's panels' `shows` is held to the registry
*and* to that position's perception bound, while the position's own `controls` — the channels a
crew member can operate — was held to neither. All seven controls are registered and perceivable
today, so the rule costs nothing and closes the pair.

And a zone's `source` is the heater bank that drives it, declared on the domain's zones and resolved
by nothing.

### Two things went wrong writing it, and both are this folder's shapes

The `inputs` resolution first ran **inside the row loop**, where the registry is still being built,
and refused eleven legitimate forward references — a value looked up before the thing it points at
exists. It resolves in a second pass now, with the whole registry in hand, through the same
`ChannelIndex` every other channel reference uses.

And the zone-source check read `vehicle.yaml`'s zones, **where `source` is not a field at all**:
`source` and `vehicle` are on the domain's zones and not on the vehicle file's, while `limit_c`,
`dwell_min_s` and `implemented_by` are the other way round. A field only one side carries is
invisible to the intersection rule by construction — so the round-6 comment claiming `source`
"belongs in the comparison" was wrong about the data, and the check was vacuous until the probe
said so. The comment is corrected and the asymmetry written down where the next person will meet
it: **a one-sided field needs its own reader**, and `vehicle` has not got one.

Verified by breaking seven copies: an input naming a channel that does not exist, an input naming a
template instantiation that does not resolve, a control that is not a channel, a control the
position cannot perceive, a zone's source naming a heater that does not exist, the residual asserted
to name both of its terms, and the unbroken corpus composing at **253** — which did not move,
because this round resolved references that were already written.

## Sixty-seven declarations of which spacecraft, held against nothing

Every power source, load and bus, every atmosphere, every thermal zone and loop, every component
the crew can be in says which spacecraft it belongs to. That is **sixty-seven `vehicle:` fields**,
and the probe is one word: `vehicle: csmx` on a power component composed, and so did a thermal zone
moved to a spacecraft that does not exist. No list existed to hold a value against.

**And the rule could not simply be written, which is the round's real finding: the key carried
three meanings.**

| sense | where | count |
|---|---|---|
| a spacecraft id | the domains, `vehicle.yaml` | 67 |
| the *document* the mission registers its channels against | `mission.yaml` — `vehicle: vehicle.yaml` | 1 |
| the narrative of what this side does about a contract check | `presentation.yaml`'s conformance table | 12 |

One key, three meanings — and the third was **enforced**: the linter requires every conformance row
to carry a non-empty `vehicle`. So the overload was not an accident of naming that nobody noticed;
it was a rule. And a key with three meanings is a key no rule can be written about, which is why the
sixty-seven went unresolved.

The two minority senses are renamed — `vehicle_document` and `disposition` — and the list the
majority is held against is `vehicle.yaml#spacecraft`, **the only place in the corpus that says how
many spacecraft there are**:

```yaml
spacecraft:
  - csm
  - lm
```

Two of the sixty-seven are held as well and are not a component's: the keys of
`atmosphere_model.volume_m3`, which `check_cabin_volumes` uses as the authority for the cabin
volume and which were a second, silent enumeration of the same fact.

The list itself is guarded against the shape that would be worst: `spacecraft: csm, lm` parses to a
*string*, and iterating a string gives its characters — so the guard refuses a malformed list rather
than emitting sixty-seven refusals about spacecraft named `c` and `s`. Absence and emptiness are the
section check's business, which is what adding the section to `VEHICLE_SECTIONS` buys.

**253 stayed 253** — the round declared a vocabulary that was already in use and renamed two keys
that meant something else, filling nothing in.

Verified by breaking eight copies: a component on a spacecraft that does not exist, an atmosphere
keyed by one, the list emptied and the list malformed, the document reference renamed back to the
overloaded key, the conformance column renamed back, the two renamed senses asserted gone from the
corpus, and the unbroken corpus composing at 253.

## The charge was published, already checked, and owed anyway

Ten stocks declared `initial: UNCONFIGURED`, each with a note saying what would close it. The notes
are honest and nine of them are still right: six cabin gases ("no source gives the fractions"), two
tank splits that are modelling decisions rather than figures — `prop_main_kg` is one node for three
engines' declared loads (SPS 18,508 kg, LM descent 8,248, LM ascent 2,376), `prop_rcs_kg` one node
for three RCS loads — and a helium charge nothing publishes. The tenth was
`power.battery_charge_j`, and its energy had been in the corpus the whole time:

| declaration | says |
|---|---|
| `domains/power/components.yaml#load_budget.csm_battery_energy_wh` | 3,360 |
| the three `battery_csm_*` cells beside it | `ah: 40` at `v_nominal: 28`, three of them |
| `check_power_inventory` | holds those two against each other, and had since it was written |

3 x 40 x 28 = 3,360 Wh, and the check that holds the product already existed. The state integrates
**joules**. So the corpus had the figure, the reconciliation and a one-line conversion, and paid for
a debt paragraph instead — which is the cost of a missing idiom rather than a missing number.
**253 became 252**, and the plant's unset scalars went 199 to 198.

`initial_source` names the other declaration, and it needed the one thing a threshold has had since
`derives_from` was written and an initial never did: a **factor**.

```yaml
  - id: battery_charge_j
    method: stock
    node: battery_energy
    unit: J
    initial: 12096000
    initial_source: "domains/power/components.yaml:load_budget.csm_battery_energy_wh"
    initial_factor: 3600
```

The check compares `initial` against `source x factor`, refuses a factor that is not a number or is
zero and a source that resolves to a non-number, and **names the factor in the refusal** when there
is one — so a reader can tell whether the wrong figure is the source or the conversion. The source
is the *CSM's* figure and not the LM's for a reason the old note had already asked for: which group
presents to the one node is a question about the node's edge, and the edge runs to `bus_a`.

### The source named a file the check had never loaded

`check_initial_sources` resolved its paths against `vehicle.yaml` and `coupling.yaml`. That was
every source a stock had until this one, and it was the narrowest of the three path idioms for no
reason but the order they were written in: `derives_from` and an edge's `derivation` have always
resolved against every document. The refusal it would have given is the tell — *"names
'domains/power/components.yaml', and the documents this check can resolve are ['coupling.yaml',
'vehicle.yaml']"* — a link called broken because the reader was narrow. It takes the same
`documents` map as the other two now.

### The debt entry had been false for hours, and nothing could have said so

The entry this round answers had said since the list was written: *"`demand_w`, `inrush_w`, `bus`,
`rated_w`, `ah` and `v_nominal` are read by nothing — 17 components and 25 loads ... `battery_charge_j`
declares no capacity at all: what the vehicle actually carries in joules exists only as a product
nobody computes."*

Four of those six fields are read now, and the last clause was about to be answered by this round.
By the time it finished, every clause of the entry was false and the entry was still in the list,
being counted.

**Nothing reads a debt.** `open_debts` is counted, printed, and grouped by the scalar each one
wants; not one word of a claim inside it is held against anything. So a debt can be answered in the
tool and left standing in the corpus, and the count — the one number a reader does see — cannot tell
the difference. This is the folder's thesis arriving at the folder's own accounting: *a declaration
no tool reads has already drifted*, and the debts are declarations. It is also why the entry is
rewritten here rather than deleted: **252 then stayed 252**, because a residue that is real stays a
debt even when the sentence around it was wrong.

### The residue was `inrush_w`, and it was closable

What the entry said was still true was that `inrush_w` was read by nothing. Twenty-five loads declare
it beside their steady draw, the file's own comment introduces it as the reason "a starting transient
is what makes a marginal bus drop a pump — the flagship chain of the experiment", and **no two
numbers had ever been compared**: the field the whole experiment turns on was free to say a load
draws less while starting than while running. Eight of the twenty-five declare a real transient
(1.4-1.7x, the fans, scrubbers and pumps); seventeen declare it equal to the demand, which is what a
load with no motor in it should say.

So the fifth relationship went in, and it is the fields' own definition and nothing more: a starting
transient is not below the steady draw it settles to. Relationship 1 also gained its precondition,
because it summed `row.get("demand_w") or 0` — a closure that holds on a hole.

What is left is the check that would *use* the figure: the **bus peak at switching**, which is the
number the flagship chain turns on — a 72 W amplifier switching on against a 250 W coolant pump whose
own start is 420 W, where the sag is what drops the pump and the pump is never the root fault. That
needs the per-bus routing the source-capacity relationship declined to assume (which bus a source
ties to is a routing decision the tie makes and no file declares), so on this corpus the transients
are bounded and unspent rather than missing. The debt says that now instead.

### The test that pins this was narrow in the same two ways

`test_every_stock_declares_where_it_starts` holds the initial conditions, and it carried its own copy
of the defects the check had: it resolved `initial_source` against the same two documents, with its
own hand-written walk, and it pinned the inventory at nine seeded stocks. It reads the linter's
`resolve_dotted` now — one rule, not a second copy of it — loads every document the corpus has,
applies the factor, and pins ten.

And the refusals had never been fired at all. `check_initial_sources` was written with the initial
conditions and no test ever broke one, so every surface of it was unexercised — which for this idiom
matters more than for most, because the whole point of `initial_source` is that it is not a
provenance note: it is the other declaration, resolvable and comparable, and a check whose refusals
never fire is a check whose silence means nothing. The new test breaks it nine ways: the two
declarations drifted, the factor dropped, wrong, textual and zero, a document that is not loaded, a
key the document does not have, a value that is not a number, and a path that names no document at
all.

**173 became 174 vehicle tests**, and the pins moved together: the debt count (252, in the status
paragraph and the state line this section restates), the plant's unset scalars (198), the
reconciliation's test count, and the plant's own copy of the debt figure. Plant: 108 of 134 states
fully configured, 26 with a debt, 57 of 77 edges carrying a sensitivity. `--strict` exits 2.

Verified by breaking thirteen copies in `.scratch/r12/`: the charge drifting from the energy its
source publishes (the source of a charge is published in Wh and the state integrates joules), the
factor dropped and so read as joules, the source naming the other spacecraft's battery, the source
ceasing to resolve, the factor zero and non-numeric, an initial that still resolves the way it always
did, a load starting on less than it runs on, a load declaring no transient and a load declaring no
steady draw, the battery no longer reported as owed, the plant counting one fewer unset scalar, and
the unbroken corpus silent.

## The scheduler's own declaration was the one nothing scheduled

Every domain file opens with two lists, and power's header says what they are for in one line:
*"Reads and writes are node ids from coupling.yaml. Domains do not call each other (plant.md §2);
these declarations are what the scheduler orders."* Eleven domains, one declaration each — and
`writes` has been held in **both** directions since the domain check was written: a state that
advances a node its domain does not claim is refused, and a declared write that no state advances is
refused because the node never changes.

`reads` was held in neither. Every entry had to resolve to a coupling node — that is the whole of
it — so a declaration of what the scheduler orders could omit the node an edge actually carries, and
the only thing that would notice was a reader. **Eleven were missing**, across six domains:

| domain | node | what carries it |
|---|---|---|
| consumables | `fc_o2_draw` | `E-FC-WATER` — 1.126 kg of reaction water per kg of O2 the cell consumes |
| consumables | `co2_removal_csm`, `co2_removal_lm` | `E-ATM-ABSORB`, `E-LM-ATM-ABSORB` — the rates that spend the man-hour counters |
| power | `coldplate_t` | `E-PLATE-BAT` — the derate on usable battery capacity |
| power | `link` | `E-AMP-LOAD` — the transmitter's 1/28 A per W on `bus_a` |
| power, eclss, propulsion | `command_executive` | `E-CMD-BUS`, `E-CMD-ATM`, `E-CMD-ENG` — one signal into each domain's own node |
| propulsion, rcs | `guidance` | `E-GNC-ENG`, `E-GNC-RCS` — the closed loop and the commanded valve set |
| crew | `water_potable` | `E-CREW-WATER` — 2.27 kg per crew-day, the one draw that is a rate of people |

`plant.md` §4 lists this among the things the linter refuses — *"A domain that reads a node it does
not declare, or writes one it does not own"* — and the write half had been implemented while the
read half was not. So the check is a refusal, and it is the exact statement of the fault: an edge
carries X into a node this domain writes, and the domain's own declaration does not name X.

**One of the eleven had the omission written down as a completeness claim.** `domains/rcs/`'s header
read *"Every node here has an edge into an RCS node in coupling.yaml, and the four are exactly the
ones that do"* — while `E-GNC-RCS` carried a fifth, `guidance`. A claim with no reader reads as a
statement of fact for as long as nobody counts, which is this folder's thesis arriving at a comment
whose whole purpose was to say the list was finished.

### The other direction is not a refusal, and that is the finding

Twenty-four declared cross-domain reads have no edge at all, and they are three different things:

| | count | example |
|---|---|---|
| the delayed half of a loop `coupling.yaml#cycles` **does** name | 1 | `thermal` reads `link`, and `E-PLATE-COMM` is C-AMP-BUS-PUMP's declared back-edge |
| a loop the edge list **does not close** | 8 | `eclss` reads `absorber_capacity_csm` and writes the removal rate that drains it; `crew` reads its own cabin |
| a one-way dependency the graph does not contain | 15 | `gnc` reads `bus_a` to stay alive; `propulsion` reads `pressurant_he` |

The middle row is the one with teeth, and it is the failure `plant.md` §4 is written against: the
graph is a DAG, so `derive_schedule` emits a total order, and whichever of the pair runs second is
reading a stale value that looks like physics — with no cycle declared to say so. The corpus already
has the machinery for it (`coupling.yaml#cycles`, a named back-edge) and these eight have nothing to
name.

The bottom row is why the direction is counted rather than refused. **One key, two meanings**: a
dependency that has not been modelled and an observation that needs no edge are written the same
way. `consumables` reading `battery_energy` is a resource ledger; `gnc` reading `bus_a` is a guidance
computer that stops when the bus does. Nothing tells them apart, so what is owed is either the
missing edges or the second key that names the difference — the same shape as `range` before
`range_kind`, and `source` before it was split three ways.

**252 became 253**: one new debt, carrying all twenty-three names in its text rather than a count
that would have to be trusted.

### And it found the next one on the way past

`domains/consumables/components.yaml` has a `consumers:` block — twelve draws on the stocks, *"with
the edge it corresponds to in coupling.yaml or the reason it has none"*. Seven name an edge, five say
why not. **The section is read by nothing at all**: the word `consumers` does not appear in any tool
in the folder, so twelve `rate_kg_s` declarations and seven edge references are a description rather
than a model, and the file's own comment about the five with no edge is the only place the question
is asked. That is the next round's instrument reading, and it is the same finding as this one: a
declaration is only as good as the check that reads it.

Verified by breaking ten copies in `.scratch/r13/`: four domains losing a declared read, an edge
re-pointed at a node its consumer never declared, a read that stops resolving (the rule that always
held it, still holding), an extra read with no edge raising the debt's own number without refusing
anything, the unbroken corpus composing at 253 and refusing nothing, an internal read not counted as
cross-domain, and the one read inside a declared cycle named as declared.

**174 became 175 vehicle tests**, and the pins moved together: the debt count (253), the
reconciliation's two, and the plant's own copy. Plant unchanged: 108 of 134 states fully configured,
26 with a debt, 57 of 77 edges carrying a sensitivity, 198 unset scalars. `--strict` exits 2.

## Twelve draws on the stocks, and the block that had no reader

Round 13 ended by naming this one, having found it on the way past: `domains/consumables/
components.yaml#consumers` is the domain's ledger of every draw on a stock — twelve entries, each
naming the stock it draws down, the coupling edge it corresponds to and a rate — and **the word
`consumers` appeared in no tool in the folder.** What made the omission visible is the block's own
header, which promises something checkable: *"Every draw on a stock, with the edge it corresponds to
in coupling.yaml **or the reason it has none**."*

Four rules, and the first three are that sentence taken literally: the stock is a node this domain
advances with a stock state; a named edge is declared **and touches that stock**; an entry with no
edge carries the reason; and a declared `derivation` is evaluated against the entry's rate by the
same rule the edges use — `check_declared_derivation`, extracted from `check_edge_derivations` so
that the twelve refusals that hold a derivation have one implementation rather than two.

**Seven of the twelve failed the sentence, and one of them had already been caught once.**

| entry | was | is |
|---|---|---|
| `fuel_cell_o2` | `edge: E-FC-DRAW-O2` | `E-O2-DRAW` |
| `fuel_cell_h2` | `edge: E-FC-DRAW-H2` | `E-H2-DRAW` |
| `fuel_cell_product_water` | `rate_kg_s: 0.45` | `UNCONFIGURED`, with the reason |
| `crew_metabolic_o2` | no edge, no reason | the reason, and a derivation |
| `cabin_leak` | no edge, no reason | the reason, and a derivation |
| `evaporator_water` | `edge: E-RAD-WATER`, `rate_kg_s: 8.9e-4` | `E-WATER-RAD`, `9.5714e-4`, with a derivation |
| `crew_potable_water` | no edge | `E-CREW-WATER`, with a derivation |
| `co2_removal`, `co2_removal_lm` | no edge | `E-ATM-ABSORB`, `E-LM-ATM-ABSORB` |

The two fuel-cell references are the pair-of-edges failure: `E-FC-DRAW-O2` is `fuel_cell ->
fc_o2_draw`, the edge that *computes* the draw, and the edge that takes the stock down is
`E-O2-DRAW`, `o2_csm -> fc_o2_draw`. They are one hop apart, they are both about the same kilogram
of oxygen per second, and a reference to either reads like a reference to the other.

### The 0.45 was already known to be fabricated

`E-FC-WATER`'s own note is the record of an earlier round finding this number:

> *this read 0.45 until the arithmetic was done, and 0.45 is not a number this reaction can produce:
> it asserts that 55 % of the reactant mass becomes neither electricity nor water nor heat, in a
> plant that conserves its stocks exactly. The old `relation` read "0.45 kg of product water per kg
> of reactants consumed", which is the value restated with a unit phrase attached and derives
> nothing — that is what a fabricated number looks like when it is wearing a derivation's clothes.*

The edge was corrected to 1.126 and a check was written to re-evaluate every `computation`, which is
why the edge cannot drift again. The **consumer** entry for the same reaction still read
`rate_kg_s: 0.45`, with that sentence, word for word, as its own relation — because the fix went
into the file that had a check and the copy was in the block that had none. It is now owed rather
than guessed, which is also the honest answer: the rate is 1.126 x the cell's oxygen draw, and that
draw is the unpublished kg-per-kWh figure its two sibling entries are already unset for. *Three
names for one unknown*, and the debt count went **up** by two because a fabricated number became a
declared one.

### And the water rate was the constant round 61 replaced

`evaporator_water` declared 8.9e-4 kg/s with a relation citing 2.45e6 J/kg — and 2,345 W / 2.45e6
J/kg is **9.5714e-4**, not 8.9e-4, because 8.9e-4 is 2,345 W at 3.8e-7 kg/s per W. That 3.8e-7 is the
figure `E-RAD-WATER` carried until round 61 swapped the water cycle's edges and left it computing
`1/2.45e6`; the edge was corrected, a `computation` was added to keep it corrected, and the two
copies in this file — the consumer's rate and `water_cooling_kg`'s `min_flow_per_s`, whose note did
the arithmetic out loud — kept the old constant for another forty rounds. The check now holds the
rate against `E-WATER-RAD`'s own per-watt figure and the evaporator's published capacity, so the
chain is three links long and every one of them is evaluated: 2.45e6 J/kg (the edge's `computation`)
→ 4.0816e-7 kg/s per W (the swapped edge) → 9.5714e-4 kg/s (the consumer's derivation).

### Six rates are held against their own arithmetic, and two deliberately are not

| entry | expression | what it binds |
|---|---|---|
| `evaporator_water` | `rejection_w * kg_per_s_per_w` | the thermal domain's evaporator capacity, the edge's own sensitivity |
| `sps_burn` | `thrust_n / (isp_s * g0)` | `vehicle.yaml#propulsion.sps` |
| `rcs_firings` | `thrust_n / (isp_s * g0)` | `vehicle.yaml#propulsion.rcs_sm` |
| `crew_potable_water` | `water_kg_per_crew_day * crew / seconds_per_day` | the metabolic table, `mission.yaml#crew.size` |
| `crew_metabolic_o2` | `o2_kg_per_crew_day * crew / seconds_per_day` | the same, at the oxygen rate |
| `cabin_leak` | `csm_kg_per_h / seconds_per_hour` | `vehicle.yaml#consumables.leak` |

The two absorber rates are named against their edges and **not** given derivations, and the reason is
in the file rather than left implicit: their rate is `E-ATM-ABSORB`'s 26.37 man-hours per kg times
eclss's removal rate, but 26.37 is itself a four-figure rounding of `1/0.03792 = 26.3713`, so a
derivation from the edge's value would refuse 5.56e-4 at its third figure on a rounding chain rather
than on a disagreement. That is a fixture the verifier keeps, so the judgement is checked rather than
asserted.

**253 became 255, 198 became 200, and 175 became 176 vehicle tests.** Plant unchanged otherwise: 108
of 134 states fully configured, 26 with a debt, 57 of 77 edges carrying a sensitivity. `--strict`
exits 2.

Verified by breaking twelve copies in `.scratch/r14/`: the evaporator's rate back at the constant the
edges carried before round 61, a consumer naming the edge that computes the draw, an edge that is not
declared, a stock this domain does not integrate, an edge-less consumer losing its reason, a
derivation's source renamed and a source that is not a source, a binding the expression never uses
and a name it never binds, an entry with a derivation and two rates, the LM absorber's rate derived
from the rounded sensitivity (which refuses, and is why it is not declared), and the unbroken corpus
composing at 255.

## The permission the machine exists to grant, and the guards that read nothing

The unread-field sweep, run over the whole corpus rather than over one block, put
`hazardous_actions_permitted` at the top of what it found: **ten declarations, in the block that
decides what the challenge *is*, and no tool in the folder mentioned the field.** Five of the six
fields around it were validated — the ids are unique, `terminal` is a boolean, `entry` and `exit`
are non-empty, the transitions name declared postures — and the sixth, which is the permission the
execution machine exists to grant, was compared to nothing.

Reading it produced four rules, and the corpus already satisfied three of them:

| rule | today |
|---|---|
| every posture declares it, as a boolean | 10 of 10 |
| a terminal posture permits nothing — `mission_diode.md`:1727's *"no hazardous verb is enabled in ABORTED"*, verified statically at startup | `complete`, `aborted`: false |
| the machine permits hazardous actions *somewhere*, or it can authorize nothing | `execute`: true |
| every transition **into** a permitting posture re-reads `mission.abort_latched` — TV-E (`:1395`), and invariant E's vehicle-side half | `prepare -> execute` does |

The fourth is the one with teeth and it is the one the machine happened to get right: the latch is
re-read on the way into the only posture that can act, and now nothing can stop doing that without
the linter saying so.

### Four of the eight non-terminal postures could not be left by any guard that reads anything

The same reading found the round's real defect, and it is not in the field — it is one block down.
`transition_evidence` had five entries for a machine with ten postures, and the five covered the
*forward* path: safe → standby → prepare → execute → hold, plus the abort. So `init`, `hold`,
`recovery` and `aborting` were the source of no transition at all, which means the guards that leave
them — `INITIALIZED`, `g_resume`, `g_standby`, `SAFE_EFFECTS_CONFIRMED` — declared **no telemetry
dependency**, while `mission_diode.md`:1720-1729 puts *"every guard declares telemetry
dependencies"* in the list a startup verifies statically.

**The three that matter are the three that resume something**, and the corpus had already written
the argument for them, one transition away. `safe -> standby`'s entry says it in as many words:

> *a latched abort dominates the transition; deciding on a stale latch is how a fleet resumes an
> abandoned mission.*

Resuming from `hold`, standing down from `recovery` and confirming safe effects after an abort are
the same decision about the same latch, and none of the three said what it read. All four are
declared now, with the reasons in the file: the latch and the mode on all three, the phase on the
two that resume into a mission, and `gnc.body_rate_xyz_deg_s` on `hold -> execute` — the channel
whose staleness is one of the two ways *into* `hold`, re-read on the way out, which is what
`execute -> hold` already named it for.

`aborting -> aborted` is the fourth and it is honest about what it cannot say: *what confirms the
safe effects is the executive's event*, and this vehicle has no single channel that means "quiet" —
the breakers, the valves and the engine states each carry part of it and nothing aggregates them.
The entry declares the latch it is confirming against and the mode it is leaving, and the note says
the rest is the far side's.

### And the round's own debt was not counted

Writing the debt this round leaves behind — *nothing joins `hazardous_actions_permitted` to the
verbs it constrains* — put a new entry in `mission.yaml#open_debts` and **the headline count did not
move.** That is the fourth time the same asymmetry has been found. `channels.yaml`'s eight have been
counted since the section existed; `coupling.yaml`'s eight were counted when its shopping list
turned out to be read by nobody; `vehicle.yaml`'s nine and the domains' twenty-four were counted
when a search for an EVA note opened the file it was sitting in — and `mission.yaml`, written last,
was still outside the number, along with `presentation.yaml`.

Thirteen obligations were therefore owed, written down, and **not fatal under `--strict`**, which is
the only thing a debt is for. **255 became 268**: nine in `mission.yaml` and four in
`presentation.yaml`, and the one this round added is the ninth of the nine — the first debt in the
folder's history to be counted on the day it was written rather than a year later.

The invariants table's row E was rewritten in the same pass, because it had said *"the posture
machine gives `aborting`/`aborted` no hazardous actions"* since it was written: true of the data,
enforced by nothing, which is the shape of claim this folder keeps finding in its own prose. It now
says which half is mechanical and which half is the executive's.

**176 became 177 vehicle tests.** Plant unchanged: 134 states over 57 nodes, 108 of 134 fully
configured, 26 with a debt, 57 of 77 edges carrying a sensitivity, 200 unset scalars. `--strict`
exits 2.

Verified by breaking eleven copies in `.scratch/r15/`: a terminal posture permitting hazardous
actions, a permission that is a word rather than a boolean, a machine that permits action nowhere,
the latch dropped from the transition into the permitting posture, each of the four guards deleted
from `transition_evidence` in turn, a debt deleted from each of the two newly-counted lists (267 in
both cases), and the unbroken corpus composing at 268 with both files in its debt list.

## The one initial that is a computed consequence, and the sum nobody could evaluate

The corpus states an ideal-gas law in `domains/eclss/components.yaml#atmosphere_model` — `P =
(sum_i n_i) R T / V` — with a volume per compartment, a molar mass per gas, and the cabin
temperature taken from the zone. Two stocks are what the law determines: `csm_cabin_o2_kg` and
`lm_cabin_o2_kg`, the free oxygen in each cabin at 5 psia. Both declared

```yaml
    initial: 2.653760
    initial_provenance:
      basis: derived
      relation: >-
        the ideal-gas relation the atmosphere model already states: 34,474 Pa over the CSM cabin's
        5.9 m3 at 295 K is 82.93 mol, and at 32 g/mol that is 2.653760 kg of oxygen.
```

— a **seven-figure literal justified by prose**. `initial_provenance` satisfies the check that every
numeric initial says where it came from, and a relation is not something a tool can evaluate, so the
sum had been done once by hand and written down. An audit of all twenty-five stocks found these are
the only two whose provenance is `derived`: every other numeric initial has an `initial_source` that
resolves, or is an accumulator that starts at zero because that is what an accumulator is.

**And the hand-done sums did not agree with each other.** The CSM's 2.653760 implies 6,894.8 Pa per
psi; the LM's 3.013592, on the same law with its own 6.7 m³, implies a little less. One law applied
to two cabins had been rounded two ways, by an amount (0.006 %) that no reading would ever catch and
that a shared derivation catches immediately.

So `initial` gained its third binding site — `initial_derivation`, evaluated by
`check_declared_derivation`, the same rule the edges and the consumables rates use — and the rule
that makes it more than optional: **a stock whose provenance says `derived` must bind what its
relation names**, because a derived value with a prose justification is a literal with a note.

| term | bound to |
|---|---|
| the cabin's pressure and temperature | `domains/thermal/components.yaml:zones.<cabin>.nominal_*` — the zone that declares them, whose vehicle-level copy the zone join already holds |
| the volume | `atmosphere_model.volume_m3.<cabin>` |
| the oxygen molar mass | `atmosphere_model.gases.o2.molar_mass_kg_per_mol` |
| the gas constant, and pascals per psi | 8.314462618153 and 6,894.757293168361 as literals — the first is the SI value and the second is exact by definition |

The values are now what the law gives: **2.653596** and **3.013405**, where the corpus had 2.653760
and 3.013592. The corrections are 0.16 g and 0.19 g of oxygen — nothing a simulation will notice,
and not the point. The point is the case the test keeps: re-rate a cabin's temperature from 295 K to
300 K **in both files, consistently**, so that the zone join and every other check is satisfied, and
the initial refuses, because it was computed from the old nominal. That is the lesson of
`E-GEOM-LINK`'s beamwidth arriving at a number that had no arithmetic to check it.

**268 stayed 268**: nothing was owed here — the law, the volumes and the molar masses were all
declared, and what was missing was a reader.

Verified by breaking ten copies in `.scratch/r16/`: each derivation removed, each value put back to
its hand-rounded literal, the gas constant nudged, the molar-mass binding renamed, the cabin volume
moved in the atmosphere model, a name the expression never binds, a cabin re-rated in both files at
once, and the unbroken corpus composing at 268.

## What the crew can see, and how finely — the third argument of the perception function

`domains/crew/components.yaml#display_contract` gives every crew position its panels and every panel
the readouts it shows. Its own `why` says what it is for:

> crew_diode.md:42 says display topology was not supplied and remains a configuration item. conflict
> register D-06 records that **this is the third argument of the perception function** and that
> `ask_crew` cannot be safely enabled without it.

What the linter held was membership. Every position must exist in `channels.yaml#crew_positions`,
every readout must be a registered channel, and every readout must be in that position's
`perceivable` list — the check is real and it found real drift when it was written. But a readout is
a **mapping** with two more keys, and neither was compared to anything:

```yaml
            - {channel: eclss.cabin_pressure_psia, displayed_precision: 0.1, units: psia}
            - {channel: power.bus_tie_state, displayed_precision: exact}
```

**`displayed_precision` — thirty-seven declarations — and `units`, thirty — in the one block whose
stated purpose is the perception bound.** So the fleet could be told "the commander can perceive
cabin pressure" and the vehicle had no statement of whether her gauge resolves 0.1 psia, 0.01, or
1. Nine of the twenty-two analogue readouts round by a factor of ten or more, which is a
decision — and decisions that nothing reads are the ones that drift.

The rules are the registry's own two fields, which is what makes them checkable at all:

| rule | why it is the rule |
|---|---|
| a panel may round the channel's published `precision`, and may not invent resolution | `precision` is what the instrument publishes; a crew member reporting a change at the *displayed* figure would be reporting a change the vehicle never measured |
| `exact` belongs only where the registry publishes `exact` | 15 of the readouts are discrete — an enum, a bool, a code — and showing figures on one is showing figures the instrument does not have |
| a numeric readout declares its unit, and it is the channel's unit or the same dimension | a bare number on a panel is a number with nothing to report it in; a unit in another dimension is not a rounding, it is a different quantity |

The corpus sits on the safe side of all three: every panel rounds or matches, every `exact` sits on
a channel the registry publishes as `exact`, and the one readout with no unit is the quaternion —
which is dimensionless, and printing "dimensionless" on a display would be prose.

### And the two units the map did not know

The third rule is where the round's finding is. `psid` and `% nominal` are each used by exactly one
readout, and the `DIMENSION` map — which says it *"covers the units this vehicle actually uses"* —
did not know either. What it really covers is the units the **checks** consult, and no check
consulted this block: `psid` is a cabin differential pressure gauge reading a channel whose unit is
`psi`, and `% nominal` is a percentage-of-nominal channel shown on a panel as `%`. Both are what the
map exists to make decidable, and neither could be decided, so the readouts were outside the map
rather than wrong in it.

An unknown unit is a **debt** rather than a refusal, which is the map's own stated policy — *"a
linter that cries wolf is a linter that gets bypassed"* — and it names the unit so that closing it
is one line. Closing them was two lines: **268 stayed 268**, because the two debts the reading
created are the two the completed map removes.

**178 became 179 vehicle tests.** Plant unchanged: 134 states over 57 nodes, 108 of 134 fully
configured, 26 with a debt, 57 of 77 edges carrying a sensitivity, 200 unset scalars. `--strict`
exits 2.

Verified by breaking twelve copies in `.scratch/r17/`: a panel inventing resolution, an analogue
quantity shown `exact`, a discrete one shown to figures, a readout with no precision and one with a
precision that is not a number and one of zero, a display unit in another dimension and one in the
same dimension (which composes), a numeric readout with no unit, a unit the map does not know, the
unbroken corpus composing at 268, and the map naming both units the contract uses.

## One vehicle, five frames, and two names for each of them

`vehicle.yaml#frames` declares the reference frames and `mission.yaml` names the frame its state
vector is in from that list. The corpus's own comment on the block states the rule it exists for —
*"no vector is valid without a declared frame"* — and the generated `HELP.md`, which is the only
description of the command surface a fleet ever sees, said this:

```
- `frame`: one of: EARTH_J2000, MOON_J2000, LVLH          (load_state_vector, gnc)
- `frame`: one of: body, lvlh, inertial_earth, inertial_moon   (set_attitude_target, rcs)
- `frame`: one of: body, lvlh, inertial_earth, inertial_moon   (request_translation, rcs)
```

**The second and third share no value with the first.** A fleet told to send `EARTH_J2000` by one
verb was told to send `inertial_earth` by the next, for the same frame, and `vehicle.yaml#frames`
declares neither lowercase name. Nothing joined the two vocabularies because nothing read either:
the linter mentioned `EARTH_J2000` **zero times**, and an argument's *values* were the one part of
an argument schema no rule looked at unless the argument's name happened to match a component class.

The rule is `check_argument_vocabularies`' third vocabulary — after `component` and `vehicle_entry` —
and it is inferred from the argument's name wherever the name says it: **an argument called `frame`
names a frame, the way one called `pump` names pumps.** `names: frame` is the override for the
arguments whose names do not say, and there are none today. The two verbs now offer the registry's
ids, and the eight published gate variables follow them:

```
"rcs_target_BODY_enable", "rcs_target_LVLH_enable",
"rcs_target_EARTH_J2000_enable", "rcs_target_MOON_J2000_enable"
```

The same check holds `mission.yaml`'s two frame references — the state vector's and the osculating
elements' — which already named `EARTH_J2000` and were the reason the second vocabulary was visibly
the odd one out. It is keyed on the document *and* the key, because **`frame` is overloaded**: in
`presentation.yaml` it is the telemetry envelope, thirteen fields of it, and a rule that matched the
word alone would read a frame registry into a packet layout. That is the fourth overloaded key this
folder has found, after `vehicle` (three meanings), `source` (three) and `range` (two) — and the
reason the rule is written where the meaning is known.

A frame that is *absent* rather than wrong is a debt rather than a refusal, because a state vector
with no frame is an omission whose fix is a decision: *"the frame this vector is expressed in — the
one thing `gnc_diode.md`:382 says makes a vector valid — is not stated."*

**268 stayed 268**, and the pins moved together: the test count, and the eight gate variable names
that the mirror publishes (the count of published gate variables is unchanged — four values either
way).

### What this leaves in the same block, named rather than half-done

`vehicle.yaml#conventions` is the same shape one screen up, and it is the next round's work. Its own
comment calls the four conventions *"the document's most valuable export"* and says they are
*"declared once, here, and `domains/gnc/` is what enforces them"* — and the enforcement is prose in
three places: `domains/gnc/components.yaml`'s header repeats `[w,x,y,z]`, `domains/gnc/points.yaml`'s
`attitude_q` note cites the block by name, and `domains/rcs/commands.yaml`'s quaternion argument says
`"unit quaternion, w-first"` without citing anything. **The word `quaternion` appears zero times in
the linter**, so the component order — which the comment itself calls "the single most consequential
line in the document", because two agents can disagree about it while both being right about the
physics — was a convention nothing held. That was this round's work: the section below is what came
of it.

**179 became 180 vehicle tests.** Plant unchanged: 134 states over 57 nodes, 108 of 134 fully
configured, 26 with a debt, 57 of 77 edges carrying a sensitivity, 200 unset scalars. `--strict`
exits 2.

Verified by breaking ten copies in `.scratch/r18/`: both second vocabularies restored, a frame
invented for one verb, a frame renamed in the registry, each of the two state-vector frame
references renamed, a frame removed from the registry, a state vector with no frame (owed rather
than refused), the unbroken corpus composing at 268, and the generated help text naming one frame
vocabulary and no other.

## The conventions block, and the claim in it that was false

`vehicle.yaml#conventions` is the block the previous round ended on. Its own comment quotes the rule
it exists for — *"no vector is valid without a declared frame"* — and then goes further:

> it fixes four conventions that a vehicle can get silently wrong, because every one of them is a
> *convention* rather than a quantity. Two agents can disagree about a quaternion's component order
> while both being right about the physics, and nothing downstream would report the disagreement:
> the attitude would simply be wrong, in a way that looks like a control problem.

It says they are *"declared once, here"* and that `domains/gnc/` *"is what enforces them"*. The
enforcement was three pieces of prose — a header comment repeating `[w,x,y,z]`, a point's note citing
the block, and an argument's note saying `"unit quaternion, w-first"` — and **the word `quaternion`
appeared zero times in the linter.** The order a fleet must send was, in the only file a fleet reads,
a phrase inside a note.

Two of the six conventions are holdable, and the check found that one of them was not just unheld but
wrong.

### The order is now a field, and the help renders it

```yaml
        quaternion:
          type: array
          items: number
          length: 4
          note: "unit quaternion"
          quaternion_order: "[w,x,y,z]"
          quaternion_order_source: "vehicle.yaml:conventions.quaternion_order"
```

That is `initial_source`'s idiom for a string instead of a number: a copy, plus a path that must
resolve and agree. The declaration must be a bracketed ordering of `w`, `x`, `y`, `z` — each exactly
once — and the two sites that state it (`domains/rcs/commands.yaml`'s argument and
`domains/gnc/points.yaml`'s `attitude_q` point) both cite it. The generated `HELP.md` now reads:

```
- `quaternion`: 4 x number, order [w,x,y,z] — unit quaternion
```

which it renders from the field the linter holds, not from the sentence that used to carry it.

**The citation walk is what makes a rename visible**, and it took two versions to get right. The
first walked the *declared* keys — for each convention the block declares, find the sites that state
it — and that is blind to exactly the failure a rename produces: rename
`conventions.quaternion_order` and the sites' `quaternion_order_source` fields still name a key that
no longer exists, while the key-walk finds nothing to look at because it is looking for the *old*
name. The walk now goes the other way, over every field ending in `_source` whose value names this
block, resolves the path, and refuses when it does not — *a citation of a declaration that has been
renamed reads exactly like a citation of one that works.*

### `units: SI` was false, and it was false by design

The other holdable convention is the unit system, and it said one word: `SI`. The registry publishes
**thirty channels in units that are not SI** — `psia`, `psi`, `mmHg`, `ft3/min`, `degC`, `deg`,
`deg/s`, `L/min`, `Wh`, `rpm` and `g` — and it publishes them deliberately, because `channels.yaml`'s
header says the apollo rows are that catalogue *"verbatim — identifiers, units, ranges, precisions and
rates included"*. A convention that cannot be checked is not a convention, so the claim now carries
what it needs:

```yaml
  units: >-
    SI, except for the units a channel's own source publishes and the registry keeps verbatim.
    Every conversion the corpus performs is written where it is used — 0.45359237 kg per lb,
    6,894.757293168361 Pa per psi, 2.45e6 J per kg of water, 3,600 J per Wh — rather than
    presupposed by the channel's unit, because the fleet reads what the source published
  units_exceptions:
    psi: ..., psia: ..., mmHg: ..., degC: ..., deg: ..., deg/s: ...,
    ft3/min: ..., L/min: ..., Wh: ..., rpm: ..., g: ...
```

Three rules keep the list honest, and they are the derivation idiom's two directions plus one of its
own: **every channel unit is SI, dimensionless or declared**; **every declared exception is used by
some channel** (so the list cannot grow to cover the rule); and **no SI unit may be declared an
exception**. An undeclared unit is a refusal rather than a debt here — a channel is a fleet-facing
number, and a number whose unit the convention does not cover is a conversion nobody has done.

The eleventh entry is the interesting one, because it resolves a collision the dimensional map still
carries: `g` is *standard gravity* on `structure.accel_xyz_g` and *grams* on the two mass-flow
channels whose unit is `g/s`, and `DIMENSION` knows only the second sense. Declaring it makes the
collision visible where a reader can act on it, which is the most a declaration can do.

**268 stayed 268**, and the four conventions still held by nothing — `quaternion_direction`,
`angular_velocity`, `covariance_units`, `mission_time` — are the same shape as this round's two and
are the next ones: each has a site that could cite it (`gnc`'s points state the rate resolution, the
covariance blocks state the per-block units, and `mission.met_s`'s monotonicity is a claim about a
published channel).

**180 became 181 vehicle tests.** Plant unchanged: 134 states over 57 nodes, 108 of 134 fully
configured, 26 with a debt, 57 of 77 edges carrying a sensitivity, 200 unset scalars. `--strict`
exits 2.

Verified by breaking fourteen copies in `.scratch/r19/`: the exception list removed, an SI unit and
an unused unit declared as exceptions, a channel given a unit the convention does not cover, an order
with three components and one with a repeat, each copy drifting from the declaration, a site stating
the order with no citation, a citation of another convention, the cited declaration renamed (which is
the case the first version of the walk could not see), the unbroken corpus composing at 268, the help
text rendering the declared order, and a drifted copy both refused and visible in the help.

## The other four conventions, and the matrix unit that was a third copy

The previous round wired two of the block's six conventions — the quaternion order and the unit
system — and named the four it left: `quaternion_direction`, `angular_velocity`, `covariance_units`
and `mission_time`. Each has a site that states it, and each site now cites the declaration:

| convention | the site that states it |
|---|---|
| `quaternion_direction` | `domains/gnc/points.yaml`'s `attitude_q` — "rotating BODY into the reference frame" |
| `angular_velocity` | the same file's `body_rate_xyz_deg_s` — the channel that says "resolved in BODY" |
| `covariance_units` | `domains/gnc/components.yaml#estimator`'s block units |
| `mission_time` | `channels.yaml`'s `mission.met_s` — the one channel the monotonicity rule is about |

A citation with no value beside it is allowed and is the honest form where the site is *departing*
from the convention rather than repeating it: the rate convention says "resolved in the vehicle's
BODY frame **unless a channel says otherwise**", and `body_rate_xyz_deg_s` is the channel that says
otherwise, so it cites the rule it is a case of.

**And a convention with no citing site at all is now refused**, which is the round's own thesis
applied to the block it read: *a convention declared once and cited nowhere is a sentence.* A rule
that checks only the sites that exist cannot tell "no site drifted" from "no site".

### The covariance was declared three times, and the third was the state's own unit

`covariance_units` is the one with substance. The corpus states it as the convention's blocks, as the
estimator's copy of them, and as `nav_covariance`'s `unit`:

```yaml
    unit: "matrix[m^2, (m/s)^2, rad^2, (m/s^2)^2, (rad/s)^2]"
```

That string is the error vector's five blocks in the error vector's order, and it was compared to
nothing — reorder `error_vector`, rename a block, change a unit in the convention, and the state's
own unit goes on describing a filter that no longer exists. The check re-derives it: the blocks come
from the convention, the order from `error_vector`, and the two must produce exactly that string. The
three copies are now one declaration, one citation and one derivation.

The convention itself had to change shape to be citable at all: prose ("per block, not one generic
unit: position m^2, …") became a mapping, because a citation needs something to resolve *to*. The
estimator's field was renamed from `covariance_units_per_block` to `covariance_units` in the same
pass, so that `<key>` and `<key>_source` pair by name — the pairing rule the check uses everywhere
else, rather than a special case for one field.

### And a declaration on a channel row was invisible

Writing the `mission_time` citation found the round's tool defect, in the tool rather than the
corpus: **`load_documents` never loaded `channels.yaml`.** Its map held only
`slowest_publish_period_ms`, the statistic computed *from* that file, so a declaration written on a
channel row was invisible to every path idiom — `derives_from`, `initial_source`, a `derivation` and
now a citation. The citation walk reported nothing, which reads exactly like a citation that is
correct. `channels.yaml` is a document like any other now, and the fixture that keeps it that way
asserts the one thing nothing else would: that a path into it resolves at all.

**268 stayed 268.** The last of the six conventions now has a reader, and the next signal in this
area is the matrix itself: `nav_covariance` is a `dynamics` state on the `internal` sentinel whose
propagation is domain code, so what the corpus can say about it is what it has said — which blocks,
in which units, in which order.

**181 became 182 vehicle tests.** Plant unchanged: 134 states over 57 nodes, 108 of 134 fully
configured, 26 with a debt, 57 of 77 edges carrying a sensitivity, 200 unset scalars. `--strict`
exits 2.

Verified by breaking fifteen copies in `.scratch/r20/`: the estimator's units drifting, a reordered
error vector, a renamed block, a changed block unit, a matrix unit that lost a block, a citation of a
path under the convention that does not exist, a citation of a different convention, each of the
three remaining citations orphaned by renaming what it cites, the last citation of a convention
deleted (which is the case the "every convention has a reader" rule exists for), one drifted block
producing exactly one refusal, the unbroken corpus composing at 268, every convention but the unit
system showing a citing site, and `channels.yaml` readable as a document.

## The fifteen stories and the faults that produce them

`coupling.yaml#failure_chains` is the experiment's story list. Each chain gives a `primary`,
a `secondary` and a `third_order` cause — in **prose**, "RCS thruster stuck on", "feed-pressure
sensor stuck high" — and the channels a fleet would see, as ids that the coupling check already
holds to the registry.

The middle was missing, and the tool that needed it said so in its own docstring:

> "Primary" is read as the fault itself rather than its chain, **because the chains are not linked
> to faults by anything but prose (see `open_debts`)**.

**And no `open_debts` list in the corpus recorded it.** The one artefact that could have closed the
gap pointed at a debt nobody had written — which is this folder's oldest finding arriving at a
citation, and the reason the round began by checking the reference rather than trusting it.

Every chain now declares `realised_by`, a list of fault ids, the way a channel's event declares the
threshold that realises it:

```yaml
    realised_by: [PRP-08-line-freeze, TCS-07-line-freeze]
```

Three rules hold it, and the second is what makes the link a claim rather than a label:

| rule | why |
|---|---|
| every named fault is declared by some domain's `fault_policy.yaml` | a rename is a refusal rather than a chain that quietly realises nothing |
| **every named fault perturbs at least one of the chain's own clues** | both sides of this join were declared all along — `perturbs` on the fault, `first_published_clue` and `observable_clues` on the chain — with nothing between them. A fault that cannot move a channel the chain says a fleet would see is not the cause of that chain |
| the chain's `scenario_use` names a declared `scenario_postures` id | fifteen chains pointing at a renamed scenario read exactly like fifteen that work — and that vocabulary join is what the unread-field sweep found first |

The realisation lists are **derived rather than invented**: every one was checked against that second
rule before it was written, and the check re-checks it on every run. Where a chain has two causes
(`F-06-cabin-leak` is `ECL-01` on the cabin volume or `STR-01` at the hatch seal; `F-09-frozen-line`
is the propellant line or the coolant one) both are named.

### What the link does not fix, which is now the debt

`tools/faults.py` draws the crisis posture's *"one guaranteed major primary"* from the **critical
class**, so the fault placed is a random critical fault and the chain that results is whichever one
that fault belongs to. The chains are linked to faults now; **the seed is still drawn from a class
rather than from a chain**, and nineteen fault ids sit in no class at all and are scheduled
stochastically rather than seeded. The docstring no longer points at a debt that does not exist — it
points at the one this round wrote, in `mission.yaml#open_debts`: what would close it is a declared
pool in the posture itself, and a decision about whether a scenario names the chain it is about.
Both are decisions about the experiment rather than about the vehicle, which is why they are recorded
rather than made.

**268 became 269**, and the pins moved together. **182 became 183 vehicle tests.** Plant unchanged:
134 states over 57 nodes, 108 of 134 fully configured, 26 with a debt, 57 of 77 edges carrying a
sensitivity, 200 unset scalars. `--strict` exits 2.

Verified by breaking eleven copies in `.scratch/r21/`: a chain naming a fault no policy declares, a
chain realised by a fault that moves none of its clues, a realisation list emptied, a fault's
`perturbs` list replaced so it no longer touches its chain, a chain's clues moved off its fault, the
scenario vocabulary renamed in both directions, every chain losing its scenario (owed rather than
refused), the unbroken corpus composing at 269, every chain's faults touching its clues on the real
corpus, and the seeding residue present as a debt with the tool pointing at it.

## The invariants, and which of them are enforced

`mission_diode.md:1264-1345` states ten safety invariants for the mission boundary. They arrived
with the document rather than from it — several are properties this vehicle already had, stated
badly or not at all — and the useful thing to do with a list like this is to say, for each one,
whether the linter would catch a violation. An invariant that nothing checks is a paragraph.

| | Invariant | Enforced by |
|---|---|---|
| A | `ExecutedEffect ⇒ verb ∈ Registry` — no unregistered input causes an effect | The command registry, and `FORBIDDEN_VERB`: a verb form that must not exist is refused by pattern, so the registry cannot grow one. |
| B | `AgentRequest ⇏ ActuatorEffect` — always a trusted step between them | Not a property of this folder. It is the diode's, and it is the reason the vehicle is behind a window at all (`docs/diode-contract.md`, `design.md` §8). |
| C | `Effect(i,t) ⇒ Guards(i,t)` — validity at admission is not enough | Every verb declares `gate`, `interlocks`, `allowed_phases` and `conflict_domain`; the linter refuses an interlock that resolves to no threshold, and the phases a verb allows must be phases the mission declares. `avionics_diode.md:496-508` adds the tenth term the predicate was missing — an irreversible action needs a synchronised clock, because its deadline is measured against one — and the linter now requires `requires_time_sync` on every irreversible verb. |
| D | `RequiredTelemetryStale ⇒ ¬HazardousEffect` | **New, and the reason for this round.** Every threshold's point must resolve to a maximum decision age, a declared age may not be shorter than the publish period, and every entry of `mission.yaml#transition_evidence` must be meetable by the channel it names. |
| E | `AbortLatched ⇒ ¬Permit(hazardous)` | **In part, and the part is now mechanical.** Ten postures declare `hazardous_actions_permitted`; exactly one (`execute`) says yes, the two terminal postures say no — the property `mission_diode.md`:1727 puts in the list a startup verifies statically — and every transition *into* a permitting posture must require `mission.abort_latched` within its evidence, which is TV-E's vehicle-side half. The dominance rule itself is still the executive's, and what the vehicle cannot yet say is which commands the permission ranges over: a verb carries `irreversible` and not `hazardous`, so the join between the permission and the command surface is named in `mission.yaml#open_debts` rather than assumed. |
| F | `AgentWrite(TelemetryMirror) ⇏ Change(MissionState)` | Not a property of this folder — it is the window's, and `plant.md`'s truth/evidence boundary is the vehicle-side half of it. |
| G | `Accepted(seq_n) ⇒ seq_n > highwater` | The canonical chain's (V-02, C-21): `seq` + `boot_id` + `stream_id`. The vehicle publishes the sequence; the ledger is the executive's. |
| H | `Effect ⇒ RemainingAfterEffect ≥ SafetyReserve` | The reserve floors: `prop_cutoff_guard_5`, `o2_reserve_10`, `water_reserve_20`, `cooling_water_reserve`, `rcs_propellant_reserve` and their siblings, each with an `interlock: true` guard evaluated at request time. |
| I | `Irreversible(a) ⇒ Registry[a].irreversible ∧ TrustedPermit(a)` | Every verb declares `irreversible`, and every one-way *state* must declare `requires_arm` or the linter refuses it — which is where the vehicle's only two-step verbs live. |
| J | `MissionState = ABORTED ⇒ MissionState ≠ EXECUTE` | The posture machine: `aborted` is `terminal: true`, `abort_latched` is not in any verb's argument schema, and the linter refuses a `mission.posture` enum that does not match `mission.yaml`'s postures. |

Six of the ten are enforced mechanically, two are enforced in part, and two are properties of the
window rather than of the vehicle. The distinction matters for reading the rest of this folder:
**the linter is a referee for claims about the vehicle, not for claims about the boundary**, and
D is the one that turned out to have teeth — applying it found that nineteen of the fifty channels
a threshold watches were gated on evidence their own publisher could not deliver in time.

## The one thing to keep in mind when extending this

The linter is not a schema validator; it is the mechanism the design calls "a conversation
with the linter" (`simulator-design.md:146-150`). You do not enumerate what the simulator
needs up front — you add a domain, run it, and it tells you what you now owe. When it reports a
debt, the debt is the deliverable: the wrong response is to fill the value with something
plausible so the build goes green.
