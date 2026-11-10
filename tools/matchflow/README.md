# Matchflow v3 command reference

Run from this checkout. Policy: [`../../docs/matching-policy.md`](../../docs/matching-policy.md).
The external workspace is private runtime storage, not a second copy of our tools.
OpenCode 2.2.6 is the tested plugin SDK/CLI version. Only restricted subagents launch.

New installation? Start with [setup and folder layout](../../docs/setup.md), then the
[orchestrator runbook](../../docs/orchestrating.md). The commands here assume the
separate official kit is already installed and the host is Windows with WSL/Wine.
Do not run setup/preflight against an active matching workspace.

## Install and diagnose

Configure ignored `matchflow.local.json` (or `F3_MATCHFLOW_WORKSPACE` for workspace):

```json
{
  "workspace": "F:/Games/f3decomp",
  "worker_model": "YOUR_PROVIDER/YOUR_CHAT_MODEL",
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
Replace the `worker_model` placeholder with your actual OpenCode model selection, optionally
including `#variant`. See [model setup](../../docs/setup.md#choosing-a-worker-model).

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
Do not rerun preflight while matching workers are running. During a stream the frozen
configuration fingerprint is checked without active preflight. Isolated new assignments,
receipts and scoped worker evidence writes are permitted; shared tooling/config/inputs
must not change.

Our `check.sh`, `check_runner.py` and `checker.py` stay here. They invoke the external
official checker through configured WSL/Wine without sourcing workspace shell scripts.
Credentials are loaded privately inside the trusted runner, never passed in argv.
An enforced `--` boundary separates trusted runner options from checker arguments.
Notes are forwarded as literal `--name=value` data, never runner configuration.
WSL is started with `--exec`, bypassing its implicit shell so backticks/`$()` in
notes are not evaluated before reaching the runner.

## Authorized streaming tools (orchestrator and bound producer)

Streaming is an alternative to the reviewed prepare/dispatch path below, not authorization
to claim or spend. Obtain the human's matching request and fresh successful online
preflight before opening. No live tests, claims or paid launches are authorized by this
reference or by offline testing.

### `matchflow_stream` (orchestrator only)

- `open`: `batch` (fresh name), `requested` 1..50, `backlog` 1..4 (default 2), optional
  `model` (`provider/model#variant`). Resolve explicit model, then local `worker_model`,
  then built-in default only if absent. Validate the exact enabled tool-capable chat
  model and optional variant, with no fallback. Create the frozen header, freeze the
  configuration fingerprint, start the watchdog and start a host-bound restricted
  `match-discovery` producer. This is a real authorized run, not a read-only probe.
- `next`: `batch`. Consume one FIFO acknowledged queued claim and launch its bound
  `match-worker`, without an additional claim. Do not call prepared `matchflow_launch`
  for these tasks. No replacement, automatic refill or mutation retry.
- `status`: `batch`. Read-only run state, not a resume or launch operation.

Example tool inputs, not shell commands:

```json
{"op":"open","batch":"YOUR_FRESH_BATCH_NAME","requested":20,"backlog":2}
{"op":"next","batch":"YOUR_FRESH_BATCH_NAME"}
{"op":"status","batch":"YOUR_FRESH_BATCH_NAME"}
```

Immediately after open the parent starts this command **in the background** with
completion notification; do not synchronously wait for it before consuming tasks:

```text
python tools/matchflow/batch.py monitor BATCH
```

### `matchflow_discovery` (bound producer only)

Only the host-bound `match-discovery` session uses `{op,data}`. It has no shell, raw
service access, credentials, source inspection, child sessions or alternate run paths.

- `catalog`: initial start_here/status/list_bands, then at most four catalog pages.
- `inspect`: exact `address` on a shortlist bounded by 4*N for requested N. Preserve
  actual service receipts and relevant local history; review substantive attempts,
  ownership, availability, verdicts and report scope under canonical policy.
- `enqueue`: `address`, `reason`, `review`. The host validates review facts and fresh
  metadata, reserves the task, claims the exact address, saves its acknowledgement and
  then queues it. Concrete next experiments and prior-unfit integration plans remain
  required under the same review contract as prepared selections.
