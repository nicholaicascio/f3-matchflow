# Matchflow v3 command reference

Run from this checkout. Policy: [`../../docs/matching-policy.md`](../../docs/matching-policy.md).
The external workspace is private runtime storage, not a second copy of our tools.
OpenCode 2.2.6 is the tested plugin SDK/CLI version. Only restricted subagents launch.

## Install and diagnose

Configure ignored `matchflow.local.json` (or `F3_MATCHFLOW_WORKSPACE` for workspace):

```json
{
  "workspace": "F:/Games/f3decomp",
  "toolchain": {
    "distro": "Ubuntu", "user": "root",
    "checker": "/root/.f3kit/f3check/f3check.py",
    "compiler": "/root/.f3kit/vs2005",
    "wine_prefix": "/root/.f3kit/wine-vs2005",
    "index": "/root/.f3kit/function_index.json",
    "symbols": "/root/.f3kit/f3decomp_symbols.json",
    "image": "F:/Games/f3decomp/Fallout 3/Fallout3.exe"
  }
}
```

Defaults above are conveniences, not automatic provisioning. The credential file defaults
to `%LOCALAPPDATA%/f3decomp/api-key`; optional `key_file` changes it. Keys must stay outside
the repository. Never put a key's value into configuration, arguments, logs or prompts.

```text
npm ci --ignore-scripts --prefix integrations/opencode
python tools/matchflow/bootstrap.py
python tools/matchflow/preflight.py --online
```

Bootstrap installs only a local bridge here, references credentials without reading them,
and migrates its exact prior generated bridge with a private ignored backup. Custom
configs are never overwritten. OpenCode watches configuration; `/reload` is available.
The plugin package and `.opencode/agents/match-worker.md` must be loaded before matching.

Preflight is read-only remotely: start_here/status through our HTTPS client and status
through the official checker's own f3notes client. It verifies exact compiler and image
without changing official tooling. `--download-url OFFICIAL_PACK_URL` optionally probes
an existing exact-claim pack URL; otherwise download readiness is explicitly unprobed.
No fake claim is taken for a probe and no work note is uploaded. Readiness is fingerprinted
against configuration/tool sources and expires after 15 minutes. Changes require rerunning.
Do not run preflight or change workspace artifacts while matching workers are running.

Our `check.sh`, `check_runner.py` and `checker.py` stay here. They invoke the external
official checker through configured WSL/Wine without sourcing workspace shell scripts.
Credentials are loaded privately inside the trusted runner, never passed in argv.
An enforced `--` boundary separates trusted runner options from checker arguments.
Notes are forwarded as literal `--name=value` data, never runner configuration.

## Screen and prepare (orchestrator only)

Use the official service tools: start_here, status, list_bands, list_functions and
function_info. Review history/reports without claiming. Save fresh actual metadata:

```json
{"checked_at":"CURRENT_UTC_ISO_TIMESTAMP","targets":[{"address":"0x00abcdef","size":160,"state":"open","co_workers":0,"lane":"research","give_ups":4,"open_problem_reports":0,"file_problem_reports":0,"history_reviewed":true,"history_sources":["function_info and saved prior report paths"],"prior_verdicts":[],"verdict_history":"unknown","new_hypothesis":"Named receiver removes the documented duplicate reload","expected_observation":"Receiver reload disappears without changing call order","reason":"Reviewed structural lead; not repeating exhausted sweeps"}]}
```

Do not copy the illustrative address or fabricate metadata. Production requires reviewed
clean verdict history. Three or more give-ups/reports route to research with a concrete
hypothesis; any prior unfit requires integration. Prepare merges known local verdicts.
Status JSON is `{"checked_at":"CURRENT_UTC_ISO_TIMESTAMP","status": ACTUAL_SERVICE_STATUS}`.

```text
python tools/matchflow/batch.py prepare BATCH --selection PRIVATE_SELECTION.json
python tools/matchflow/batch.py dispatch BATCH --method subagents --status PRIVATE_STATUS.json
python tools/matchflow/batch.py monitor BATCH
```

