from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tvsub_mcp.subtitles import load_subtitle, validate_timeline, write_srt


SRT = """1
00:00:01,000 --> 00:00:02,500
<i>Hello</i>

2
00:01:05.25 --> 00:01:07.750
Second line

"""


class SubtitleTests(unittest.TestCase):
    def test_srt_preserves_timecodes_and_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "sample.srt"
            output = Path(temp) / "sample.ko.srt"
            source.write_text(SRT, encoding="utf-8")
            document = load_subtitle(source)
            self.assertEqual(document.encoding, "UTF-8")
            self.assertEqual(document.format, "srt")
            self.assertEqual(len(document.cues), 2)
            self.assertEqual(document.cues[0].timecode, "00:00:01,000 --> 00:00:02,500")
            self.assertEqual(document.cues[0].text, "Hello")
            write_srt(document.cues, {1: "안녕", 2: "둘째 줄"}, output)
            translated = load_subtitle(output)
            validate_timeline(document, translated)

    def test_cp949_smi_selects_korean_track(self) -> None:
        smi = """<SAMI><BODY>
<SYNC Start=1000><P Class=KRCC>첫째<br>줄
<SYNC Start=2000><P Class=KRCC>&nbsp;
<SYNC Start=1000><P Class=ENCC>First line
<SYNC Start=2000><P Class=ENCC>&nbsp;
</BODY></SAMI>"""
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "sample.smi"
            source.write_bytes(smi.encode("cp949"))
            document = load_subtitle(source)
            self.assertEqual(document.encoding, "CP949")
            self.assertEqual(document.selected_class, "KRCC")
            self.assertEqual(len(document.cues), 1)
            self.assertEqual(document.cues[0].lines, ["첫째", "줄"])
            self.assertEqual(document.cues[0].end, 2.0)


if __name__ == "__main__":
    unittest.main()

