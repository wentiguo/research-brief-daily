# -*- coding: utf-8 -*-
"""Regression tests for the three changes requested on 2026-10-06.

1. Poster picks must prefer *published* papers; a preprint may only take a slot
   when the day produced no further published candidate.
2. `_quote_abstract` must still reject a fabricated quote, but must tolerate a
   quote that stitches two verbatim fragments together (the elision case that
   used to throw away a correctly grounded Chinese bullet and drop the poster
   back to printing the English abstract).
3. The whole suite must keep passing - these guards exist because the previous
   behaviour was wrong in both directions at once.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "scripts"))

import generate_research_brief as grb  # noqa: E402


ABSTRACT = (
    "We study magnetic spin splitting in layered Mn3Ge2 using first-principles "
    "density functional theory. The Néel vector is fixed along the in-plane axis and "
    "spin-orbit coupling is included. We find that the sliding operation switches the "
    "spin polarization and the valley polarization on and off. These results suggest a "
    "route to reconfigurable magnetic devices."
)


def paper(title, venue, doi="10.1103/x", source="feed", abstract=ABSTRACT):
    return {
        "title": title, "abstract": abstract, "venue": venue,
        "published": "2026-10-05", "source": source, "doi": doi,
        "score": 5.0, "reasons": ["magnetism"],
    }


class PosterPrefersPublishedTests(unittest.TestCase):
    """Slot 2 used to go to the best preprint whenever one existed."""

    CONFIG = {"digest_images": {"poster_papers": 2}}

    def setUp(self):
        self.cfg = dict(self.CONFIG)
        # journal_tier reads the allowlist; give these two a known tier.
        self._tier = grb.journal_tier
        grb.journal_tier = lambda venue, config: (
            1 if "Physical Review Letters" in (venue or "") else
            2 if "Physical Review B" in (venue or "") else 3)
        self._rec = grb.recommendation_score
        grb.recommendation_score = lambda p, c: {"total": 5.0}

    def tearDown(self):
        grb.journal_tier = self._tier
        grb.recommendation_score = self._rec

    def test_two_published_papers_give_no_preprint_slot(self):
        papers = [
            paper("Altermagnetic bilayer", "Physical Review Letters"),
            paper("Valley Hall insulator", "Physical Review B"),
            paper("A preprint that scores highest", "arXiv", doi="10.48550/arXiv.1"),
        ]
        picked = grb.pick_poster_papers(papers, self.cfg)
        self.assertEqual(len(picked), 2)
        for p in picked:
            self.assertFalse(grb.is_preprint(p),
                             "a preprint took a poster slot while two published "
                             "papers were available")

    def test_preprint_takes_the_slot_only_when_nothing_published_is_left(self):
        papers = [
            paper("Altermagnetic bilayer", "Physical Review Letters"),
            paper("A preprint", "arXiv", doi="10.48550/arXiv.1"),
        ]
        picked = grb.pick_poster_papers(papers, self.cfg)
        self.assertEqual(len(picked), 2)
        self.assertTrue(grb.is_preprint(picked[1]),
                        "with only one published paper the preprint should fill slot 2")

    def test_only_preprints_still_yields_two_posters(self):
        papers = [
            paper("Preprint one", "arXiv", doi="10.48550/arXiv.1"),
            paper("Preprint two", "arXiv", doi="10.48550/arXiv.2"),
        ]
        picked = grb.pick_poster_papers(papers, self.cfg)
        self.assertEqual(len(picked), 2, "a preprint-only day must still yield two posters")


class QuoteVerificationTests(unittest.TestCase):
    """The anti-hallucination gate must stay shut, but stop over-rejecting."""

    def test_fabricated_quote_is_still_rejected(self):
        self.assertFalse(grb._quote_abstract(
            "we observe a room temperature magnetic gap in the sample",
            ABSTRACT))

    def test_verbatim_quote_is_accepted(self):
        self.assertTrue(grb._quote_abstract(
            "magnetic spin splitting in layered", ABSTRACT))

    def test_quote_stitching_two_verbatim_fragments_is_accepted(self):
        # The real failure that cost a Chinese poster: each half is verbatim, but
        # they are not adjacent in the abstract (the model wrote "..." between them).
        self.assertTrue(grb._quote_abstract(
            "we study magnetic spin splitting in layered Mn3Ge2 using "
            "first-principles density functional theory and we find that the "
            "sliding operation switches the spin polarization", ABSTRACT))

    def test_a_two_word_quote_cannot_support_a_bullet(self):
        # Regression: the gate used to measure the evidence in characters, so "we
        # study" (8 characters) passed a `len < 6` check and was enough to get a
        # claim printed on the poster.
        self.assertFalse(grb._quote_abstract("we study", ABSTRACT))
        self.assertFalse(grb._quote_abstract("spin polarization", ABSTRACT))

    def test_four_word_quote_is_the_working_minimum(self):
        self.assertEqual(grb.MIN_QUOTE_WORDS, 4)
        self.assertTrue(grb._quote_abstract(
            "spin orbit coupling is included", ABSTRACT))

    def test_empty_inputs_are_rejected(self):
        self.assertFalse(grb._quote_abstract("", ABSTRACT))
        self.assertFalse(grb._quote_abstract("we find that", ""))


class ScopeStillIntactTests(unittest.TestCase):
    """The poster change must not have narrowed the topic gate.

    Asserted against the SHIPPED default gate rather than one user's vocabulary: these
    papers have to clear the generic condensed-matter profile. A user who replaces
    `topic_gate` is expected to adjust this test to their own scope.
    """

    def test_default_gate_admits_its_own_default_scope(self):
        """The shipped default profile must actually admit its own topics.

        A DIRECT phrase passes unaided, so any phrase from
        `_DEFAULT_TOPIC_SIGNAL_DIRECT` clears the gate with no config at all. This is
        what makes a fresh clone produce a non-empty brief.
        """
        cfg = {}
        for title, abstract in [
            ("Ferroelastic switching in a multiferroic thin film",
             "Ferroelastic switching is imaged by piezoresponse force microscopy."),
            ("Berry curvature of a topological insulator surface",
             "We report the Berry curvature of a topological insulator by ARPES."),
        ]:
            p = {"title": title, "abstract": abstract,
                 "venue": "Physical Review Letters"}
            self.assertTrue(grb.is_domain_match(p, cfg), title)

    def test_default_gate_rejects_an_unrelated_field(self):
        """Widening the gate must not turn it into 'everything passes'."""
        cfg = {}
        p = {"title": "Rheology of a gravity-stretched liquid jet",
             "abstract": "We study the breakup of a liquid jet under gravity.",
             "venue": "Physical Review Letters"}
        self.assertFalse(grb.is_domain_match(p, cfg))


if __name__ == "__main__":
    unittest.main()
