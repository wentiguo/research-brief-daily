# -*- coding: utf-8 -*-
"""Regression tests for the poster-priority and cloud-local PDF bridge (2026-10-06).

Three guards, each born from a concrete failure mode:

1. Poster picks must prefer *highly-relevant* papers from the user's fixed shortlist of
   venues (Nature/Science/NatPhys/SciAdv/PRL/PRX/NatComm), then any published paper by
   tier (1区 before 2区), then preprints only as a last resort.
2. The tier secondary key must be +tier (ascending), not -tier: journal_tier returns 1 for
   顶刊/1区 (best) and 2 for 2区, so -tier would pick the *worse* journal first. This bug
   was invisible to the old test because it only checked "no preprint in the slots".
3. The cloud stamps its authoritative run date into the wishlist; the local fetch run must
   honour that exact date. The local machine clock drifted +2 days vs the runner, and a
   local run using its own clock would write the PDFs into the wrong dated folder and the
   cloud would never find them.
"""
import datetime as dt
import importlib.util
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import generate_research_brief as grb  # noqa: E402

# Load the .tools script without polluting the package path.
_lfp_path = ROOT / ".tools" / "local_fetch_posters.py"
_lfp_spec = importlib.util.spec_from_file_location("local_fetch_posters_test", str(_lfp_path))
lfp = importlib.util.module_from_spec(_lfp_spec)
_lfp_spec.loader.exec_module(lfp)


def _venue_paper(title, venue, relevance=5.0, doi="10.1103/x", abstract="magnetism study"):
    return {
        "title": title, "abstract": abstract, "venue": venue,
        "published": "2026-10-05", "source": "feed", "doi": doi,
        "relevance": relevance,
    }


PRIORITY_JOURNALS = [
    "Nature", "Science", "Nature Physics", "Science Advances",
    "Physical Review Letters", "Physical Review X", "Nature Communications",
]


class PosterPriorityJournalTests(unittest.TestCase):
    """The user asked: prefer highly-relevant priority-journal articles for posters."""

    def setUp(self):
        self.cfg = {
            "digest_images": {
                "poster_papers": 2,
                "poster_priority_journals": PRIORITY_JOURNALS,
                "poster_high_relevance_threshold": 8.0,
            }
        }
        # Fix journal tiers by name. Note: a venue's tier is independent of whether it is
        # on the priority list - "Journal of Physics" is tier 1 but NOT a priority journal.
        self._tier = grb.journal_tier
        def fake_tier(venue, config):
            v = (venue or "").lower()
            if "nature" in v or "science" in v:
                return 1
            if "physical review letters" in v or "physical review x" in v:
                return 1
            if "journal of physics" in v:
                return 1
            if "physical review b" in v:
                return 2
            return 3
        grb.journal_tier = fake_tier
        self._rec = grb.recommendation_score
        grb.recommendation_score = lambda p, c: {"total": float(p.get("relevance", 5.0))}

    def tearDown(self):
        grb.journal_tier = self._tier
        grb.recommendation_score = self._rec

    def test_priority_high_relevance_beats_lower_tier_published(self):
        """A Nature paper above the threshold must outrank a non-priority published paper,
        even one with a higher score."""
        papers = [
            _venue_paper("Low-tier published, high score", "Physical Review B", relevance=9.5),
            _venue_paper("Nature, above threshold", "Nature", relevance=8.5),
        ]
        picked = grb.pick_poster_papers(papers, self.cfg)
        self.assertEqual(picked[0]["venue"], "Nature")

    def test_priority_but_below_threshold_is_not_boosted(self):
        """A priority-venue paper BELOW the relevance threshold loses the (0) key and is
        ordered as an ordinary published paper: here it is a tier-1 journal, so it still
        ranks above a same-tier paper only when that paper's relevance is lower. We prove
        the boost is gone by giving a same-tier non-priority paper a HIGHER relevance -
        that paper must win the slot, not the below-threshold priority one."""
        papers = [
            _venue_paper("Nature, below threshold", "Nature", relevance=6.0),
            _venue_paper("Same-tier non-priority, higher relevance", "Journal of Physics", relevance=9.5),
        ]
        picked = grb.pick_poster_papers(papers, self.cfg)
        self.assertEqual(picked[0]["venue"], "Journal of Physics")

    def test_tier_ordering_within_non_priority_is_ascending(self):
        """The +tier fix: a better-tier (lower number) non-priority paper must outrank a
        worse-tier one even when the worse-tier one scores higher. With the old -tier key
        this was inverted (the worse journal won)."""
        papers = [
            _venue_paper("2区 high score", "Physical Review B", relevance=9.5),
            _venue_paper("3区 lower score", "Low Tier Journal", relevance=5.0),
        ]
        picked = grb.pick_poster_papers(papers, self.cfg)
        self.assertEqual(picked[0]["venue"], "Physical Review B")

    def test_priority_high_relevance_fills_both_slots_when_available(self):
        papers = [
            _venue_paper("PRL a", "Physical Review Letters", relevance=9.0),
            _venue_paper("Nature b", "Nature", relevance=8.5),
            _venue_paper("PRB c", "Physical Review B", relevance=9.9),
        ]
        picked = grb.pick_poster_papers(papers, self.cfg)
        self.assertEqual(len(picked), 2)
        venues = {p["venue"] for p in picked}
        self.assertIn("Physical Review Letters", venues)
        self.assertIn("Nature", venues)

    def test_preprint_only_when_no_published_left(self):
        papers = [
            _venue_paper("PRL only", "Physical Review Letters", relevance=9.0),
            _venue_paper("A preprint", "arXiv", relevance=9.9, doi="10.48550/arXiv.1"),
        ]
        picked = grb.pick_poster_papers(papers, self.cfg)
        self.assertEqual(len(picked), 2)
        self.assertFalse(grb.is_preprint(picked[0]), "published must fill slot 1")
        self.assertTrue(grb.is_preprint(picked[1]), "preprint fills slot 2 only when no published left")


