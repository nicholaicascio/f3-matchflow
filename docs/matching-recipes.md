# Matching references, loaded on demand

Sources: [f3twins](https://github.com/mathisto/f3twins), its PLAYBOOK.md and
MATCHING_TIPS.md; [orchestration notes](https://gist.github.com/mathisto/af6c792f3023c42fc4658727535145e9);
[our earlier run summary](https://gist.github.com/nicholaicascio/04e4ba6c2607aa32fdfe9a3e94bb59d8).
These are empirical leads, not service policy or proven model rankings.

## Find structure before codegen

Review/recheck the packet's starting material first. Look up the top three FNV twins,
confirm with strings, constants, RTTI and call order, then inspect neighbouring functions.
A float-only lead is weak; the optional Xbox file bridge is corroboration, not a reranker.
Keep getters, redundant null checks, field widths and source layers in a literal port.
Use FO3 pack types/offsets/enums and its actual flags, not FNV's layouts or VS2008 codegen.
If the twin is unrelated, use faithful pack siblings or reconstruct from the target.

## Read the first symptom

- Frame too large: check address escape and scopes of short-lived address-taken objects.
- Whole-function register recolouring: inspect widths, getter layers and extra temporaries
  before declaration-order sweeps. A structural hypothesis is better than a raw near-miss score.
- Repeated reloads across calls: inspect faithful same-file helper bodies and alias facts.
  Never invent fake helper semantics simply to steer compilation.
- Word/byte mismatches: check bool/char/short signedness and calling convention/return width.
- Stack slots only: fix source/liveness/escape first, then scopes and temporary lifetimes.
- x87 ordering: use official compiler tips; FO3 Havok FPU differs from FNV SSE.
- Suspected flags: a plausible prologue is not proof. Only an exact diagnostic MATCH plus
  source-bearing checkpoint can support the compile-flags report route.

Use official compiler_tips/large_functions and pinned upstream MATCHING_TIPS for detail.
The scoped tool interface deliberately has no arbitrary script execution; unimplemented
experiments must be requested from the parent rather than bypassing containment.

## Operational lessons

Screen centrally. Fresh clean functions and exhausted research targets are not comparable
model samples. Separate production/research/integration. Measure unsuccessful attempts,
tokens per verified contribution, actual checker duration and parent-measured elapsed time.
Keep parent context compact using canonical results, but investigate contradictions rather
than treating a worker's RESULT line as authoritative. Respect provider cooldowns and do
not automatically restart/refill failed workers.

Do NOT adopt screening by claim/giveup, worker-selected bands, mutation retries, fabricated
metrics, old V1 permission examples, or changes to official toolchain files from upstream
field notes when they conflict with current service rules and this pipeline's policy.
