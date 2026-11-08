export const meta = {
  name: 'match-batch',
  description: 'Disabled legacy matching path; use matchflow_launch restricted subagents.',
  phases: ['Match'],
};
throw new Error('Legacy matching Workflows are disabled. Use the session-scoped matchflow_launch subagent route from f3-matchflow.');
