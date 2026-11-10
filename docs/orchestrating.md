# Orchestrator runbook

Start from [setup](setup.md), in the public checkout. Read the
[matching policy](matching-policy.md) and [command reference](../tools/matchflow/README.md).
This runbook does not authorize matching: obtain the human's request first. Do not start
paid agents or take service claims to test the installation.

## Roles and boundaries

- The **human** approves the account, supplies their own game and chooses whether to
  pay for matching work.
- The **orchestrator** runs here, screens without claiming, reviews exact addresses,
  opens authorized streams or prepares assignments and supervises closure. It has trusted host authority and must
  not use it to bypass worker restrictions.
- The streaming **match-discovery** child is bound by the host before its first prompt.
  It reviews exact targets through `matchflow_discovery` only; it has no shell, raw
  service access, credentials, source inspection or alternate launch path.
- Each **match-worker** child is bound to one immutable assignment and real session
  identity before its first prompt. It uses only `matchflow_worker` operations. Its
  editable area is `WORKSPACE/work/batches/BATCH/workerNN/files/`, even though the host
  plugin is loaded from this checkout. No shell or arbitrary temp scripts.

## Streaming authorized runs

Use this path only for a human-requested matching run. The reviewed prepared-batch path
below remains available. Before opening, with no active workers, pass fresh online
preflight and resolve the external workspace. Do not use a live run as a setup test.

Call the orchestrator tool **`matchflow_stream`**, not a shell command:

```json
{"op":"open","batch":"YOUR_FRESH_BATCH_NAME","requested":20,"backlog":2}
```

`requested` is 1..50; backlog is 1..4, default 2. Optional `model` overrides local
`worker_model`, otherwise the built-in default applies when that setting is absent.
Open freezes the batch header and configuration fingerprint, validates the exact
tool-capable model/variant, starts the watchdog and starts a bound `match-discovery`
producer. No silent model fallback is permitted.

**Immediately after open**, run this as a background host command with completion
notification, keeping the parent available for its events:

```text
python tools/matchflow/batch.py monitor BATCH
```

The producer uses `matchflow_discovery` catalog (initial start_here/status/list_bands,
at most four catalog pages), then inspect on a shortlist of at most 4*N targets for
requested N. Inspect preserves actual receipts and local history. Review availability,
ownership, substantive history, verdicts and report scope under policy, including a
next experiment and integration plan where required. No claim is taken just to screen.

Producer enqueue sends the exact address, reason and review. The host validates facts
and fresh metadata, reserves the task, claims that exact address, saves acknowledgement
and queues it. Pre-claim unavailability may be skipped without counting toward N.
Producer wait is a bounded local backlog wait; finish explicitly records exhaustion;
status is read-only. Exhaustion may end the run short rather than expanding bounds.

As queued claims become available, call:

```json
{"op":"next","batch":"YOUR_FRESH_BATCH_NAME"}
```

Each next consumes one FIFO acknowledged claim and launches a bound `match-worker`
using that claim, without a second claim. Do not use `matchflow_launch` to consume a
stream task. `matchflow_stream` status is read-only:

```json
{"op":"status","batch":"YOUR_FRESH_BATCH_NAME"}
```

At quota, `producer_done` means N acknowledged claims. It does not mean the consumer
has assigned N workers, nor that workers closed or reports/notes are complete. Retain
the immutable `batch.json` header and append-only `stream_tasks`; readers use
`lifecycle.manifest` for the combined view, not a cached header-only worker list.

The monitor supervises the dynamic queue and workers, coalesces routine completions,
and prioritizes STOP, cleanup, heartbeat loss and other urgent events. Re-arm it after
notifications while discovery, queued work or workers remain. Startup failures require
STOP and explicit reconciliation, not a replacement task. Never retry ambiguous
mutations or automatically resume/refill. Follow the closure/recovery sections below.

The frozen configuration fingerprint is checked during the run. Do not rerun preflight
while active or change shared tooling, configuration or inputs. Isolated new assignments
and receipts may be written while workers work, along with their scoped evidence writes.

## Prepared-batch path

### 1. Screen, review and prepare

The official read-only sequence starts with `start_here`, `status`, `list_bands`,
`list_functions`, then `function_info` for the final shortlist. The screen CLI performs
that bounded sequence and saves actual responses. It never takes a screening claim:

