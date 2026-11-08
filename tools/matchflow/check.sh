#!/bin/bash
# Our wrapper, not an official checker. Its source lives only in f3-matchflow.
set -eu
exec python3 "$(dirname "$0")/check_runner.py" "$@"
