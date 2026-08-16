from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from .subtitles import SubtitleDocument, load_subtitle, validate_timeline, write_srt
from .glossary import load_glossary, prompt_glossary
from .backends import SelectedBackend, cli_prompt, run_cli_batch, select_backend

MODEL = "claude-sonnet-5"
# 표준가 $3/$15 per MTok. 단 2026-08-31까지는 도입가 $2/$10이 적용된다
# (2026-08-15 확인). 아래 기본값은 표준가라 그 기간 추정치는 실제보다 50% 높게 나온다.
# 실비에 맞추려면 TVSUB_PRICE_INPUT_PER_MTOK=2 TVSUB_PRICE_OUTPUT_PER_MTOK=10.
DEFAULT_INPUT_PRICE_PER_MTOK = 3.0
DEFAULT_OUTPUT_PRICE_PER_MTOK = 15.0


def language_tag(language: str) -> str:
    value = language.strip().lower().replace("_", "-")
    aliases = {
        "korean": "ko", "한국어": "ko", "japanese": "ja", "일본어": "ja",
        "english": "en", "영어": "en", "spanish": "es", "스페인어": "es",
        "french": "fr", "프랑스어": "fr", "german": "de", "독일어": "de",
        "chinese": "zh", "중국어": "zh", "繁體中文": "zh-hant", "简体中文": "zh-hans",
    }
    value = aliases.get(value, value)
    if not value or len(value) > 16 or not all(ch.isalnum() or ch == "-" for ch in value):
        raise ValueError(f"유효한 BCP-47 언어 태그가 아닙니다: {language!r}")
    return value


def estimate(document: SubtitleDocument, batch_size: int) -> dict:
    chars = sum(len(cue.flat_text) for cue in document.cues)
    batches = max(1, (len(document.cues) + batch_size - 1) // batch_size)
    input_tokens = round(chars / 3 + batches * 750)
    # Korean->English E2E (2026-08-16): 10 cues produced 2,027 output tokens
    # from 1,162 actual input tokens, while the old estimate was only 86.
    # Cover both observed ratios: 1.8x input and 210 tokens/cue (rounded up).
    output_tokens = max(round(input_tokens * 1.8), len(document.cues) * 210)
    input_price = float(os.getenv("TVSUB_PRICE_INPUT_PER_MTOK", DEFAULT_INPUT_PRICE_PER_MTOK))
    output_price = float(os.getenv("TVSUB_PRICE_OUTPUT_PER_MTOK", DEFAULT_OUTPUT_PRICE_PER_MTOK))
    usd = input_tokens * input_price / 1_000_000 + output_tokens * output_price / 1_000_000
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "usd": round(usd, 4),
        "price_basis": {
            "input_usd_per_mtok": input_price,
            "output_usd_per_mtok": output_price,
            "note": "Sonnet 기준 기본 추정치이며 환경변수로 최신 가격을 덮어쓸 수 있습니다.",
        },
    }


def estimate_for_backend(document: SubtitleDocument, batch_size: int, backend: str) -> dict:
    value = estimate(document, batch_size)
    if backend != "api":
        value["usd"] = 0.0
        value["price_basis"] = {"note": "구독 포함"}
    return value


def _system_prompt(target: str) -> str:
    return f"""당신은 영화 자막 번역가다. 모든 대사를 목표 언어 {target}로 자연스럽게 번역한다.
- 입력 큐 하나당 출력 큐 하나를 유지하고 id를 절대 바꾸거나 빠뜨리지 않는다.
- 화면에 잠깐 보이는 입말로 간결하게 옮기고, 화자·존대·고유명사를 일관되게 유지한다.
- 원문에 없는 설명을 추가하지 않고 태그·화자 표시·줄바꿈 의도는 보존한다.
- context는 참고만 하고 번역 결과에 포함하지 않는다.
반드시 JSON 스키마에 맞는 결과만 반환한다."""


