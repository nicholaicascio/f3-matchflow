import test from 'node:test';
import assert from 'node:assert/strict';
import { modelRef, launch, stream, workerPermissions, discoveryPermissions, invoke, requireWorkerModel,
  toolOutputPattern, readableJson, discoveryFailureText } from './core.mjs';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const availableModel = (providerID='p', id='m') => ({providerID, id, enabled:true,
  type:'chat', capabilities:{tools:true}, variants:[{id:'max'}]});
const availableContext = ctx => ({...ctx, location:{directory:'/fixture/checkout'},
  model:{list:async()=>({data:[availableModel(),availableModel('provider','model')]})}});

test('model binding is explicit and deny-by-default covers every other tool', () => {
  assert.deepEqual(modelRef('provider/model#max'), {providerID:'provider', id:'model', variant:'max'});
  assert.throws(() => modelRef('model'));
  assert.equal(workerPermissions[0].effect, 'deny');
  assert.equal(workerPermissions[0].action, '*');
  assert.deepEqual(workerPermissions.filter(r=>r.effect==='allow').map(r=>r.action), ['matchflow_worker']);
});

// Mirrors OpenCode's documented rule semantics: whole-value wildcards, last match wins.
function permitted(rules, action, resource) {
  const match = (pattern, value) => new RegExp('^' + pattern.replace(/[.+^${}()|[\]\\]/g, '\\$&')
    .replace(/\*/g, '.*').replace(/\?/g, '.') + '$', 'i').test(value);
  const normalized = resource.replace(/\\/g, '/');
  let effect = 'ask';
  for (const rule of rules) if (match(rule.action, action) && match(rule.resource, normalized)) effect = rule.effect;
  return effect;
}

test('discovery may read only saved oversized tool results', () => {
  assert.equal(toolOutputPattern({}, 'C:\\Users\\someone'), 'C:/Users/someone/.local/share/opencode/tool-output/*');
  assert.equal(toolOutputPattern({XDG_DATA_HOME:'/data/'}, '/home/x'), '/data/opencode/tool-output/*');
  const saved = toolOutputPattern().slice(0, -1) + 'tool_1c49c0c18001example';
  assert.equal(permitted(discoveryPermissions, 'read', saved), 'allow');
  assert.equal(permitted(discoveryPermissions, 'external_directory', toolOutputPattern()), 'allow');
  for (const [action, resource] of [['read', '/workspace/keys/f3decomp.key'], ['read', 'matchflow.local.json'],
      ['read', toolOutputPattern().slice(0, -2)], ['read', toolOutputPattern().replace('tool-output/*', 'opencode.db')],
      ['external_directory', '/workspace/*'], ['edit', saved], ['grep', '*'], ['shell', 'cat ' + saved],
      ['f3decomp_claim', '*'], ['subagent', 'general']]) {
    assert.equal(permitted(discoveryPermissions, action, resource), 'deny', `${action} ${resource}`);
  }
  for (const action of ['read', 'external_directory', 'edit']) {
    assert.equal(permitted(workerPermissions, action, saved), 'deny');
  }
});

test('discovery results are line-split so saved files stay readable', () => {
  const functions = Array.from({length:500}, (_, i) => ({address:'0x' + (0x400000 + i * 16).toString(16).padStart(8, '0'),
    size:200, band:'<=255', earlier_claims:0, give_ups:i % 3, failed_verifications:0, near_misses:0,
    open_problem_reports:0, file_problem_reports:1}));
  const value = {order:'give_ups', flagged_note:'x'.repeat(400), functions, next:'cursor'};
  const text = readableJson(value);
  assert.deepEqual(JSON.parse(text), value);
  assert.ok(text.split('\n').length > 500);
  assert.ok(Math.max(...text.split('\n').map(line => line.length)) < 2000);
  for (const small of [{}, [], null, 3, 'x', {a:[]}, {receipt:{nested:[1, {b:'c'.repeat(1300)}]}}]) {
    assert.deepEqual(JSON.parse(readableJson(small)), small);
  }
});

