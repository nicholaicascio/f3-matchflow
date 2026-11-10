import { fileURLToPath } from 'node:url';
import { invoke, launch, stream, workerContext, readableJson } from './core.mjs';
import workerSchema from './worker-schema.json' with { type: 'json' };

const script = fileURLToPath(new URL('../../tools/matchflow/broker.py', import.meta.url));
const object = { type: 'object', additionalProperties: true, properties: {
  notes: { type: 'object', additionalProperties: false, properties: {
    ...Object.fromEntries(['reason', 'category', 'tried', 'still_differs', 'suspected_cause', 'note']
      .map(key => [key, { type: 'string' }])),
    near_miss_review: { type: 'object', additionalProperties: false, properties: {
      best_check_id: { type: 'integer', minimum: 1 },
      tested: { type: 'string' },
      untried: { type: 'string' },
      stop_reason: { type: 'string' },
    }, required: ['best_check_id', 'tested', 'untried', 'stop_reason'] },
  } },
  check_id: { type: 'integer', minimum: 1 },
  no_source_reason: { type: 'string', minLength: 1 },
} };

// Plugin.define is an identity helper in the pinned V2 SDK. Export its structural
// contract directly so production has no third-party runtime dependency graph.
/** @type {import('@opencode/plugin').Plugin.Plugin} */
const plugin = {
  id: 'f3-matchflow',
  async setup(ctx) {
    const python = typeof ctx.options?.python === 'string' ? ctx.options.python : 'python';
    const host = (command, value, signal) => invoke(python, script, command, value, signal);
    // Defense in depth: no broad built-in/MCP tools even enter a worker request.
    for (const kind of ['context', 'generate', 'compaction']) {
      await ctx.session.hook(kind, event => workerContext(event, host));
    }
    await ctx.tool.transform(editor => {
      editor.add({
        name: 'matchflow_stream',
        description: 'Orchestrator only: open an authorized streaming run, start its restricted claim-discovery producer, consume next FIFO claim into a matching worker, or inspect status. Start background batch monitor immediately after open. No automatic refills or retries.',
        input: {type:'object',additionalProperties:false,properties:{
          op:{type:'string',enum:['open','next','status']},batch:{type:'string'},
          requested:{type:'integer',minimum:1,maximum:50},backlog:{type:'integer',minimum:1,maximum:4},model:{type:'string'},
        },required:['op','batch']},
        options:{codemode:false},
        execute:async(input,context)=>{
          await ctx.permission.assert({sessionID:context.sessionID,agent:context.agent,
            action:'matchflow_stream',resources:['*'],save:[]});
          return {content:JSON.stringify(await stream(ctx,host,input,context))};
        },
      });
      editor.add({
        name:'matchflow_discovery',
        description:'Bound discovery producer only. catalog (page until quota or pool exhaustion), inspect(address), enqueue(address,reason,review), wait (local backlog wait), finish(pool exhaustion reason), status. No page or inspection-count cap. Host claims reviewed exact targets and enqueues acknowledged claims. No shell, raw service, credentials, source inspection, child sessions or alternate run paths.',
        input:{type:'object',additionalProperties:false,properties:{
          op:{type:'string',enum:['catalog','inspect','enqueue','wait','finish','status']},
          data:{type:'object',additionalProperties:true},
        },required:['op']},
        options:{codemode:false},
        execute:async(input,context)=>{
          await ctx.permission.assert({sessionID:context.sessionID,agent:context.agent,
            action:'matchflow_discovery',resources:['*'],save:[]});
          return {content:readableJson(await host('discovery',{session:context.sessionID,op:input.op,data:input.data||{}},context.signal))};
        },
      });
      editor.add({
        name: 'matchflow_launch',
        description: 'Orchestrator only: launch ONE prepared fixed-address assignment as a restricted child subagent. Requires dispatch and fresh successful online preflight. Makes a real claim; use only when the human requests matching work.',
        input: { type: 'object', properties: { batch: { type: 'string' }, worker: { type: 'string' } },
          required: ['batch', 'worker'], additionalProperties: false },
        options: { codemode: false },
        execute: async (input, context) => {
          await ctx.permission.assert({ sessionID: context.sessionID, agent: context.agent,
            action: 'matchflow_launch', resources: ['*'], save: [] });
          return { content: JSON.stringify(await launch(ctx, host, input, context)) };
        },
      });
      editor.add({
        name: 'matchflow_worker',
        description: 'Session-scoped matching tools. op: status (also measured remaining time, near-miss advisory and parent help), target(offset,limit; own assigned function only, private local inspection), read(area=files|pack|packet,path,offset,limit), list/search(area,text), write(path,text), reference(name=policy|recipe|compiler_tips|large_functions|upstream_tips|fnv), twin, check(source,stage,change,why,expect,observed,regions,flags), permute(source,stage,trials,jobs,timeout_seconds,flags,change,why,expect,observed,regions; bounded official permuter then one verified check), checks (own IDs, metrics and snapshot availability), gate(decision,reason,hypothesis,expected,integration_plan), register_wall(evidence), escalate(reason,need,evidence; asks the parent while the claim stays active), promote, checkpoint(phase,note), close(operation,source OR check_id,notes OBJECT with reason/category/tried/still_differs/suspected_cause and near_miss_review {best_check_id,tested,untried,stop_reason} for a voluntary near-miss give-up,no_source_reason), report. check_id selects an own checked giveup snapshot and works after STOP without compiling or editing. Paths are relative. No shell, claims, replacement targets or arbitrary URLs.',
        input: workerSchema,
        options: { codemode: false },
        execute: async (input, context) => {
          await ctx.permission.assert({ sessionID: context.sessionID, agent: context.agent,
            action: 'matchflow_worker', resources: ['*'], save: [] });
          return { content: JSON.stringify(await host('worker', {
            session: context.sessionID, op: input.op, data: input.data || {},
          }, context.signal)) };
        },
      });
    });
  },
};
export default plugin;
