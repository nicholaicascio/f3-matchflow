# Orchestrator runbook

Start from [setup](setup.md), in the public checkout. Read the
[matching policy](matching-policy.md) and [command reference](../tools/matchflow/README.md).
This runbook does not authorize matching: obtain the human's request first. Do not start
paid agents or take service claims to test the installation.

## Roles and boundaries

- The **human** approves the account, supplies their own game and chooses whether to
  pay for matching work.
- The **orchestrator** runs here, screens without claiming, reviews exact addresses,
  prepares assignments and supervises closure. It has trusted host authority and must
  not use it to bypass worker restrictions.
- Each **match-worker** child is bound to one immutable assignment and real session
  identity before its first prompt. It uses only `matchflow_worker` operations. Its
  editable area is `WORKSPACE/work/batches/BATCH/workerNN/files/`, even though the host
  plugin is loaded from this checkout. No shell or arbitrary temp scripts.

## 1. Screen, review and prepare

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

## 2. Validate, dispatch and launch

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

## 3. Supervise and close

Read monitor notifications. Answer capability/help requests through `batch.py help`
and `batch.py answer`; do not edit tooling to grant a worker shell access. Re-arm the
background monitor after a notification while workers remain. `claim_closed` is not
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
