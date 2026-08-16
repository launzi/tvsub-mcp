#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
import tomllib
from pathlib import Path


root = Path(__file__).resolve().parents[1]
local_mode = "--local" in sys.argv[1:]
if any(argument != "--local" for argument in sys.argv[1:]):
    raise SystemExit("usage: check-release-version.py [--local]")

tag = os.environ.get("GITHUB_REF_NAME", "")
expected = tag.removeprefix("v")
if not local_mode and (not expected or tag == expected):
    raise SystemExit(f"release tag must have the form v<version>; got {tag!r}")

with (root / "pyproject.toml").open("rb") as handle:
    project_version = tomllib.load(handle)["project"]["version"]
server = json.loads((root / "server.json").read_text(encoding="utf-8"))
server_version = server["version"]
package: dict[str, object] = {}
exec((root / "tvsub_mcp" / "__init__.py").read_text(encoding="utf-8"), package)
package_version = package["__version__"]
readme = (root / "README.md").read_text(encoding="utf-8")
readme_versions = re.findall(r"\btvsub-mcp==([0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?)", readme)
if not readme_versions:
    raise SystemExit("README.md: no tvsub-mcp==<version> installation pin found")

versions = {
    "pyproject.toml": project_version,
    "server.json": server_version,
    "tvsub_mcp.__version__": package_version,
}
if not local_mode:
    versions["tag"] = expected
for index, item in enumerate(server.get("packages", []), start=1):
    versions[f"server.json packages[{index}]"] = item.get("version")
for version in readme_versions:
    versions[f"README.md pin {len([key for key in versions if key.startswith('README.md pin')]) + 1}"] = version
if len(set(versions.values())) != 1:
    for source, version in versions.items():
        print(f"{source}: {version}", file=sys.stderr)
    raise SystemExit("release versions do not match")
matched = project_version if local_mode else expected
mode = "local" if local_mode else "release"
print(f"{mode} version check: PASS ({matched}; {len(versions)} values)")
