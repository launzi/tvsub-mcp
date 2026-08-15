from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from pathlib import Path

TIME_RE = re.compile(
    r"(?P<a>\d{1,2}:\d{1,2}:\d{1,2}[,.]\d{1,3})\s*-->\s*"
    r"(?P<b>\d{1,2}:\d{1,2}:\d{1,2}[,.]\d{1,3})"
)
SYNC_RE = re.compile(r"<sync\s+start\s*=\s*[\"']?(-?\d+)[\"']?[^>]*>", re.I)
P_RE = re.compile(r"<p\b([^>]*)>", re.I)
CLASS_RE = re.compile(r"class\s*=\s*[\"']?([A-Za-z0-9_.-]+)", re.I)


@dataclass(slots=True)
class Cue:
    start: float
    end: float
    lines: list[str]
    timecode: str

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @property
    def flat_text(self) -> str:
        return " ".join(self.lines)


@dataclass(slots=True)
class SubtitleDocument:
    path: Path
    format: str
    encoding: str
    cues: list[Cue]
    classes_found: list[str] = field(default_factory=list)
    selected_class: str | None = None

    def info(self, display_path: str | None = None) -> dict:
        first = self.cues[0]
        last = self.cues[-1]
        return {
            "path": display_path if display_path is not None else self.path.name,
            "filename": self.path.name,
            "format": self.format.upper(),
            "encoding": self.encoding,
            "cue_count": len(self.cues),
            "span": {
                "start_ms": round(first.start * 1000),
                "end_ms": round(last.end * 1000),
                "duration_ms": round((last.end - first.start) * 1000),
            },
            "classes_found": self.classes_found,
            "selected_class": self.selected_class,
        }


def decode_subtitle(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    candidates = [
        ("utf-8-sig", "UTF-8 (BOM)" if raw.startswith(b"\xef\xbb\xbf") else "UTF-8"),
        ("utf-8", "UTF-8"),
        ("cp949", "CP949"),
        ("euc-kr", "EUC-KR"),
        ("shift_jis", "Shift_JIS"),
        ("latin-1", "ISO-8859-1 (추정)"),
    ]
    for codec, label in candidates:
        try:
            return raw.decode(codec), label
        except UnicodeDecodeError:
            continue
    raise ValueError(f"인코딩을 판별하지 못했습니다: {path}")


def _seconds(value: str) -> float:
    hh, mm, tail = value.replace(",", ".").split(":")
    return int(hh) * 3600 + int(mm) * 60 + float(tail)


def _timecode(seconds: float) -> str:
    ms = max(0, round(seconds * 1000))
    return (
        f"{ms // 3_600_000:02d}:{(ms // 60_000) % 60:02d}:"
        f"{(ms // 1000) % 60:02d},{ms % 1000:03d}"
    )


def _clean_markup(value: str) -> list[str]:
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]*>", "", value)
    value = html.unescape(value).replace("\xa0", " ").replace("\r", "")
    return [line.strip() for line in value.split("\n") if line.strip()]


def parse_srt(text: str, path: Path, encoding: str) -> SubtitleDocument:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cues: list[Cue] = []
    i = 0
    while i < len(lines):
        match = TIME_RE.search(lines[i])
        if not match:
            i += 1
            continue
        tc = lines[i].strip()
        start, end = _seconds(match.group("a")), _seconds(match.group("b"))
        i += 1
        body: list[str] = []
        while i < len(lines) and lines[i].strip() and not TIME_RE.search(lines[i]):
            body.append(lines[i].rstrip())
            i += 1
        # 끝에 붙은 인덱스 숫자 줄 제거 — 빈 줄 없이 다음 큐 타임코드가 바로 붙은 경우에만.
        # 빈 줄로 정상 종료된 블록의 마지막 숫자 줄은 대사다("1995" 등). tvsub 코어 parseSRT 와 동일 규칙.
        stopped_at_timecode = i < len(lines) and bool(TIME_RE.search(lines[i]))
        if stopped_at_timecode and len(body) > 1 and body[-1].strip().isdigit():
            body.pop()
        cleaned = _clean_markup("\n".join(body))
        if cleaned and end > start:
            cues.append(Cue(start, end, cleaned, tc))
    if not cues:
        raise ValueError(f"SRT 큐를 하나도 찾지 못했습니다: {path}")
    return SubtitleDocument(path, "srt", encoding, sorted(cues, key=lambda cue: cue.start))


