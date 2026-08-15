from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .service import TvsubService


def _load_env(env_path: Path) -> None:
    """Load only ANTHROPIC_API_KEY from the MCP project root's explicit .env file."""
    if not env_path.is_file() or "ANTHROPIC_API_KEY" in os.environ:
        return
    for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "ANTHROPIC_API_KEY":
            os.environ[key.strip()] = value.strip().strip("'\"")
            return


def build_server(service: TvsubService) -> FastMCP:
    mcp = FastMCP(
        "tvsub",
        instructions=(
            "Apple TV.app의 현재 재생 항목을 확인하고 자막 선택·LLM 번역·오버레이·폰트·싱크를 제어합니다. "
            "TV.app을 직접 실행하지 말고, 사용자가 재생을 시작한 뒤 도구를 호출하세요."
        ),
    )

    @mcp.tool(description="MediaRemote/JXA와 TV.app AppleScript 교차 검증으로 현재 제목, 콘텐츠 ID, 위치와 재생 상태를 조회합니다.")
    def now_playing() -> dict:
        return service.now_playing()

    @mcp.tool(description="tvsub subtitles/ 라이브러리의 SRT/SMI/VTT 파일과 파싱 정보를 나열합니다.")
    def list_subtitles() -> dict:
        return service.list_subtitles()

    @mcp.tool(description="콘텐츠에 사용할 자막을 선택합니다. 기존 싱크 앵커는 기본적으로 보존합니다.")
    def load_subtitle(subtitle: str, store_id: str = "", reset_anchors: bool = False) -> dict:
        return service.load_subtitle(subtitle, store_id, reset_anchors)

    @mcp.tool(description="Claude Sonnet 5로 자막을 임의 언어로 번역합니다. 타임코드·큐 수를 검증하고 캐시합니다.")
    def translate_subtitle(
        subtitle: str,
        target_language: str = "ko",
        dry_run: bool = False,
        force: bool = False,
        batch_size: int = 40,
        glossary: str = "",
    ) -> dict:
        return service.translate_subtitle(subtitle, target_language, dry_run, force, batch_size, glossary)

    @mcp.tool(description=(
        "tvsub NSPanel 오버레이를 선택 자막·스타일로 시작합니다. "
        "bottom_margin은 TV 영상 창 높이 기준이며 창을 찾지 못할 때만 설정 화면 기준으로 폴백합니다."
    ))
    def start_overlay(
        subtitle: str = "",
        store_id: str = "",
        font_family: str = "",
        font_size: float = 0,
        color: str = "",
        outline_width: float = -1,
        bottom_margin: float = -1,
    ) -> dict:
        return service.start_overlay(subtitle, store_id, font_family, font_size, color,
                                     outline_width, bottom_margin)

    @mcp.tool(description="이 MCP 서버가 시작한 tvsub 오버레이 프로세스를 정상 종료합니다.")
    def stop_overlay() -> dict:
        return service.stop_overlay()

    @mcp.tool(description="macOS CoreText 설치 폰트를 열거하고 목표 언어 샘플 글리프 지원 여부를 확인합니다.")
    def list_fonts(language: str = "ko") -> dict:
        return service.list_fonts(language)

    @mcp.tool(description=(
        "폰트·크기·색·외곽선·TV 영상 창 하단 위치를 저장하고 실행 중 오버레이에 즉시 반영합니다. "
        "outline_width는 pt가 아니라 **폰트 크기 대비 퍼센트**입니다(5 = 5%). 4~7이 실용 범위이고, "
        "10을 넘기면 글자 속까지 외곽선 색으로 덮여 읽기 어려워집니다. "
        "bottom_margin은 TV 영상 창 높이 대비 비율(0.075 = 7.5%)이며 창 취득 실패 때만 화면 기준입니다."
    ))
    def set_style(
        font_family: str = "",
        font_size: float = 0,
        color: str = "",
        outline_color: str = "",
        outline_width: float = -1,
        bottom_margin: float = -1,
        background_alpha: float = -1,
        max_lines: int = 0,
        language: str = "ko",
    ) -> dict:
        return service.set_style(
            font_family=font_family, font_size=font_size, color=color,
            outline_color=outline_color, outline_width=outline_width,
            bottom_margin=bottom_margin, background_alpha=background_alpha,
            max_lines=max_lines, language=language,
        )

    @mcp.tool(description=(
        "현재 대사와 일시정지 위치로 영화별 싱크 앵커를 기록하고 2점 scale+offset을 계산합니다. "
        "권장 경로는 spoken_text(지금 들린 대사 일부)입니다. "
        "⚠ subtitle_time은 **자막 파일 원본 시각**이며, tvsub CLI의 `anchor --raw-sub-time`과 같은 의미입니다. "
        "화면에 보인 시각(보정 적용 후)을 넣는 `anchor --sub-time`과 다르므로, "
        "이미 offset이 걸린 상태에서 화면 시각을 넣으면 앵커가 offset만큼 틀어집니다."
    ))
    def calibrate_sync(
        spoken_text: str,
        subtitle_time: float = -1,
        store_id: str = "",
        actual_time: float = -1,
    ) -> dict:
        return service.calibrate_sync(spoken_text, subtitle_time, store_id, actual_time)

    @mcp.tool(description=(
        "오버레이, 재생 항목, 선택 자막, 싱크 보정, 스타일과 창 기준 위치 추적 계약을 한 번에 요약합니다."
    ))
    def status() -> dict:
        return service.status()

    return mcp


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    default_root = Path(__file__).resolve().parents[2] / "appletv-subtitle-poc"
    parser = argparse.ArgumentParser(description="tvsub stdio MCP server")
    parser.add_argument("--tvsub-root", type=Path, default=Path(os.getenv("TVSUB_ROOT", default_root)))
    parser.add_argument("--mock", action="store_true", default=os.getenv("TVSUB_MCP_MOCK") == "1")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    project_root = Path(__file__).resolve().parent.parent
    _load_env(project_root / ".env")
    service = TvsubService(args.tvsub_root, mock=args.mock)
    print(f"[tvsub-mcp] mock={service.mock}", file=sys.stderr, flush=True)
    build_server(service).run(transport="stdio")


if __name__ == "__main__":
    main()
