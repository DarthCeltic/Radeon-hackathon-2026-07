#!/usr/bin/env bash
# Written with AI assistance. Run --selftest to check that selected faults are detected.
# The checks and native Windows entry point are in verify.py.
set -u
cd -- "$(dirname -- "$0")" || exit 2
for candidate in python3 python; do
  if exe=$(command -v "$candidate") && "$exe" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    exec "$exe" -I -B verify.py "$@"
  fi
done
printf '%s\n' 'Python 3.11+ is required.' >&2
exit 2
