# Contributing

Reusable tooling, policies, prompts, sanitized fixtures and tests belong in this
repository. Matching artifacts and installed official dependencies do not. See
[setup](docs/setup.md) for the folder boundary and `AGENTS.md` for agent instructions.

## Offline development

Python 3.10+ and Node 22+ suffice for offline tests. No game, service credential, paid
model or live claim is needed. Configure `F3_MATCHFLOW_WORKSPACE` to an absolute external
scratch path before running tests (CI uses a temporary external path); do not point it
at a live workspace used by workers. The tests create their own isolated fixtures.

For example, in PowerShell:

```powershell
$env:F3_MATCHFLOW_WORKSPACE = Join-Path $env:TEMP 'matchflow-offline-tests'
```

In a POSIX shell:

```sh
export F3_MATCHFLOW_WORKSPACE="${TMPDIR:-/tmp}/matchflow-offline-tests"
```

An ignored `matchflow.local.json` with an external scratch workspace is also supported.

From the checkout:

```text
npm ci --ignore-scripts --prefix integrations/opencode
python -m unittest discover -s tools/matchflow -v
npm test --prefix integrations/opencode
git diff --check
```

CI runs offline Python tests on Windows and Linux and checks the pinned OpenCode SDK.
No claims, submissions, live preflight or paid agents may be used as tests without an
explicit human request. Avoid changing an active worker's workspace or host tooling.

Keep changes consistent with existing capability boundaries, receipt validation and
no-retry semantics. Add offline regression tests. Do not modify official checker/compiler
dependencies or weaken permissions to work around a refusal. Update the setup/runbook
when paths, configuration or operator steps change. New repositories use `master`.

## Public changes and reports

Before staging, inspect the diff for credentials, personal paths, claim-specific source,
game-derived data and raw service output. Never attach packs, binaries, disassembly,
databases, session exports or local configuration to issues or pull requests, even if
the checkout was previously private. Use synthetic fixtures, not captured game data.

This repository's MIT license covers original tooling/documentation. Contributions
must be yours to share under that license; keep required third-party notices. Optional
dependencies remain separately licensed as documented in `third_party/README.md`.