`prepare --model provider/model#variant` sets the explicit worker model. It generates
immutable v3 assignments and prompt snapshots from the repository template. No claims
are taken by prepare/dispatch. The parent runs monitor in the background with completion
notification, then calls `matchflow_launch({batch:"BATCH",worker:"worker01"})` per worker.
This tool makes a real fixed-address claim and starts a linked child session, so use it
only for requested matching work, never code tests. It binds identity before the prompt,
rechecks service capacity/availability, downloads the exact pack safely and registers it.
No replacement/refill, auto-resume or mutation retries. Legacy batches cannot launch.

## Restricted worker contract

The only available worker tool is `matchflow_worker({op, data})`. Its real session identity
comes from OpenCode, not the model. Workers cannot select addresses/claims, databases,
executables, credentials or host paths. Bindings and assignment/manifest hashes live in
host control storage. Paths are relative and reject traversal, drives, UNC, alternate
streams, devices, symlinks, junctions and other reparse points. The dedicated agent and
session both deny every other tool, and request hooks remove broad tools defensively.
This is tool-boundary containment, not a separate OS sandbox or protection against the
human changing host files. Other trusted plugins run with host authority.

Operations:
- `status`: assigned target, measured timing, state and closure marker.
- `read`: `area=files|pack|packet`, `path`, optional one-based `offset`, bounded `limit`.
- `list`, `search`: files or assigned pack; search is literal and bounded.
- `write`: relative `path`, `text`; only candidate/source/note files inside `workerNN/files`.
- `reference`: `name=policy|recipe|compiler_tips|large_functions|upstream_tips|fnv`.
  FNV also needs a relative source `path`. References/packs are read-only.
- `twin`: look up only the assigned address in a versioned private cache.
- `target`: private read-only instruction inspection of the assigned function only,
  with byte `offset` and instruction-count `limit` pagination. No worker-supplied
  address/extent, raw bytes, shell or service upload. Never paste output into notes.
- `checkpoint`: `phase=plan|draft_review|notes`, substantive `note`; forced own claim.
  Closing notes are accepted only after acknowledged closure and must have their own
  acknowledged checkpoint receipt; a plan/proof checkpoint or give-up prose is not enough.
- `check`: `source`, `stage=baseline|attempt|final`, `change/why/expect`, `observed`
  from check two, and required `regions`. Optional complete comma-separated codegen
  `flags` is allowlisted; include/output/path/compiler-injection options are refused.
- `gate`: `decision`, `reason`, `hypothesis`, `expected`, optional `integration_plan`.
- `register_wall`: evidence for a register/slot-only wall; does not reset clocks.
- `promote`: freezes only an unchanged final official MATCH to host-owned `match.cpp`.
- `close`: `operation=submit|giveup|flag-proof`. Giveup uses `source` plus `notes` containing
  reason/category/tried/still_differs/suspected_cause, or explicit `no_source_reason`.
  Flag-proof needs `notes.note`. Submit uses only the host's promoted artifact.
- `report`: generates a canonical v3 evidence report after closure and acknowledged
  closing notes. Unclaimed blocked assignments do not require closing notes.

The worker does not get a shell to run private scripts or permuter loops. Ask the parent
for a reviewed additional capability rather than broadening permissions. Scalar timing
and metrics derive from host/checker records, not worker estimates. Final baseline MATCH
still needs a continue gate and final check before promotion. No local result proves
server verification or full-unit compatibility.

## Closure, failure and audit

```text
python tools/matchflow/batch.py audit BATCH --status PRIVATE_STATUS_AFTER.json
python tools/matchflow/service.py reconcile PRIVATE_BATCH_FOLDER worker01
python tools/matchflow/matchflow.py import-status PRIVATE_OFFICIAL_STATUS.json
python tools/matchflow/matchflow.py summary
```

The local watchdog enforces conservative dispatch-based deadlines, 20-minute wall,
30-minute stagnation and 40+5 minute total limits, shortened by actual service lease.
All checks, including those labelled `final`, must pass the experiment guards again
immediately before starting compilation. There is no closure-time compile
exception: the five-minute closure period is for preserving already checked evidence,
promotion/submission of an unchanged final MATCH, giving up and closing notes.
Supervisor loss/STOP means closure only. The parent must respond to monitor completion,
inspect exact sessions and reconcile only its owned claims. Completion notices are
event-driven. OpenCode/plugin restarts never automatically replay work or reset leases.

