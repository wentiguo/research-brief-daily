"""The attachment gate that keeps a cited government report out of a physics mail.

A publisher landing page lists everything it cites next to the article, and the scan
that collects "supplementary" links cannot tell the two apart. Only the bytes can - so
this lock down what has to be true before a downloaded file is allowed to travel under
an article's name.
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from fetch_paper_attachments import _file_is_about  # noqa: E402


def _write_bytes(path: Path, text: str) -> Path:
    path.write_bytes(text.encode("utf-8"))
    return path


class TestAttachmentIdentity(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(os.environ.get("TMPTEST", os.path.dirname(__file__))) / "_tmp_identity"
        self.tmp.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        for child in self.tmp.iterdir():
            child.unlink()
        self.tmp.rmdir()

    def test_doi_in_the_url_is_enough(self) -> None:
        """The publisher's own endpoint names the DOI; the PDF need not repeat it."""
        target = _write_bytes(self.tmp / "prb.pdf", "%PDF-1.4 no metadata at all")
        self.assertTrue(_file_is_about(target, "https://journals.aps.org/prb/pdf/10.1103/stw8-9mld",
                                       "10.1103/stw8-9mld", "Electronic structure and correlations"))

    def test_doi_printed_in_the_pdf_counts(self) -> None:
        """A preprint prints the DOI on page one, broken across lines like every DOI."""
        target = _write_bytes(self.tmp / "arxiv.pdf", "%PDF-1.4 title author doi:10.1103/\nStw8-9mld more")
        self.assertTrue(_file_is_about(target, "https://arxiv.org/pdf/2501.17671v6", "10.1103/stw8-9mld",
                                       "Graph neural networks in the Wilson loop representation"))

    def test_unrelated_document_is_rejected(self) -> None:
        """The real failure this guards: a DOE validation report mailed as a Nature paper."""
        target = _write_bytes(self.tmp / "文件4.pdf", "%PDF-1.4 Validation of LOCA2 and STAR-ESDM v2.docx")
        self.assertFalse(_file_is_about(target, "https://eesm.science.energy.gov/reports/LOCA2.pdf",
                                        "10.1038/s42256-026-01308-7",
                                        "Regional climate risk assessment from climate model ensembles"))

    def test_a_different_article_is_rejected(self) -> None:
        target = _write_bytes(self.tmp / "file.pdf", "%PDF-1.4 doi:10.1103/PhysRevB.999.999999")
        self.assertFalse(_file_is_about(target, "https://example.org/files/a.pdf", "10.1103/abcdefg-1234",
                                        "Graph neural networks in the Wilson loop representation"))
        self.assertFalse(_file_is_about(target, "https://example.org/files/a.pdf", "10.1103/abcdefg-1234",
                                        "Electronic structure and dynamical correlations"))

    def test_the_preprint_with_no_doi_anywhere_still_counts(self) -> None:
        """arXiv prints neither DOI in the URL nor on page one - two title words must do."""
        target = _write_bytes(self.tmp / "2501.17671v6.pdf",
                              "%PDF-1.4 Z2 topological signatures of the optical bound maximal Berry curvature")
        self.assertTrue(_file_is_about(target, "https://arxiv.org/pdf/2501.17671v6", "10.1103/l6dk-qwwg",
                                       "Z2 topological signatures of the optical bound on maximal "
                                       "Berry curvature: Application to two-dimensional "
                                       "time-reversal-symmetric insulators"))

    def test_one_generic_title_word_is_not_enough(self) -> None:
        """"Regional assessment" alone must not let an unrelated report through."""
        target = _write_bytes(self.tmp / "file.pdf", "%PDF-1.4 regional assessment data products")
        self.assertFalse(_file_is_about(target, "https://example.org/a.pdf",
                                        "10.1038/s42256-026-01308-7",
                                        "Regional climate risk assessment from climate model ensembles"))

    def test_a_generic_title_falls_back_to_the_doi_only(self) -> None:
        """A title with nothing distinctive left (all short words) must not be guessed at."""
        target = _write_bytes(self.tmp / "file.pdf", "%PDF-1.4 topological topological quantum")
        self.assertFalse(_file_is_about(target, "https://example.org/a.pdf", "10.1103/abcdefg-1234",
                                        "Loop and graph and loop"))

    def test_punctuation_tolerant_on_both_sides(self) -> None:
        target = _write_bytes(self.tmp / "file.pdf", "10.1103 /  Stw8 - 9mld")
        self.assertTrue(_file_is_about(target, "https://example.org/a.pdf", "10.1103/stw8-9mld",
                                       "Graph neural networks in the Wilson loop representation"))


if __name__ == "__main__":
    unittest.main()