- `wait`: bounded local backlog wait, not remote polling or an expanded search budget.
- `finish`: explicit exhaustion with `reason`; a bounded search may finish short.
- `status`: read-only producer/run state.

Unavailability discovered before claiming can be skipped and does not count toward the
requested total. An ambiguous claim or other mutation is never retried. Startup failure
requires STOP and explicit reconciliation, never a replacement target or automatic resume.
Retain reservations and receipts for reviewed recovery, including acknowledged queued
claims that never reached a worker. Do not infer closure from an empty local queue.

At quota `producer_done` means N acknowledged claims, separate from consumer assigned N,
claim closure, acknowledged closing notes and canonical reports. Explicit exhaustion may
leave fewer than N. The monitor follows the dynamic queue and worker set, coalesces routine
completions, and prioritizes STOP, cleanup, heartbeat loss and other urgent events. Re-arm
after notifications while discovery, queued claims or workers remain.

`batch.json` is an immutable streaming header; task records are append-only in
`stream_tasks`. Use `lifecycle.manifest` to obtain the combined header/task view rather
than treating the header as a complete worker list. Isolated assignment/receipt writes
may continue while workers work. Never change shared tooling, configuration or inputs;
the run checks its frozen config fingerprint without rerunning preflight while active.

## Screen and prepare (orchestrator only, retained prepared-batch path)

Use the official service tools: start_here, status, list_bands, list_functions and
function_info. Review history/reports without claiming. Save fresh actual metadata:

For a bounded first pass (read-only remotely, no game inspection or agents):

```text
python tools/matchflow/screen.py --output screening-01 --count 8 --max-pages 2 --max-size 1023
```

Relative outputs live under the external `work/matchflow/`. The command calls
start_here first, then status/bands, up to two 500-entry catalog pages, and function_info
only for the final shortlist. Responses, notices and relevant local ledger history are
preserved without scanning source trees. File reports are
review signals, not automatic evidence of a defect in every function. No claim, source
hypothesis, report scope or clean history is invented. `review.json` is NOT an accepted
selection: review history, report scope and known local verdicts, annotate evidence and
prepare the facts-based selection below. A saved draft and a sibling-report count do not
rank or exclude a target. A raced target is not replaced automatically.
Do not exhaustively page every band or inspect original game instructions by default.
Insufficient candidates produce an incomplete review, not more requests in a retry loop.

```json
{"checked_at":"CURRENT_UTC_ISO_TIMESTAMP","targets":[{"address":"0x00abcdef","size":160,"state":"open","co_workers":0,"give_ups":4,"open_problem_reports":0,"file_problem_reports":1,"reason":"Reviewed structural lead; not repeating exhausted sweeps","review":{"history_reviewed":true,"history_sources":["function_info and saved prior report paths"],"prior_verdicts":[],"verdict_history":"reviewed","report_scope":"sibling","report_evidence":"Saved report concerns a different function in this file; no shared helper","prior_unfit":false,"next_experiment":"Named receiver removes the documented duplicate reload","integration_plan":null}}]}
```

Do not copy the illustrative address or fabricate metadata. There is no mutually exclusive
lane: record the facts separately. A sibling-only report does not exclude the target. A
function-scoped or true unit-wide report, three or more give-ups, or a prior unfit requires
a concrete `next_experiment`; prior unfit also requires an `integration_plan`. `prior_unfit`
may be true because of verdicts or because you discovered it; `prepare` merges known local
verdicts. A saved draft is starting material, never a selection preference.
Status JSON is `{"checked_at":"CURRENT_UTC_ISO_TIMESTAMP","status": ACTUAL_SERVICE_STATUS}`.

### Supported preparation helpers (no temporary Python scripts)

`operator_cli.py` is orchestrator-only. It never claims, compiles, launches workers or
mutates the service. Create a review template from the saved screen, edit that private
JSON with the actual review findings, then join it to the original service evidence:

```text
python tools/matchflow/operator_cli.py review-template --screen PRIVATE_SCREEN_FOLDER --output review-annotations.json
python tools/matchflow/operator_cli.py selection --screen PRIVATE_SCREEN_FOLDER --annotations PRIVATE_ANNOTATIONS.json --output selection.json
python tools/matchflow/operator_cli.py status --output status-before-dispatch.json
```

Relative **output** paths resolve under the external `work/matchflow/`; input paths
(`--screen`, `--annotations`) are explicit paths relative to the current directory or
absolute. Outputs must be inside the external workspace, reject links/reparse points,
and are created exclusively, never overwritten. Choose a new name for each snapshot.

The template has `targets`, each with only `address`, `reason` and `review`. Remove
unselected targets. Fill in the reason and every review fact from the evidence;
`history_reviewed=false` and unknown/empty fields are intentionally not ready to dispatch.
No service metadata belongs in annotations. Review substantive history and report scope
yourself; the command does not invent hypotheses or mark history reviewed for you.

Selection joins at most 50 targets to unchanged `info-*.json` receipts in the screen's
shortlist, validates the existing selection/review contract, and preserves the **oldest
selected function_info timestamp**, plus each receipt's path and hash. Re-saving annotations
cannot refresh old facts: receipts older than 15 minutes fail. Re-screen and review again
when stale; there is no automatic replacement, refresh or retry. `batch.py prepare` still
merges known local verdicts and enforces integration requirements as before.

Status calls `start_here` then `status` once each through the trusted transport, saves the
actual status response in the format consumed by dispatch/audit, and returns notices.
An adjacent `OUTPUT.start.json` preserves the start response, including one-shot notices,
even if status fails. On failure inspect that receipt before trying again; no retry is
automatic. The snapshot's conservative timestamp is the request start, not a copied
response with a newly invented timestamp. It does not run preflight or authorize launches.

Use the generated selection and status paths with prepare/dispatch below. After workers
finish, create another status snapshot for audit instead of reusing the dispatch snapshot.

```text
python tools/matchflow/batch.py prepare BATCH --selection PRIVATE_SELECTION.json
python tools/matchflow/batch.py dispatch BATCH --method subagents --status PRIVATE_STATUS.json
python tools/matchflow/batch.py monitor BATCH
```

`prepare --model provider/model#variant` overrides local `worker_model` for one batch.
Without the option, it reads that ignored local setting, then falls back to built-in
`opencode-go/deepseek-v4.1-flash#max` only if the setting is absent. Invalid settings fail;
variants are optional. The resolved choice is frozen into the assignment. It generates
immutable v3 assignments and prompt snapshots from the repository template. No claims
are taken by prepare/dispatch. The parent runs monitor in the background with completion
notification, then calls `matchflow_launch({batch:"BATCH",worker:"worker01"})` per worker.
This tool makes a real fixed-address claim and starts a linked child session, so use it
only for requested matching work, never code tests. It binds identity before the prompt,
rechecks service capacity/availability, downloads the exact pack safely and registers it.
No replacement/refill, auto-resume or mutation retries. Historical unsupported manifest
versions cannot launch; the reviewed v3 prepared-batch path remains available alongside
streaming.

Before child creation/binding or claiming, the plugin verifies the exact prepared
provider/model is enabled, chat/tool-capable and offers the optional variant in OpenCode's
active catalog for the checkout. Missing models and catalog-read failures stop with setup
guidance, never fallback. Python `preflight.py --online` checks service/toolchain only,
not model availability. The catalog check makes no paid generation request and cannot
guarantee provider credit/network/credentials; later failures follow normal cleanup rules.
Changing the local setting affects new batches only, never immutable prepared assignments.

## Restricted worker contract

### Streaming crash supervision and pre-handoff recovery

The plugin renews a durable producer-controller heartbeat every ten seconds. The
independent watchdog stops the run after thirty seconds without renewal, even if
no matching workers exist; discovery also has a forty-minute total bound. A FIFO
launch reservation abandoned for two minutes latches STOP. A lost `next` response
requests STOP immediately, never another dequeue. These deadlines do not renew
claim leases or worker experiment budgets.

