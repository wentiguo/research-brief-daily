"""Tests for the 2026-10-11 experimental-venue gate.

User rule: experimental studies are only pushed from Nature/Science; theory,
computation and ML studies keep the normal journal allowlist. The classification
is theory-first: any theory/computation vocabulary in title+abstract means the
paper is NOT experimental, so a DFT paper that compares against spectroscopy is
never misclassified and dropped.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import generate_research_brief as grb  # noqa: E402

CONFIG = {
    "experimental_venue_gate": {
        "enabled": True,
        "allowed_venues": ["Nature", "Science"],
    },
}


def paper(title: str, abstract: str, venue: str, *, doi: str = "") -> dict:
    return {"title": title, "abstract": abstract, "venue": venue, "doi": doi,
            "published": "2026-10-11", "source": "feed"}


class ExperimentalClassificationTests(unittest.TestCase):
    def test_measurement_abstract_is_experimental(self) -> None:
        p = paper("Room-temperature ferromagnetism in a layered crystal",
                  "We measured the magnetization with SQUID magnetometry; the resistivity shows a sharp anomaly at the Neel temperature.",
                  "Physical Review Letters")
        self.assertTrue(grb.is_experimental_study(p))

    def test_dft_paper_is_not_experimental_even_with_spectroscopy(self) -> None:
        """Theory-first: DFT + comparison to ARPES must stay computational."""
        p = paper("First-principles study of altermagnetic spin splitting",
                  "Density functional theory calculations predict a large spin splitting, consistent with ARPES spectroscopy on similar compounds.",
                  "Physical Review B")
        self.assertFalse(grb.is_experimental_study(p))

    def test_ml_paper_is_not_experimental(self) -> None:
        p = paper("Machine learning screening of topological materials",
                  "A graph neural network trained on high-throughput DFT data predicts new topological insulators.",
                  "npj Computational Materials")
        self.assertFalse(grb.is_experimental_study(p))

    def test_no_signal_text_conservative(self) -> None:
        """Neither list matches -> conservative: NOT experimental (never dropped unjudged)."""
        p = paper("A note on layered materials", "", "Physical Review Letters")
        self.assertFalse(grb.is_experimental_study(p))

    def test_mixed_simulation_and_measurement_is_theory_first(self) -> None:
        p = paper("Model of skyrmion dynamics",
                  "We simulate the dynamics of skyrmions; the simulated hysteresis loops qualitatively reproduce experimental observations.",
                  "Physical Review B")
        self.assertFalse(grb.is_experimental_study(p))


class ExperimentalVenueGateTests(unittest.TestCase):
    def test_experimental_prl_dropped(self) -> None:
        p = paper("Spin texture measured by ARPES in a layered magnet",
                  "We measured the spin texture using ARPES on single crystals grown by flux method.",
                  "Physical Review Letters")
        self.assertFalse(grb.passes_experimental_venue_gate(p, CONFIG))

    def test_experimental_nature_kept(self) -> None:
        p = paper("Spin texture measured by ARPES in a layered magnet",
                  "We measured the spin texture using ARPES on single crystals grown by flux method.",
                  "Nature")
        self.assertTrue(grb.passes_experimental_venue_gate(p, CONFIG))

    def test_experimental_science_kept(self) -> None:
        p = paper("Direct observation of altermagnetic domains",
                  "X-ray microscopy experiments reveal altermagnetic domain patterns.",
                  "Science")
        self.assertTrue(grb.passes_experimental_venue_gate(p, CONFIG))

    def test_nature_communications_is_not_nature(self) -> None:
        """Exact-venue matching: 'Nature Communications' must NOT satisfy the gate."""
        p = paper("Spin texture measured by ARPES in a layered magnet",
                  "We measured the spin texture using ARPES on single crystals grown by flux method.",
                  "Nature Communications")
        self.assertFalse(grb.passes_experimental_venue_gate(p, CONFIG))

    def test_computational_prl_kept(self) -> None:
        p = paper("Tight-binding model of altermagnetic magnon-phonon coupling",
                  "We derive an effective model Hamiltonian and predict a hybridized magnon-phonon band structure.",
                  "Physical Review Letters")
        self.assertTrue(grb.passes_experimental_venue_gate(p, CONFIG))

    def test_computational_nano_letters_kept(self) -> None:
        p = paper("High-throughput screening of ferroelectric polymers",
                  "Machine learning models trained on DFT calculations screen 10k candidates for energy storage applications.",
                  "Nano Letters")
        self.assertTrue(grb.passes_experimental_venue_gate(p, CONFIG))

    def test_experimental_preprint_dropped(self) -> None:
        p = paper("Magnetotransport in a candidate altermagnet",
                  "We report transport measurements on single crystals; the resistivity and Hall effect are discussed.",
                  "arXiv")
        p["url"] = "https://arxiv.org/abs/2610.12345"
        self.assertFalse(grb.passes_experimental_venue_gate(p, CONFIG))

    def test_gate_disabled_passes_everything(self) -> None:
        p = paper("Spin texture measured by ARPES in a layered magnet",
                  "We measured the spin texture using ARPES on single crystals grown by flux method.",
                  "Physical Review Letters")
        self.assertTrue(grb.passes_experimental_venue_gate(
            p, {"experimental_venue_gate": {"enabled": False}}))


if __name__ == "__main__":
    unittest.main()