test('discovery failure tells the parent to cancel, not fall back', () => {
  const text = discoveryFailureText('nov10-s3');
  assert.match(text, /CANCELLED/);
  assert.match(text, /Do not screen, prepare, dispatch, open another stream or launch/);
});

test('real child is bound and claimed before first prompt; no model-selected identity', async () => {
  const calls = [];
  const ctx = {session:{
    get: async () => ({id:'ses_parent', agent:'build'}),
    create: async input => {calls.push(['create',input]); return {id:'ses_real'};},
    prompt: async input => {calls.push(['prompt',input]);},
    wait: async () => {},
  }};
  const host = async (command, value) => {
    calls.push([command,value]);
    if(command==='task') return {model:'provider/model#max',address:'0x00123456',prompt:'prepared'};
    if(command==='start') return {ready:true,claim_id:'clm_test'};
    return {};
  };
  const result = await launch(availableContext(ctx), host, {batch:'test',worker:'worker01'}, {sessionID:'ses_parent'});
  assert.equal(result.sessionID, 'ses_real');
  assert.deepEqual(calls.slice(0,5).map(c=>c[0]), ['task','create','bind','start','prompt']);
  assert.equal(calls.find(c=>c[0]==='bind')[1].session, 'ses_real');
  assert.equal(calls.find(c=>c[0]==='create')[1].agent, 'match-worker');
  assert.deepEqual(calls.find(c=>c[0]==='create')[1].permissions, workerPermissions);
});

test('worker cannot spawn child or touch host', async () => {
  const ctx = {session:{get:async()=>({agent:'match-worker'})}};
  await assert.rejects(launch(ctx, ()=>assert.fail(), {}, {sessionID:'ses_worker'}), /cannot launch/);
});

test('ambiguous host failure returns exact session and never retries', async () => {
  let starts=0, prompts=0, failures=0;
  const ctx={session:{get:async()=>({id:'ses_parent'}),create:async()=>({id:'ses_child'}),prompt:async()=>{prompts++;}}};
  const host=async (command,value,signal)=>{
    if(command==='task') return {model:'p/m',address:'0x00123456'};
    if(command==='start'){starts++; throw new Error('ambiguous');}
    if(command==='failure'){
      failures++;
      assert.equal(signal,undefined);
      assert.equal(value.session,'ses_child');
      assert.equal(value.parent,'ses_parent');
    }
    return {};
  };
  const result=await launch(availableContext(ctx),host,{batch:'test',worker:'worker01'},{sessionID:'ses_parent'});
  assert.equal(result.sessionID,'ses_child');
  assert.equal(result.cleanup_required,true);
  assert.equal(starts,1);
  assert.equal(prompts,0);
  assert.equal(failures,1);
  assert.equal(result.barrier_persisted,true);
});

test('failed prompt admission persists cleanup barrier before returning',async()=>{
  const calls=[];
  const ctx={session:{get:async()=>({id:'ses_parent'}),create:async()=>({id:'ses_child'}),
    prompt:async()=>{throw new Error('offline prompt failure');}}};
  const host=async(command,value,signal)=>{
    calls.push(command);
    if(command==='task') return {model:'p/m',address:'0x00123456'};
    if(command==='start') return {ready:true,claim_id:'clm_fixture'};
    if(command==='failure'){
      assert.equal(value.reason,'prompt_failed');
      assert.equal(signal,undefined);
    }
    return {};
  };
  const result=await launch(availableContext(ctx),host,{batch:'test',worker:'worker01'},{sessionID:'ses_parent'});
  assert.equal(result.cleanup_required,true);
  assert.equal(result.barrier_persisted,true);
  assert.equal(calls.at(-1),'failure');
  assert.equal(calls.filter(c=>c==='start').length,1);
});

