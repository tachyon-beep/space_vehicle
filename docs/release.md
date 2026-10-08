# Validation, release and adoption

A merge is a development checkpoint. Vehicle acceptance requires the separate
V/M gates in [completion.md](completion.md). There is no deployment pipeline in
this scaffold. The following release procedure is prospective until its linked
work packages have implemented the required checks.

## Evidence required at each checkpoint

| Checkpoint | Required evidence | Evidence owner |
|---|---|---|
| Development PR | Exact diff SHA, locked tool versions, lint/composition, complete JUnit with zero skips, independent review, readiness report and scope limitations | Implementer and independent reviewer |
| Integrated slice | Two-window shared state, command-to-tick-to-evidence trace, arbitration/refusal and failure probes through the production entrypoint | WP01/WP08 reviewers |
| Complete vehicle | Strict 0, no executable gaps, phase/configuration/command/fault coverage matrix, independent physical cases and non-vacuous conservation | WP11 reviewer |
| Mission acceptance | Full nominal trace and defined degraded/crisis outcomes, deterministic replay, restart and hidden-truth checks, live/replay timing results | WP12 reviewer and operator |
| Stack adoption | Exact accepted vehicle SHA plus chassis SHA/images, parent reconciliation/suite, containment/window probes, operator stop/recovery and bounded preflight | Chassis maintainer and operator |

Acceptance artifacts need a durable location with a manifest: source SHAs,
config/input/scenario hashes, Python/tool versions, seed/RNG identity, clock and
multiplier policy, full invocation, start/end times, expected and observed
outcomes and process exit statuses, raw trace/checkpoint references, checkpoint
format/compatibility identifiers, checksum list, limitations and reviewer
sign-off. Record validation, publication, merge, tagging, adoption and deployment
as separate delivery states. CI's short-lived artifact retention is development evidence, not the
permanent release archive. WP13 must establish that archive before tagging an
accepted release. Preserve failed acceptance evidence as well as successful runs.

No release tag is called accepted until V and M pass at its exact SHA. A tagged
incomplete build must say `development` and list the missing gates. The operator
accepts the mission and authorises adoption; passing CI does not authorise a
model-backed campaign. Paid-model preflight and full experiment acceptance are
later chassis/operator actions with their own limits and protocol.

Adopt an accepted vehicle in `space_chassis` with a separate pointer and any
required reconciliation rows, then run that repository's complete gate and live
probes. Use process/file adapters across the boundary, never imports. Rollback
selects a previously accepted vehicle/chassis/image set and follows its documented
checkpoint compatibility policy; do not guess that a new or old engine can read
an arbitrary saved world.

## Merge policy and enforcement record

The policy is topic-branch PRs, independent review, no unresolved blocking
feedback, both Python development checks and the readiness diagnostic succeeding
(refusals remain fatal; declared incompleteness is reported). Acceptance evidence
and dependencies determine work-package closure, not the green check alone.

At the 2026-10-03 scaffold baseline, GitHub `main` has no branch protection or
rulesets. This document states a policy; it does not claim enforcement. After
these workflows exist on the default branch, the maintainer should configure
required check contexts, restrict direct pushes and require resolved discussions.
GitHub approving reviews require a separate eligible reviewer; an author's own
review is not an independent approval. Record the actual settings and any
maintainer exception here, with date and API/setting evidence. The publication
PR does not by itself change repository settings or prove main was adopted.

The accountable maintainer is the repository owner, `tachyon-beep`. Work-package
implementers/reviewers are assigned when work is claimed, rather than fabricated
in advance. There is no completion date commitment while required evidence,
implementation size and performance feasibility remain unresolved. Review the
next ready package after every merge; reassess scope, dependencies and risks at
each milestone. Escalate evidence dead ends or failed acceptance as concrete
operator decisions; never relabel an incomplete gate green.
