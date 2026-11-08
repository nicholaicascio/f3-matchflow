# Pinned public dependencies

`f3twins.lock.json` pins mathisto/f3twins to an upstream commit and SHA-256 for each
installed file. Copyright (c) 2026 mathisto, MIT. `twins.py install` installs that
unmodified public code, seeds, references and LICENSE under this repository's ignored
`.deps/f3twins/<commit>/`. No game-derived data is installed there. The original MIT
notice travels with every installed copy; do not redistribute it without LICENSE.

Our adapter/configuration/tests live here in Git. The public dependency is installed,
not duplicated into f3decomp. FNV source clones are separately configured read-only
dependencies. Private features/results/manifests are always in the external workspace.
Only locally generated caches are trusted; upstream pickle caches must never be downloaded.
