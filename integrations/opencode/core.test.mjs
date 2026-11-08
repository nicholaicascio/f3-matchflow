import test from 'node:test';
import assert from 'node:assert/strict';
import { modelRef, launch, workerPermissions } from './core.mjs';

test('model binding is explicit and deny-by-default covers every other tool', () => {
  assert.deepEqual(modelRef('provider/model#max'), {providerID:'provider', id:'model', variant:'max'});
  assert.throws(() => modelRef('model'));
  assert.equal(workerPermissions[0].effect, 'deny');
  assert.equal(workerPermissions[0].action, '*');
  assert.deepEqual(workerPermissions.filter(r=>r.effect==='allow').map(r=>r.action), ['matchflow_worker']);
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
  const result = await launch(ctx, host, {batch:'test',worker:'worker01'}, {sessionID:'ses_parent'});
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
  let starts=0, prompts=0;
  const ctx={session:{get:async()=>({id:'ses_parent'}),create:async()=>({id:'ses_child'}),prompt:async()=>{prompts++;}}};
  const host=async command=>{
    if(command==='task') return {model:'p/m',address:'0x00123456'};
    if(command==='start'){starts++; throw new Error('ambiguous');}
    return {};
  };
  const result=await launch(ctx,host,{batch:'test',worker:'worker01'},{sessionID:'ses_parent'});
  assert.equal(result.sessionID,'ses_child');
  assert.equal(result.cleanup_required,true);
  assert.equal(starts,1);
  assert.equal(prompts,0);
});
