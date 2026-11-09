# f3-matchflow

Orchestration tooling for [f3decomp](https://f3decomp.com): screen functions without
claiming them, assign exact targets to restricted OpenCode subagents, preserve checker
evidence, and audit claim closure. Matching work can incur model costs and mutate your
contributor account. Installation and offline tests never authorize matching runs.

This is **not** Fallout 3 source, a game distribution, a compiler, or a replacement for
the official f3decomp checker. You need your own legitimately purchased compatible PC
copy of Fallout 3 and a service account. The [official guide](https://f3decomp.com/agents.md)
governs contributions, account approval, toolchain installation and game compatibility.

## Start here

1. Read the [installation and folder-layout guide](docs/setup.md).
2. Install the official dependencies and configure your private external workspace.
3. Install the local OpenCode bridge and pass online preflight.
4. For authorized matching, follow the [orchestrator runbook](docs/orchestrating.md).

**Run OpenCode from this repository, not from the game folder or runtime workspace.**
The orchestrator stays here. Workers are bound child sessions whose writable source
area is an external `work/batches/BATCH/workerNN/files/` directory, not a Git worktree.

## Keep code and private data separate

```text
code/f3-matchflow/                 Public Git checkout; tooling, docs, tests
  matchflow.local.json             Your ignored machine-specific configuration
  opencode.jsonc                  Generated ignored OpenCode bridge

private/f3-workspace/              External, git-free runtime workspace
  packs/                          Exact claim packs (downloaded by the host)
  work/batches/                   Assignments, worker files, receipts, reports
  work/matchflow/                  Ledger, checked-source snapshots, screening data

games/Fallout 3/Fallout3.exe        Your private game, optionally inside the workspace
toolchains/f3kit/                  Separately installed official checker/compiler
private-credentials/api-key        Outside BOTH checkout and runtime workspace
```

These are illustrative names, not required paths. On Windows the checker/compiler
run inside WSL with Wine; the checkout and private workspace must be accessible there.
The setup guide explains Windows versus WSL paths and supplies a complete configuration.

Workers are not tied to OpenCode Go. Set `worker_model` in your ignored
`matchflow.local.json` to an available tool-capable chat model (`provider/model`, optionally
`#variant`). `batch.py prepare --model` overrides that setting for one batch. The launcher
checks OpenCode's active catalog before creating a child or taking a claim and rejects
unavailable models/variants without fallback. See [model setup](docs/setup.md#choosing-a-worker-model).

Never commit or upload packs, claim candidates, game files, disassembly, checker output,
databases, session exports or secrets. Ignore rules are a safeguard, not permission to
put private runtime data in the checkout.

## How it works

- `screen.py` saves a bounded read-only shortlist. Review history and report scope before
  selecting; neither a saved draft nor a near-miss count proves a target is ready.
- `operator_cli.py` provides status snapshots, review templates, evidence-backed selections
  and bounded read-only ledger/lifecycle inspection. No temp-folder Python is required.
- `batch.py` prepares immutable assignments, supervises workers, audits exact identities
  and summarizes results. Recovery requires explicit reviewed per-worker actions.
- `matchflow_launch` binds a dedicated `match-worker` child before its first prompt,
  takes one exact-address claim just in time and obtains its exact pack.
- Workers use session-scoped tools only: own files, read-only context/references, official
  checks, notes and own-claim closure. No shell, credentials, sibling access or selection.

This is a tool-capability boundary, **not an OS sandbox**. Trusted host plugins have
host authority. General/headless/Workflow matching launch paths are disabled. Local
MATCH, submitted and server-verified are distinct states.

## Requirements and support

Python 3.10+, Node 22+, OpenCode V2 (tested CLI/plugin SDK **2.2.6**), and separately
installed official dependencies. The live checker runner currently targets **Windows
with WSL/Wine**. Offline tests run on Windows and Linux; that does not establish native
Linux/macOS live orchestration support. OpenChamber can supply the background shell
notifications used by the runbook; another host needs equivalent reliable monitoring.

## Documentation

- [Setup, private paths and troubleshooting](docs/setup.md)
- [Orchestrator runbook](docs/orchestrating.md)
- [Full command and worker-tool reference](tools/matchflow/README.md)
- [Canonical local matching policy](docs/matching-policy.md)
- [Matching techniques](docs/matching-recipes.md)
- [Contributing and offline testing](CONTRIBUTING.md)
- [Pinned optional dependencies](third_party/README.md)

## Acknowledgements

Optional cross-game matching assistance uses [f3twins](https://github.com/mathisto/f3twins)
by [mathisto](https://github.com/mathisto), licensed under MIT. Thank you for the upstream
tooling, seeds and matching guidance. Our adapter installs a pinned, hash-verified copy
with its original license; f3twins is not required for ordinary matching. See
[dependency details](third_party/README.md) and the
[optional twins setup](tools/matchflow/README.md#optional-twins).

## License

Original tooling and documentation are [MIT licensed](LICENSE). Separately installed
dependencies retain their own licenses. This license grants no rights to Fallout 3,
private context packs or other service-provided materials. f3-matchflow is a contributor
tool, not an official Bethesda product.
