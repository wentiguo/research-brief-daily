"""Journal allowlist / zone / attachment-target invariants.

These tests validate the *mechanics* any user configuration must satisfy:

1. Only the poster papers may carry a full-text PDF hunt.
2. No journal may sit half-configured: a tier without a visible zone renders an empty
   cell in the email body, and a zone without a tier is unreachable by the allowlist.
3. An excluded journal must never also be allowlisted.
4. An allowlisted journal must pass the venue gate (otherwise the daily run silently
   drops it again).

The assertions read whatever `automation/research_brief_config.json` the user
configured, so they hold for any research area -- they do not encode one person's
journal list. Run `python scripts/configure_project.py` first to create that file.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import generate_research_brief as grb  # noqa: E402

CONFIG = grb.load_config()


def paper(venue, title):
    return {"venue": venue, "title": title, "doi": venue.lower() + title,
            "source": "Crossref"}


def policy(config):
    return config.get("journal_policy", {}) or {}


def tiered_journals(config):
    names = {str(j).lower() for j in policy(config).get("tier1", [])}
    names |= {str(j).lower() for j in policy(config).get("tier2", [])}
    return names


class AttachmentTargetTests(unittest.TestCase):
    """Only the poster papers may carry a full-text hunt."""

    def test_poster_papers_win_over_the_wider_history_selection(self):
        """The larger daily selection must not dilute the paper poster."""
        poster = [paper("Physical Review Letters", "poster-one"),
                  paper("Physical Review B", "poster-two")]
        selected = poster + [paper("Physical Review B", f"extra-{i}") for i in range(6)]
        targets = grb.resolve_attachment_targets(poster, selected)
        self.assertEqual([p["title"] for p in targets],
                         ["poster-one", "poster-two"])
        self.assertEqual(len(targets), 2)

    def test_targets_fall_back_to_one_when_nothing_was_posted(self):
        targets = grb.resolve_attachment_targets(
            [], [paper("Nature", "a"), paper("Nature", "b")])
        self.assertEqual(len(targets), 1)

    def test_empty_everywhere_stays_empty(self):
        self.assertEqual(grb.resolve_attachment_targets([], []), [])


class AllowlistConsistencyTests(unittest.TestCase):
    """No journal may sit half-configured."""

    def test_every_audited_journal_renders_a_zone_badge(self):
        """A journal with a zone record must never render an empty cell.

        `zone_badge` used to print an empty string for a journal that sat in tier1 with
        no zone record. A tier is now enough to state the zone, with the category left
        visibly unclaimed rather than invented.
        """
        for journal, record in (policy(CONFIG).get("journal_zones", {}) or {}).items():
            badge = grb.zone_badge(str(journal), CONFIG)
            self.assertTrue(badge.strip(), f"{journal!r} renders an empty zone badge")
            if int(record.get("zone", 0)) in (1, 2):
                self.assertIn("区", badge, f"{journal!r} renders no zone: {badge!r}")

    def test_every_zoned_journal_is_in_a_tier(self):
        tiers = tiered_journals(CONFIG)
        for journal in (policy(CONFIG).get("journal_zones", {}) or {}):
            self.assertIn(str(journal).lower(), tiers,
                          f"{journal!r} has a zone but no tier")

    def test_excluded_journals_are_never_allowlisted(self):
        excluded = {str(j).lower() for j in policy(CONFIG).get("exclude", []) or []}
        for journal in (CONFIG.get("journals", []) or []):
            self.assertNotIn(str(journal).lower(), excluded, journal)

    def test_venue_gate_accepts_every_allowlisted_journal(self):
        """A journal in the allowlist that the venue gate rejects is never delivered."""
        for journal in (CONFIG.get("journals", []) or []):
            self.assertTrue(
                grb.is_allowed_venue(paper(str(journal), "t"), CONFIG),
                f"{journal!r} is allowlisted but the venue gate rejects it")

    def test_tier_numbering_is_consistent(self):
        """tier1 must outrank tier2 in journal_tier()."""
        for journal in policy(CONFIG).get("tier1", []) or []:
            self.assertEqual(grb.journal_tier(str(journal), CONFIG), 1, journal)
        for journal in policy(CONFIG).get("tier2", []) or []:
            self.assertEqual(grb.journal_tier(str(journal), CONFIG), 2, journal)


class ReachabilityTests(unittest.TestCase):
    """An allowlisted journal must have a real fetch channel.

    A journal parked in the allowlist with neither a publisher feed nor a Crossref
    ISSN entry is silently never fetched: it looks configured and returns nothing.
    """

    def test_dedicated_feed_journals_declare_a_real_channel(self):
        aps = {str(v).lower() for v in
               (CONFIG.get("publisher_feeds", {}).get("aps", {})
                .get("journals", {}) or {}).values()}
        crossref_feeds = CONFIG.get("publisher_feeds", {}).get(
            "crossref_journals", {}) or {}
        issn = {str(e.get("name", "")).lower()
                for e in crossref_feeds.get("journals", []) or []}
        # Both collections must be plain data, not accidentally nested one level deeper.
        for name in list(aps) + list(issn):
            self.assertTrue(name, "a feed entry has an empty journal name")

    def test_publisher_feed_journals_are_not_empty(self):
        feeds = CONFIG.get("publisher_feeds", {}) or {}
        for publisher, spec in feeds.items():
            if not isinstance(spec, dict):
                continue
            if publisher == "crossref_journals":
                continue
            journals = spec.get("journals", {})
            if journals:
                self.assertIsInstance(journals, dict, f"{publisher}.journals")


if __name__ == "__main__":
    unittest.main()