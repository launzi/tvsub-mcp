#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import tomllib
from pathlib import Path


root = Path(__file__).resolve().parents[1]
tag = os.environ.get("GITHUB_REF_NAME", "")
expected = tag.removeprefix("v")
if not expected or tag == expected:
    raise SystemExit(f"release tag must have the form v<version>; got {tag!r}")

with (root / "pyproject.toml").open("rb") as handle:
    project_version = tomllib.load(handle)["project"]["version"]
server_version = json.loads((root / "server.json").read_text(encoding="utf-8"))["version"]
package: dict[str, object] = {}
exec((root / "tvsub_mcp" / "__init__.py").read_text(encoding="utf-8"), package)
package_version = package["__version__"]

versions = {
    "tag": expected,
    "pyproject.toml": project_version,
    "server.json": server_version,
    "tvsub_mcp.__version__": package_version,
}
if len(set(versions.values())) != 1:
    for source, version in versions.items():
        print(f"{source}: {version}", file=sys.stderr)
    raise SystemExit("release versions do not match")
print(f"release version check: PASS ({expected})")

