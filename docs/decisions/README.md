# Decision records

Create `NNNN-short-title.md` using [the template](template.md). Status is
`proposed`, `accepted` or `superseded`. A proposal is not implementation approval.
Record the decision owner, review evidence and superseding record. Maintainers
own delivery mechanics; the operator owns scope, evidence-standard and mission
performance changes. Model/interface decisions need the affected repository's
review; a vehicle record cannot unilaterally alter the frozen diode contract.

| Decision to resolve | Owner | Needed before |
|---|---|---|
| Single executive ownership, bounded ingress/arbitration, private persistent truth and recovery | Vehicle maintainer, chassis reviewer for adapter/mount implications | WP01 implementation design; WP08 acceptance |
| Mission clock, multiplier, overload, pause and downtime policy | Operator with vehicle/chassis reviewers | WP08 implementation |
| Composite hardware effectivity, RCS impulse/geometry and applicable numerical sources | Operator with model/evidence reviewer | WP02/WP03 evidence acceptance and dependent physical rules |
| Numerical tolerances, independent references and mission/scenario outcome thresholds | Operator with independent physics reviewer | WP11 coverage acceptance; WP12 full runs |
| Profile-driven runtime optimisation and replay budget feasibility | Vehicle maintainer; operator for target changes | WP12 performance acceptance |
| Exact release/adoption and campaign protocol | Operator with vehicle/chassis maintainers | WP13 adoption; experiment trials |

Records: [0001](0001-shared-executive.md) — one executive owns the physical world; windows are
ingress and evidence views (accepted 2026-10-09; chassis review pending).
[0002](0002-mission-clock-and-continuity.md) — the mission clock, private checkpoints and restart
continuity (proposed 2026-10-09; the operator decides A, C, D, F and confirms B, E, L(b); the
maintainer with the chassis reviewer decides G–K, L(a), M; all open, with an `Answer:` line each;
WP08's policy-independent children are named in it and `tools/measure_clock.py` reads its figures).

The table lists design questions, not extra product features. Link each
record from its public work package. The roadmap can proceed through research
and design while dependent implementation waits for the required decision.