test('lost cleanup barrier is explicitly unresolved and never triggers setup retry',async()=>{
  let starts=0;
  const ctx={session:{get:async()=>({id:'ses_parent'}),create:async()=>({id:'ses_child'})}};
  const host=async command=>{
    if(command==='task') return {model:'p/m',address:'0x00123456'};
    if(command==='start'){starts++;throw new Error('offline setup failure');}
    if(command==='failure') throw new Error('offline barrier storage failure');
    return {};
  };
  const result=await launch(availableContext(ctx),host,{batch:'test',worker:'worker01'},{sessionID:'ses_parent'});
  assert.equal(result.cleanup_required,true);
  assert.equal(result.barrier_persisted,false);
  assert.equal(starts,1);
});

test('host protocol rejects non-object JSON and preserves safe structured errors', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'matchflow-protocol-'));
  try {
    const script = join(folder, 'fake-host.cjs');
    writeFileSync(script, `
      process.stdin.resume();
      process.stdin.on('end', () => {
        const mode = process.argv[2];
        if (mode === 'error') {
          console.log(JSON.stringify({error:'closure notes must be an object',error_type:'AttributeError',cleanup_required:true}));
          process.exitCode = 2;
        } else if (mode === 'invalid') {
          process.stderr.write('PRIVATE DIAGNOSTIC');
          process.exitCode = 2;
        } else console.log(mode);
      });
    `);
    for (const mode of ['null', '[]', '42', 'invalid']) {
      await assert.rejects(invoke(process.execPath, script, mode, {}), /Invalid Matchflow host result/);
    }
    await assert.rejects(invoke(process.execPath, script, 'error', {}), /closure notes must be an object \[AttributeError\]/);
  } finally {
    rmSync(folder, {recursive:true, force:true});
  }
});

test('unavailable provider/model/variant stops before child creation, binding or claim', async()=>{
  for(const data of [[], [availableModel('other','m')], [availableModel('p','other')],
    [{...availableModel(),enabled:false}], [{...availableModel(),type:'image'}],
    [{...availableModel(),type:'decision'}], [{...availableModel(),capabilities:{tools:false}}],
    [{...availableModel(),variants:[]}], [{...availableModel(),variants:[{id:'high'}]}]]) {
    const calls=[];
    const ctx={location:{directory:'/fixture/checkout'},model:{list:async input=>{
      assert.deepEqual(input,{location:{directory:'/fixture/checkout'},types:['chat']});
      calls.push('catalog');return {data};
    }},session:{get:async()=>({id:'ses_parent'}),create:async()=>assert.fail('no child allowed')}};
    const host=async command=>{calls.push(command);assert.equal(command,'task');return {model:'p/m#max'};};
    await assert.rejects(launch(ctx,host,{}, {sessionID:'ses_parent'}),/No claim taken; no automatic fallback/);
    assert.deepEqual(calls,['task','catalog']);
  }
});

test('model inventory failure is fail-closed without leaking provider errors',async()=>{
  for (const list of [async()=>{throw new Error('PRIVATE')}, async()=>({}), async()=>[]]) {
    await assert.rejects(requireWorkerModel({location:{directory:'/fixture'},model:{list}},'p/m'),error=>{
      assert.match(error.message,/Configure that provider/);
      assert.doesNotMatch(error.message,/PRIVATE/);
      return true;
    });
  }
});

test('model check allows exact variants and models without a variant; never changes selection',async()=>{
  const ctx=availableContext({});
  assert.deepEqual(await requireWorkerModel(ctx,'p/m'),{providerID:'p',id:'m'});
  assert.deepEqual(await requireWorkerModel(ctx,'p/m#max'),{providerID:'p',id:'m',variant:'max'});
  for(const value of ['p/m#', 'p/m#max#high', '#p/m', 'p/m#max/other', '', null]) {
    assert.throws(()=>modelRef(value),/Explicit provider\/model/);
  }
  assert.deepEqual(modelRef('router/vendor/model'),{providerID:'router',id:'vendor/model'});
});

