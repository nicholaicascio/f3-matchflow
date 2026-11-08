# Matchflow command reference

Matching policy lives in the configured external workspace's `AGENTS.md`;
repository development rules live in [`../../AGENTS.md`](../../AGENTS.md).
Read this file for CLI usage. Run with Windows `python` from that workspace;
`check` invokes its WSL/Wine checker. Examples use a `tools/matchflow` junction;
without it, substitute the absolute path to this checkout's `tools/matchflow`.
Use `--help` on any subcommand. Optional `--db PATH` goes before the subcommand.

## Prepare and launch a batch

Policy and method choice: workspace `AGENTS.md`. The orchestrator supplies selection JSON:

```json
{"checked_at":"CURRENT_UTC_ISO_TIMESTAMP","targets":[{"address":"0x00abcdef","size":160,"state":"open","co_workers":0,"lane":"research","give_ups":4,"open_problem_reports":0,"file_problem_reports":0,"history_reviewed":true,"history_sources":["function_info and saved prior report paths"],"prior_verdicts":[],"verdict_history":"unknown","new_hypothesis":"Named receiver removes the documented extra reload at the first mismatch","expected_observation":"Receiver reload disappears without changing the call sequence","reason":"Prior structural lead reviewed; not repeating declaration-order sweeps"}]}
```

Copy current service fields, including reports, give-ups and `prior_unfit` if known;
do not fabricate metadata. `verdict_history` is `reviewed` or `unknown`; unknown is
not clean production history. Research needs `new_hypothesis` and
`expected_observation`. Prepare merges known verdicts from the existing local ledger
read-only; prior `unfit` requires integration. Refresh availability within 15 minutes of launch.
Status JSON is `{"checked_at":"CURRENT_UTC_ISO_TIMESTAMP","status": ACTUAL_SERVICE_STATUS}`.

```text
python tools/matchflow/batch.py prepare BATCH --selection work/selection.json
python tools/matchflow/batch.py headless BATCH --status work/status.json --jobs 8
python tools/matchflow/batch.py monitor BATCH
python tools/matchflow/batch.py audit BATCH --status work/status-after.json
```

`prepare --model provider/model#variant` overrides the worker default. It refuses
duplicate/band/excluded/held/stale selections and existing batch directories. Each
worker gets `assignment.json` and `prompt.txt`; `batch.json` supplies subagent prompts
and explicit models, `workflow-args.json` supplies the saved `match-batch` workflow.
Headless runs once from root and logs private JSON events/stderr and session IDs.
For workflows/subagents, **before dispatching tools**, run:

```text
python tools/matchflow/batch.py dispatch BATCH --method workflow --status work/status.json
python tools/matchflow/batch.py dispatch BATCH --method subagents --status work/status.json
python tools/matchflow/lifecycle.py bind work/batches/BATCH worker01 ses_REAL_ID
```

Choose ONE method; do not execute both dispatch examples. Headless dispatches itself.
Run `headless` and the separate `monitor` command with the shell tool in background.
For the other methods, dispatch, start `monitor` in background, then use the workflow
or subagent tools with the prepared prompts. Bind returned session IDs; headless
binds them automatically. The monitor returns on completion, the first closure
request or supervisor loss, so the harness wakes the orchestrator without remote
progress polling. It is a local deadline monitor, not a service polling loop.

All methods use `lifecycle.sqlite3`: durable one-attempt claim reservations,
monotonic elapsed time and fixed UTC deadlines starting at dispatch (conservative
when a tool queues). An independent local watchdog maintains a heartbeat and
writes `CLOSE_REQUESTED.json` and `RECONCILE_REQUIRED.json`. It never kills agents or
releases claims. The parent must act on monitor completion and reconcile exact
sessions/claims. Lost supervisors fail closed after 30 seconds; no automatic restart
or replay resets the window. v1 batches cannot launch; preserve them as evidence.

Managed checks enforce the experiment deadline and stale-watchdog guard. Final
checks are allowed during the existing closure window only. Baseline/check metrics
update stagnation timing only for an objectively lower instruction count under
the same flags. Mark a register-only wall once with:

```text
python tools/matchflow/lifecycle.py register-wall work/batches/BATCH worker01 --evidence "CHECK_ID and remaining register/slot-only differences"
```

