#!/usr/bin/env bash
set -euo pipefail

# Selects a Python 3.11+ interpreter (tomllib) and runs install.py with the
# given options; see ./install.sh --help.
installer_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ -n "${PYTHON_BIN:-}" ]]; then
    candidates=("$PYTHON_BIN")
else
    candidates=(python3 python3.14 python3.13 python3.12 python3.11)
fi
python=''
for candidate in "${candidates[@]}"; do
    if command -v "$candidate" >/dev/null 2>&1 &&
        "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
        python="$(command -v "$candidate")"
        break
    fi
done
if [[ -z "$python" ]]; then
    printf '%s\n' 'Python 3.11+ is required; install it or set PYTHON_BIN to its executable.' >&2
    exit 1
fi

exec "$python" -B "$installer_root/install.py" "$@"
