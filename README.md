# f3-matchflow

Private orchestration tooling for f3decomp. **Our code, policy, prompts, wrappers and
tests live here.** The external git-free workspace contains official dependencies,
the contributor's game and generated private runtime artifacts, not another pipeline.

## Restricted subagents

The orchestrator screens and assigns one exact function per worker. `matchflow_launch`
creates a dedicated `match-worker` child, binds its real session, claims just in time,
obtains the exact pack and registers evidence before sending the prepared prompt.

Workers have only session-scoped matching tools: own-folder candidate edits, read-only
pack/references, managed official checks and own-claim checkpoints/closure. No general
shell, raw service credentials, target selection, sibling access or tooling edits.
Both permission rules and host-side capabilities enforce the boundary. This is not an
OS sandbox. Legacy general/headless/Workflow matching launch paths are disabled.

## Setup

Requires Python 3.10+, Node 22+, OpenCode V2 (tested SDK/CLI 2.2.6), and the separately
installed official checker/compiler plus WSL/Wine on Windows. Configure ignored local
`matchflow.local.json` with an external absolute workspace, e.g.:

```json
{"workspace":"F:/Games/f3decomp"}
```

`F3_MATCHFLOW_WORKSPACE` overrides that path. Runtime data and credentials may not live
in this repository. Credentials default to the existing private OS-local key file.

```text
npm ci --ignore-scripts --prefix integrations/opencode
python tools/matchflow/bootstrap.py
python tools/matchflow/preflight.py --online
```

Bootstrap writes the ignored local OpenCode bridge, loading repository policy, official
service guide, local plugin and external key-file reference. It preserves custom
configuration. No setup/test command starts paid workers or takes real claims.

See [command reference](tools/matchflow/README.md) for configuration, central selection,
subagent dispatch, safe recovery and optional pinned f3twins integration. Canonical local
policy is [docs/matching-policy.md](docs/matching-policy.md); matching techniques load
on demand from [docs/matching-recipes.md](docs/matching-recipes.md). Official contribution
rules at https://f3decomp.com/agents.md take precedence.

## Source boundaries and tests

Tracked: source, sanitized fixtures, policy, templates, dependency locks and CI.
Ignored/external: packs, game/toolchain, sources under claim, raw output/disassembly,
databases, logs, keys and caches. Public optional dependencies install under `.deps`;
their derived private data stays in the external workspace. Never stage private files.
Workspace junctions/deployed workflow copies are no longer required or authoritative.

```text
python -m unittest discover -s tools/matchflow -v
npm test --prefix integrations/opencode
```

CI tests Python on Windows/Linux and the pinned OpenCode plugin SDK. Live matching is
separate from offline verification and requires an explicit matching request.
