import { fileURLToPath } from 'node:url';
import { invoke, launch, workerContext } from './core.mjs';

const script = fileURLToPath(new URL('../../tools/matchflow/broker.py', import.meta.url));
const object = { type: 'object', additionalProperties: true };

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
        description: 'Session-scoped matching tools. op: status, target(offset,limit; own assigned function only, private local inspection), read(area=files|pack|packet,path,offset,limit), list/search(area,text), write(path,text), reference(name=policy|recipe|compiler_tips|large_functions|upstream_tips|fnv), twin, check(source,stage,change,why,expect,observed,regions,flags), gate(decision,reason,hypothesis,expected,integration_plan), register_wall(evidence), promote, checkpoint(phase,note), close(operation,source,notes,no_source_reason), report. Paths are relative. No shell, claims, replacement targets or arbitrary URLs.',
        input: { type: 'object', properties: { op: { type: 'string' }, data: object },
          required: ['op'], additionalProperties: false },
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