Transport exceptions, invalid responses and ambiguous mutations preserve claim/closure
reservations. Never relaunch to repair them. HTTP 401, 403 edge rejection/access denial,
429 and 503 are distinguished without printing keys or response bodies. 429/503 writes
STOP/cooldown evidence; failures without acknowledged closure stop new starts and wake
the parent. No browser impersonation, global retry loop or official checker patching.
Host setup or prompt-admission failure persists a batch-wide cleanup barrier before
returning. The barrier is latched in lifecycle storage; deleting STOP does not clear it.
Reconcile that batch and prepare a fresh reviewed batch, never automatically resume it.

If a worker crashes or setup fails after a claim, the parent uses existing host-only
`service.py giveup --source ... --notes ...` (or explicit no-source reason), submit or
flag-proof as appropriate, then reconciles. A lost claim response needs the exact claim
ID confirmed by official status. Missing receipt is unresolved, not invented success.
Closing notes remain required. No automatic bulk cleanup, unrelated release or claim retry.

For host recovery after an early setup failure:
```text
python tools/matchflow/service.py notes PRIVATE_BATCH_FOLDER worker01 --notes PRIVATE_CLOSING_NOTES.json
python tools/matchflow/evidence.py report PRIVATE_BATCH_FOLDER worker01
```
The notes JSON has `{"note":"Substantive closing notes describing the setup failure"}`.
This follows an acknowledged give-up, not instead of one. Pending/ambiguous notes are
never replayed or inferred successful. A pre-registration failure may produce a
receipt-only report only when saved official ownership, host failure evidence,
explicit no-source give-up acknowledgement and closing-notes receipt agree. Its pack,
source and checker fields remain null; no ledger attempt or check is invented.

Audit validates canonical raw checker evidence/hashes, ownership, session identity,
receipt contents, source handoff and actual verdict separately. Absence from open claims
does not prove submission/retention. Non-clean audits exit 2. v1/v2 history stays preserved,
never silently upgraded. Host-only ledger/reclassify/import/usage CLI utilities remain
available for operator investigation; they are not worker capabilities.
Report v3 independently validates closing-notes claim/session/phase, post-closure
timestamps, checkpoint acknowledgement and receipt hash. Historical reports/receipts
remain untouched and are not relabelled v3 or accepted without the required proof.

The launcher captures raw child-session usage at completion. Aggregate it with
`python tools/matchflow/usage.py PRIVATE_BATCH_FOLDER --method subagents`. Missing
counters stay missing; input/cache semantics and billed cost are not guessed.
Silent provider/model failover is rejected before the next model request; the parent
must review/reconcile instead of silently attributing work to a different model.

## Optional twins

```text
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r tools/matchflow/requirements-twins.txt
python tools/matchflow/twins.py install
python tools/matchflow/twins.py build
python tools/matchflow/twins.py all
```

Configure `fnv_decomp` to a clean public FNV clone, `twins_python` to the virtualenv Python,
and `twins_inputs` with host-readable image/index/symbols. Public dependencies may live
under this repository's ignored `.deps`; private cache is forced into external work/.
The lock pins upstream code/seeds/references and MIT license. Cache identity includes
image/shared-file hashes, FNV revision/units and upstream revision; stale results fail
closed. Only local pickle caches are trusted. `bridge` optionally uses configured
`fnv_x360`; lookup adds its file/name corroboration, never reranks by it. Missing twins
are explicit and do not prevent ordinary assembly-led matching.

## Offline verification

```text
python -m unittest discover -s tools/matchflow -v
npm test --prefix integrations/opencode
```

These tests mock service/compilation and never claim/submit or launch paid matching agents.
They exercise session/path isolation, archive/HTTP safety, compiler-option restrictions,
SDK registration, lifecycle, exact ownership and a full mocked claim-to-audit flow.
