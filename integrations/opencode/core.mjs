import { spawn } from 'node:child_process';
import { homedir } from 'node:os';
import { join } from 'node:path';

export const workerPermissions = [
  { action: '*', resource: '*', effect: 'deny' },
  { action: 'matchflow_worker', resource: '*', effect: 'allow' },
];

// OpenCode saves oversized tool results here and tells the model to read the file.
// Discovery may read only those saved results, never the workspace, keys or packs.
export function toolOutputPattern(env = process.env, home = homedir()) {
  const data = env.XDG_DATA_HOME || join(home, '.local', 'share');
  return join(data, 'opencode', 'tool-output').replace(/\\/g, '/').replace(/\/+$/, '') + '/*';
}
export const discoveryReadTools = ['read'];
export const discoveryPermissions = [
  { action: '*', resource: '*', effect: 'deny' },
  { action: 'matchflow_discovery', resource: '*', effect: 'allow' },
  { action: 'external_directory', resource: toolOutputPattern(), effect: 'allow' },
  { action: 'read', resource: toolOutputPattern(), effect: 'allow' },
];

// One JSON value per line where possible: OpenCode's read tool cuts lines at 2000
// characters, so a single-line 100 KB catalog saved to a file would be unreadable.
export function readableJson(value, indent = '') {
  const flat = JSON.stringify(value);
  if (flat === undefined || flat.length <= 1000 || value === null || typeof value !== 'object') return flat ?? 'null';
  const next = indent + ' ';
  if (Array.isArray(value)) {
    if (!value.length) return '[]';
    return '[\n' + value.map(item => next + readableJson(item, next)).join(',\n') + '\n' + indent + ']';
  }
  const entries = Object.entries(value).filter(([, item]) => item !== undefined && typeof item !== 'function');
  if (!entries.length) return '{}';
  return '{\n' + entries.map(([key, item]) => next + JSON.stringify(key) + ':' + readableJson(item, next)).join(',\n')
    + '\n' + indent + '}';
}

export const discoveryFailureText = batch =>
  `Matchflow ${batch} discovery failed; the requested matching run is CANCELLED. STOP, reconcile only this run's `
  + 'durable claims and workers, and report the blocker to the human. Do not screen, prepare, dispatch, open another '
  + 'stream or launch any matching worker as a fallback; the host refuses new runs from this session.';

export function modelRef(model) {
  if (typeof model !== 'string' || !/^[^/\s#]+\/[^\s#]+(?:#[^/\s#]+)?$/.test(model)) throw new Error('Explicit provider/model[#variant] required');
  const slash = model.indexOf('/');
  const [id, variant] = model.slice(slash + 1).split('#');
  return { providerID: model.slice(0, slash), id, ...(variant ? { variant } : {}) };
}

export async function requireWorkerModel(ctx, selection) {
  const ref = modelRef(selection);
  const remedy = 'Configure that provider in OpenCode, or set worker_model in matchflow.local.json '
    + 'or use batch.py prepare --model with an available provider/model[#variant]. '
    + 'Prepare a new batch if changing models; existing assignments are immutable. No claim taken; no automatic fallback.';
  let catalog;
  try {
    catalog = await ctx.model.list({ location: { directory: ctx.location.directory }, types: ['chat'] });
  } catch {
    throw new Error(`Cannot validate worker model ${selection} through OpenCode. ${remedy}`);
  }
  if (!Array.isArray(catalog?.data)) throw new Error(`Invalid OpenCode model catalog. ${remedy}`);
  const model = catalog.data.find(m => m.providerID === ref.providerID && m.id === ref.id);
  if (!model || model.enabled !== true || (model.type !== undefined && model.type !== 'chat')
      || model.capabilities?.tools !== true) {
    throw new Error(`Worker model ${selection} is unavailable, disabled, or not a tool-capable chat model. ${remedy}`);
  }
  if (ref.variant && (!Array.isArray(model.variants) || !model.variants.some(v => v.id === ref.variant))) {
    throw new Error(`Worker model variant ${selection} is unavailable. ${remedy}`);
  }
  return ref;
}

export async function workerContext(event, host) {
  if (event.agent === 'match-discovery') {
    for (const name of Object.keys(event.tools)) {
      if (name !== 'matchflow_discovery' && !discoveryReadTools.includes(name)) delete event.tools[name];
    }
    const state = await host('discovery', {session:event.sessionID, op:'status', data:{}});
    const expected = modelRef(state.model);
    if (event.model?.providerID !== expected.providerID || event.model?.id !== expected.id ||
        event.model?.variant !== expected.variant) throw new Error('Assigned discovery model changed');
    return;
  }
  if (event.agent !== 'match-worker') return;
  for (const name of Object.keys(event.tools)) {
    if (name !== 'matchflow_worker') delete event.tools[name];
  }
  // Also fail before a paid request if somebody starts the worker by a generic
  // subagent path, or a provider failover silently changes the assigned model.
  const bound = await host('worker', {session:event.sessionID, op:'status', data:{}});
  const expected = modelRef(bound.ownership.model);
  if (event.model?.providerID !== expected.providerID || event.model?.id !== expected.id ||
      event.model?.variant !== expected.variant) {
    throw new Error('Assigned worker model changed; parent must reconcile, not silently reroute');
  }
}

export function invoke(python, script, command, value, signal) {
  return new Promise((resolve, reject) => {
    // No shell and no prompt/source/secret in command arguments. Stderr stays private.
    const child = spawn(python, [script, command], { shell: false, stdio: ['pipe', 'pipe', 'pipe'], signal });
    let output = '';
    let excess = false;
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', chunk => {
      if (output.length + chunk.length > 2 * 1024 * 1024) { excess = true; child.kill(); }
      else output += chunk;
    });
    child.stderr.on('data', () => {});
    child.on('error', () => reject(new Error('Matchflow host process unavailable; reconcile without retry')));
    child.on('close', code => {
      if (excess) return reject(new Error('Matchflow output exceeded limit; reconcile without retry'));
      let result;
      try { result = JSON.parse(output); }
      catch { return reject(new Error('Invalid Matchflow host result; reconcile without retry')); }
      if (!result || typeof result !== 'object' || Array.isArray(result)) {
        return reject(new Error('Invalid Matchflow host result shape; reconcile without retry'));
      }
      if (code !== 0) {
        const kind = typeof result.error_type === 'string' && /^[A-Za-z_]{1,64}$/.test(result.error_type)
          ? ` [${result.error_type}]` : '';
        return reject(new Error((result.error || 'Matchflow operation failed; cleanup_required') + kind));
      }
      resolve(result);
    });
    child.stdin.on('error', () => {});
    child.stdin.end(JSON.stringify(value));
  });
}

