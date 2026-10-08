# Contributing to the vehicle

Start with [the roadmap](ROADMAP.md), [completion gates](docs/completion.md),
[AGENTS.md](AGENTS.md) and [plant.md](plant.md). This is the vehicle repository;
operator services, the frozen research corpus and the diode contract belong to
`space_chassis`. Tests and runtime tools here must run without that checkout.

## Reproduce the development environment

Linux with Python 3.12 and 3.13 has the supported development lanes. The hash-locked file
includes tools and transitive dependencies; `pyproject.toml` remains the runtime
manifest. From a standalone checkout:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements-dev.lock
mkdir -p .scratch/validation
python -m ruff check . --no-cache
python tools/check_vehicle.py
python -m pytest -n 2 -q -o addopts='' --junitxml=.scratch/validation/tests.xml
python tools/plant.py --readiness
python tools/plant.py --build-order
python tools/check_vehicle.py --strict
```

The final command currently reports incomplete work. Exit `2` is expected while
honest debts remain; it does **not** pass the completion gate. Linter exit `1`
(refusal), `3` (unreadable), a crash, or any unexpected exit is always a failure.
All referee tests must pass with zero skips. PyYAML is mandatory in this
standalone environment: the referee's optional-import escape is not permission
to omit vehicle collection. Run the whole suite before committing, including
for scaffold changes. Do not add skip/xfail markers or weaken a refusal to make
an implementation pass.

The [development workflow](.github/workflows/development.yml) checks lint, normal
composition and the full referee in both Python lanes. Its separate readiness
job publishes strict output and current build gaps. A green development workflow
can coexist with an incomplete vehicle; neither job proves mission acceptance.

To update the development lock, use `uv pip compile --generate-hashes
--python-version 3.12 requirements-dev.in -o requirements-dev.lock` with uv
0.10.2, review dependency changes, and run both CI lanes. The lock header records
the generating command. Never silently rebuild another checkout's environment.

## Take and land a bounded package

1. Choose a public [work package](ROADMAP.md). Confirm prerequisites and claim it
   in GitHub before starting; record the implementer and independent reviewer.
   Split a package into child issues if it cannot fit a focused, reviewable PR.
2. Record a short design and acceptance examples in the issue. Make unresolved
   model choices explicit using [decision records](docs/decisions/README.md).
   Numerical evidence must meet [the evidence policy](docs/evidence.md).
3. Work on `codex/<topic>` (or a maintainer-chosen topic branch). For a behavioral
   fix, reproduce the failure, implement one coherent outcome, and add the
   regression with the implementation. Configuration/refusal findings retain
   the one-finding round discipline in AGENTS.md. Documentation-only work does
   not need invented test code or a new physical-model round.
4. Run the full development gate and the affected acceptance probes. Preserve
   command lines, source SHA, environment, results, limitations and raw artifacts.
   Update README/tool/test pins together when model figures change. Keep live
   model counts out of unvalidated planning prose.
5. Commit as `vehicle: <the finding or outcome>` and open a PR using the template.
   Obtain an independent review of the actual diff and acceptance evidence.
   A maintainer merges only after the required checks pass and blockers resolve.
   Do not push implementation directly to `main`.
6. Close the issue only when its acceptance criteria have evidence at the merged
   SHA. A partial PR may close a child issue, never its unfinished parent package.
   Report local validation, PR publication, merge, and release acceptance separately.

The desired merge policy is successful `check (3.12)`, `check (3.13)` and
`readiness diagnostic` jobs from the Development workflow, and an independent
review with no unresolved blocking discussions. See [release guidance](docs/release.md)
for the distinction between this policy and enforced GitHub settings.

## Across the repository boundary

A vehicle PR changes this repository only. After merge, `space_chassis` adopts
that exact vehicle SHA in a **separate** submodule-pointer/reconciliation commit,
using its own full suite and containment/contract checks. Changed numeric rows
and test counts are reconciled there. No vehicle tool imports operator services,
opens the parent research files, or launches paid-model agents.