These are supported-path controls, not a security sandbox: direct MCP mutations or
compiler calls can bypass them. Managed workers must use the gateway/checker path;
the strict audit detects missing lifecycle/ledger provenance.
Creating `work/batches/BATCH/STOP` prevents queued headless starts; a streamed error
event also sets it. Already running workers still require normal service closure.

`audit` returns separate `lease_closure`, `evidence_integrity`, `source_handoff`, and
`verification` fields, plus actionable errors. It validates report schema, session
identity, timestamps, hashes, canonical ledger IDs, raw checker records and actual
acknowledgement contents. Non-clean audits exit 2; lease absence is not a receipt.
Legacy reports fail v2 integrity rather than being silently upgraded. Snapshots and
local receipts remain operator-supplied evidence, not cryptographically authenticated.
`batch.py`, `matchflow.py`, `lifecycle.py`, `evidence.py` and `usage.py` do not read
credentials or make remote mutations; only the explicit `service.py` gateway does.

## Claim and close through the service gateway

```text
python tools/matchflow/service.py claim work/batches/BATCH worker01
python tools/matchflow/service.py giveup work/batches/BATCH worker01 --source work/ADDRESS_CLAIM/best.cpp --notes work/giveup-notes.json
python tools/matchflow/service.py submit work/batches/BATCH worker01
python tools/matchflow/service.py flag-proof work/batches/BATCH worker01 --notes work/proof-notes.json
python tools/matchflow/service.py reconcile work/batches/BATCH worker01
```

`claim` rechecks the fixed target, reserves one attempt BEFORE mutation, saves the
response, confirms exact ownership through fresh official status and writes ownership.
Read its private `claim-response.json` for the pack/history. No automatic retries.
The key is read privately from `%LOCALAPPDATA%/f3decomp/api-key`; never place it in
arguments or the project. HTTP 429/503 writes STOP/cooldown evidence and prints
Retry-After without retrying or logging response bodies/source.

Give-up notes JSON needs `reason`, `category`, `tried`, `still_differs`,
`suspected_cause`. The gateway freezes exact source bytes, sends `best_code` ONLY
(no ambiguous checkpoint combination), saves the actual receipt and assesses source
retention independently of release. If nothing could compile, omit `--source` and
provide `--no-source-reason "..."`. Submit requires a promoted unchanged final MATCH
under pack/accept_also flags. For a promoted `proof_ready` candidate, `flag-proof`
uses the official source-bearing checkpoint/flag route with the exact checked flags;
proof notes JSON contains `{"note":"specific flag discrepancy supported by this MATCH"}`.
It requires an exact diagnostic final MATCH, never merely a closer score. Unknown
flag acknowledgement formats stay unresolved rather than implying release.
Save service-required closing notes separately after either action.

On transport/crash ambiguity, never retry the mutation. `reconcile` uses read-only
status to recover a saved claim response or acknowledged closure. A lost claim
response requires the exact `--claim-id` confirmed through official status. Original
deadlines remain. Lost closure receipts remain unresolved; absence alone does not
produce a fabricated success. If a receipt was recovered after a ledger failure,
use `record` below before generating the report. Source-handoff failures are retained
as failures even when release succeeded; never reopen a claim to hide one.

## Record an owned claim and its checks

```text
python tools/matchflow/matchflow.py register --claim ID --address 0xADDRESS --lane production --model "ACTUAL MODEL" --variant max --harness OpenCode --pack packs/ID --deadline "REAL_ISO_DEADLINE" --assignment work/batches/BATCH/worker01/assignment.json
python tools/matchflow/matchflow.py check --claim ID --stage baseline --source work/UNIQUE/baseline.cpp --change "recheck draft" --why "current-pack baseline" --expect "confirm prior result"
python tools/matchflow/matchflow.py gate --claim ID --decision continue --reason "first mismatch isolated" --hypothesis "named pointer removes reload" --expected "reload disappears"
python tools/matchflow/matchflow.py check --claim ID --stage attempt --source work/UNIQUE/candidate.cpp --change "..." --observed "..." --why "..." --expect "..."
python tools/matchflow/matchflow.py check --claim ID --stage final --source work/UNIQUE/candidate.cpp --change "freeze candidate" --observed "..." --why "verify submission artifact" --expect "MATCH"
python tools/matchflow/matchflow.py promote --claim ID --output work/UNIQUE/match.cpp
```

