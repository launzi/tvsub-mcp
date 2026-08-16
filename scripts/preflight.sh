#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
FAILURES=""
PASSED=0
TOTAL=7
PYTHON=""
RUNTIME_READY=0
RUNTIME_FAILED=0

cleanup() {
  rm -rf -- "$REPO_ROOT/dist"
}
trap cleanup EXIT

find_python() {
  local candidate
  for candidate in python3.12 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      PYTHON=$(command -v "$candidate")
      return 0
    fi
  done
  echo "no Python interpreter found (tried python3.12, python3, python)" >&2
  return 1
}

record_failure() {
  local name=$1 reason=$2
  echo "FAIL [$name]: $reason" >&2
  if [[ -n "$FAILURES" ]]; then
    FAILURES="$FAILURES, $name"
  else
    FAILURES=$name
  fi
}

run_check() {
  local name=$1 reason=$2
  shift 2
  if "$@"; then
    PASSED=$((PASSED + 1))
    echo "PASS [$name]"
  else
    record_failure "$name" "$reason"
  fi
}

check_metadata() {
  "$PYTHON" - "$REPO_ROOT" <<'PY'
import sys
import tomllib
from pathlib import Path

root = Path(sys.argv[1])
with (root / "pyproject.toml").open("rb") as handle:
    data = tomllib.load(handle)
if data["project"].get("readme") != "README.md":
    raise SystemExit('pyproject.toml must contain readme = "README.md"')
readme = (root / "README.md").read_text(encoding="utf-8")
if "<!-- mcp-name: io.github.launzi/tvsub-mcp -->" not in readme:
    raise SystemExit("README.md is missing the MCP ownership marker")
package_find = data.get("tool", {}).get("setuptools", {}).get("packages", {}).get("find", {})
if package_find.get("include") != ["tvsub_mcp*"]:
    raise SystemExit('[tool.setuptools.packages.find] include must be exactly ["tvsub_mcp*"]')
PY
}

ensure_runtime() {
  if [[ $RUNTIME_READY -eq 1 ]]; then
    return 0
  fi
  if [[ $RUNTIME_FAILED -eq 1 ]]; then
    return 1
  fi
  if "$PYTHON" -c 'import mcp, build' >/dev/null 2>&1; then
    RUNTIME_READY=1
    return 0
  fi
  if [[ ! -x "$REPO_ROOT/.venv/bin/python" ]]; then
    "$PYTHON" -m venv "$REPO_ROOT/.venv"
  fi
  if ! "$REPO_ROOT/.venv/bin/python" -m pip install -e "$REPO_ROOT" build; then
    RUNTIME_FAILED=1
    return 1
  fi
  PYTHON="$REPO_ROOT/.venv/bin/python"
  RUNTIME_READY=1
}

run_units() {
  ensure_runtime || return 1
  (cd "$REPO_ROOT" && "$PYTHON" -m unittest discover -s tests -p 'test_*.py')
}

run_smoke() {
  ensure_runtime || return 1
  (cd "$REPO_ROOT" && TVSUB_TEST_PYTHON="$PYTHON" "$PYTHON" tests/stdio_smoke.py)
}

run_build() {
  ensure_runtime || return 1
  (cd "$REPO_ROOT" && "$PYTHON" -m build)
  if "$PYTHON" -c 'import twine' >/dev/null 2>&1; then
    (cd "$REPO_ROOT" && "$PYTHON" -m twine check dist/*)
  else
    echo "SKIP [twine]: twine is not available"
  fi
}

check_description() {
  "$PYTHON" - "$REPO_ROOT/server.json" <<'PY'
import json
import sys
from pathlib import Path

description = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["description"]
length = len(description)
if length > 100:
    raise SystemExit(f"server.json description is {length} characters (maximum 100)")
PY
}

cd "$REPO_ROOT"
if ! find_python; then
  record_failure "python" "required interpreter is unavailable"
  echo "PREFLIGHT FAIL (${PASSED}/${TOTAL} checks passed; failed: $FAILURES)" >&2
  exit 1
fi

run_check "versions" "version values differ or a version location is missing" \
  "$PYTHON" scripts/check-release-version.py --local
run_check "metadata" "required PyPI/MCP/setuptools metadata is invalid" check_metadata
run_check "hygiene" "public repository hygiene gate rejected repository content" \
  bash scripts/hygiene-check.sh
run_check "unit" "unit test suite failed" run_units
run_check "smoke" "stdio MCP smoke test failed" run_smoke
run_check "build" "distribution build or twine validation failed" run_build
run_check "description" "server.json description exceeds 100 characters" check_description

if [[ -n "$FAILURES" ]]; then
  echo "PREFLIGHT FAIL (${PASSED}/${TOTAL} checks passed; failed: $FAILURES)" >&2
  exit 1
fi
echo "PREFLIGHT PASS (${PASSED} checks)"
