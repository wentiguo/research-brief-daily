# -*- coding: utf-8 -*-
"""The bridge manifest must never become a mail attachment (2026-10-10).

Two different failures came out of one line, `offer(manifest_path)`:

1. The reader was mailed a file called manifest.json. It records which route answered
   and which hash was verified - bookkeeping for whoever operates the bridge, and in
   the mailbox a file that tells the recipient nothing at all.
2. Far worse: it made `reuse_synced_attachments` return a non-empty paper list on a day
   whose real PDFs had not arrived yet. `_wait_and_reuse` reads "got something" as its
   own success condition, so it skipped every further poll for the local bridge and
   mailed the brief straight away. That is why the 3600 s wait window went unused on
   2026-10-09: the manifest counted as the missing PDF.
"""
import datetime as dt
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fetch_paper_attachments as fpa  # noqa: E402

STAMP = "2026-10-10"
DOI = "10.1038/demo-paper"
PDF_NAME = "demo_fulltext.pdf"
CARD_NAME = f"{STAMP}_题录与获取指引.txt"


def _build(tmp: Path, *, with_pdf: bool = True, with_card: bool = True) -> Path:
    """Create a dated attachment folder shaped like the local bridge writes it."""
    out_dir = tmp / "research_briefs" / "attachments" / STAMP
    out_dir.mkdir(parents=True, exist_ok=True)
    files = []
    if with_pdf:
        (out_dir / PDF_NAME).write_bytes(b"%PDF-1.4\n" + b"0" * 40_000)
        files.append({"label": f"1_demo_paper", "name": PDF_NAME,
                      "doi": DOI, "kind": "fulltext", "bytes": 40_010})
    (out_dir / "manifest.json").write_text(
        json.dumps({"date": STAMP, "files": files}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    if with_card:
        (out_dir / CARD_NAME).write_text("citation routes\n", encoding="utf-8")
    return out_dir


def _reuse(tmp: Path):
    options = {"dir": str(tmp / "research_briefs" / "attachments")}
    return fpa.reuse_synced_attachments(
        dt.date.fromisoformat(STAMP), {"paper_attachments": options}, note="",
        papers=[{"doi": DOI, "title": "A demo article"}],
    )


class ManifestNotMailedTests(unittest.TestCase):
    def test_manifest_and_guide_are_never_attachments(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            _build(tmp)
            pairs, cards, _note = _reuse(tmp)
            names = [p.name for _l, p in pairs]
            self.assertIn(PDF_NAME, names, "the real full text must still be attached")
            self.assertNotIn("manifest.json", names,
                             "manifest.json is operator bookkeeping, not reading material")
            self.assertTrue(all(not n.startswith(STAMP) or n == PDF_NAME for n in names),
                            f"unexpected dated file among attachments: {names}")
            self.assertEqual([c.name for c in cards], [CARD_NAME])

    def test_no_real_pdf_means_no_papers_so_the_callers_keeps_polling(self):
        """The day whose PDFs have not arrived must NOT look satisfied.

        With the card present but no full text, the old code still reported one
        "reused attachment" (the manifest) and `_wait_and_reuse` stopped waiting.
        """
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            _build(tmp, with_pdf=False)
            pairs, cards, _note = _reuse(tmp)
            self.assertEqual([p.name for _l, p in pairs], [],
                             "a manifest with no full text must yield zero paper files, "
                             "otherwise the cloud stops waiting for the local bridge")
            self.assertEqual([c.name for c in cards], [CARD_NAME])

    def test_nothing_at_all_returns_empty(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            (tmp / "research_briefs" / "attachments" / STAMP).mkdir(parents=True)
            pairs, cards, _note = _reuse(tmp)
            self.assertEqual(pairs, [])
            self.assertEqual(cards, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
