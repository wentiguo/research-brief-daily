"""Regression test for the 2026-10-09 email failure.

Root cause: generate_research_brief.make_bibtex -> bibtex_key assumed
`paper["authors"]` is always a non-empty, comma-separated string. When a
selected paper had an empty/malformed authors field, `"" .split(",")[0].split()[-1]`
raised IndexError *before* the email was assembled, so the whole daily run
crashed with exit code 1 and no mail was sent.

The fix makes bibtex_key / make_bibtex tolerant of:
  * authors missing entirely
  * authors == "" (empty string)
  * authors == " ,, " (whitespace / empty segments)
  * authors given as a list
  * title missing or empty
and make_bibtex drops entries that end up with no usable fields instead of
emitting a broken empty `@article{}`.
"""

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import generate_research_brief as grb  # noqa: E402


class TestBibtexKeyRobustness(unittest.TestCase):
    def test_empty_authors_string(self):
        k = grb.bibtex_key({"title": "A study of X", "published": "2026-10-09", "authors": ""})
        self.assertTrue(k)
        self.assertIn("paper", k)  # fell back to the safe token

    def test_missing_authors(self):
        k = grb.bibtex_key({"title": "A study of X", "published": "2026-10-09"})
        self.assertIn("paper", k)

    def test_whitespace_only_authors(self):
        k = grb.bibtex_key({"title": "X", "published": "2026-10-09", "authors": " ,, "})
        self.assertIn("paper", k)

    def test_list_authors(self):
        k = grb.bibtex_key({
            "title": "A study of X", "published": "2026-10-09",
            "authors": ["Smith, J.", "Doe, A."],
        })
        self.assertTrue(k.startswith("smith"))

    def test_normal_authors(self):
        k = grb.bibtex_key({"title": "A study", "published": "2026-10-09", "authors": "Smith, J."})
        self.assertTrue(k.startswith("smith"))

    def test_empty_title(self):
        k = grb.bibtex_key({"title": "", "published": "2026-10-09", "authors": "Smith, J."})
        self.assertIn("smith", k)  # author still extracted

    def test_missing_published(self):
        # year should fall back to "nd" rather than crash
        k = grb.bibtex_key({"title": "A study", "authors": "Smith, J."})
        self.assertIn("nd", k)


class TestMakeBibtexRobustness(unittest.TestCase):
    def test_good_and_bad_mixed(self):
        papers = [
            {"title": "Good paper on altermagnetism", "published": "2026-10-09",
             "authors": "Smith, J.", "venue": "PRL", "doi": "10.1103/x"},
            {"title": "", "published": "", "authors": ""},  # fully malformed
        ]
        out = grb.make_bibtex(papers, 10)
        self.assertIn("Good paper on altermagnetism", out)
        self.assertIn("@article", out)
        # the malformed paper must NOT produce a broken empty @article{} block
        self.assertNotIn("@article{\n\n}", out)
        self.assertNotIn("@article{paperndpaper", out)

    def test_list_authors_in_body(self):
        papers = [{"title": "T", "published": "2026-10-09",
                   "authors": ["Smith, J.", "Doe, A."], "venue": "PRL"}]
        out = grb.make_bibtex(papers, 10)
        self.assertIn("Smith, J. and Doe, A.", out)  # canonical "and"-separated bibtex

    def test_empty_list(self):
        self.assertEqual(grb.make_bibtex([], 10), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