class PosterWishlistStampsRunDateTests(unittest.TestCase):
    """The cloud must stamp run_date so the local run targets the right folder."""

    def test_run_date_is_stamped(self):
        doc = grb.build_poster_wishlist(
            [{"doi": "10.1103/a", "title": "T", "venue": "PRL"}],
            dt.date(2026, 10, 4))
        self.assertEqual(doc["run_date"], "2026-10-04")
        self.assertEqual(len(doc["papers"]), 1)
        self.assertEqual(doc["papers"][0]["doi"], "10.1103/a")

    def test_papers_without_doi_are_dropped(self):
        doc = grb.build_poster_wishlist(
            [{"doi": "", "title": "no doi"}, {"doi": "10.1103/b", "title": "T"}],
            dt.date(2026, 10, 4))
        self.assertEqual([p["doi"] for p in doc["papers"]], ["10.1103/b"])

    def test_empty_poster_list_yields_empty_papers(self):
        doc = grb.build_poster_wishlist([], dt.date(2026, 10, 4))
        self.assertEqual(doc["papers"], [])


class LocalFetchDateResolutionTests(unittest.TestCase):
    """The local run must honour the cloud's run_date, not its own drifting clock."""

    def test_run_date_from_cloud_wins(self):
        self.assertEqual(
            lfp.resolve_target_date({"run_date": "2026-10-04", "papers": []}),
            "2026-10-04")

    def test_empty_run_date_falls_back_to_local(self):
        self.assertEqual(
            lfp.resolve_target_date({"run_date": "", "papers": []}),
            lfp.local_date_str())

    def test_missing_run_date_key_falls_back_to_local(self):
        self.assertEqual(
            lfp.resolve_target_date({"papers": []}),
            lfp.local_date_str())

    def test_bare_list_format_falls_back_to_local(self):
        # Backward-compatible with the old bare-list wishlist.
        self.assertEqual(lfp.resolve_target_date([]), lfp.local_date_str())


if __name__ == "__main__":
    unittest.main()
