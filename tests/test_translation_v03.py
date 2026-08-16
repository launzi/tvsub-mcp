from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tvsub_mcp.subtitles import load_subtitle
from tvsub_mcp.translation import estimate, translate


SRT = """1
00:00:01,000 --> 00:00:02,000
하나

2
00:00:03,000 --> 00:00:04,000
둘

3
00:00:05,000 --> 00:00:06,000
셋

"""


class FakeMessages:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        target = kwargs["messages"][0]["content"].split("[번역 대상]\n", 1)[1].split("\n\n", 1)[0]
        ids = [int(line.split("\t", 1)[0]) for line in target.splitlines() if "\t" in line]
        payload = {"translations": [{"id": value, "text": f"translated-{value}"} for value in ids]}
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(payload))],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )


class TranslationV03Tests(unittest.TestCase):
    def test_cost_estimate_uses_conservative_output_ratio(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "sample.srt"
            source.write_text(SRT, encoding="utf-8")
            value = estimate(load_subtitle(source), batch_size=40)
            self.assertEqual(value["output_tokens"], max(
                round(value["input_tokens"] * 1.8), 3 * 210,
            ))

    def test_glossary_provenance_and_partial_merge(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"ANTHROPIC_API_KEY": "fixture"}):
            root = Path(temp)
            source = root / "drama.ko.srt"
            output = root / "drama.en.srt"
            source.write_text(SRT, encoding="utf-8")
            (root / "drama.glossary.json").write_text(json.dumps({
                "names": {"하나": "Hana"}, "terms": {}, "relationships": [],
                "forbidden_translations": [], "notes": [],
            }, ensure_ascii=False), encoding="utf-8")
            fake = FakeMessages()
            first = translate(source, "en", output, client_factory=lambda: SimpleNamespace(messages=fake))
            self.assertTrue(first["glossary_applied"])
            provenance = json.loads((root / "drama.en.srt.provenance.json").read_text())
            self.assertEqual(provenance["status"], "ai_draft")
            self.assertIsNotNone(provenance["glossary_sha256"])
            self.assertIn("Hana", fake.calls[0]["messages"][0]["content"])

            before = output.read_text(encoding="utf-8")
            partial = translate(source, "en", output, line_range=(2, 2), force=True,
                                client_factory=lambda: SimpleNamespace(messages=fake))
            after = output.read_text(encoding="utf-8")
            self.assertTrue(partial["partial"])
            self.assertIn("translated-1", after)
            self.assertIn("translated-3", after)
            self.assertEqual(before, after)  # fake emits the same line 2; untouched cues remain byte-stable.
            provenance = json.loads((root / "drama.en.srt.provenance.json").read_text())
            self.assertEqual(provenance["partial_updates"][0]["cue_ids"], [2])


if __name__ == "__main__":
    unittest.main()
