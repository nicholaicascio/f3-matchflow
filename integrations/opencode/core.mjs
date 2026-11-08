import { spawn } from 'node:child_process';

export const workerPermissions = [
  { action: '*', resource: '*', effect: 'deny' },
  { action: 'matchflow_worker', resource: '*', effect: 'allow' },
];

export function modelRef(model) {
  if (typeof model !== 'string' || !/^[^\s/]+\/[^\s]+$/.test(model)) throw new Error('Explicit model required');
  const slash = model.indexOf('/');
  const [id, variant] = model.slice(slash + 1).split('#');
  return { providerID: model.slice(0, slash), id, ...(variant ? { variant } : {}) };
}

export async function workerContext(event, host) {
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
      if (code !== 0) return reject(new Error(result.error || 'Matchflow operation failed; cleanup_required'));
      resolve(result);
    });
    child.stdin.on('error', () => {});
    child.stdin.end(JSON.stringify(value));
  });
}

export async function launch(ctx, host, input, context) {
  const parent = await ctx.session.get({ sessionID: context.sessionID });
  if (parent.agent === 'match-worker') throw new Error('Matching workers cannot launch sessions');
  const task = await host('task', { ...input, parent: parent.id }, context.signal);
  const child = await ctx.session.create({
    parentID: parent.id, title: `Matchflow ${input.batch}/${input.worker} ${task.address}`,
    agent: 'match-worker', model: modelRef(task.model), permissions: workerPermissions,
  });
  // Bind before any model sees a prompt or can execute a tool. Never let a prompt
  // choose its own session ID, claim, directory or model. No auto-resume/replay.
  let ready;
  try {
    await host('bind', { ...input, session: child.id, parent: parent.id }, context.signal);
    ready = await host('start', { session: child.id }, context.signal);
  } catch (error) {
    return { sessionID: child.id, batch: input.batch, worker: input.worker,
      cleanup_required: true, error: `Host setup/claim failed: ${String(error.message).slice(0,512)}. Inspect exact lifecycle/receipts; never retry launch.` };
  }
  if (!ready.ready) return { sessionID: child.id, ...ready };
  try {
    await ctx.session.prompt({ sessionID: child.id, text: task.prompt });
  } catch {
    return { sessionID: child.id, ...ready, cleanup_required: true,
      error: 'Prompt admission failed after claim; reconcile this exact claim, do not relaunch.' };
  }
  // Wait is local/event-driven, not worker/service polling. It does not reset leases.
  void ctx.session.wait({ sessionID: child.id }).then(async () => {
    const metadata = await ctx.session.get({ sessionID: child.id });
    const result = await host('completion', { session: child.id,
      metadata: { id: metadata.id, tokens: metadata.tokens, model: metadata.model, outcome: metadata.outcome } });
    await ctx.session.prompt({ sessionID: parent.id, delivery: 'queue',
      text: `Matchflow subagent finished: ${JSON.stringify(result)}. Audit/reconcile this exact assignment; do not refill.` });
  }).catch(async () => {
    try { await host('completion', { session: child.id }); } catch { /* durable lifecycle still requires reconciliation */ }
    await ctx.session.prompt({ sessionID: parent.id, delivery: 'queue',
      text: `Matchflow ${input.batch}/${input.worker} session ${child.id} completion failed. cleanup_required; reconcile, never replay.` });
  });
  return { sessionID: child.id, ...ready, method: 'restricted-subagent' };
}
