# tvsub MCP

<!-- mcp-name: io.github.launzi/tvsub-mcp -->

`tvsub-mcp` is the MCP companion for [tvsub](https://youngji.kim/tvsub), an
experimental subtitle overlay for Apple TV.app on macOS. It lets an MCP client
inspect the current playback item, choose or translate a subtitle file, adjust
its appearance, start or stop the overlay, and calibrate subtitle timing.

The server does not download subtitles, bypass DRM, modify video, or launch
TV.app. You provide subtitle files that you have the right to use and start
playback yourself.

## Requirements

- macOS
- [tvsub](https://youngji.kim/tvsub), installed and built
- Python 3.12 or later
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/) for the
  recommended `uvx` installation
- An Anthropic API key only if you use subtitle translation

## Install and register with Claude Code

Replace `/absolute/path/to/tvsub` with the directory containing tvsub's
`build/`, `config/`, `src/`, and `subtitles/` directories.

```bash
brew install uv

claude mcp add --transport stdio --scope user tvsub -- \
  uvx tvsub-mcp==0.1.0 \
  --tvsub-root /absolute/path/to/tvsub

claude mcp get tvsub
claude mcp list
```

To enable translation, export your key and include it when registering the
server. The key is not needed for any other tool.

```bash
export ANTHROPIC_API_KEY="your-key"

claude mcp add --transport stdio --scope user \
  --env ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  tvsub -- uvx tvsub-mcp==0.1.0 \
  --tvsub-root /absolute/path/to/tvsub
```

Other stdio MCP clients can launch the same command:

```bash
uvx tvsub-mcp==0.1.0 --tvsub-root /absolute/path/to/tvsub
```

## Tools

| Tool | Purpose |
| --- | --- |
| `now_playing` | Read the current Apple TV.app title, content ID, position, and playback state. |
| `list_subtitles` | List and parse SRT, SMI, SAMI, and VTT files in tvsub's subtitle library. |
| `load_subtitle` | Select a subtitle file for the current content while preserving sync anchors by default. |
| `translate_subtitle` | Estimate or perform an LLM translation with cue and timecode validation. |
| `list_fonts` | List installed macOS fonts and check sample glyph coverage. |
| `set_style` | Change font, size, colors, outline, background, and screen position. |
| `start_overlay` | Start tvsub with the selected subtitle and style. |
| `stop_overlay` | Stop only the overlay process started by this server. |
| `calibrate_sync` | Store one or more dialogue anchors and calculate timing offset and drift. |
| `status` | Summarize playback, overlay, subtitle, style, and calibration state. |

Before translating, call `translate_subtitle` with `dry_run=true` to review the
cue count, batch count, and estimated cost.

## Important notices

- **Experimental software:** expect rough edges and breaking changes. Keep a
  backup of your tvsub configuration and subtitle files.
- **Data sent to Anthropic:** translation sends the selected subtitle text and
  surrounding subtitle context to the Anthropic API. Loading, styling, sync,
  and overlay controls do not send subtitle text to Anthropic.
- **User-paid API usage:** translation uses your Anthropic API key. All API
  charges are your responsibility; estimates can differ from the final bill.
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

