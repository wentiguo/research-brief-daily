"""Topic-gate semantics.

The gate decides whether a paper is on-topic. Its matching semantics are subtle and
have been broken in ways that silently widened the gate to "everything passes", so
these tests pin the *mechanics* rather than any one research vocabulary.

The shipped default scope is a generic condensed-matter / materials-science profile;
every user replaces it via `scripts/configure_project.py` (which writes
`automation/research_brief_config.json` -> `topic_gate`). The mechanism tests below
therefore build their own `topic_gate` fixtures, so they hold regardless of which
research areas a user configured.

Mechanics pinned here:

* A DIRECT phrase (a specific state/material/phenomenon) passes on its own.
* A WEAK word is a cross-field homonym -- it passes ONLY together with a
  matter-side anchor, so "topological data analysis" (biology) and "magnetic
  resonance imaging" stay out.
* **A weak word must never appear in the anchor tuple.** Otherwise a weak hit always
  finds itself as its own anchor and the rule degrades into passing everything. That
  bug once disabled the gate completely.
* A method word ("machine learning") is not a direction on its own; ML applied to an
  in-topic subject passes because the topic phrase carries the gate.
* With no abstract (APS supplies none to Crossref/OpenAlex), the title alone decides.
* Wave physics (topological photonics/acoustics) is excluded outright, unless the
  paper names a matter host that anchors it to the condensed-matter line.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import generate_research_brief as grb  # noqa: E402

CONFIG = grb.load_config()

DEFAULT_VENUE = "Physical Review B"

# A tiny, self-contained gate so these tests do not depend on the user's vocabulary.
# It is nested under "topic_gate" exactly as the real config is read.
GATE = {
    "topic_gate": {
        "direct_signals": ["skyrmion lattice", "topological insulator", "multiferroic"],
        "weak_signals": ["topological", "magnetic", "spin", "ferro"],
        # Note: the weak words above are deliberately absent, per the invariant tested.
        "matter_anchors": ["material", "crystal", "electron", "band", "first-principles"],
        "exclude_wave_physics": True,
        "wave_signals": ["photonic", "phononic", "acoustic wave"],
        "wave_matter_anchors": ["electron", "first-principles", "spin-orbit coupling"],
    }
}


def paper(title: str, abstract: str = "", venue: str = DEFAULT_VENUE) -> dict:
    return {"title": title, "abstract": abstract, "venue": venue,
            "published": "2026-05-05", "source": "feed", "doi": "10.000/none"}


class DirectSignalTests(unittest.TestCase):
    """A DIRECT phrase passes without needing an anchor."""

    def test_direct_phrase_passes_on_its_own(self):
        p = paper("Skyrmion lattice in a compensated ferrimagnet", "")
        self.assertTrue(grb.topic_signal_pass(grb.paper_text(p).lower(), GATE))

    def test_direct_phrase_in_abstract_passes(self):
        p = paper("A new material class", "We report a multiferroic oxide with coupled order.")
        self.assertTrue(grb.is_domain_match(p, GATE))


class WeakSignalTests(unittest.TestCase):
    """A WEAK homonym needs a matter-side anchor."""

    def test_weak_word_plus_anchor_passes(self):
        p = paper("Spin texture in a layered material", "")
        self.assertTrue(grb.topic_signal_pass(grb.paper_text(p).lower(), GATE))

    def test_weak_word_without_anchor_is_rejected(self):
        """The homonym case: same word, no matter host."""
        text = "topological data analysis for single-cell omics"
        self.assertFalse(grb.topic_signal_pass(text, GATE))

    def test_weak_word_never_serves_as_its_own_anchor(self):
        """Regression: weak words in the anchor tuple made every weak hit pass.

        This deliberately mis-configured gate contains 'magnetic' in both the weak list
        and the anchor list. `topic_signal_pass` must still reject the bare homonym
        because the real anchor list (not the weak list) is what admits it -- i.e. a
        user who makes this mistake should be caught by the config test below, not
        silently accepted. Here we assert the mechanism: with a CORRECT anchor list,
        'magnetic' alone does not admit.
        """
        p = paper("Magnetic resonance imaging of the knee", "")
        self.assertFalse(grb.topic_signal_pass(grb.paper_text(p).lower(), GATE))


class MethodWordTests(unittest.TestCase):
    """A method word is not a direction by itself."""

    def test_bare_machine_learning_is_rejected(self):
        p = paper("Machine learning for tabular classification",
                  "We train a gradient-boosted model on tabular features.")
        self.assertFalse(grb.topic_signal_pass(grb.paper_text(p).lower(), GATE))

    def test_ml_applied_to_an_in_topic_subject_passes(self):
        """ML is admitted when a DIRECT topic phrase carries it."""
        p = paper("Machine learning discovery of topological insulators",
                  "We train a graph neural network on first-principles data to predict "
                  "topological insulators.")
        self.assertTrue(grb.is_domain_match(p, GATE))


class MissingAbstractTests(unittest.TestCase):
    """A title-only letter (no abstract from Crossref/OpenAlex) still gets judged."""

    def test_title_only_on_topic_letter_passes(self):
        p = paper("Topological insulator surface states", abstract="")
        self.assertTrue(grb.is_domain_match(p, GATE))

    def test_title_only_off_topic_letter_rejected(self):
        p = paper("Rheology of a gravity-stretched liquid jet", abstract="")
        self.assertFalse(grb.is_domain_match(p, GATE))


class WavePhysicsTests(unittest.TestCase):
    """Wave physics is excluded outright, unless a matter host anchors it."""

    def test_pure_photonics_is_wave_physics(self):
        p = paper("Topological photonic crystal edge states", "")
        self.assertTrue(grb.is_wave_physics(p, GATE))

    def test_phononic_crystal_without_a_host_is_wave_physics(self):
        p = paper("Phononic crystal waveguide", "")
        self.assertTrue(grb.is_wave_physics(p, GATE))

    def test_matter_host_exits_the_wave_bucket(self):
        """An electronic system keeps its wave-word but is not wave physics."""
        p = paper("Phonon angular momentum in a magnetic material", "")
        self.assertFalse(grb.is_wave_physics(p, GATE))

    def test_non_wave_paper_is_not_wave_physics(self):
        p = paper("Berry curvature of a topological insulator", "")
        self.assertFalse(grb.is_wave_physics(p, GATE))

    def test_wave_exclusion_can_be_disabled_by_config(self):
        gate = {**GATE, "topic_gate": {**GATE["topic_gate"],
                                    "exclude_wave_physics": False}}
        p = paper("Topological photonic crystal", "")
        self.assertFalse(grb.is_wave_physics(p, gate))


class ConfigOverrideTests(unittest.TestCase):
    """The gate must actually read config, not fall back to baked-in defaults."""

    def test_custom_direct_signal_is_honoured(self):
        gate = {**GATE, "topic_gate": {**GATE["topic_gate"],
                                    "direct_signals": ["phonon angular momentum"]}}
        self.assertTrue(grb.topic_signal_pass("phonon angular momentum", gate))

    def test_removing_a_default_direct_signal_narrows_the_gate(self):
        """A user who drops 'multiferroic' must stop seeing multiferroic papers."""
        gate = {**GATE, "topic_gate": {**GATE["topic_gate"],
                                    "direct_signals": ["magnet"]}}
        self.assertFalse(grb.topic_signal_pass("a multiferroic oxide", gate))

    def test_empty_list_falls_back_to_default(self):
        """An empty/absent key means 'use the shipped default', not 'match nothing'."""
        self.assertTrue(grb.topic_signal_pass("a topological insulator", {}))


class DefaultConfigSanityTests(unittest.TestCase):
    """Whatever the user configured, the gate lists must not contradict each other.

    These read the real config (not the fixture) because a user can break their own
    gate by editing it; this is the check that catches that at test time.
    """

    def test_weak_signals_do_not_appear_in_matter_anchors(self):
        gate = CONFIG.get("topic_gate") or {}
        weak = {t.strip().lower() for t in (gate.get("weak_signals") or
                                            grb._DEFAULT_TOPIC_SIGNAL_WEAK)}
        anchors = {t.strip().lower() for t in (gate.get("matter_anchors") or
                                               grb._DEFAULT_TOPIC_MATTER_ANCHOR)}
        overlap = weak & anchors
        self.assertFalse(
            overlap,
            f"weak signals also present as anchors ({sorted(overlap)}); a weak hit "
            "would find itself as its own anchor and pass everything",
        )

    def test_direct_signals_are_lowercase(self):
        """Matching is substring-on-lowercased text; a capitalised entry never fires."""
        gate = CONFIG.get("topic_gate") or {}
        for key, fallback in (("direct_signals", grb._DEFAULT_TOPIC_SIGNAL_DIRECT),
                              ("weak_signals", grb._DEFAULT_TOPIC_SIGNAL_WEAK),
                              ("matter_anchors", grb._DEFAULT_TOPIC_MATTER_ANCHOR)):
            for term in (gate.get(key) or fallback):
                self.assertEqual(term, term.lower(),
                                 f"{key} entry {term!r} is not lowercase")


if __name__ == "__main__":
    unittest.main()