Recovery inventory includes `partial_binding_session_id` when the capability
registry was committed but the lifecycle session write was interrupted. Explicit
reviewed recovery may use `binding_session_id` and `session_id` set to that exact
saved identity, plus `unprompted_binding: true`. The host requires STOP, unchanged
assignment/header hashes, the exact parent, and evidence that setup never began.
It repairs the local identity only, then uses the ordinary acknowledged no-source
give-up, notes, report and audit path. No child is run or fake completion invented.

For a stopped claim that never acquired a capability binding, use `session_id: null`
and `unassigned: true`. Both cases require an explicit `no_source_reason`, structured
give-up notes and substantive `closing_note`. Neither can carry source/check IDs.
Ordinary bound workers still require real host completion evidence. Pending claim
or closure responses remain reconciliation cases, never automatically replayed.

The only available worker tool is `matchflow_worker({op, data})`. Its real session identity
comes from OpenCode, not the model. Workers cannot select addresses/claims, databases,
executables, credentials or host paths. Bindings and assignment/manifest hashes live in
host control storage. Paths are relative and reject traversal, drives, UNC, alternate
streams, devices, symlinks, junctions and other reparse points. The dedicated agent and
session both deny every other tool, and request hooks remove broad tools defensively.
This is tool-boundary containment, not a separate OS sandbox or protection against the
human changing host files. Other trusted plugins run with host authority.

Operations:
- `status`: assigned facts, measured remaining experiment time, state, closure marker,
  near-miss advisory (best valid check at most 40 instructions different) and parent help
  requests/answers.
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
- `permute`: owned `source`, optional `stage=attempt|final`, allowlisted `flags`, bounded
  `trials` 10..400, `jobs` 1..4 and `timeout_seconds` 60..600. The host copies the exact
  candidate to a private run directory, invokes the official `--permute` search without a
  worker-selected path or shell, preserves stdout/stderr and input/output hashes, writes
  the best source to the worker files area, then runs one ordinary verified check of that
  source with the supplied notes. Timed-out or STOP-cancelled searches preserve raw
  output and a `verification_disposition`; they never publish a candidate or start
  verification. An unconfirmed process-tree stop latches worker cleanup and wakes the
  parent instead of allowing more experiments.
- `checks`: own check IDs/metrics/flags and durable snapshot availability, even after STOP.
  Every new check saves the exact input bytes under host-owned external
  `work/matchflow/sources/CLAIM/HASH.cpp` before compiling. Editing a working file cannot
  destroy an earlier source. Old checks without snapshots remain explicitly unavailable.
- `gate`: `decision`, `reason`, `hypothesis`, `expected`, optional `integration_plan`.
- `escalate`: `reason`, `need`, bounded `evidence`; records a durable parent request while
  the claim stays active. The monitor wakes with `worker_help_requested`; the parent answers
  with `batch.py answer` and the worker reads it in `status help`. It never extends,
  replaces or closes the claim.
- `register_wall`: evidence for a register/slot-only wall; does not reset clocks.
- `promote`: freezes only an unchanged final official MATCH to host-owned `match.cpp`.
- `close`: `operation=submit|giveup|flag-proof`. Giveup uses `source` OR `check_id` plus `notes` containing
  reason/category/tried/still_differs/suspected_cause, or explicit `no_source_reason`.
  Notes MUST be a JSON object, not an encoded string. A voluntary give-up with a near miss
  also needs `notes.near_miss_review` with `best_check_id`, `tested`, `untried` and
  `stop_reason`; forced closure and host recovery record it as not required instead of
  blocking cleanup. `check_id` selects only an own,
  valid checked snapshot, verifies its hash and works after STOP with no edits or compile.
  Flag-proof needs `notes.note`. Submit uses only the host's promoted artifact.
- `report`: generates a canonical v4 evidence report after closure and acknowledged
  closing notes; historical v3 reports stay readable under their original schema.
  Unclaimed blocked assignments do not require closing notes.

The worker does not get a shell to run private scripts. The only permutation capability is
the bounded host `permute` operation, not an arbitrary loop. Ask the parent for a reviewed
additional capability rather than broadening permissions. Scalar timing and metrics derive
from host/checker records, not worker estimates. Final baseline MATCH still needs a
continue gate and final check before promotion. No local result proves server verification
or full-unit compatibility.

