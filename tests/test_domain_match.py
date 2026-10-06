"""Why 2026-10-03's brief carried exactly one Advanced Materials paper and no PRL.

That day Crossref reported 12 Physical Review Letters, 5 Physical Review X and
2 PRX Quantum items online. The brief pushed none of them. Two independent gates did
that, and both are re-tested here because each looks harmless on its own.

First, `is_domain_match` used to kill every priority-journal paper whose abstract was
empty. APS supplies Crossref and OpenAlex no abstract at all, so 11 of the 12 PRL items
were dropped before scoring - the condition was reading a missing field as "not my
field". Second, the wave-physics family (topological photons, phonons, acoustics) used
to sit in the same pool as magnetic and Berry-curvature work, which is not the same
research line.

The magnon / Chern-insulator paper is the fixture for both: it is a subscribed APS
letter with no abstract, it carries none of the strict topic vocabulary, and it is
exactly the kind of contribution that used to disappear.
"""

import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import generate_research_brief as grb  # noqa: E402

RUN_DATE = dt.date(2026, 10, 3)

# A real 2026-10-03 PRL item: "Magnon Chern insulator in two-dimensional fully
# compensated ferrimagnetic heterostructures". No abstract is available anywhere.
PRL_MAGNON_CHERN = {
    "title": "Magnon Chern insulator in two-dimensional fully compensated ferrimagnetic heterostructures",
    "abstract": "",
    "venue": "Physical Review Letters",
    "published": "2026-10-02",
    "doi": "10.1103/wh99-b81w",
}

# Also a real 2026-10-03 PRL item with no abstract anywhere: Compton scattering.
# It must stay out - a missing abstract must not be read as "therefore on-topic".
PRL_UNRELATED = {
    "title": "First measurement of Compton scattering on a trapped ion",
    "abstract": "",
    "venue": "Physical Review Letters",
    "published": "2026-10-03",
    "doi": "10.1103/gn45-7lpx",
}

MAGNON_CHERN = dict(PRL_MAGNON_CHERN, abstract="We report a magnon Chern band structure.")

# Ion-trap phonon paper, no magnetism and no material framework: wave physics.
PRL_ION_TRAP_PHONON = {
    "title": "Observation of topological Berry phases with a single phonon in an ion microtrap array",
    "abstract": "",
    "venue": "Physical Review X",
    "published": "2026-10-03",
    "doi": "10.1103/td5k-t1mf",
}

# Topological photonics, kept as the documented fallback family.
PRXQ_POLARITON = {
    "title": "Polariton-polariton coherent coupling in a molecular spin-superconductor chip",
    "abstract": "",
    "venue": "PRX Quantum",
    "published": "2026-10-03",
    "doi": "10.1103/xvjl-h44h",
}

# Not in the zone table at all, so it must not ride in on the missing-abstract bypass.
PR_THIRD_TIER = {
    "title": "Domain-wall conductivity in a third-tier journal",
    "abstract": "",
    "venue": "Physical Review Materials",
    "published": "2026-10-03",
    "doi": "10.1103/placeholder",
}

CONFIG = {
    "research_profile": {
        "priority_topics": ["magnetism", "topological insulator"],
        "core_topics": ["magnetic materials", "topological materials", "quantum materials"],
        "recommendation_groups": [
            {"name": "交错磁", "terms": ["magnetic"]},
        ],
        "domain_keywords": ["topology", "condensed matter", "machine learning"],
    },
    "journals": ["Physical Review Letters", "Physical Review B", "Physical Review X"],
    "journal_policy": {
        "allowlist_only": True,
        "allow_preprints": True,
        # The zones this fixture cares about, copied from automation/research_brief_config.json.
        "journal_zones": {
            "Physical Review Letters": {"zone": 1, "category": "物理与天体物理"},
            "Physical Review X": {"zone": 1, "category": "物理与天体物理"},
            "PRX Quantum": {"zone": 1, "category": "物理与天体物理"},
            "Physical Review B": {"zone": 2, "category": "物理与天体物理"},
        },
    },
}


class TestPriorityJournalNotKilledByMissingAbstract(unittest.TestCase):
    def test_magnon_chern_letter_without_abstract_is_kept(self) -> None:
        """The 2026-10-03 regression: an abstract-less PRL must not be refused outright."""
        self.assertTrue(grb.is_domain_match(PRL_MAGNON_CHERN, CONFIG))

    def test_abstract_hit_on_a_topic_still_matches(self) -> None:
        paper = dict(PRL_MAGNON_CHERN, abstract="We study magnetism in a magnetic lattice.")
        self.assertTrue(grb.is_domain_match(paper, CONFIG))

    def test_abstract_present_but_off_topic_is_dropped(self) -> None:
        """With an abstract available the full text must be judged, and a miss must miss."""
        paper = dict(PRL_UNRELATED, abstract="We study the rheology of a gravity-stretched liquid jet.")
        self.assertFalse(grb.is_domain_match(paper, CONFIG))

    def test_unrelated_aps_letter_without_abstract_is_dropped(self) -> None:
        """A missing abstract is not a licence: Compton scattering stays out."""
        self.assertFalse(grb.is_domain_match(PRL_UNRELATED, CONFIG))

    def test_non_priority_journal_still_needs_a_domain_keyword(self) -> None:
        self.assertFalse(grb.is_domain_match(PR_THIRD_TIER, CONFIG))


class TestWavePhysicsIsSeparated(unittest.TestCase):
    def test_pure_photonics_is_wave_physics(self) -> None:
        self.assertTrue(grb.is_wave_physics(PRXQ_POLARITON))

    def test_ion_trap_phonon_without_magnetism_is_wave_physics(self) -> None:
        self.assertTrue(grb.is_wave_physics(PRL_ION_TRAP_PHONON))

    def test_magnon_chern_insulator_is_not_wave_physics(self) -> None:
        """Magnons are magnetic quasiparticles, not the acoustic/optical family."""
        self.assertFalse(grb.is_wave_physics(PRL_MAGNON_CHERN))

    def test_ordinary_condensed_matter_claim_is_not_wave_physics(self) -> None:
        paper = {"title": "First-principles study of a磁性 topological insulator thin film",
                 "abstract": "", "venue": "Physical Review B"}
        self.assertFalse(grb.is_wave_physics(paper))


if __name__ == "__main__":
    unittest.main()