- `check` captures source/pack SHA-256, exact command, checker JSON/output and measured checker duration. Supply service-required notes, including `--observed` from check two and `--regions` when required.
- Exit 0 means evidence captured, including WIP. Inspect `valid` and `result.status`. Missing JSON, checker errors or changed inputs can produce exit 2.
- Alternate flags: `--flags /MT,/O2,/GS,/EHsc,/Gy`. `promote` requires unchanged final MATCH evidence; accepted flags yield `submit_ready`, unaccepted flags `proof_ready`. It will not overwrite a different artifact.
- Integration gates also need `--integration-plan` describing the actual unit failure and a test. `gate --decision stop` records a decision only.
- For unfit history discovered after claiming, the orchestrator runs `reclassify --claim ID --integration-plan "actual unit failure and proposed test"`; this changes the lane to integration and invalidates the old gate. Record a new integration gate before continuing.
- Checker failures distinguish process failure (including empty stdout/stderr, exit 15), missing output, ambiguous JSON, rejected source and checker mismatch. A failed process is not repaired by accepting looser JSON. Raw output/return code/duration remain private evidence. Summary/gate select the latest valid baseline.
- A direct official JSON check can be imported with the same `check` arguments plus `--import-evidence RAW.json`. Its exact invocation, source/pack hashes, unchanged marker and measured `seconds` must match; it is copied as a new ledger record without executing the compiler. Unproven provenance is refused. This is not a way to bless hand-authored MATCH text.

## Record service acknowledgements and verdicts

Call the service first; these commands never perform remote actions:

```text
python tools/matchflow/matchflow.py record --claim ID --kind submitted --identifier SUBMISSION_ID
python tools/matchflow/matchflow.py record --claim ID --kind flag_review --identifier FLAG_ACK_OR_ID --checkpoint CHECKPOINT_ID
python tools/matchflow/matchflow.py record --claim ID --kind given_up --identifier PATH_TO_SERVICE_ACK --checkpoint BEST_CHECKPOINT_ID --note "remaining blocker"
python tools/matchflow/matchflow.py import-status work/matchflow/status.json
python tools/matchflow/matchflow.py summary
```

Save the official status response as JSON before importing. Only `recent[]` entries are imported; absent submissions remain unknown. A proof may later get a submission ID: correlate using its checkpoint, not just its address.

## Optional utilities and data

- `assess FILE`: JSON array of function metadata annotated with `history_reviewed`, `prior_verdicts`, `flags_suspected`, `repeated_blocker`. Missing review/availability blocks readiness; three or more give-ups routes to research. Readiness is not a match prediction.
- `import-reports work`: imports `dsmax*/report.json` as unvalidated historical evidence; cannot promote a candidate.
- `work/matchflow/ledger.sqlite3`: private SQLite/WAL ledger; `work/matchflow/checks/`: immutable evidence, potentially containing disassembly.
- Generate v2 reports, do not hand-author authoritative fields: `python tools/matchflow/evidence.py report work/batches/BATCH worker01`. It derives ownership/timing and baseline/final/latest checks from lifecycle/ledger records, preserving diagnostic flags separately. An absent final check stays null; the latest probe is not relabelled final. Session IDs may be bound after completion and reports regenerated. Blocked/unclaimed assignments can generate reports without fake packs or receipts.
- Raw usage collection: `python tools/matchflow/usage.py work/batches/BATCH --method headless`. Workflow uses `--method workflow --input WORKFLOW_STATUS.json`; subagents use `--method subagents --input SESSIONS.json`, with `{"sessions":[{"session_id":"ses_REAL","raw":{"input":1,"output":1,"reasoning":1,"cacheRead":0}}]}` from exported session metadata. Missing counters remain missing. `--input-semantics unknown` is the default and omits service usage; use `uncached` or `total` only with confirmed provider semantics. Billing remains null, even if harness cost is zero. Raw headless counters are also captured per worker.
- Offline tests: `python -m unittest discover -s tools/matchflow -v`. They mock the compiler and do not establish integration.
