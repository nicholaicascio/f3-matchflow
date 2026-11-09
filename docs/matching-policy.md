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
  Record reviewed facts, not a mutually exclusive lane: history and verdict confidence,
  report scope (function, sibling-only, unit-wide or unknown), prior unfit and the next
  experiment. A sibling report does not exclude an otherwise good target; a report on the
  assigned function, a true unit-wide defect or a prior unfit does change the requirements.
  Prior unfit needs a concrete integration plan; recurring blockers need a next experiment.
- A saved draft is never a selection preference by itself. It is starting material after
  the function is chosen on reviewed history, report scope and a concrete lead. The same
  applies to a near-miss count: it is not a readiness score.
- Resolve worker model at preparation: explicit `--model`, then local `worker_model`,
  then built-in opencode-go/deepseek-v4.1-flash#max. Honor explicit human overrides.
  Before child creation/claim, require that exact enabled tool-capable chat model and
  optional variant in OpenCode's active catalog. Missing/catalog failure is a setup
  error, never permission to fall back. Record actual model, variant, harness and usage,
  never guessed billing. Local setting changes never change prepared assignments.
- Prepare fresh v3 assignments with review facts and fresh successful online preflight. Dispatch subagents,
  then use matchflow_launch for each exact prepared worker. Its trusted host claims just
  before work, validates ownership, obtains the exact pack and registers evidence.
- Only dedicated match-worker sessions with deny-by-default scoped tools may match.
  General subagents, legacy headless and Workflows are disabled for matching.
- Keep orchestration bounded: use the read-only screen CLI, inspect only the final
  shortlist, then review actual histories/reports. No game/disassembly scans or
  speculative source hypotheses by default. Selection leads are tentative, not facts.

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
- A near miss may use the bounded official permuter through the host. The host accepts
  only an owned candidate, allowlisted flags and bounded trials/jobs/timeout, preserves
  input and output hashes, and rechecks the best source once with the ordinary checker.
  It is a managed capability, not a shell or an unrestricted source sweep.
- If the next useful experiment needs an unavailable capability, use the host escalation
  request while the claim is active. The parent monitor is notified; the claim, pack and
  deadlines do not change. Continue useful work or read the answer in status help.
- Preserve pack inputs. Diagnostic flags may prove a defect only with an exact MATCH.
  Never submit an unaccepted-flags MATCH as an ordinary contribution.

## Time and closure

- Measured stopping limits: 20 minutes at a register/slot-only wall, 30 minutes without
  objective same-flags instruction-count improvement, 40 minutes total from dispatch.
  Allow at most five additional minutes to preserve evidence and close. Lease margins
  can shorten both windows; queues never extend them. Do not infer timing from prose.
- STOP, stale/missing supervisor or closure marker means freeze and close, no new probes.
  This applies equally to `final` checks: no new compilation may start after stopping.
  A host monitor must run in the background so the parent wakes for closure requests.
- A ready worker ending without complete closure requests cleanup for that worker and
  blocks new launches, not healthy siblings' experiments. Setup/prompt-admission failures,
  service cooldowns and an explicit batch STOP remain batch-wide. Barriers are durable;
  removing markers does not reopen experiments. Re-arm the monitor after a notification
  while other workers remain; repeated requests are deduplicated until escalation.
- Final official MATCH plus unchanged source/pack/flags permits promotion and submission.
  Alternate-flags proof uses source-bearing checkpoint plus flag-proof. Otherwise give
  up with exact best C++ and category/tried/still_differs/suspected_cause/reason.
- A voluntary give-up with a near miss (best valid check at most 40 instructions
  different) also needs a near-miss review: the best check ID, what was tested, what
  concrete experiment remains untried (or "none"), and why stopping now is justified.
  If experiment time and a concrete lead remain, test the lead first. Deadlines, STOP,
  supervisor loss and host recovery are forced closures: they record the review as not
  required and are never blocked by missing model prose.
- Do not retry ambiguous mutations. Save durable responses, reconcile read-only, and
  escalate cleanup_required. HTTP 429/503 stops new starts, preserves Retry-After, no loops.
- After closure save required closing notes and generate the canonical report. Parent
  checks the acknowledged closing-notes receipt independently of give-up/submit prose and
  reconciles fresh official status and actual receipts, including after crashes/quota
  failures. Never release unrelated claims. Missing leases alone are not acknowledgements.
- Every new check preserves its exact source by hash. After STOP use an own check ID
  for give-up, without editing or recompiling. Reports link the handed-off source to
  its check separately from the latest check. Historical evidence is not rewritten.
- Recovery inventory is read-only remotely. Applying recovery needs an explicit reviewed
  per-worker action list, exact claim/session/address and host completion evidence.
  Never replay pending mutations, automatically choose sources/notes or clean up unrelated claims.
- Local MATCH, submitted, server verified and integrated are different states. Only
  official verdicts establish verified credit. Failed source retention remains a failure.

## Boundaries

All reusable prompts, wrappers and utilities are versioned here. Generated prompt
snapshots, candidates, packs, checker output, databases, private twin caches and logs
stay outside Git. Keys stay in private external files, never prompts/argv/logs. The
official checker/compiler are installed dependencies, not vendored or modified here.
Never upload game files, machine code or disassembly. Do not modify a workspace used
by running workers. Scoped tools are a capability boundary, not an OS sandbox.