export async function launch(ctx, host, input, context, queuedTask = null) {
  const parent = await ctx.session.get({ sessionID: context.sessionID });
  if (['match-worker','match-discovery'].includes(parent.agent)) throw new Error('Restricted workers cannot launch sessions');
  const task = queuedTask || await host('task', { ...input, parent: parent.id }, context.signal);
  // Read the active catalog in this checkout, not a static provider definition.
  // No child, binding or real claim exists if this local availability check fails.
  let model, child;
  try {
    model = await requireWorkerModel(ctx, task.model);
    child = await ctx.session.create({
    parentID: parent.id, title: `Matchflow ${input.batch}/${input.worker} ${task.address}`,
    agent: 'match-worker', model, permissions: workerPermissions,
    });
  } catch (error) {
    if (queuedTask) await host('stream', {op:'fail',batch:input.batch,parent:parent.id});
    throw error;
  }
  // Bind before any model sees a prompt or can execute a tool. Never let a prompt
  // choose its own session ID, claim, directory or model. No auto-resume/replay.
  let ready;
  try {
    await host('bind', { ...input, session: child.id, parent: parent.id }, context.signal);
    ready = await host('start', { session: child.id }, context.signal);
  } catch (error) {
    let barrierPersisted = false;
    try {
      await host('failure', {...input, session:child.id, parent:parent.id, reason:'launch_failed'});
      barrierPersisted = true;
    } catch { /* return an explicit unresolved barrier, never retry setup */ }
    return { sessionID: child.id, batch: input.batch, worker: input.worker,
      barrier_persisted: barrierPersisted,
      cleanup_required: true, error: `Host setup/claim failed: ${String(error.message).slice(0,512)}. Inspect exact lifecycle/receipts; never retry launch.` };
  }
  if (!ready.ready) return { sessionID: child.id, ...ready };
  try {
    await ctx.session.prompt({ sessionID: child.id, text: task.prompt });
    if (queuedTask) await host('stream', {op:'assigned',...input,parent:parent.id}, context.signal);
  } catch {
    let barrierPersisted = false;
    try {
      await host('failure', {...input, session:child.id, parent:parent.id, reason:'prompt_failed'});
      barrierPersisted = true;
    } catch { /* parent must reconcile a failed host barrier as well */ }
    return { sessionID: child.id, ...ready, cleanup_required: true,
      barrier_persisted: barrierPersisted,
      error: 'Prompt admission failed after claim; reconcile this exact claim, do not relaunch.' };
  }
  // Wait is local/event-driven, not worker/service polling. It does not reset leases.
  void ctx.session.wait({ sessionID: child.id }).then(async () => {
    const metadata = await ctx.session.get({ sessionID: child.id });
    const result = await host('completion', { session: child.id,
      metadata: { id: metadata.id, tokens: metadata.tokens, model: metadata.model, outcome: metadata.outcome } });
    // Streaming routine completions are coalesced by the durable monitor. Urgent
    // cleanup still wakes the parent immediately, even while discovery is active.
    if (!queuedTask || result.cleanup_required) {
      await ctx.session.prompt({ sessionID: parent.id, delivery: 'queue',
        text: `Matchflow subagent finished: ${JSON.stringify(result)}. Audit/reconcile this exact assignment; do not refill.` });
    }
  }).catch(async () => {
    try { await host('completion', { session: child.id }); } catch { /* durable lifecycle still requires reconciliation */ }
    await ctx.session.prompt({ sessionID: parent.id, delivery: 'queue',
      text: `Matchflow ${input.batch}/${input.worker} session ${child.id} completion failed. cleanup_required; reconcile, never replay.` });
  });
  return { sessionID: child.id, ...ready, method: 'restricted-subagent' };
}