## Closure, failure and audit

### Bounded read-only local inspection

These commands replace ad-hoc SQL and lifecycle dump scripts. They use SQLite `mode=ro`
and `query_only`, never create/migrate databases, and expose only named summary columns,
not source, raw checker output or arbitrary SQL. Output is private JSON on stdout.

```text
python tools/matchflow/operator_cli.py inspect claims --address ACTUAL_ADDRESS --limit 20
python tools/matchflow/operator_cli.py inspect checks --claim ACTUAL_CLAIM --limit 20 --offset 0
python tools/matchflow/operator_cli.py inspect verdicts --address ACTUAL_ADDRESS
python tools/matchflow/operator_cli.py inspect lifecycle --batch BATCH --worker worker01
```

Ledger inspection uses the configured workspace's `work/matchflow/ledger.sqlite3`.
Lifecycle inspection requires a batch and optionally an exact worker. Every inspection
supports `--limit` (1..100, default 20) and `--offset` (default 0); filters are optional
except `--batch` for lifecycle. Missing databases fail instead of creating empty ones.
Local state is not proof of closure or server verification; use the identity-safe audit
for those conclusions. `batch.py debrief` now also returns `handoff_note` from the validated
acknowledged closing-notes receipt and `remaining` from the report; absent notes stay null.
It never reconstructs a give-up reason from missing data or rewrites historical reports.

### Scoped observability and identity-safe audits

`schema` returns the published input schema and per-operation requirements. The plugin
publishes enums and fields from `integrations/opencode/worker-schema.json`. Check stages
are `baseline|attempt|final`; permute stages are `attempt|final` (default `attempt`).

Every new ordinary check captures the official `--full-diff` in private host evidence.
`diff(check_id,offset,limit)` paginates that hash-checked output for an own check only,
including after STOP, without compiling or restoring a candidate. Older checks explicitly
report unavailable rather than fabricate a full diff. `symbols(text,limit)` is a bounded
literal lookup of official symbol names/addresses (2..160 chars, at most 100 results),
not game bytes, sibling inspection or a worker-supplied file path.

Parent answers are attached to every successful worker tool response as
`unread_parent_help`. Workers acknowledge the specific answer IDs with `help_ack`.
Voluntary closure is blocked while an answer is unread, including an atomic recheck at
closure reservation. STOP/deadline/supervisor loss closure never waits for acknowledgement.

The monitor records durable, deduplicated `claim_closed`, `notes_complete` and
`report_complete` milestones. Only `all_complete` means every report and acknowledged
closing note exists. Streaming coalesces routine completion notifications and prioritizes
urgent STOP/cleanup/heartbeat events while supervising dynamic tasks. Re-arm after
intermediate events while discovery, queued work or workers remain. It automatically saves a compact
private `debrief.json`; its verification is unknown until supplied fresh official status.

Use the identity-asserting audit helper for a finished-worker notification:

```text
python tools/matchflow/batch.py audit-worker BATCH --status PRIVATE_STATUS.json --worker worker01 --address ACTUAL_ADDRESS --claim-id ACTUAL_CLAIM --session-id ACTUAL_SESSION
python tools/matchflow/batch.py debrief BATCH --status PRIVATE_STATUS.json
```

Whole-batch `audit --expect PRIVATE_IDENTITIES.json` also accepts a reviewed
`{"batch":"BATCH","workers":[{"worker":"worker01","address":"ACTUAL_ADDRESS","claim_id":"ACTUAL_CLAIM","session_id":"ACTUAL_SESSION"}]}`.
Wrong batch/address/claim/session assertions fail before audit conclusions are returned.
Permuter run records now include effective/requested timeouts, output presence, progress
availability, private raw-evidence references and next-action diagnostics. Raw stdout/stderr
stays in private evidence, not ledger summaries or reports. Failed searches return structured
diagnostics to the scoped worker. A timeout never
implies that every requested trial ran and never publishes an unverified candidate.

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
After a monitor notification, re-arm `batch.py monitor BATCH` if other workers remain.
Its durable cursor suppresses already-notified requests but still wakes on escalation.
A `worker_help_requested` notification is not a closure request: the worker keeps its
claim and deadlines. List or answer it while the session runs:

```text
python tools/matchflow/batch.py help BATCH
python tools/matchflow/batch.py answer BATCH --worker worker01 --text "Bounded answer"
```

Transport exceptions, invalid responses and ambiguous mutations preserve claim/closure
reservations. Never relaunch to repair them. HTTP 401, 403 edge rejection/access denial,
429 and 503 are distinguished without printing keys or response bodies. 429/503 writes
STOP/cooldown evidence; failures without acknowledged closure stop new starts and wake
the parent. Completion of a ready worker requests only its own closure; healthy siblings
keep their original experiment budgets. The durable start barrier prevents new launches.
No browser impersonation, global retry loop or official checker patching.
Host setup or prompt-admission failure persists a batch-wide cleanup barrier before
returning. The barrier is latched in lifecycle storage; deleting STOP does not clear it.
Reconcile that batch and prepare a fresh reviewed batch, never automatically resume it.

If a worker crashes or setup fails after a claim, the parent uses existing host-only
`service.py giveup --source ... --notes ...` (or explicit no-source reason), submit or
flag-proof as appropriate, then reconciles. A lost claim response needs the exact claim
ID confirmed by official status. Missing receipt is unresolved, not invented success.
Closing notes remain required. No automatic bulk cleanup, unrelated release or claim retry.

To reduce recovery round-trips, inventory the whole batch in one read-only remote call:

```text
python tools/matchflow/batch.py recover BATCH
```

This lists exact assignments, leases, receipts, notes and checked-source IDs without
mutating the service. To close specific finished sessions, prepare a private reviewed
actions JSON and opt in explicitly:

```json
{"batch":"BATCH","workers":[{"worker":"worker01","address":"ACTUAL_ADDRESS","claim_id":"ACTUAL_CLAIM_ID","session_id":"ACTUAL_SESSION_ID","session_finished":true,"operation":"giveup","check_id":123,"notes":{"reason":"Actual stopping reason","category":"instruction_order","tried":"Actual experiments and measured results","still_differs":"Actual remaining offsets/regions","suspected_cause":"Evidence-backed next hypothesis"},"closing_note":"Substantive final handoff for this function"}]}
```

```text
python tools/matchflow/batch.py recover BATCH --actions PRIVATE_REVIEWED_ACTIONS.json --apply
```

Only explicitly listed, exact bound identities with host completion evidence may be
touched. Use `source` (relative to that worker's files) plus its reviewed `source_sha256`
instead of `check_id` for a legacy candidate, or an explicit `no_source_reason`. `operation: "finish"`
only completes notes/report/audit after acknowledged closure. Existing acknowledged
notes are validated rather than sent again; `closing_note` is required when missing.
Pending/unacknowledged mutations stop recovery without replay. Wrong identities,
running sessions, pending notes and unrelated claims are never auto-cleaned. The
command performs fresh ownership checks, close, notes, report and independent audit,
stopping on the first unresolved result. Reports already present are not overwritten.

Flag-proof receipts may return `claim_released` and `flagged` without a `flag_id`.
Validation requires the exact bound claim and flagged address; legacy `released` plus
`flag_id` receipts remain supported. Recovery reuses the saved receipt and source
checkpoint, records `flag_review` locally, and never sends the proof again. It preserves
the receipt's original closure time, including the 30-minute closing-notes window.

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
timestamps, checkpoint acknowledgement and receipt hash. New assignments generate report
v4 with `review` facts, `permute_runs` and `near_miss` instead of a lane; v3 validation is
retained read-only for historical reports. Historical reports/receipts are never relabelled
or accepted without the required proof. New ordinary reports also include
`retained_check`: the check whose hash describes `closure-source.cpp`, separate from
`latest_valid`. It is null for an unchecked handoff.
Audits verify this link and any new source snapshot; old reports remain unchanged.

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
