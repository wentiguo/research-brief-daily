"""The local -> cloud hand-over, and the one line it used to lie about.

A local run fetches what the cloud cannot; the files land in
`research_briefs/attachments/<date>/` and the cloud mail reads the manifest back. Two
things about that hand-over are load bearing and easy to break: only a manifest for *this*
day may be reused, and the citation card which travels with the papers is not one of them.
The second is why a mail once reported `attached 1 paper files` when every attachment
pass had fetched 0 - the card was sitting in the paper list.
"""

import datetime as dt
import json
import os
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from fetch_paper_attachments import reuse_synced_attachments  # noqa: E402


class TestSyncedAttachmentReuse(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(os.environ.get("TMPTEST", os.path.dirname(__file__))) / "_tmp_reuse"
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.date = dt.date(2026, 10, 2)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _folder(self, stamped: str, with_card: bool = True, with_manifest: bool = True) -> Path:
        """A day's folder the way the fetch pass leaves it, with `paper_attachments.dir` aimed here."""
        out_dir = self.tmp / stamped
        out_dir.mkdir(parents=True, exist_ok=True)
        paper = out_dir / "1_paper.pdf"
        paper.write_bytes(b"%PDF-1.4 the article")
        if with_card:
            (out_dir / f"{stamped}_题录与获取指引.txt").write_text("routes", encoding="utf-8")
        if with_manifest:
            (out_dir / "manifest.json").write_text(json.dumps({
                "date": stamped,
                "files": [{"label": "PRB", "name": paper.name, "sha256": "x", "bytes": 21}],
            }), encoding="utf-8")
        return out_dir

    def _config(self) -> dict:
        # The folder is named by `paper_attachments.dir` + the run date, so aim it at the temp tree.
        return {"paper_attachments": {"dir": str(self.tmp)}}

    def test_what_the_manifest_lists_is_what_travels(self) -> None:
        """The manifest is the hand-over list, not something the reader is handed.

        It used to be offered as an attachment too, which put a file called
        manifest.json in the mailbox, and worse: it made this function report a paper on
        a day whose full text had not arrived, so the caller stopped waiting for the
        local bridge and mailed the brief immediately. Only its entries travel now.
        """
        self._folder(self.date.isoformat())
        pairs, cards, _note = reuse_synced_attachments(self.date, self._config())
        self.assertEqual([path.name for _label, path in pairs], ["1_paper.pdf"])
        self.assertEqual([path.name for path in cards],
                         [f"{self.date.isoformat()}_题录与获取指引.txt"])

    def test_the_citation_card_is_not_counted_as_a_paper(self) -> None:
        """This is the `attached 1 paper files` bug: the card must not sit in the paper list."""
        self._folder(self.date.isoformat())
        pairs, cards, _note = reuse_synced_attachments(self.date, self._config())
        self.assertEqual(len(cards), 1)
        self.assertEqual([path.name for path in cards], ["2026-10-02_题录与获取指引.txt"])
        self.assertTrue(all(path.suffix.lower() != ".txt" for _label, path in pairs))
        self.assertEqual(1, sum(1 for _label, path in pairs if path.suffix.lower() == ".pdf"))

    def test_a_file_about_another_paper_is_left_behind(self) -> None:
        """2026-10-03 mailed 12 of yesterday's PDFs under one citation.

        The date on the manifest is not the question; whether the file belongs to a paper
        in this mail is. With the mail's DOIs known, anything else stays on the disk.
        """
        out_dir = self._folder(self.date.isoformat())
        (out_dir / "manifest.json").write_text(json.dumps({
            "date": self.date.isoformat(),
            "files": [
                {"label": "PRL", "name": "1_paper.pdf", "doi": "10.1103/wh99-b81w"},
                {"label": "PRB", "name": "2_yesterday.pdf", "doi": "10.1103/old-one-0001"},
            ],
        }), encoding="utf-8")
        (out_dir / "2_yesterday.pdf").write_bytes(b"%PDF-1.4 someone else")
        pairs, cards, _note = reuse_synced_attachments(
            self.date, self._config(), papers=[{"doi": "10.1103/wh99-b81w"}])
        names = [path.name for _label, path in pairs]
        self.assertNotIn("2_yesterday.pdf", names)
        self.assertIn("1_paper.pdf", names)
        self.assertTrue(out_dir.exists())

    def test_an_unidentified_file_is_kept_only_when_asked_for(self) -> None:
        """A manifest entry with no DOI cannot be vouched for, so it is withheld by default."""
        self._folder(self.date.isoformat())
        pairs, _cards, _note = reuse_synced_attachments(
            self.date, self._config(), papers=[{"doi": "10.1103/unrelated-0002"}])
        self.assertNotIn("1_paper.pdf", [path.name for _label, path in pairs])
        pairs, _cards, _note = reuse_synced_attachments(
            self.date, self._config(), papers=[{"doi": "10.1103/unrelated-0002"}],
            allow_unidentified=True)
        self.assertIn("1_paper.pdf", [path.name for _label, path in pairs])

    def test_a_paper_named_in_the_manifest_travels(self) -> None:
        """The day's real work still reaches the mail: named in the manifest, present on disk.

        The manifest naming it no longer comes along - see the note above.
        """
        out_dir = self._folder(self.date.isoformat())
        pairs, _cards, _note = reuse_synced_attachments(self.date, self._config())
        names = [path.name for _label, path in pairs]
        self.assertNotIn("manifest.json", names)
        self.assertIn("1_paper.pdf", names)
        self.assertTrue(all(path.exists() for _label, path in pairs))
        self.assertTrue(out_dir.exists())

    def test_another_days_manifest_is_refused(self) -> None:
        """A manifest from another run is another day's work; it must not ride this mail."""
        self._folder(self.date.isoformat(), with_card=False)
        pairs, cards, _note = reuse_synced_attachments(self.date + dt.timedelta(days=1),
                                                       self._config())
        self.assertEqual(pairs, [])
        self.assertEqual(cards, [])

    def test_a_manifest_dated_elsewhere_is_refused_with_three_values(self) -> None:
        """A manifest that landed under today's folder but belongs to yesterday must not ride.

        The refusal used to return a 2-tuple from a function documented to return three,
        so the moment the branch was really taken the caller's `pairs, cards, note = ...`
        raised ValueError, the broad `except Exception` around it swallowed the warning,
        and the mail went out with no attachments and nothing to explain why. The test
        below puts the manifest in *today's* folder with yesterday's stamp on it, which
        is the only shape that reaches the branch.
        """
        out_dir = self._folder(self.date.isoformat(), with_card=False)
        (out_dir / "manifest.json").write_text(json.dumps({
            "date": "2026-10-01",
            "files": [{"label": "PRB", "name": "1_paper.pdf", "sha256": "x"}],
        }), encoding="utf-8")
        pairs, cards, note = reuse_synced_attachments(self.date + dt.timedelta(days=1),
                                                      self._config())
        self.assertEqual(pairs, [])
        self.assertEqual(cards, [])
        self.assertEqual(note, "")

    def test_one_pdf_under_two_names_is_one_attachment(self) -> None:
        """Two routes can hand over the same bytes under two names; the mail wants one copy.

        Pruning already collapses this before the manifest is written, but a manifest
        assembled by hand - or by a run predating the pruning - still lists both, and
        identity by path is not identity by content.
        """
        out_dir = self._folder(self.date.isoformat(), with_card=False)
        twin = out_dir / "1_paper_v2.pdf"
        twin.write_bytes((out_dir / "1_paper.pdf").read_bytes())
        (out_dir / "manifest.json").write_text(json.dumps({
            "date": self.date.isoformat(),
            "files": [
                {"label": "PRB", "name": "1_paper.pdf", "sha256": "x"},
                {"label": "PRB", "name": "1_paper_v2.pdf", "sha256": "x"},
            ],
        }), encoding="utf-8")
        pairs, _cards, _note = reuse_synced_attachments(self.date, self._config())
        names = [path.name for _label, path in pairs]
        self.assertEqual(names.count("1_paper.pdf"), 1)
        self.assertEqual(names.count("1_paper_v2.pdf"), 0)

    def test_an_empty_manifest_still_offers_the_routes(self) -> None:
        """A cloud run that fetched nothing still owes the reader the legal routes.

        That run writes a manifest with no files and a citation card; the card is the only
        part of the hand-over it can still honour, and it must not be dropped for being
        the only thing left.
        """
        out_dir = self._folder(self.date.isoformat(), with_card=True)
        (out_dir / "manifest.json").write_text(
            json.dumps({"date": self.date.isoformat(), "files": []}), encoding="utf-8")
        pairs, cards, _note = reuse_synced_attachments(self.date, self._config())
        # No *papers* came over, and saying otherwise is what short-circuited the wait
        # for the local bridge. The card itself still travels.
        self.assertEqual([path.name for _label, path in pairs], [])
        self.assertEqual([path.name for path in cards],
                         [f"{self.date.isoformat()}_题录与获取指引.txt"])
        self.assertEqual([path.name for _label, path in pairs if path.suffix.lower() == ".pdf"], [])
        self.assertEqual([path.name for path in cards],
                         [f"{self.date.isoformat()}_题录与获取指引.txt"])


if __name__ == "__main__":
    unittest.main()
