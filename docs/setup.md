# Installation and private workspace layout

## 1. Understand the four locations

Choose these before installing. The example paths below are placeholders:

- **Checkout:** `C:/dev/f3-matchflow`. Public code, tests, policy and prompts. Start
  OpenCode and run all documented host commands here. Do not copy the tools elsewhere.
- **Runtime workspace:** `D:/private/f3-workspace`. A separate ordinary folder, not a
  Git repository, not inside the checkout, and not a parent of the checkout. No junctions
  or symlinks. The host creates its runtime subdirectories when needed.
- **Official dependencies:** for example `/home/contributor/.f3kit` inside WSL. This
  contains the official checker, VS2005 compiler, Wine prefix and shared files. They are
  installed separately, never vendored here. The game can remain at its existing Windows
  installation path or be placed under the workspace. Nothing moves it automatically.
- **Credential file:** for example `C:/Users/YOU/AppData/Local/f3decomp/api-key`, outside
  both checkout and runtime workspace. Restrict OS access to your user. This is a private
  text file containing only the API key. Never put the value in JSON, prompts or argv.

The name `f3decomp` is not a special directory requirement. Existing installations may
use that name for the runtime workspace, a credential directory or official kit storage;
configure each path by its role rather than merging these directories.

After a matching run, the private workspace looks roughly like:

```text
D:/private/f3-workspace/
  packs/CLAIM/                       Official exact-claim context pack
  work/
    matchflow/
      ledger.sqlite3                 Local checks and verdict evidence
      sources/CLAIM/HASH.cpp          Immutable checked-source snapshots
      screening-01/                  Saved service responses and review material
      selection.json                 Explicit reviewed selection
      status-before-dispatch.json    Saved actual service status
    batches/BATCH/
      batch.json                     Immutable manifest
      lifecycle.sqlite3              Host lifecycle/supervision state
      worker01/
        assignment.json              One immutable address and model
        prompt.txt                   Generated prompt snapshot
        files/                       Worker's writable candidate/note area
        report.json                  Canonical evidence report after closure
        closing-notes.json           Acknowledged closing-note receipt
        ...                          Host receipts and private evidence
```

Workers are child sessions bound to assignments, not separate copies of this repository.
They cannot choose another address, inspect siblings or write outside their own allowed
files. The host owns the pack, checked-source snapshots and closure evidence. Do not edit
workspace artifacts or run setup/preflight while workers are active.

## 2. Install prerequisites and connect the service

Install Windows Python 3.10+, Node 22+, OpenCode V2 (tested 2.2.6), WSL and Wine in your
chosen WSL distribution. You need a model provider configured in OpenCode, but do not
need OpenCode Go or DeepSeek specifically. Set your own persistent worker model below.
Never silently fall back to a different provider/model.

Follow [the official service guide](https://f3decomp.com/agents.md) to connect your own
contributor account, approve the terms and save the resulting key privately. Do not reuse
someone else's key. An assisting agent must use a private secret mechanism, not request
that you paste the key into chat. The official first tool is `start_here`.

Install the official kit using that guide:

- Official f3check and its required Python dependencies (including target inspection's
  `capstone`) in the WSL Python environment used by the runner.
- Microsoft Visual C++ 2005 SP1 **14.00.50727.762**, including SDK headers such as
  `SDK/Include/windows.h`. The kit's `get_vs2005.py` documents extraction from the official
  SDK image. This repository does not download or install the compiler for you.
- Official `function_index.json`, `f3decomp_symbols.json`, `compiler_tips.md` and
  `large_functions.md`, refreshed when the service changes the shared-file version.
- Your own compatible Fallout 3 **1.7.0.3** executable, validated by official f3check
  with `--check-image` and an **IMAGE OK** result. Steam 1.7.0.4 is not compatible as
  installed. Follow the official instructions for your edition; no patching is performed
  by this project. If validation fails, stop, do not bypass it or obtain a game elsewhere.

Account tiers restrict sizes and addresses. New accounts receive practice functions;
the pipeline's exact-address production assignments require service eligibility (Regular
or above). Complete the official practice workflow before using production dispatch.

## 3. Configure paths

Create `matchflow.local.json` in the checkout (it is ignored). Replace every example
value with your actual paths. Use forward slashes in JSON to avoid backslash escapes:

```json
{
  "workspace": "D:/private/f3-workspace",
  "worker_model": "YOUR_PROVIDER/YOUR_CHAT_MODEL",
  "key_file": "C:/Users/YOU/AppData/Local/f3decomp/api-key",
  "toolchain": {
    "distro": "Ubuntu",
    "user": "contributor",
    "checker": "/home/contributor/.f3kit/f3check/f3check.py",
    "compiler": "/home/contributor/.f3kit/vs2005",
    "wine_prefix": "/home/contributor/.f3kit/wine-vs2005",
    "index": "/home/contributor/.f3kit/function_index.json",
    "symbols": "/home/contributor/.f3kit/f3decomp_symbols.json",
    "image": "D:/Games/Fallout 3/Fallout3.exe"
  }
}
```

`workspace`, `key_file` and `image` above are Windows host paths. Checker, compiler,
Wine prefix, index and symbols are WSL paths. The runner converts host paths to `/mnt/`
paths as needed. The configured WSL user must be able to read the key, checkout, image
and workspace and write runtime evidence; use its own Wine prefix. Root is not required.

