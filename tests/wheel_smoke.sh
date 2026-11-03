#!/usr/bin/env bash
# Install the built core wheel into a fresh venv and exercise it outside the checkout.
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
wheels=("$repo"/dist/*.whl)
[ "${#wheels[@]}" -eq 1 ] || { echo "expected one wheel in dist/, found ${#wheels[@]}" >&2; exit 1; }
uv venv --quiet "$work/core"
uv pip install --quiet --python "$work/core/bin/python" "${wheels[0]}"
cd "$work"
"$work/core/bin/python" -I "$repo/tests/wheel_smoke.py"
"$work/core/bin/qcl" --version
"$work/core/bin/qcl" --help | grep -q '^usage: qcl'
for retired in clake context-lake; do
  if [ -e "$work/core/bin/$retired" ]; then echo "unexpected $retired command" >&2; exit 1; fi
done
