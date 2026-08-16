from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


DEFAULT_GLOSSARY: dict[str, Any] = {
    "version": 1,
    "names": {},
    "relationships": [],
    "terms": {},
    "forbidden_translations": [],
    "notes": [],
}


def glossary_path(source: Path) -> Path:
    """`<작품>.glossary.json`: 언어 suffix가 있으면 작품 stem까지 되짚는다."""
    stem = source.stem
    parts = stem.split(".")
    if len(parts) > 1 and 1 < len(parts[-1]) <= 16:
        stem = ".".join(parts[:-1])
    return source.with_name(f"{stem}.glossary.json")


def validate_glossary(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("glossary는 JSON object여야 합니다.")
    merged = {**DEFAULT_GLOSSARY, **payload, "version": 1}
    for key in ("names", "terms"):
        if not isinstance(merged[key], dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in merged[key].items()
        ):
            raise ValueError(f"glossary.{key}는 문자열→문자열 object여야 합니다.")
    for key in ("relationships", "forbidden_translations", "notes"):
        if not isinstance(merged[key], list):
            raise ValueError(f"glossary.{key}는 array여야 합니다.")
    return merged


def load_glossary(source: Path) -> tuple[Path, dict[str, Any] | None, str | None]:
    path = glossary_path(source)
    if not path.exists():
        return path, None, None
    payload = validate_glossary(json.loads(path.read_text(encoding="utf-8")))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return path, payload, digest


def prompt_glossary(payload: dict[str, Any] | None) -> str:
    if not payload:
        return ""
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
