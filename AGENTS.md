# f3-matchflow

This repository holds orchestration code and canonical local policy. The configured
private workspace is separate and retains private runtime artifacts, not another
pipeline or an authoritative policy copy. Official dependencies and the game stay external.

For a new installation read `docs/setup.md`; for matching follow
`docs/orchestrating.md` and the canonical `docs/matching-policy.md`. Start the parent
from this checkout. Worker editable files live in the configured external workspace,
not in this repository or a new worktree. `CONTRIBUTING.md` covers offline-only changes.

## Matching orchestration from this checkout

This checkout is also a valid orchestrator location when the user requests matching
work. "Code only" describes the Git boundary, not a prohibition on orchestration.
Before selecting targets, resolve the external workspace from `matchflow.local.json`
or `F3_MATCHFLOW_WORKSPACE`. Read `docs/matching-policy.md`, the canonical local
matching policy. The official service guide remains authoritative on contribution
rules. Workspace AGENTS.md is legacy environment context, not the policy source.

The local OpenCode bridge loads the repository policy and official service guide,
and connects the `f3decomp` MCP server using an external key-file reference. On a
fresh installation run `python tools/matchflow/bootstrap.py` after configuring the
workspace. If service tools are absent, report the missing bridge/connection, not
that selecting functions is undefined. Do not fabricate targets. Streaming discovery
may be delegated only to the host-bound restricted `match-discovery` producer.

Selection starts with the service's `start_here`, `status`, `list_bands`,
`list_functions` and `function_info` tools, without claiming for screening. Review
availability, history and reports under the workspace policy, select exact addresses,
then prepare reviewed selection/status JSON and use the pipeline command reference.
Code/configuration tests never authorize claims or paid worker launches. Start those
only when the user requests them. Keep all runtime artifacts in the external workspace.

For an authorized streaming run use `matchflow_stream` open with a fresh batch name,
`requested` 1..50, `backlog` 1..4 (default 2) and optional `model`. Open freezes the
header/config fingerprint, validates the resolved model, starts the watchdog and binds
the discovery producer. Immediately start `python tools/matchflow/batch.py monitor BATCH`
in the background. Do not rerun preflight during an active run. Discovery is bounded to
four catalog pages and at most 4*N shortlist inspections, with actual receipts/local
history. Only host-validated reviewed enqueues reserve tasks and claim exact addresses.
`matchflow_stream` next consumes one FIFO acknowledged claim and launches a bound worker
without another claim; status is read-only. Producer_done means N acknowledged claims,
not N consumer assignments or closures. Explicit exhaustion may end discovery short.
Pre-claim unavailability may be skipped without counting; startup failure requires STOP
and explicit reconciliation, never replacement or ambiguous mutation retries. Keep the
immutable `batch.json` header and append-only `stream_tasks`; use `lifecycle.manifest`
for the combined view. The reviewed prepare/dispatch path remains available.

Use `operator_cli.py` for status snapshots, review templates/evidence-backed selections,
and bounded read-only ledger/lifecycle inspection; use `batch.py audit-worker` and
`debrief` for closure summaries. Do not reimplement these steps as temp-folder scripts.
New reusable capabilities belong in this repository with offline tests. Review facts
remain human/agent annotations; never re-date copied service facts to make them fresh.

Matching launches use `matchflow_stream` next or prepared `matchflow_launch`, and the
dedicated `match-worker` agent only.
The host binds each child session before its first prompt. Do not dispatch matching
through general subagents, headless CLI or Workflows; those legacy paths are disabled.
Workers have no shell or raw service access. All edits/checks go through session-scoped
tools. Never broaden permissions or edit tooling to work around a worker refusal.

Worker model resolution is stream open `model` or `prepare --model`, then local
`worker_model`, then built-in default.
Do not assume another user's provider access. Launch validates the exact prepared model
and variant before child creation/claim; fix configuration or prepare a new reviewed
batch if unavailable, never silently substitute a model or mutate an assignment.

## Pipeline development

- Read `README.md` and `tools/matchflow/README.md` before pipeline changes.
- Never commit packs, game files, checker distributions, disassembly, credentials,
  databases, claim sources, logs, or session exports, even to this private repository.
- Keep machine-specific configuration in ignored `matchflow.local.json` or the
  `F3_MATCHFLOW_WORKSPACE` environment variable. Workspace paths must be external.
- Run offline tests before pushing. No paid workers, real claims or service
  mutations as tests unless the user explicitly requests them.
- While workers run, allow only isolated new assignment/receipt writes and normal scoped
  worker evidence writes. Do not change shared tooling, configuration or inputs. Streaming
  checks the frozen configuration fingerprint without rerunning active preflight. Preserve
  historical artifacts and user changes. No automatic replay or claim retries.
- New repositories use `master` as the default branch.
