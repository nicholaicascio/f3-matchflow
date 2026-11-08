export const meta = {
  name: 'match-batch',
  description: 'Run prepared fixed-address matching assignments; explicit model per worker, no replacements.',
  phases: ['Match'],
};
const tasks = args.tasks;
if (args.schema_version !== 2 || !args.lifecycle_dispatch_required || !args.batch) throw new Error('Prepare v2 assignments and run batch.py dispatch --method workflow before approval; worker lifecycle guards fail closed without it');
if (!Array.isArray(tasks) || !tasks.length || tasks.length > 200) throw new Error('Supply prepared workflow-args.json; 1-200 assignments required');
if (new Set(tasks.map(t => t.address)).size !== tasks.length) throw new Error('Duplicate assigned address');
for (const t of tasks) {
  if (!/^0x[0-9a-f]{8}$/.test(t.address) || !t.prompt || !t.model) throw new Error('Every task needs explicit address, prepared prompt and model');
}
const results = await parallel(tasks.map(t => () => agent(t.prompt, {label:t.id, phase:'Match', model:t.model})));
return {results, cleanup:'Orchestrator: reconcile prepared batch ownership against fresh official status, preserve best source and explicitly close only these workers unfinished claims. Stop/Resume is not lease cleanup. Never replay completed or closed assignments; prepare a new batch after reviewing current state.'};
