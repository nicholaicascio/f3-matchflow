import test from 'node:test';
import assert from 'node:assert/strict';
import plugin from './index.mjs';
import { Plugin } from '@opencode/plugin';
import { workerContext } from './core.mjs';

test('installed SDK registers scoped-only tools and strips broad tools from worker requests', async()=>{
  assert.equal(Plugin.define(plugin), plugin);
  const hooks = new Map();
  const tools = new Map();
  await plugin.setup({
    options:{},
    session:{hook:async(name,handler)=>hooks.set(name,handler)},
    tool:{transform:async callback=>callback({add:tool=>tools.set(tool.name,tool)})},
  });
  assert.deepEqual([...tools.keys()],['matchflow_stream','matchflow_discovery','matchflow_launch','matchflow_worker']);
  const schema=tools.get('matchflow_worker').input;
  assert.equal(schema.additionalProperties,false);
  assert.deepEqual(Object.keys(schema.properties),['op','data']);
  assert.deepEqual(schema.properties.data.properties.stage.enum,['baseline','attempt','final']);
  assert.ok(schema.properties.op.enum.includes('diff'));
  assert.ok(schema.properties.op.enum.includes('help_ack'));
  assert.ok(schema.properties.op.enum.includes('symbols'));
  assert.equal(schema.properties.data.properties.notes.type,'object');
  assert.deepEqual([...hooks.keys()],['context','generate','compaction']);
  for(const hook of hooks.values()){
    const event={sessionID:'ses_real',agent:'match-worker',model:{providerID:'p',id:'m'},tools:{shell:{},read:{},execute:{},subagent:{},f3decomp_claim:{},matchflow_launch:{},matchflow_worker:{}}};
    await workerContext(event, async()=>({ownership:{model:'p/m'}}));
    assert.deepEqual(Object.keys(event.tools),['matchflow_worker']);
    const parent={agent:'build',tools:{shell:{},matchflow_launch:{}}};
    await hook(parent);
    assert.equal(Object.keys(parent.tools).length,2);
  }
});

test('worker request rejects unbound identity and silent model failover before generation',async()=>{
  const event={sessionID:'ses_real',agent:'match-worker',model:{providerID:'other',id:'model'},tools:{}};
  await assert.rejects(workerContext(event,async()=>({ownership:{model:'p/m'}})),/model changed/);
  await assert.rejects(workerContext(event,async()=>{throw new Error('unbound');}),/unbound/);
});

test('discovery hook keeps only discovery and scoped read, and rejects model failover',async()=>{
  const event={sessionID:'ses_discovery',agent:'match-discovery',model:{providerID:'p',id:'m'},
    tools:{shell:{},execute:{},subagent:{},read:{},grep:{},glob:{},edit:{},write:{},webfetch:{},
      matchflow_stream:{},matchflow_worker:{},matchflow_discovery:{}}};
  await workerContext(event,async()=>({model:'p/m'}));
  assert.deepEqual(Object.keys(event.tools).sort(),['matchflow_discovery','read']);
  await assert.rejects(workerContext(event,async()=>({model:'other/m'})),/model changed/);
});
