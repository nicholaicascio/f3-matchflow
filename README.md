# f3-matchflow

Private, version-controlled orchestration tooling for the f3decomp workspace.
The repository contains pipeline code, offline tests, a report schema and an
OpenChamber workflow. It does **not** contain the game, checker/toolchain,
context packs, claim sources, credentials, databases or runtime artifacts.

## Configure the separate workspace

Requires Python 3.10+; offline tests use only the standard library. Actual matching
also requires the separately provisioned workspace, WSL/Wine, official checker and
compiler, and OpenChamber's bundled OpenCode.

Set `F3_MATCHFLOW_WORKSPACE` to the absolute external workspace path, or create
ignored `matchflow.local.json` in this checkout:

```json
{"workspace":"F:/Games/f3decomp"}
```

The environment variable takes precedence. Missing configuration, relative paths
and workspaces inside this repository are rejected. No runtime payload belongs in
the checkout. Credentials remain outside both source control and the workspace;
only the explicit service gateway reads the existing private key file.

## OpenChamber orchestrator setup

Moving a session to this Git checkout does not inherit the old workspace's OpenCode
configuration or `AGENTS.md`. After configuring the workspace, run:

```powershell
python tools/matchflow/bootstrap.py
```

This creates ignored local `opencode.jsonc` that loads the workspace matching policy
and official service guide and connects the f3decomp MCP server. It references the
existing private credential file; it never reads or copies the key. Custom project
configs are not overwritten. Use `--key-file` for a non-default credential location.
Check `/mcps` for connection status; use `/reload` if needed to refresh an existing
session. A fresh session must see matching instructions and service discovery tools
before selecting targets. No claims or workers are started by setup.

## Test

```powershell
python -m unittest discover -s tools/matchflow -v
```

For a fresh checkout/CI without local configuration, point the environment variable
at an external scratch workspace before running tests. Tests mock compiler/network
operations; they do not establish live service or game integration.

## Run

Invoke scripts by their absolute checkout path, from the configured workspace.
Generated worker prompts also use absolute pipeline paths, so code and runtime
data can live on different drives. See the [command reference](tools/matchflow/README.md).

An existing workspace may retain a directory junction from `tools/matchflow` to
this checkout's `tools/matchflow`. This is a compatibility entry point, not a second
source copy. The workspace's `.openchamber/workflows/match-batch.js` is a deployed
copy of the tracked workflow: redeploy it after changes, only with workers stopped.
Workspace policies remain in that workspace's `AGENTS.md`; repository instructions
do not replace the service rules or the matching policies.

## Source control boundaries

Only source/docs, sanitized offline tests, schema, workflow and CI are tracked.
Do not stage from the private workspace or import historical reports as fixtures.
Review `git diff --cached` and `git status` before every push. `.gitignore` is a
convenience guard, not permission to store sensitive material or use `git add -f`.
