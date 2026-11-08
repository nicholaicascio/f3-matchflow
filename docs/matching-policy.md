# Local matching policy

This repository owns policy and tooling. The official https://f3decomp.com/agents.md
is authoritative on service rules, claim leases, readable C++, work notes and privacy.
The external workspace stores official dependencies and private runtime artifacts only.

## Selection and dispatch (orchestrator only)

- Matching must be requested by the human. Setup/tests do not authorize claims or paid runs.
- Call start_here first, then status, list_bands, list_functions and function_info.
  Screen without claims. Review availability, ownership, reports, substantive history,
  known verdicts and exclusions. Never fabricate service metadata.
- One exact address per assignment, no bands, co-work, replacements, reclaims or refills.
  Exclude user-blocked 0x00ad1250. Production requires reviewed clean history; recurring
  blockers need research with a concrete new hypothesis; prior unfit requires integration.
- Default worker model remains opencode-go/deepseek-v4.1-flash#max; honor explicit human
  overrides. Record actual model, variant, harness and usage, never guessed billing.
- Prepare fresh v3 selections and fresh successful online preflight. Dispatch subagents,
  then use matchflow_launch for each exact prepared worker. Its trusted host claims just
  before work, validates ownership, obtains the exact pack and registers evidence.
- Only dedicated match-worker sessions with deny-by-default scoped tools may match.
  General subagents, legacy headless and Workflows are disabled for matching.

## Worker method

- The real child session is bound to one immutable assignment. Workers never choose an
  address, claim ID, model, database or host path. No shell, raw HTTP/MCP mutations,
  credential reads, session management, tooling/config edits or sibling access.
- Read the exact packet/history and pack neighbourhood. Review the supplied starting
  draft and recheck it before using another lead. Twins supply hypotheses, not bytes.
- Inspect original instructions through the read-only target tool, bounded to your
  assigned function. Output stays local; never paste disassembly into service payloads.
- Save required draft_review and standard/full plans before checking. Plans describe
  FRAME, every SLOT/CALL, and address-ordered REGIONs at the official required granularity.
- Official checker only, exact claim pack, full work notes on every attempt. Record a
  baseline, then a concrete hypothesis gate. One experiment at a time; first difference
  first. Never falsify checker output or promote unproven imported results.
- Preserve pack inputs. Diagnostic flags may prove a defect only with an exact MATCH.
  Never submit an unaccepted-flags MATCH as an ordinary contribution.

## Time and closure

- Measured stopping limits: 20 minutes at a register/slot-only wall, 30 minutes without
  objective same-flags instruction-count improvement, 40 minutes total from dispatch.
  Allow at most five additional minutes to preserve evidence and close. Lease margins
  can shorten both windows; queues never extend them. Do not infer timing from prose.
- STOP, stale/missing supervisor or closure marker means freeze and close, no new probes.
  A host monitor must run in the background so the parent wakes for closure requests.
- Final official MATCH plus unchanged source/pack/flags permits promotion and submission.
  Alternate-flags proof uses source-bearing checkpoint plus flag-proof. Otherwise give
  up with exact best C++ and category/tried/still_differs/suspected_cause/reason.
- Do not retry ambiguous mutations. Save durable responses, reconcile read-only, and
  escalate cleanup_required. HTTP 429/503 stops new starts, preserves Retry-After, no loops.
- After closure save required closing notes and generate the canonical report. Parent
  reconciles fresh official status and actual receipts, including after crashes/quota
  failures. Never release unrelated claims. Missing leases alone are not acknowledgements.
- Local MATCH, submitted, server verified and integrated are different states. Only
  official verdicts establish verified credit. Failed source retention remains a failure.

## Boundaries

All reusable prompts, wrappers and utilities are versioned here. Generated prompt
snapshots, candidates, packs, checker output, databases, private twin caches and logs
stay outside Git. Keys stay in private external files, never prompts/argv/logs. The
official checker/compiler are installed dependencies, not vendored or modified here.
Never upload game files, machine code or disassembly. Do not modify a workspace used
by running workers. Scoped tools are a capability boundary, not an OS sandbox.
