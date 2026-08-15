from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Callable

from .subtitles import SubtitleDocument, load_subtitle, validate_timeline, write_srt

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
    output_tokens = round(chars / 2)
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
    client_factory: Callable | None = None,
) -> dict:
    if batch_size < 1 or batch_size > 100:
        raise ValueError("batch_size는 1~100이어야 합니다.")
    tag = language_tag(target_language)
    document = load_subtitle(source_path)
    output_path = output_path or source_path.with_name(f"{source_path.stem}.{tag}.srt")
    metadata_path = output_path.with_suffix(output_path.suffix + ".translation.json")
    source_hash = _source_hash(source_path)
    cost_estimate = estimate(document, batch_size)
    common = {
        "source": str(source_path),
        "output": str(output_path),
        "target_language": tag,
        "model": MODEL,
        "cue_count": len(document.cues),
        "cost_estimate": cost_estimate,
    }
    if dry_run:
        return {**common, "dry_run": True, "api_called": False, "cached": False}

    if not force and output_path.exists() and metadata_path.exists():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("source_sha256") == source_hash and metadata.get("target_language") == tag \
                and metadata.get("model") == MODEL:
            validate_timeline(document, load_subtitle(output_path))
            return {**common, "dry_run": False, "api_called": False, "cached": True,
                    "actual_usage": metadata.get("actual_usage")}

    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY가 없습니다. 실행 환경 또는 tvsub MCP 프로젝트 루트의 .env에 설정하세요."
        )
    if client_factory is None:
        from anthropic import Anthropic
        client_factory = Anthropic
    client = client_factory()
    translated: dict[int, str] = {}
    input_tokens = output_tokens = 0
    started = time.monotonic()
    for start in range(0, len(document.cues), batch_size):
        stop = min(len(document.cues), start + batch_size)
        expected = set(range(start + 1, stop + 1))
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = client.messages.create(
                    model=MODEL,
                    max_tokens=16000,
                    system=_system_prompt(tag),
                    output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
                    messages=[{"role": "user", "content": _message(document, start, stop, context, glossary)}],
                )
                text_block = next(block.text for block in response.content if block.type == "text")
                payload = json.loads(text_block)
                got = {int(item["id"]): item["text"] for item in payload["translations"]}
                if set(got) != expected:
                    raise ValueError(f"큐 id 불일치: 기대 {sorted(expected)}, 실제 {sorted(got)}")
                translated.update(got)
                input_tokens += int(response.usage.input_tokens)
                output_tokens += int(response.usage.output_tokens)
                break
            except Exception as exc:  # API/shape failures are retried as one batch.
                last_error = exc
                if attempt == 2:
                    raise RuntimeError(f"번역 배치 {start + 1}~{stop}가 3회 실패했습니다: {exc}") from exc
                time.sleep(attempt + 1)
        if last_error is not None and not expected.issubset(translated):
            raise last_error

    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_srt(document.cues, translated, output_path)
    output_document = load_subtitle(output_path)
    validate_timeline(document, output_document)
    input_price = cost_estimate["price_basis"]["input_usd_per_mtok"]
    output_price = cost_estimate["price_basis"]["output_usd_per_mtok"]
    actual_usage = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_usd": round(input_tokens * input_price / 1_000_000 + output_tokens * output_price / 1_000_000, 4),
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }
    metadata = {
        "source_sha256": source_hash,
        "target_language": tag,
        "model": MODEL,
        "cue_count": len(document.cues),
        "actual_usage": actual_usage,
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**common, "dry_run": False, "api_called": True, "cached": False,
            "timeline_verified": True, "actual_usage": actual_usage}
