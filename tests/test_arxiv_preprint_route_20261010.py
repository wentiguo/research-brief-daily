# -*- coding: utf-8 -*-
"""The arXiv preprint route has to work for papers that DO carry a DOI (2026-10-10).

For months the mail arrived with no full text even when arXiv held the paper's own
preprint. Two separate gates were closing it, and neither was a network problem:

1. The local bridge asked for arXiv only `if not doi`. Every paper this pipeline
   shortlists carries a DOI, so the one route that historically completed was dead
   code for exactly the papers that needed it.
2. Both downloaders then re-checked identity by searching the PDF's raw bytes for the
   DOI or for title words. arXiv's own PDFs keep their text encoded, so those strings
   are frequently not recoverable as bytes. The check said "not this article" about a
   file arXiv had just matched by DOI and title - 11.6 MB of the right paper, thrown
   away. "I cannot read it" was being answered as "I read it and it is wrong".
"""
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

_spec = importlib.util.spec_from_file_location(
    "fetch_pdfs_local_test", str(ROOT / ".tools" / "fetch_pdfs_local.py"))
fl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fl)

import fetch_paper_attachments as fpa  # noqa: E402

DOI = "10.1021/acsnano.6c10210"
TITLE = "Millimeter-Scale, Atomically Controlled 2D Topological Insulators"


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _preprint_bytes() -> bytes:
    """A PDF whose bytes carry neither the DOI nor any searchable title word."""
    return b"%PDF-1.5\n" + b"\x80\x01\x9c/xObject << /Font << >> >>\n" + b"\x00" * 30_000


class LocalArxivRouteTests(unittest.TestCase):
    def test_arxiv_is_consulted_when_a_doi_is_present(self):
        with tempfile.TemporaryDirectory() as raw:
            out_dir = Path(raw)
            seen: dict[str, bool] = {}

            def fake_arxiv_pdf(doi_or_title, title):
                seen["called"] = True
                return "http://arxiv.org/pdf/2603.14199v1"

            original_arxiv = fl.arxiv_pdf
            original_open = fl._open
            try:
                fl.arxiv_pdf = fake_arxiv_pdf
                fl.aps_pdf_url = lambda *a, **k: ""
                fl.unpaywall_pdf = lambda *a, **k: ("", "")
                fl._open = lambda *a, **k: _FakeResponse(_preprint_bytes())
                path, note = fl.try_routes(DOI, TITLE, "ACS Nano", out_dir)
            finally:
                fl.arxiv_pdf = original_arxiv
                fl._open = original_open

            self.assertTrue(seen.get("called"),
                            "arXiv must be searched even though this paper has a DOI")
            self.assertIsNotNone(path, f"preprint should have been accepted, note was: {note}")
            self.assertTrue(str(note).lower().startswith("arxiv"),
                            f"the note should name the route that answered: {note}")


class CloudIdentityCheckTests(unittest.TestCase):
    def test_arxiv_route_accepts_a_pdf_whose_bytes_are_not_text_searchable(self):
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "1_acs_nano_2603_14199v1.pdf"
            target.write_bytes(_preprint_bytes())
            url = "http://arxiv.org/pdf/2603.14199v1"

            # Without trust it is refused: the bytes carry neither DOI nor title.
            self.assertFalse(
                fpa._file_is_about(target, url, DOI, TITLE, trust_route=False),
                "an unreadable-bytes file must stay rejected for a fished-off link")
            # With trust - because arXiv matched this article to that URL - it is kept.
            self.assertTrue(
                fpa._file_is_about(target, url, DOI, TITLE, trust_route=True),
                "a route whose identity arXiv settled must not be re-rejected on bytes")

    def test_trust_does_not_excuse_a_file_that_is_not_a_pdf(self):
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "1_acs_nano_notapdf.pdf"
            target.write_bytes(b"<html><body>Sign in to continue</body></html>\n" + b"0" * 30_000)
            self.assertFalse(
                fpa._file_is_about(target, "http://arxiv.org/pdf/x", DOI, TITLE,
                                   trust_route=True),
                "trust relaxes the identity test, not the 'is it even a PDF' test")


if __name__ == "__main__":
    unittest.main(verbosity=2)