SCHEMA = {
    "type": "object",
    "properties": {
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "text": {"type": "string"}},
                "required": ["id", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["translations"],
    "additionalProperties": False,
}


def _message(document: SubtitleDocument, start: int, stop: int, context: int, glossary: str) -> str:
    before = document.cues[max(0, start - context):start]
    chunk = document.cues[start:stop]
    after = document.cues[stop:stop + context]
    parts = []
    if glossary.strip():
        parts.append("[용어집]\n" + glossary.strip())
    if before:
        parts.append("[앞 context — 번역하지 말 것]\n" + "\n".join(c.flat_text for c in before))
    parts.append(
        "[번역 대상]\n" + "\n".join(
            f"{index}\t{cue.text.replace(chr(10), r'\n')}"
            for index, cue in enumerate(chunk, start + 1)
        )
    )
    if after:
        parts.append("[뒤 context — 번역하지 말 것]\n" + "\n".join(c.flat_text for c in after))
    return "\n\n".join(parts)


def _source_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def provenance_path(output_path: Path) -> Path:
    return output_path.with_suffix(output_path.suffix + ".provenance.json")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def translate(
    source_path: Path,
    target_language: str,
    output_path: Path | None = None,
    *,
    dry_run: bool = False,
    force: bool = False,
    batch_size: int = 40,
    context: int = 6,
    glossary: str = "",
    line_range: tuple[int, int] | None = None,
    time_range: tuple[float, float] | None = None,
    client_factory: Callable | None = None,
    backend: str | None = None,
    subprocess_run: Callable | None = None,
) -> dict:
    if batch_size < 1 or batch_size > 100:
        raise ValueError("batch_size는 1~100이어야 합니다.")
    tag = language_tag(target_language)
    document = load_subtitle(source_path)
    output_path = output_path or source_path.with_name(f"{source_path.stem}.{tag}.srt")
    metadata_path = output_path.with_suffix(output_path.suffix + ".translation.json")
    source_hash = _source_hash(source_path)
    glossary_file, glossary_payload, glossary_hash = load_glossary(source_path)
    effective_glossary = prompt_glossary(glossary_payload)
    if glossary.strip():
        effective_glossary += ("\n\n[호출 시 추가 지침]\n" + glossary.strip())
    selected = list(range(len(document.cues)))
    if line_range is not None:
        first, last = line_range
        if first < 1 or last < first or last > len(document.cues):
            raise ValueError(f"line_range는 1~{len(document.cues)} 안의 포함 범위여야 합니다.")
        selected = list(range(first - 1, last))
    if time_range is not None:
        start_time, end_time = time_range
        if start_time < 0 or end_time <= start_time:
            raise ValueError("time_range는 0 이상이며 끝이 시작보다 커야 합니다.")
        selected = [i for i, cue in enumerate(document.cues)
                    if cue.start < end_time and cue.end > start_time]
        if not selected:
            raise ValueError("지정 시간과 겹치는 자막 큐가 없습니다.")
    partial = line_range is not None or time_range is not None
    try:
        selected_backend = select_backend(backend, api_key=os.getenv("ANTHROPIC_API_KEY"),
                                          run=subprocess_run or subprocess.run)
    except RuntimeError:
        if not dry_run:
            raise
        # dry-run은 추정만 하므로 백엔드가 없어도 진행한다 (선택 결과만 표시).
        selected_backend = SelectedBackend("unavailable", model="claude-sonnet-5")
    cost_estimate = estimate_for_backend(document, batch_size, selected_backend.name)
    actual_model = selected_backend.model or "unknown"
    common = {
        "source": str(source_path),
        "output": str(output_path),
        "target_language": tag,
        "model": actual_model,
        "backend": selected_backend.name,
        "cue_count": len(document.cues),
        "cost_estimate": cost_estimate,
        "glossary": str(glossary_file) if glossary_payload else None,
        "glossary_applied": glossary_payload is not None or bool(glossary.strip()),
        "partial": partial,
    }
    if dry_run:
        return {**common, "dry_run": True, "api_called": False, "cached": False}

    if not partial and not force and output_path.exists() and metadata_path.exists():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("source_sha256") == source_hash and metadata.get("target_language") == tag \
                and metadata.get("backend", "api") == selected_backend.name \
                and metadata.get("model", MODEL) == actual_model \
                and metadata.get("glossary_sha256") == glossary_hash:
            validate_timeline(document, load_subtitle(output_path))
            return {**common, "dry_run": False, "api_called": False, "cached": True,
                    "actual_usage": metadata.get("actual_usage")}

    client = None
    if selected_backend.name == "api":
        if client_factory is None:
            from anthropic import Anthropic
            client_factory = Anthropic
        client = client_factory()
    if partial:
        if not output_path.exists():
            raise FileNotFoundError("부분 재번역 대상 출력 SRT가 없습니다. 먼저 전체 번역을 실행하세요.")
        existing = load_subtitle(output_path)
        validate_timeline(document, existing)
        translated: dict[int, str] = {i: cue.text for i, cue in enumerate(existing.cues, 1)}
    else:
        translated = {}
    input_tokens = output_tokens = 0
    started = time.monotonic()
    groups: list[tuple[int, int]] = []
    for index in selected:
        if not groups or index != groups[-1][1]:
            groups.append((index, index + 1))
        else:
            groups[-1] = (groups[-1][0], index + 1)
    batches = [(start, min(stop, start + batch_size)) for group_start, group_stop in groups
               for start in range(group_start, group_stop, batch_size)
               for stop in [group_stop]]
    for start, stop in batches:
        expected = set(range(start + 1, stop + 1))
        last_error: Exception | None = None
        for attempt in range(3 if selected_backend.name == "api" else 1):
            try:
                if selected_backend.name == "api":
                    response = client.messages.create(
                        model=MODEL, max_tokens=16000, system=_system_prompt(tag),
                        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
                        messages=[{"role": "user", "content": _message(document, start, stop, context, effective_glossary)}],
                    )
                    text_block = next(block.text for block in response.content if block.type == "text")
                    payload = json.loads(text_block)
                    input_tokens += int(response.usage.input_tokens)
                    output_tokens += int(response.usage.output_tokens)
                else:
                    prompt = cli_prompt(_system_prompt(tag), _message(document, start, stop, context, effective_glossary))
                    payload, detected_model = run_cli_batch(selected_backend, prompt, run=subprocess_run or subprocess.run)
                    if detected_model:
                        actual_model = detected_model
                got = {int(item["id"]): item["text"] for item in payload["translations"]}
                if set(got) != expected:
                    raise ValueError(f"큐 id 불일치: 기대 {sorted(expected)}, 실제 {sorted(got)}")
                translated.update(got)
                break
            except Exception as exc:  # API/shape failures are retried as one batch.
                last_error = exc
                if attempt == (2 if selected_backend.name == "api" else 0):
                    detail = "3회" if selected_backend.name == "api" else "실행/검증 중"
                    raise RuntimeError(f"번역 배치 {start + 1}~{stop}가 {detail} 실패했습니다: {exc}") from exc
                time.sleep(attempt + 1)
        if last_error is not None and not expected.issubset(translated):
            raise last_error

    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_srt(document.cues, translated, output_path)
    output_document = load_subtitle(output_path)
    validate_timeline(document, output_document)
    input_price = cost_estimate["price_basis"].get("input_usd_per_mtok", 0)
    output_price = cost_estimate["price_basis"].get("output_usd_per_mtok", 0)
    actual_usage = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_usd": round(input_tokens * input_price / 1_000_000 + output_tokens * output_price / 1_000_000, 4),
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }
    metadata = {
        "schema_version": 1,
        "status": "ai_draft",
        "source_sha256": source_hash,
        "glossary_sha256": glossary_hash,
        "glossary_path": glossary_file.name if glossary_payload else None,
        "target_language": tag,
        "model": actual_model,
        "backend": selected_backend.name,
        "actual_model": actual_model,
        "cue_count": len(document.cues),
        "created_at": _utc_now(),
        "translation_parameters": {"batch_size": batch_size, "context": context},
        "actual_usage": actual_usage,
        "partial_updates": [],
    }
    prov_path = provenance_path(output_path)
    if partial and prov_path.exists():
        previous = json.loads(prov_path.read_text(encoding="utf-8"))
        metadata["created_at"] = previous.get("created_at", metadata["created_at"])
        metadata["partial_updates"] = list(previous.get("partial_updates", []))
        metadata["partial_updates"].append({
            "at": _utc_now(),
            "line_range": list(line_range) if line_range else None,
            "time_range": list(time_range) if time_range else None,
            "cue_ids": [i + 1 for i in selected],
            "glossary_sha256": glossary_hash,
        })
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    prov_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**common, "dry_run": False, "api_called": True, "cached": False,
            "timeline_verified": True, "actual_usage": actual_usage,
            "provenance": str(prov_path), "status": "ai_draft"}
