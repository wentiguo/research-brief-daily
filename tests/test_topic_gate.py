"""The topic gate the user actually asked for, expressed as regression tests.

Four rules were agreed on 2026-10-04, and each one had a hole in the running code:

1. Broad-scope journals (Nature Communications, Science, Advanced Materials) stay in the
   pool but every item must map onto one of the four directions. They used to get in on
   the strength of the journal alone.
2. "Machine learning x direction" means machine learning *and* a core direction. A paper
   whose only vocabulary hit is `machine learning` / `inverse design` / `active learning`
   is off-topic - and `machine learning` sitting in `core_topics` made that hole easy to
   miss, because the strict vocabulary returned True on it.
3. Theory and computation first; a paper whose only evidence is a lab measurement is
   ranked after the theory work, not alongside it.
4. Wave physics (topological photonics, acoustics, phononic crystals) is excluded
   outright, not kept as a last-resort fallback.

The trap in this file is the phonon boundary. Bare `phonon` is a wave signal, so the
reader's own phonon-angular-momentum line would have been dropped, while the
ion-trap Berry-phase-of-a-single-phonon PRL correctly stays wave physics. The line is
drawn by the matter host: `magnetocrystalline` / `phonon angular momentum` / `magnon` are
anchors that pull a phonon paper back onto the condensed-matter side.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import generate_research_brief as grb  # noqa: E402

CONFIG = {
    "research_profile": {
        "priority_topics": ["magnetism", "magnetic", "topological insulator",
                            "multiferroic materials", "machine learning magnetism"],
        "core_topics": ["magnetism", "topological insulator", "multiferroic materials",
                        "magnetoelectric materials", "ferroelectric materials",
                        "topological materials", "quantum materials", "machine learning"],
        "recommendation_groups": [
            {"name": "交错磁", "terms": ["magnetic"]},
            {"name": "拓扑绝缘体", "terms": ["topological insulator"]},
        ],
        "domain_keywords": ["magnetism", "topological insulator", "multiferroic"],
    },
    "journals": ["Nature Communications", "Physical Review B"],
    "evaluation_model": {"theory_terms": ["tight-binding", "first-principles", "hamiltonian"]},
    "theory_preference": {"enabled": True, "experiment_penalty": -4.0},
}

NCOMMS = "Nature Communications"
PRB = "Physical Review B"


def paper(title: str, abstract: str, venue: str = NCOMMS) -> dict:
    return {"title": title, "abstract": abstract, "venue": venue,
            "published": "2026-10-02", "source": "feed"}


class TopicGateTests(unittest.TestCase):
    """Rule 1 + 2: broad-scope journals filtered by content, ML never on its own."""

    def test_bare_topological_word_does_not_admit_an_omics_paper(self) -> None:
        """Topological data analysis for single-cell omics: cross-field homonym, no host."""
        p = paper("Topological data analysis for single-cell omics",
                  "Topological data analysis for single-cell omics. We apply persistent homology "
                  "to classify cell states and predict developmental trajectories.")
        self.assertFalse(grb.is_domain_match(p, CONFIG))

    def test_ml_method_words_alone_do_not_admit_a_materials_paper(self) -> None:
        """Machine learning for inverse design of materials: ML without a direction."""
        p = paper("Machine learning for inverse design of functional materials",
                  "Machine learning for inverse design of functional materials. We perform "
                  "materials discovery via active learning with a surrogate model.")
        self.assertFalse(grb.is_domain_match(p, CONFIG))

    def test_virus_design_paper_is_not_admitted(self) -> None:
        p = paper("Generative design of lipid nanoparticles for RNA delivery",
                  "A generative model designs lipid nanoparticles for RNA vaccine delivery "
                  "with high-throughput screening.")
        self.assertFalse(grb.is_domain_match(p, CONFIG))

    def test_ml_with_a_core_direction_is_admitted(self) -> None:
        p = paper("Machine learning discovery of magnetic materials",
                  "Machine learning discovery of magnetic materials. We train a graph "
                  "neural network on first-principles data and predict the anomalous Hall effect.")
        self.assertTrue(grb.is_domain_match(p, CONFIG))

    def test_strong_topic_word_is_admitted(self) -> None:
        p = paper("Altermagnetic spin splitting and the spin Nernst effect",
                  "Altermagnetic spin splitting drives the spin Nernst effect in compensated "
                  "magnets, computed with first-principles density functional theory.")
        self.assertTrue(grb.is_domain_match(p, CONFIG))

    def test_weak_topic_word_with_a_matter_anchor_is_admitted(self) -> None:
        """PRX 'Topological Mixed States' says topological *order*; the host is electronic."""
        p = paper("Topological mixed states in an electronic lattice model",
                  "Topological mixed states in an electronic system. The lattice model is solved "
                  "diagonally with an effective Hamiltonian and a band structure reconstruction.")
        self.assertTrue(grb.is_domain_match(p, CONFIG))

    def test_weak_topic_word_without_a_matter_anchor_is_rejected(self) -> None:
        p = paper("Topological order in a quantum simulator",
                  "Topological order detected in a trapped ion array; we measure entanglement "
                  "entropy across quench trajectories.")
        self.assertFalse(grb.is_domain_match(p, CONFIG))

    def test_title_only_admission_still_works_when_abstract_is_missing(self) -> None:
        """The 2026-10-03 magnon-Chern PRL has no abstract anywhere and must stay in."""
        p = {"title": "Magnon Chern insulator in two-dimensional fully compensated "
                      "ferrimagnetic heterostructures",
             "abstract": "", "venue": "Physical Review Letters", "published": "2026-10-02"}
        self.assertTrue(grb.is_domain_match(p, CONFIG))


class WaveBoundaryTests(unittest.TestCase):
    """Rule 4, and the phonon host boundary that rule 4 must not break."""

    def test_topological_photonics_is_wave_physics(self) -> None:
        p = paper("Topological photonics in a photonic crystal waveguide",
                  "Topological photonics in a photonic crystal waveguide: robust light transport "
                  "around a microcavity lattice, measured via optical near-field imaging.")
        self.assertTrue(grb.is_wave_physics(p))

    def test_phononic_crystal_is_wave_physics(self) -> None:
        p = paper("Topological phononic crystal with band gaps",
                  "We observe topologically protected edge states in a phononic crystal "
                  "waveguide lattice.")
        self.assertTrue(grb.is_wave_physics(p))

    def test_ion_trap_phonon_stays_wave_physics(self) -> None:
        """No material host: a single phonon in an ion trap is not a condensed-matter host."""
        p = {"title": "Observation of topological Berry phases with a single phonon in an "
                      "ion microtrap array",
             "abstract": "", "venue": "Physical Review X", "published": "2026-10-03"}
        self.assertTrue(grb.is_wave_physics(p))

    def test_hosted_phonon_angular_momentum_is_not_wave_physics(self) -> None:
        """Phonons, but hosted in a magnetic crystal: the host wins its exit."""
        p = paper("Phonon angular momentum texture in a magnetic crystal",
                  "We compute the phonon angular momentum from first-principles density "
                  "functional theory and scan the unit cell across the phase diagram.")
        self.assertFalse(grb.is_wave_physics(p))

    def test_optical_conductivity_of_a_magnetic_metal_is_not_wave_physics(self) -> None:
        """optical is a wave signal here, but the host is an electronic magnet."""
        p = paper("Optical conductivity signature of magnetic spin splitting",
                  "We compute the optical conductivity of a magnetic metal from first-principles "
                  "DFT with Berry curvature.")
        self.assertFalse(grb.is_wave_physics(p))

    def test_magnon_chern_insulator_is_not_wave_physics(self) -> None:
        p = {"title": "Magnon Chern insulator in two-dimensional fully compensated "
                      "ferrimagnetic heterostructures",
             "abstract": "", "venue": "Physical Review Letters", "published": "2026-10-02"}
        self.assertFalse(grb.is_wave_physics(p))


class TheoryPreferenceTests(unittest.TestCase):
    """Rule 3: theory first, lab-only work ranked after it."""

    def test_experiment_only_paper_is_penalised(self) -> None:
        p = paper("Ferroelectric stripe domains in epitaxial films",
                  "We image ferroelectric stripe domains experimentally with the transport "
                  "measurement recorded in a magneto-optical setup.",
                  venue=PRB)
        score, reasons = grb.score_paper(p, CONFIG)
        self.assertLess(score, 0)
        self.assertTrue(any("实验为主" in r for r in reasons))

    def test_paper_with_theory_vocabulary_is_not_penalised(self) -> None:
        p = paper("Tight-binding model of magnetic spin splitting",
                  "We build a tight-binding model and an effective Hamiltonian for the "
                  "magnetic phase.", venue=PRB)
        score, _ = grb.score_paper(p, CONFIG)
        self.assertGreaterEqual(score, 0)

    def test_paper_showing_neither_lab_nor_model_is_neutral(self) -> None:
        p = paper("Altermagnetic spin splitting",
                  "Altermagnetic spin splitting and band topology in compensated magnets.",
                  venue=PRB)
        score, reasons = grb.score_paper(p, CONFIG)
        self.assertGreaterEqual(score, 0)
        self.assertFalse(any("实验为主" in r for r in reasons))

    def test_disabled_preference_removes_the_penalty(self) -> None:
        cfg = {"theory_preference": {"enabled": False}, "evaluation_model": {"theory_terms": []}}
        p = paper("Ferroelectric domains", "We measured the domains experimentally in a thin film.")
        score, _ = grb.score_paper(p, cfg)
        self.assertGreaterEqual(score, 0)


class GroupOrderTests(unittest.TestCase):
    """Direction groups claim papers before the featured list does.

    On 2026-10-03 the featured list was filled first, so all six papers landed in
    'priority-journal selected' and every direction group reported 'no candidates'.
    """

    def setUp(self) -> None:
        self.groups = [
            {"name": "交错磁", "terms": ["magnetic"]},
            {"name": "拓扑绝缘体", "terms": ["topological insulator"]},
        ]
        self.config = {
            "research_profile": {"recommendation_groups": self.groups},
            "papers_per_direction": 2,
            "extra_papers_per_direction": 1,
            "excellent_score_threshold": 9.0,
            "max_papers": 8,
        }

    def test_direction_group_gets_the_directly_on_topic_paper(self) -> None:
        p = paper("Altermagnetic spin splitting in compensated magnets",
                  "Altermagnetic spin splitting and the anomalous Hall effect.", venue=PRB)
        p["score"] = 12.0
        p["doi"] = "10.1103/aaaa-bbbb"
        grouped, unique = grb.group_recommendations([p], self.config)
        names = {name for name, _ in grouped}
        self.assertIn("交错磁", names)
        self.assertEqual([p["doi"] for _n, items in grouped for p in items], ["10.1103/aaaa-bbbb"])
        self.assertTrue(unique)


if __name__ == "__main__":
    unittest.main()
