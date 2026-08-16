#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

if [[ $# -ne 1 ]]; then
  echo "usage: scripts/bump-version.sh <new-version>" >&2
  exit 2
fi
NEW_VERSION=$1
if [[ ! "$NEW_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z]+([.-][0-9A-Za-z]+)*)?(\+[0-9A-Za-z]+([.-][0-9A-Za-z]+)*)?$ ]]; then
  echo "error: version must be SemVer (for example, 0.2.2 or 1.0.0-rc.1)" >&2
  exit 2
fi

PYTHON=""
for candidate in python3.12 python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PYTHON=$(command -v "$candidate")
    break
  fi
done
if [[ -z "$PYTHON" ]]; then
  echo "error: no Python interpreter found" >&2
  exit 1
fi

"$PYTHON" - "$REPO_ROOT" "$NEW_VERSION" <<'PY'
import json
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
version = sys.argv[2]

def replace_one(path, pattern, replacement, label):
    text = path.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise SystemExit(f"{path.name}: expected exactly one {label}")
    path.write_text(updated, encoding="utf-8")

replace_one(root / "pyproject.toml", r'^(version\s*=\s*)"[^"]+"', rf'\g<1>"{version}"', "project version")
server_path = root / "server.json"
server = json.loads(server_path.read_text(encoding="utf-8"))
server["version"] = version
for package in server.get("packages", []):
    package["version"] = version
server_path.write_text(json.dumps(server, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
replace_one(root / "tvsub_mcp" / "__init__.py", r'^(__version__\s*=\s*)"[^"]+"', rf'\g<1>"{version}"', "__version__")
readme_path = root / "README.md"
readme = readme_path.read_text(encoding="utf-8")
readme, count = re.subn(r'\btvsub-mcp==[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?', f'tvsub-mcp=={version}', readme)
if count == 0:
    raise SystemExit("README.md: no installation pin found")
readme_path.write_text(readme, encoding="utf-8")
PY

echo "Updated version to $NEW_VERSION in:"
printf '%s\n' \
  "  pyproject.toml" \
  "  server.json (top-level and packages[].version)" \
  "  tvsub_mcp/__init__.py" \
  "  README.md (all installation pins)"

(cd "$REPO_ROOT" && "$PYTHON" scripts/check-release-version.py --local)
