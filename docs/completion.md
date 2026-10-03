# Completion contract

The deliverable is the configured, eleven-domain vehicle executing its full
192-hour mission at 50 physical ticks per simulated second, visible through the
frozen diode interface. A small demonstrator is useful evidence on the way;
it does not replace that objective. Changing scope, the evidence standard or
performance targets requires an explicit operator decision.

This contract specifies acceptance to be implemented; it does not claim the
current tools already implement every check. Current model status is derived
from `tools/check_vehicle.py`, `tools/plant.py --readiness` and `--build-order`;
[README.md](../README.md) holds the tool-checked numerical status and round history.

| Gate | Evidence required | What it permits |
|---|---|---|
| D — reproducible development | Isolated locked dependencies; normal lint exit 0; all referee tests pass, none skipped; both supported Python lanes; independent PR review | Continue bounded development with explicit debts |
| I — integrated slice | The deployed-path executive advances one authoritative physical state; two windows command and observe that same state; deterministic arbitration and receipts; evidence is generated from stepped truth | Demonstrate the integration path with an explicitly incomplete fixture |
| V — complete vehicle | Strict lint exit 0; no unresolved executable-state gaps; every applicable phase/configuration and intended command effect exercised; independent physics, protection, conservation, fault and instrument checks | Begin full mission acceptance under scripted control |
| M — accepted mission | Complete nominal mission and specified degraded/crisis cases through the same executive; expected outcomes; deterministic replay; restart continuity; measured live and replay budgets | Release the vehicle for stack acceptance |
| E — accepted experiment apparatus | Exact vehicle/chassis/image SHAs; containment and diode probes; campaign-ready chassis; bounded stub and model-backed preflight; fixed protocol and objective evaluator | Run interpretable agent trials |

D, I, V and M are vehicle gates. E is owned jointly by the chassis/operator and
is recorded there, with a link back to the accepted vehicle. A vehicle PR or
release must never claim E from standalone referee results.

## Non-negotiable acceptance properties

- One vehicle has one authoritative state, event queue, mission clock, RNG lineage
  and conservation ledgers. Per-agent directories are ingress/evidence views.
  The fleet cannot access truth, checkpoints, private fault plans or credentials.
- Physics advances independently of agent thinking and publication requests.
  Simulation time, safety dwell time, receipt order and wall time have defined
  semantics. Pause, overload and downtime policies are decided before use;
  restarting cannot silently reset physical state or advance it by guessed time.
- Inputs are bounded and validated before mutation. Arbitration, conflicting and
  irreversible commands, failures and receipts are deterministic and auditable.
  Floods, malformed writes and stalled readers cannot corrupt another window.
- Independent analytic/reference cases validate the model, rather than comparing
  two executions of the same implementation alone. Open-system sources and sinks
  are accounted for; mass and energy checks must execute on nontrivial transfers.
- Faults change physical behavior; instruments publish their permitted measured,
  delayed, saturated and uncertain evidence, never hidden truth. Command effect,
  detection, isolation and recovery are checked through the real executive.
- Acceptance covers all phases, configuration transitions, resource depletion,
  contact loss, protection actions and irreversible effects. Scenario names are
  not proof that those behaviors occurred. A designated unrecoverable crisis
  must reach its expected terminal outcome, not be hidden as a harness failure.

## Performance and expensive-run prerequisites

The full profile is 34,560,000 ticks. At multiplier `m` simulated seconds per
wall second, the live executive plus amortised ten-window I/O must meet
`20 ms / m` wall deadlines. Record p50/p95/p99/max, input/queue ages, missed
deadlines and the agreed overload behavior under nominal and adversarial I/O.
Replay retains the `plant.md` full-mission/300-second goal, about 8.7 µs/tick.
Replay equivalence follows `plant.md` §6: same build and platform, same seed and
recorded input trace, byte-identical per-tick state hashes. Validate that trace
through command receipts and permitted evidence as well as internal state.
Cross-platform numerical equivalence needs a separate documented guarantee; the
same-build guarantee does not imply it.

Replay speed and live scheduling are separate measurements. Optimise only after
profiling a correct model; changing these targets requires an operator decision.

V and a bounded phase-spanning acceptance run precede the expensive full run.
A short diagnostic mission that reports unresolved gaps does not pass V or M,
even if its process exits 0. Strict lint 0 is necessary, but not sufficient, for V.
A green CI badge is never a spacecraft acceptance certificate.