export async function stream(ctx, host, input, context) {
  const parent = await ctx.session.get({sessionID:context.sessionID});
  if (['match-worker','match-discovery'].includes(parent.agent)) throw new Error('Restricted children cannot orchestrate');
  if (input.op === 'open') {
    // Resolve the host's configured model before opening a run or making a claim.
    // open itself performs only local preflight/state initialization, not claims.
    let result, pulse;
    try {
      result = await host('stream', {...input,parent:parent.id}, context.signal);
      let pulsing = false;
      pulse = setInterval(async()=>{
        if (pulsing) return;
        pulsing = true;
        try { await host('stream',{op:'pulse',batch:input.batch,parent:parent.id}); }
        catch {
          clearInterval(pulse);
          try { await host('stream',{op:'fail',batch:input.batch,parent:parent.id}); } catch { /* watchdog lease catches process loss */ }
          try { await ctx.session.prompt({sessionID:parent.id,delivery:'queue',text:
            discoveryFailureText(input.batch)}); } catch { /* durable monitor owns wake-up */ }
        } finally { pulsing = false; }
      },10000);
      pulse.unref?.();
      const model = await requireWorkerModel(ctx,result.model);
      const child = await ctx.session.create({parentID:parent.id,title:`Matchflow ${input.batch} discovery`,
        agent:'match-discovery',model,permissions:discoveryPermissions});
      await host('stream',{op:'bind',batch:input.batch,parent:parent.id,session:child.id},context.signal);
      await ctx.session.prompt({sessionID:child.id,text:
        `Discover and secure ${result.requested} reviewed functions for this bound run. Use matchflow_discovery for every operation. If a matchflow_discovery result was saved to a file because it was too large, use read with offset/limit on that exact saved path only; read nothing else. Start with catalog, inspect candidates, then enqueue with evidence-backed reason/review. Continue paging and inspecting until the quota is secured; there is no page, inspection-count or total discovery-time cap. Rejected candidates are not exhaustion. Stream each claim immediately. On backpressure use wait, not repeated status polling. Stop when producer_state is done. Finish short only when the catalog has no next page and all potentially suitable candidates have been reviewed or excluded on evidence, with the actual pool exhaustion reason. Never restart the catalog or retry ambiguous mutations. STOP, service limits or repeated cursors require stopping and reporting the error.`});
      void ctx.session.wait({sessionID:child.id}).then(async()=>{
        clearInterval(pulse);
        const ended = await host('stream',{op:'producer_end',batch:input.batch,parent:parent.id});
        if (ended.cleanup_required) await ctx.session.prompt({sessionID:parent.id,delivery:'queue',text:
          discoveryFailureText(input.batch)});
      }).catch(async()=>{
        clearInterval(pulse);
        try { await host('stream',{op:'fail',batch:input.batch,parent:parent.id}); } catch { /* controller lease expires */ }
        try { await ctx.session.prompt({sessionID:parent.id,delivery:'queue',text:discoveryFailureText(input.batch)}); } catch { /* durable monitor owns wake-up */ }
      });
      return {...result,discoverySessionID:child.id,monitor:`python tools/matchflow/batch.py monitor ${input.batch}`};
    } catch (error) {
      clearInterval(pulse);
      try { await host('stream',{op:'fail',batch:input.batch,parent:parent.id}); } catch { /* incomplete open remains explicit; watchdog lease expires */ }
      throw error;
    }
  }
  if (input.op === 'next') {
    let task;
    try { task = await host('stream',{...input,parent:parent.id},context.signal); }
    catch (error) {
      // The host may already have reserved the FIFO claim before losing its
      // response. Never call next again to discover what happened.
      try { await host('stream',{op:'fail',batch:input.batch,parent:parent.id}); } catch { /* durable launch lease is the backstop */ }
      throw error;
    }
    if (task.empty) return task;
    return launch(ctx,host,{batch:input.batch,worker:task.worker},context,task);
  }
  return host('stream',{...input,parent:parent.id},context.signal);
}
