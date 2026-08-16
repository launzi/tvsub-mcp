# tvsub MCP

<!-- mcp-name: io.github.launzi/tvsub-mcp -->

`tvsub-mcp` is the MCP companion for [tvsub](https://youngji.kim/tvsub), an
experimental subtitle overlay for Apple TV.app on macOS. It lets an MCP client
inspect the current playback item, choose or translate a subtitle file, adjust
its appearance, start or stop the overlay, and calibrate subtitle timing.

Supported player: purchased and rented films in macOS Apple TV.app
(Prime Video support is being explored and is not currently available).
Subtitle formats: SRT, SMI/SAMI, VTT. Translation runs on your choice of
three backends: an Anthropic API key, a signed-in Claude Code CLI
(Claude subscription), or a signed-in Codex CLI (ChatGPT subscription) —
subscription backends add no API charges.

The server does not download subtitles, bypass DRM, modify video, or launch
TV.app. You provide subtitle files that you have the right to use and start
playback yourself.

## Two directions, one workflow

If a foreign, classic, or multilingual film you purchased in Apple TV.app does not include Korean—or another language you need—bring a lawfully obtained subtitle file, translate it with your Claude or ChatGPT subscription, and display it as an overlay. The same workflow works in the other direction: viewers worldwide can translate lawfully obtained subtitles for Korean films and series into their own language.

Apple TV.app에서 구매한 외화·고전·다국어 영화에 한국어 또는 원하는 언어 자막이 없다면, 정당하게 보유한 자막 파일을 불러와 Claude나 ChatGPT 구독으로 번역한 뒤 오버레이로 표시할 수 있습니다. 같은 방식으로 전 세계 시청자도 한국 영화와 시리즈의 정당하게 보유한 자막을 자신의 언어로 번역해 시청할 수 있습니다.

## Requirements

- macOS
- [tvsub](https://youngji.kim/tvsub), installed and built
- Python 3.12 or later
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/) for the
  recommended `uvx` installation
- For subtitle translation, one of: an Anthropic API key, a signed-in
  Claude Code CLI (Claude subscription), or a signed-in Codex CLI
  (ChatGPT subscription). No key or CLI is needed for any other tool

## Install and register with Claude Code

Replace `/absolute/path/to/tvsub` with the directory containing tvsub's
`build/`, `config/`, `src/`, and `subtitles/` directories.

```bash
brew install uv

claude mcp add --transport stdio --scope user tvsub -- \
  uvx tvsub-mcp==0.2.1 \
  --tvsub-root /absolute/path/to/tvsub

claude mcp get tvsub
claude mcp list
```

Translation picks a backend automatically: an Anthropic API key if present,
then a signed-in Claude Code CLI, then a signed-in Codex CLI. Set
`TVSUB_TRANSLATE_BACKEND` (`auto`, `api`, `claude`, `codex`) or the
`backend` tool argument to override. With a subscription CLI signed in you
can skip the key entirely. To use the API backend, export your key and
include it when registering the server — or store it once in macOS Keychain
(service `kim.youngji.tvsub.anthropic`), which the server also reads.

```bash
export ANTHROPIC_API_KEY="your-key"

claude mcp add --transport stdio --scope user \
  --env ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  tvsub -- uvx tvsub-mcp==0.2.1 \
  --tvsub-root /absolute/path/to/tvsub
```

Other stdio MCP clients can launch the same command:

```bash
uvx tvsub-mcp==0.2.1 --tvsub-root /absolute/path/to/tvsub
```

## Tools

| Tool | Purpose |
| --- | --- |
| `now_playing` | Read the current Apple TV.app title, content ID, position, and playback state. |
| `list_subtitles` | List and parse SRT, SMI, SAMI, and VTT files in tvsub's subtitle library. |
| `load_subtitle` | Select a subtitle file for the current content while preserving sync anchors by default. |
| `translate_subtitle` | Estimate or perform an LLM translation with cue and timecode validation. Supports backend selection, glossary injection, and partial retranslation by line or time range. |
| `set_glossary` | Create or update a per-title glossary (names, honorifics, relationships, forbidden translations) that is injected into translation prompts. |
| `mark_reviewed` | Promote a translated subtitle's provenance from `ai_draft` to `user_reviewed`. |
| `list_fonts` | List installed macOS fonts and check sample glyph coverage. |
| `set_style` | Change font, size, colors, outline, background, and screen position. |
| `start_overlay` | Start tvsub with the selected subtitle and style. |
| `stop_overlay` | Stop only the overlay process started by this server. |
| `calibrate_sync` | Store one or more dialogue anchors and calculate timing offset and drift. |
| `status` | Summarize playback, overlay, subtitle, style, and calibration state. |

Before translating, call `translate_subtitle` with `dry_run=true` to review the
cue count, batch count, and estimated cost. Subscription backends report
`$0 (included in subscription)`. Every translation writes a `.provenance.json`
sidecar recording backend, hashes, and review status.

## Important notices

- **Experimental software:** expect rough edges and breaking changes. Keep a
  backup of your tvsub configuration and subtitle files.
- **Data sent to Anthropic:** translation sends the selected subtitle text and
  surrounding subtitle context to the Anthropic API. Loading, styling, sync,
  and overlay controls do not send subtitle text to Anthropic.
- **User-paid API usage:** the `api` backend uses your Anthropic API key and
  all charges are your responsibility; estimates can differ from the final
  bill. The `claude` and `codex` backends run through your own signed-in
  subscription CLIs and add no API charges.
- **Private API risk:** tvsub reads Apple playback state through undocumented
  macOS MediaRemote interfaces. Apple does not support this integration and a
  macOS update may change or disable it.
- **Content rights:** you are responsible for having the right to process and
  translate subtitle files. Do not redistribute protected content without
  permission.
- This project is independent from and not affiliated with Apple or Anthropic.

## Development

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
TVSUB_TEST_PYTHON="$PWD/.venv/bin/python" .venv/bin/python tests/stdio_smoke.py
bash scripts/hygiene-check.sh
```

Linux can run the unit tests and mock stdio smoke test. Apple TV.app,
MediaRemote, CoreText, and the real overlay require macOS.

## License

MIT. See [LICENSE](LICENSE).

