"""Regression tests for the 2026-10-11 content-policy change.

Two user directives drove this:
  1. Group the brief ONLY by research direction. The separate "顶刊/已发表精选"
     (top-journal featured) section is removed - it both created the unwanted
     "top-journal" distinction AND was the main relevance leak, because it admitted
     ANY priority-journal paper that carried an abstract, regardless of direction.
  2. Pushed articles must be closely related. Removing the featured section plus
     wiring the existing is_wave_physics() exclusion into the domain gate kills the
     two biggest leak sources (off-topic priority-journal papers and topological
     photonics / acoustics that slip through the bare "topological" / "phonon" words).

These tests pin the new behaviour so a later refactor cannot quietly bring the
"顶刊" bucket or the wave-physics leak back.
"""

import datetime
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import generate_research_brief as grb  # noqa: E402

PRL = "Physical Review Letters"
NCOMMS = "Nature Communications"
PRB = "Physical Review B"

CONFIG = {
    "research_profile": {
        "recommendation_groups": [
            {"name": "交错磁", "terms": ["altermagnetic", "altermagnetism", "交错磁"]},
            {"name": "拓扑材料",
             "terms": ["topological insulator", "topological semimetal", "weyl semimetal"]},
            {"name": "多铁与铁性",
             "terms": ["multiferroic", "ferroelectric", "skyrmion", "magnetoelectric"]},
            {"name": "储能电池",
             "terms": ["lithium battery", "solid-state battery", "energy storage", "battery"]},
        ],
    },
    "papers_per_direction": 2,
    "extra_papers_per_direction": 1,
    "excellent_score_threshold": 9.0,
    "max_papers": 8,
    "language": "zh-CN",
}


def paper(title: str, abstract: str, venue: str = PRL, *, doi: str = "", score: float = 10.0) -> dict:
    return {
        "title": title,
        "abstract": abstract,
        "venue": venue,
        "published": "2026-10-10",
        "source": "feed",
        "doi": doi,
        "score": score,
    }


class DirectionOnlyGroupingTests(unittest.TestCase):
    def test_no_featured_top_journal_group(self) -> None:
        """An off-topic priority-journal paper must NOT create a '顶刊' bucket or be listed."""
        off_topic = paper(
            "Magnetic recording media for next-generation hard disk drives",
            "We optimize granular magnetic recording media for areal density gains.",
            PRL, doi="10.1103/physrevlett.0001")
        grouped, unique = grb.group_recommendations([off_topic], CONFIG)
        names = {name for name, _ in grouped}
        self.assertNotIn("顶刊/已发表精选", names)
        self.assertNotIn("顶刊已发表精选", names)
        self.assertEqual(unique, [])  # off-topic paper excluded entirely
        # With no on-topic paper, no direction group should carry it either.
        for _name, items in grouped:
            self.assertNotIn(off_topic["doi"], [p.get("doi") for p in items])

    def test_on_topic_priority_journal_lands_in_direction(self) -> None:
        """A relevant PRL must still appear - just inside its direction group, not a '顶刊' block."""
        alt = paper(
            "Altermagnetic spin splitting and the anomalous Hall effect",
            "We report altermagnetic spin splitting in a compensated magnet and the resulting Hall response.",
            PRL, doi="10.1103/physrevlett.0002")
        grouped, unique = grb.group_recommendations([alt], CONFIG)
        names = {name for name, _ in grouped}
        self.assertIn("交错磁", names)
        self.assertNotIn("顶刊/已发表精选", names)
        flat = [p.get("doi") for _n, items in grouped for p in items]
        self.assertIn(alt["doi"], flat)
        self.assertIn(alt["doi"], [p.get("doi") for p in unique])

    def test_paper_topic_no_longer_returns_top_journal(self) -> None:
        off_topic = paper(
            "Magnetic recording media for hard disk drives",
            "Granular magnetic recording media optimization.", PRL, doi="10.1103/prl.9001")
        alt = paper(
            "Altermagnetic spin splitting in compensated magnets",
            "Altermagnetic spin splitting and anomalous Hall.", PRL, doi="10.1103/prl.9002")
        self.assertEqual(grb.paper_topic(off_topic, CONFIG), "未分类")
        self.assertNotEqual(grb.paper_topic(off_topic, CONFIG), "顶刊已发表精选")
        self.assertEqual(grb.paper_topic(alt, CONFIG), "交错磁")


class WavePhysicsGateTests(unittest.TestCase):
    def test_topological_photonics_excluded(self) -> None:
        """Bare 'topological' + 'photonic' must not admit topological photonics."""
        photonic = paper(
            "Topological photonics in a photonic crystal waveguide",
            "We realize topological photonics using a photonic crystal waveguide; the optical edge state shows robust transport.",
            NCOMMS, doi="10.1038/s41467-0001")
        self.assertTrue(grb.is_wave_physics(photonic))
        self.assertFalse(grb.is_domain_match(photonic, CONFIG))

    def test_altermagnet_phonon_kept(self) -> None:
        """Altermagnet + phonon keeps its matter anchor and must stay in scope."""
        alt_phonon = paper(
            "Phonon angular momentum in altermagnetic materials",
            "We predict a large phonon angular momentum in altermagnetic compounds.",
            PRB, doi="10.1103/physrevb.0003")
        self.assertFalse(grb.is_wave_physics(alt_phonon))
        self.assertTrue(grb.is_domain_match(alt_phonon, CONFIG))

    def test_electronic_topological_insulator_kept(self) -> None:
        ti = paper(
            "A magnetic topological insulator hosting the quantum anomalous Hall state",
            "We observe the quantum anomalous Hall effect in a magnetic topological insulator.",
            NCOMMS, doi="10.1038/s41467-0004")
        self.assertFalse(grb.is_wave_physics(ti))
        self.assertTrue(grb.is_domain_match(ti, CONFIG))


class MarkdownDirectionOnlyTests(unittest.TestCase):
    def test_markdown_has_no_featured_header(self) -> None:
        alt = paper(
            "Altermagnetic spin splitting and the anomalous Hall effect",
            "Altermagnetic spin splitting in a compensated magnet and the resulting Hall response.",
            PRL, doi="10.1103/prl.7001")
        ti = paper(
            "A magnetic topological insulator hosting the quantum anomalous Hall state",
            "Quantum anomalous Hall effect in a magnetic topological insulator.",
            NCOMMS, doi="10.1038/s41467.7002")
        md = grb.make_markdown([alt, ti], CONFIG, datetime.date(2026, 10, 10))
        self.assertNotIn("顶刊/已发表精选", md)
        self.assertIn("分方向推荐", md)


if __name__ == "__main__":
    unittest.main()
