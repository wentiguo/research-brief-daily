# -*- coding: utf-8 -*-
"""Regression tests for the 2026-10-10 changes.

1. Supplementary-material discovery: publisher-host supplement links are kept, support /
   ad / non-supplement links are rejected.
2. fetch_vor_and_supplements: the journal VOR (APS / OA) is reported as kind "fulltext"
   and never as "preprint"; an arXiv fallback is labelled "preprint"; supplements are
   downloaded from the landing page.
3. _merge_synced: a locally-fetched journal VOR must win over the cloud's own arXiv copy,
   and supplements must survive.
4. Poster digest: _extract_poster_digest reports dropped sections; _is_generic flags
   hollow bullets; _fallback_digest yields a Chinese-framed digest (never a raw English dump).
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, ".tools"))

import fetch_pdfs_local as fl  # noqa: E402
import generate_research_brief as grb  # noqa: E402


class FakeResp:
    def __init__(self, data, url="https://example.com/x", headers=None, status=200):
        self._data = data
        self._url = url
        self.headers = headers or {}
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, *a):
        return self._data

    def geturl(self):
        return self._url


ABSTRACT = (
    "We study altermagnetic spin splitting in layered Co2Br5 using first-principles "
    "density functional theory. The Néel vector is fixed along the in-plane axis and "
    "spin-orbit coupling is included. We find that the sliding operation switches the "
    "spin polarization and the valley polarization on and off. These results suggest a "
    "route to reconfigurable altermagnetic devices."
)


class SupplementDiscoveryTests(unittest.TestCase):
    def test_publisher_host_supplements_kept(self):
        landing = "https://www.nature.com/articles/abc123"
        html = """
        <a href="/articles/supplementary/fig1.pdf">Supplementary Fig 1</a>
        <a href="https://www.nature.com/articles/abc123/MediaObjects/123_esm.pdf">ESM</a>
        <a href="https://support.nature.com/support/home">Support</a>
        <a href="https://www.nature.com/articles/abc123.pdf">Article PDF</a>
        <a href="https://evil.example.com/abc_supp.pdf">evil</a>
        """
        links = fl._scan_supplement_links(html, landing, "10.1038/abc123")
        self.assertEqual(len(links), 2)
        self.assertTrue(all("nature.com" in u for u in links))
        self.assertTrue(any("MediaObjects" in u for u in links))
        self.assertTrue(any("supplementary" in u for u in links))

    def test_no_false_positive_without_token(self):
        landing = "https://journals.aps.org/prl/abstract/x"
        html = '<a href="https://journals.aps.org/prl/pdf/x">PDF</a>'
        self.assertEqual(fl._scan_supplement_links(html, landing, "10.1103/x"), [])


class FetchVorSupplementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vor_test_"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _patch(self, main_kind):
        landing = "https://www.nature.com/articles/abc123"
        html = ('<a href="https://www.nature.com/articles/abc123/MediaObjects/123_esm.pdf">'
                "ESM</a>")
        main_path = self.tmp / "vor.pdf"
        main_path.write_bytes(b"%PDF-1.4 fake journal pdf")
        if main_kind == "fulltext":
            fl.try_routes = lambda *a, **k: (main_path, "aps (valid PDF (100 KB), 100 KB)")
        else:
            fl.try_routes = lambda *a, **k: (main_path, "arxiv (valid PDF (100 KB), 100 KB)")
        fl._fetch_landing_page = lambda doi: (landing, html)
        # A real supplementary file is at least a few KB; the < 4096 B gate must
        # keep small/error payloads out, so the stub must clear it.
        fl._open = lambda url, *a, **k: FakeResp(b"%PDF-1.4 " + b"x" * 5000)

    def test_vor_fulltext_and_supplements(self):
        self._patch("fulltext")
        res = fl.fetch_vor_and_supplements("10.1038/abc123", "A title", "Nature", self.tmp)
        self.assertEqual(res["main_kind"], "fulltext")
        self.assertEqual(len(res["supplements"]), 1)
        self.assertTrue(res["supplements"][0][0].exists())

    def test_arxiv_fallback_labelled_preprint(self):
        self._patch("preprint")
        res = fl.fetch_vor_and_supplements("10.1038/abc123", "A title", "Nature", self.tmp)
        self.assertEqual(res["main_kind"], "preprint")

    def test_supplement_magic_rejected(self):
        landing = "https://www.nature.com/articles/abc123"
        html = '<a href="https://www.nature.com/articles/abc123/MediaObjects/123_esm.pdf">ESM</a>'
        fl.try_routes = lambda *a, **k: (None, "no route")
        fl._fetch_landing_page = lambda doi: (landing, html)
        fl._open = lambda url, *a, **k: FakeResp(b"<html>not a file</html>")  # no %PDF magic
        res = fl.fetch_vor_and_supplements("10.1038/abc123", "A title", "Nature", self.tmp)
        self.assertEqual(res["supplements"], [])


class MergeVorBeatsArxivTests(unittest.TestCase):
    def test_vor_wins_over_cloud_arxiv(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            arxiv = d / "1_paper_arxiv_version.pdf"
            vor = d / "1_paper_journal_vor.pdf"
            supp = d / "1_paper_supp_fig1.pdf"
            for p in (arxiv, vor, supp):
                p.write_bytes(b"%PDF-1.4 x")
            article_files = [("1_paper", arxiv)]
            synced = [("1_paper", vor), ("1_paper", supp)]
            grb._merge_synced(article_files, synced)
            names = [p.name for _, p in article_files]
            self.assertIn(vor.name, names)
            self.assertIn(supp.name, names)
            self.assertNotIn(arxiv.name, names)  # arXiv dropped in favour of VOR

    def test_arxiv_kept_when_no_vor(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            arxiv = d / "1_paper_arxiv_version.pdf"
            arxiv.write_bytes(b"%PDF-1.4 x")
            article_files = [("1_paper", arxiv)]
            synced = []  # no local VOR
            grb._merge_synced(article_files, synced)
            self.assertEqual([p.name for _, p in article_files], [arxiv.name])


class PosterDigestTests(unittest.TestCase):
    def test_extract_reports_missing_section(self):
        parsed = {
            "title_cn": "中文题名",
            "background": {"text": "我们采用第一性原理密度泛函理论研究 Co2Br5 的交换劈裂。",
                           "quote": "first-principles density functional theory"},
            "problem": {"text": "", "quote": ""},
            "highlights": [{"text": "滑移操作可开关自旋极化。", "quote": "sliding operation switches the spin polarization"}],
            "takeaway": {"text": "", "quote": ""},
            "outlook": {"text": "", "quote": ""},
        }
        digest, missing = grb._extract_poster_digest(parsed, ABSTRACT)
        self.assertIsNotNone(digest)
        self.assertIn("problem", missing)
        self.assertIn("takeaway", missing)
        self.assertIn("outlook", missing)

    def test_highlights_unverifiable_returns_none(self):
        parsed = {
            "title_cn": "x",
            "background": {"text": "", "quote": ""},
            "problem": {"text": "", "quote": ""},
            "highlights": [{"text": "这是一个空泛的陈述。", "quote": "totally invented phrase not in abstract"}],
            "takeaway": {"text": "", "quote": ""},
            "outlook": {"text": "", "quote": ""},
        }
        digest, _ = grb._extract_poster_digest(parsed, ABSTRACT)
        self.assertIsNone(digest)

    def test_is_generic(self):
        self.assertTrue(grb._is_generic("本文提出了一种新方法"))
        self.assertFalse(grb._is_generic(
            "采用第一性原理密度泛函理论计算 Co2Br5 的滑移依赖能带拓扑"))

    def test_fallback_is_chinese_framed_not_raw_english(self):
        paper = {"title": "T", "abstract": ABSTRACT}
        d = grb._fallback_digest(paper)
        self.assertIn("据出版商摘要", d["background"])
        # The raw abstract is never emitted verbatim as a section body.
        self.assertNotIn(ABSTRACT, d["background"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
