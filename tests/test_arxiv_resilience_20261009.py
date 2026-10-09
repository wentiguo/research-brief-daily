"""Regression: arXiv channel must not die on a single transient timeout (2026-10-09).

Root cause: run #141 (2026-10-06) fetched a published PRL's arXiv preprint as a PDF,
but from 2026-10-07 every brief shipped 0 attachments because the Actions runner began
timing out on export.arxiv.org and `arxiv_pdf` flipped ARXIV_UNAVAILABLE on the first
failure, disabling the only cloud route that could deliver a preprint for the rest of
the run. The fix tolerates a few consecutive failures (reset on success) and only
retires the channel after ARXIV_FAIL_THRESHOLD of them.
"""

import io
import sys
import os
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import fetch_paper_attachments as fpa  # noqa: E402
import urllib.error  # noqa: E402


def _atom_entry(entry_id: str, title: str, doi: str = "") -> str:
    doi_block = f'        <arxiv:doi xmlns:arxiv="http://arxiv.org/schemas/atom">{doi}</arxiv:doi>\n' if doi else ""
    return f"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>{entry_id}</id>
    <title>{title}</title>
{doi_block}  </entry>
</feed>"""


def _xml_bytes(xml: str) -> io.BytesIO:
    return io.BytesIO(xml.encode("utf-8"))


class TestArxivResilience(unittest.TestCase):
    def setUp(self):
        # Each test starts clean.
        fpa.ARXIV_UNAVAILABLE = False
        fpa.ARXIV_FAIL_STREAK = 0

    def test_single_timeout_then_success_keeps_channel_alive(self):
        """One stalled query must not disable the channel; a later success still wins."""
        title = "Topological Altermagnetic Spintronics in Bilayers"
        doi = "10.1103/physrevlett.123.456789"
        good = _xml_bytes(_atom_entry("http://arxiv.org/abs/2301.12345",
                                      "Topological Altermagnetic Spintronics in Bilayers",
                                      doi))

        call = {"n": 0}

        def fake_open(url, timeout=30, data=None):
            call["n"] += 1
            if call["n"] == 1:
                raise urllib.error.URLError("timed out")
            # Second call succeeds with a DOI-exact match.
            return _xml_bytes(_atom_entry("http://arxiv.org/abs/2301.12345",
                                          "Topological Altermagnetic Spintronics in Bilayers",
                                          doi))

        with mock.patch.object(fpa, "_open", side_effect=fake_open):
            res = fpa.arxiv_pdf(title, doi)
        self.assertEqual(res["url"], "http://arxiv.org/pdf/2301.12345")
        self.assertFalse(fpa.ARXIV_UNAVAILABLE, "channel must survive a single timeout")
        self.assertEqual(fpa.ARXIV_FAIL_STREAK, 0, "streak resets on success")

    def test_title_match_after_timeout_still_delivers(self):
        """Even without a DOI match, a title match after a transient stall must work."""
        title = "Valley Hall Effect in Graphene Altermagnet Heterojunctions"
        calls = {"n": 0}

        def fake_open(url, timeout=30, data=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.URLError("timed out")
            return _xml_bytes(_atom_entry(
                "http://arxiv.org/abs/2402.99999",
                "Valley Hall Effect in Graphene Altermagnet Heterojunctions"))

        with mock.patch.object(fpa, "_open", side_effect=fake_open):
            res = fpa.arxiv_pdf(title, "")
        self.assertEqual(res["url"], "http://arxiv.org/pdf/2402.99999")
        self.assertFalse(fpa.ARXIV_UNAVAILABLE)

    def test_channel_retires_only_after_threshold(self):
        """Repeated failures (>= threshold) retire the channel; a single one does not."""
        # One failure below threshold must NOT retire.
        with mock.patch.object(fpa, "_open", side_effect=urllib.error.URLError("down")):
            fpa._arxiv_query_with_retries('ti:"x"')
        self.assertFalse(fpa.ARXIV_UNAVAILABLE, "one failure must not retire the channel")
        self.assertEqual(fpa.ARXIV_FAIL_STREAK, 1)

        # A second consecutive failure reaches the threshold and retires.
        with mock.patch.object(fpa, "_open", side_effect=urllib.error.URLError("down")):
            fpa._arxiv_query_with_retries('ti:"y"')
        self.assertTrue(fpa.ARXIV_UNAVAILABLE, "channel should retire after >= threshold failures")

    def test_retired_channel_skips_further_lookups(self):
        fpa.ARXIV_UNAVAILABLE = True
        res = fpa.arxiv_pdf("Some Title", "10.1234/x")
        self.assertIn("skipped", res)
        self.assertEqual(res["url"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