Without explicit toolchain settings the defaults target `Ubuntu`, user `root`, paths
under `/root/.f3kit`, and `WORKSPACE/Fallout 3/Fallout3.exe`. These are conveniences for
existing installs, not a provisioning promise. `F3_MATCHFLOW_WORKSPACE` overrides only
the workspace setting. The key defaults to `%LOCALAPPDATA%/f3decomp/api-key` on Windows;
an explicit `key_file` avoids ambiguity. All configured paths must actually exist where
required; create the empty workspace directory before bootstrap.

### Choosing a worker model

Replace `YOUR_PROVIDER/YOUR_CHAT_MODEL` with an exact model selection available in your
OpenCode installation. Use OpenCode's model picker to find the provider, model and
supported variants; configure/authenticate the provider first. Workers require a
**tool-capable chat model**, not an image or decision model. Model-provider credentials
are managed by OpenCode, separately from the f3decomp API-key file.

Selection precedence, evaluated when preparing a batch:

1. `batch.py prepare ... --model "provider/model#variant"` (per-batch override).
2. `worker_model` in ignored `matchflow.local.json` (persistent local choice).
3. Built-in `opencode-go/deepseek-v4.1-flash#max` (legacy default if neither is set).

Use `provider/model` without `#variant` if you do not want an explicit variant. Do not
copy `#max` to another model unless it actually offers it. The first slash separates
provider from model; model IDs may themselves contain slashes. There is no automatic
fallback, and the orchestrator's own model does not determine the worker model.

`prepare` validates syntax and freezes the resolved model into assignments and claim
metadata. The Python CLI has no model-catalog access: **online preflight checks the
service/toolchain, not model availability**. At every `matchflow_launch`, the plugin
reads OpenCode's active catalog for this checkout and verifies the exact prepared
provider/model is enabled, chat/tool-capable and has the requested variant. This happens
before child creation, binding and claiming. Missing models, invalid variants or a
catalog-read failure produce an actionable setup error with no claim taken.

Changing `worker_model` affects newly prepared batches only. Do not edit existing
assignments. After a pre-claim model rejection, either make that exact model available
or prepare a fresh reviewed batch with another explicit model. The catalog check makes
no paid generation request and cannot prove provider credit, network health or future
credential validity. Later failures require normal supervision/cleanup; never automatically
relaunch an ambiguous claim.

## 4. Install the bridge and validate

From the checkout, with no matching workers running:

```text
npm ci --ignore-scripts --prefix integrations/opencode
python tools/matchflow/bootstrap.py
python tools/matchflow/preflight.py --online
```

Bootstrap writes an ignored `opencode.jsonc` here. It loads repository policy, the
official guide, the local plugin and a **file reference** to the external key. It does
not install the toolchain, move files, read/copy the key, take claims or launch workers.
It refuses to overwrite custom OpenCode configuration. If you already have a config,
merge the bridge's instructions, plugin and MCP settings manually using `bootstrap.py`
as the reference; keep a file reference, never a literal key.

Open/reload OpenCode in this checkout. Confirm the `f3decomp` MCP connection and that
`matchflow_launch` is available to the orchestrator. `.opencode/agents/match-worker.md`
and the plugin must be loaded; workers get only `matchflow_worker`. If tools are absent,
fix the bridge/plugin connection rather than broadening worker permissions.

Preflight validates configured compiler/image and both host and official work-note
transport. It is read-only remotely, but writes private local readiness evidence. A
successful result expires after 15 minutes and is invalidated by configuration/tool
changes. Without `--download-url` it explicitly leaves exact-pack download readiness
unprobed; never claim just to test downloads. Passing preflight is not permission to
start paid matching. See the [runbook](orchestrating.md) when matching is requested.

## Troubleshooting

- **Missing workspace:** create the external directory and check the environment
  override as well as `matchflow.local.json`. Do not solve it by pointing at the checkout.
- **Missing compiler/headers or wrong version:** install the official compiler and SDK
  completely in the configured WSL location. Do not change compiler flags to hide setup
  failures or patch the official checker.
- **WSL paths fail:** check the distribution/user names and drive mounts. Spaces in
  paths are allowed; the runner passes arguments without an implicit shell.
- **IMAGE CHECK FAILED:** stop and follow the official edition-specific guide. No
  automatic downgrade, extraction or game download is part of setup.
- **401/403:** verify account approval, key-file permissions and service connectivity.
  Do not print credentials or attempt browser impersonation.
- **429/503:** respect `Retry-After` and stop new starts. No retry loops.
- **Stale selection/status/readiness:** obtain fresh read-only evidence and review it;
  do not update a timestamp on copied data. Never refresh setup against active workers.
- **Missing optional twins:** ordinary matching still works. See the command reference
  for the pinned optional install; private derived caches stay in the workspace.
- **Worker model unavailable:** configure/authenticate its provider in OpenCode, or set
  your own `worker_model` and prepare a new batch. Check spelling and supported variants.
  The pre-claim catalog check never changes the model or takes a test claim.

For help, share only sanitized error messages, versions and reproduction steps. Do not
attach local configuration, keys, packs, checker output, game-derived data or session
exports to public issues.
