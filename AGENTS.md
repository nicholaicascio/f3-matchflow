# f3-matchflow

This repository holds orchestration code only. The configured private workspace is
separate and retains its own matching policies, game installation and artifacts.

- Read `README.md` and `tools/matchflow/README.md` before pipeline changes.
- Never commit packs, game files, checker distributions, disassembly, credentials,
  databases, claim sources, logs, or session exports, even to this private repository.
- Keep machine-specific configuration in ignored `matchflow.local.json` or the
  `F3_MATCHFLOW_WORKSPACE` environment variable. Workspace paths must be external.
- Run offline tests before pushing. No paid workers, real claims or service
  mutations as tests unless the user explicitly requests them.
- Do not modify the external workspace while another worker is running. Preserve
  historical artifacts and user changes. No automatic replay or claim retries.
- New repositories use `master` as the default branch.