def parse_smi(
    text: str, path: Path, encoding: str, preferred_class: str | None = None
) -> SubtitleDocument:
    syncs = list(SYNC_RE.finditer(text))
    if not syncs:
        raise ValueError(f"SMI <SYNC> 태그를 찾지 못했습니다: {path}")

    segments: list[dict] = []
    class_count: dict[str, int] = {}
    for index, match in enumerate(syncs):
        body_start = match.end()
        body_end = syncs[index + 1].start() if index + 1 < len(syncs) else len(text)
        body = text[body_start:body_end]
        p_tags = list(P_RE.finditer(body))
        segment = {"start": float(match.group(1)) / 1000, "classes": {}, "fallback": []}
        if not p_tags:
            segment["fallback"] = _clean_markup(body)
        for p_index, p_match in enumerate(p_tags):
            chunk_start = p_match.end()
            chunk_end = p_tags[p_index + 1].start() if p_index + 1 < len(p_tags) else len(body)
            content = _clean_markup(body[chunk_start:chunk_end])
            class_match = CLASS_RE.search(p_match.group(1))
            if class_match:
                class_name = class_match.group(1).upper()
                segment["classes"][class_name] = content
                if content:
                    class_count[class_name] = class_count.get(class_name, 0) + 1
            else:
                segment["fallback"] = content
        segments.append(segment)

    selected = preferred_class.upper() if preferred_class else None
    if selected not in class_count:
        selected = None
    if selected is None:
        korean = {k: v for k, v in class_count.items() if k.startswith(("KR", "KO"))}
        pool = korean or class_count
        selected = max(pool, key=pool.get) if pool else None
    multi_class = len(class_count) > 1

    def belongs(segment: dict) -> bool:
        if selected and selected in segment["classes"]:
            return True
        if segment["fallback"]:
            return True
        return not multi_class and len(segment["classes"]) == 1

    next_same = [-1] * len(segments)
    next_index = -1
    for index in range(len(segments) - 1, -1, -1):
        next_same[index] = next_index
        if belongs(segments[index]):
            next_index = index

    cues: list[Cue] = []
    for index, segment in enumerate(segments):
        if not belongs(segment):
            continue
        lines = segment["classes"].get(selected, []) if selected else []
        if not lines:
            lines = segment["fallback"]
        if not lines and not multi_class and len(segment["classes"]) == 1:
            lines = next(iter(segment["classes"].values()))
        lines = [line for line in lines if line.strip()]
        if not lines:
            continue
        start = segment["start"]
        nxt = next_same[index]
        end = segments[nxt]["start"] if nxt >= 0 and segments[nxt]["start"] > start else start + 4
        cues.append(Cue(start, end, lines, f"{_timecode(start)} --> {_timecode(end)}"))
    if not cues:
        raise ValueError(f"선택한 SMI 트랙에 큐가 없습니다: {path}")
    return SubtitleDocument(
        path,
        "smi",
        encoding,
        sorted(cues, key=lambda cue: cue.start),
        sorted(class_count),
        selected,
    )


def load_subtitle(path: str | Path, preferred_class: str | None = None) -> SubtitleDocument:
    source = Path(path).expanduser().resolve()
    text, encoding = decode_subtitle(source)
    suffix = source.suffix.lower()
    head = text[:2000].lower()
    if suffix in {".smi", ".sami"} or "<sami" in head or "<sync" in head:
        return parse_smi(text, source, encoding, preferred_class)
    return parse_srt(text, source, encoding)


def write_srt(cues: list[Cue], texts: dict[int, str], path: str | Path) -> None:
    blocks: list[str] = []
    for index, cue in enumerate(cues, 1):
        translated = texts[index].replace("\\n", "\n").strip()
        blocks.append(f"{index}\n{cue.timecode}\n{translated}\n")
    Path(path).write_text("\n".join(blocks), encoding="utf-8")


def validate_timeline(source: SubtitleDocument, output: SubtitleDocument) -> None:
    if len(source.cues) != len(output.cues):
        raise ValueError(f"큐 수 불일치: 입력 {len(source.cues)}, 출력 {len(output.cues)}")
    for index, (left, right) in enumerate(zip(source.cues, output.cues), 1):
        if left.timecode != right.timecode:
            raise ValueError(
                f"타임코드 변경 감지(큐 {index}): {left.timecode!r} != {right.timecode!r}"
            )
