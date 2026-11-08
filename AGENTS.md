# f3-matchflow

This repository holds orchestration code only. The configured private workspace is
separate and retains its own matching policies, game installation and artifacts.

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
that selecting functions is undefined. Do not fabricate targets or delegate selection.

Selection starts with the service's `start_here`, `status`, `list_bands`,
`list_functions` and `function_info` tools, without claiming for screening. Review
availability, history and reports under the workspace policy, select exact addresses,
then prepare reviewed selection/status JSON and use the pipeline command reference.
Code/configuration tests never authorize claims or paid worker launches. Start those
only when the user requests them. Keep all runtime artifacts in the external workspace.

Matching launches use `matchflow_launch` and the dedicated `match-worker` agent only.
The host binds each child session before its first prompt. Do not dispatch matching
through general subagents, headless CLI or Workflows; those legacy paths are disabled.
Workers have no shell or raw service access. All edits/checks go through session-scoped
tools. Never broaden permissions or edit tooling to work around a worker refusal.

## Pipeline development

- Read `README.md` and `tools/matchflow/README.md` before pipeline changes.
- Never commit packs, game files, checker distributions, disassembly, credentials,
  databases, claim sources, logs, or session exports, even to this private repository.
- Keep machine-specific configuration in ignored `matchflow.local.json` or the
  `F3_MATCHFLOW_WORKSPACE` environment variable. Workspace paths must be external.
- Run offline tests before pushing. No paid workers, real claims or service
  mutations as tests unless the user explicitly requests them.
- Do not modify the external workspace while another worker is running. Preserve
  historical artifacts and user changes. No automatic replay or claim retries.
- New repositories use `master` as the default branch.