```text
python tools/matchflow/screen.py --output screening-01 --count 8 --max-pages 2 --max-size 1023
```

Review availability, history, known verdicts and actual report scope. An incomplete
shortlist is not permission to expand the search indefinitely. Use actual evidence,
not guessed metadata, saved-draft counts or near-miss counts as a readiness score.

```text
python tools/matchflow/operator_cli.py review-template --screen PRIVATE_SCREEN_FOLDER --output annotations-01.json
```

Edit the generated private annotations JSON: remove unselected targets, fill in each
reason and explicit review facts, including concrete next experiments where required.
Prior unfit requires an integration plan. The template deliberately starts unreviewed.
Then assemble and validate the selection without re-dating the saved service evidence:

```text
python tools/matchflow/operator_cli.py selection --screen PRIVATE_SCREEN_FOLDER --annotations PRIVATE_ANNOTATIONS_JSON --output selection-01.json
python tools/matchflow/batch.py prepare BATCH --selection PRIVATE_SELECTION_JSON
```

Uppercase arguments are placeholders, not literal paths. Relative helper outputs go
under `WORKSPACE/work/matchflow/`; **inputs need their explicit paths**. Choose fresh
batch/output names, never overwrite historical evidence. `prepare` uses your persistent
`worker_model` from `matchflow.local.json`; `--model "provider/model#variant"` overrides it
for this batch. With neither set, the legacy OpenCode Go/DeepSeek default remains. Choose
a provider you have configured; see [model setup](setup.md#choosing-a-worker-model).

### 2. Validate, dispatch and launch

Before launching, with no workers active, obtain fresh successful online preflight and
an actual status snapshot. Setup does not automatically run these again:

```text
python tools/matchflow/preflight.py --online
python tools/matchflow/operator_cli.py status --output status-before-dispatch.json
python tools/matchflow/batch.py dispatch BATCH --method subagents --status PRIVATE_STATUS_JSON
```

Run `python tools/matchflow/batch.py monitor BATCH` as a **background host command with
completion notification** before launching. Do not run it synchronously and wait before
launching. Keep the parent session available to handle its notifications. A host without
reliable background supervision is not ready for unattended matching.

For each exact prepared worker call the orchestrator tool, for example:

```json
{"batch":"YOUR_BATCH_NAME","worker":"worker01"}
```

That is input to **`matchflow_launch`**, not a shell command or the general subagent
tool. It first validates the exact prepared model/variant against OpenCode's active catalog
for this checkout. An unavailable or non-chat/tool-capable model fails before child creation
or claiming, with configuration guidance and no fallback. It performs the real claim just
before work, binds identity, obtains the exact
pack and starts the restricted child. Prepare/dispatch do not claim. No replacements,
refills, co-work or automatic reclaims. Never dispatch matching through general agents,
headless CLI, Workflows or manually constructed child sessions.

## Supervise and close (both paths)

Read monitor notifications. Answer capability/help requests through `batch.py help`
and `batch.py answer`; do not edit tooling to grant a worker shell access. Re-arm the
background monitor after a notification while workers or streaming discovery/queued work
remain. Routine streaming completions may be coalesced; urgent events take priority. `claim_closed` is not
the same as `notes_complete` or `report_complete`; only `all_complete` means every
required report and acknowledged closing note is present.

STOP, supervisor loss or deadlines freeze experiments, including final compilations.
Workers preserve an already-checked source by check ID, close their own claim, save
closing notes and generate the report. Local MATCH alone never proves server credit.

After closure, get fresh status and audit exact identities using `audit-worker` or a
reviewed whole-batch expectation file (see command reference). Summarize with:

```text
python tools/matchflow/operator_cli.py status --output status-after-closure.json
python tools/matchflow/batch.py audit BATCH --status PRIVATE_FRESH_STATUS_JSON
python tools/matchflow/batch.py debrief BATCH --status PRIVATE_FRESH_STATUS_JSON
```

These results and all raw evidence remain private. A missing open lease does not prove
successful source retention or submission; inspect actual receipts and verdicts.

## Crashes and recovery

Do not relaunch or replay ambiguous mutations. `batch.py recover BATCH` inventories
read-only remote state. Applying recovery requires an explicit reviewed action list
with exact worker/address/claim/session, source/check choice, notes and host completion
evidence. Follow the command reference. Never close unrelated claims, automatically
choose handoff sources or delete state markers to reopen experiments.