test('stream open binds restricted producer before first prompt without claiming',async()=>{
  const calls=[];
  const ctx=availableContext({session:{get:async()=>({id:'ses_parent',agent:'build'}),
    create:async input=>{calls.push(['create',input]);return {id:'ses_producer'};},
    prompt:async input=>{calls.push(['prompt',input]);},wait:()=>new Promise(()=>{})}});
  const host=async(command,value)=>{calls.push([command,value]);return {model:'p/m',requested:20};};
  const result=await stream(ctx,host,{op:'open',batch:'stream',requested:20},{sessionID:'ses_parent'});
  assert.equal(result.discoverySessionID,'ses_producer');
  assert.deepEqual(calls.map(c=>c[0]),['stream','create','stream','prompt']);
  assert.equal(calls[1][1].agent,'match-discovery');
  assert.deepEqual(calls[1][1].permissions,discoveryPermissions);
  assert.equal(calls[2][1].op,'bind');
  const prompt=calls[3][1].text;
  assert.match(prompt,/Continue paging and inspecting until the quota is secured/);
  assert.match(prompt,/no page, inspection-count or total discovery-time cap/);
  assert.match(prompt,/Finish short only when the catalog has no next page/);
  assert.doesNotMatch(prompt,/bounded search/);
});

test('stream next hands off preclaimed task and suppresses routine child completion prompts',async()=>{
  const calls=[];
  const ctx=availableContext({session:{get:async input=>({id:input.sessionID,agent:'build'}),
    create:async()=>({id:'ses_child'}),prompt:async input=>calls.push(['prompt',input]),wait:async()=>{}}});
  const host=async(command,value)=>{
    calls.push([command,value]);
    if(command==='stream'&&value.op==='next') return {model:'p/m',worker:'worker01',address:'0x00123456',prompt:'work'};
    if(command==='start')return {ready:true,claim_id:'clm_existing'};
    return {cleanup_required:false};
  };
  await stream(ctx,host,{op:'next',batch:'stream'},{sessionID:'ses_parent'});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(calls.filter(c=>c[0]==='task').length,0);
  assert.equal(calls.filter(c=>c[0]==='prompt').length,1);
  assert.equal(calls.filter(c=>c[0]==='stream'&&c[1].op==='assigned').length,1);
});

test('stream startup failure latches stop and never consumes another claim',async()=>{
  const calls=[];
  const ctx=availableContext({session:{get:async()=>({id:'ses_parent'}),create:async()=>{throw new Error('offline');}}});
  const host=async(command,value)=>{
    calls.push(value.op);
    return {model:'p/m',worker:'worker01',address:'0x00123456'};
  };
  await assert.rejects(stream(ctx,host,{op:'next',batch:'stream'},{sessionID:'ses_parent'}),/offline/);
  assert.deepEqual(calls,['next','fail']);
});

test('restricted producer cannot orchestrate or launch matching children',async()=>{
  const ctx={session:{get:async()=>({id:'ses_producer',agent:'match-discovery'})}};
  await assert.rejects(stream(ctx,()=>assert.fail(),{op:'next'},{sessionID:'ses_producer'}),/cannot orchestrate/);
  await assert.rejects(launch(ctx,()=>assert.fail(),{},{sessionID:'ses_producer'}),/cannot launch/);
});

test('lost next response requests a durable stop without consuming another task',async()=>{
  const calls=[];
  const ctx={session:{get:async()=>({id:'ses_parent'})}};
  const host=async(command,value)=>{
    calls.push(value.op);
    if(value.op==='next')throw new Error('response lost after reservation');
    return {};
  };
  await assert.rejects(stream(ctx,host,{op:'next',batch:'stream'},{sessionID:'ses_parent'}),/response lost/);
  assert.deepEqual(calls,['next','fail']);
});
