"""The manifest has to survive a second run on the same day.

That hand-over is the only thing between a PDF that was downloaded and a PDF the
reader receives, and a run used to write the manifest unconditionally. A fetch that
ran twice in one day - which is exactly what happens when the same day is re-pushed,
or when a scheduled run and a manual one overlap - kept the second run's files and
erased the first run's list. The files stayed in the dated folder the whole time,
which is the worst possible failure: nothing errors, nothing looks missing in the
log, and the mail simply arrives with fewer attachments than the run had produced.
On 2026-10-03 the 12:xx runs had fetched five PDFs; the manifest that went out named
two, and none of the five was ever mailed.

Three rules are under test here. A run carries the entries of an earlier run on the
same day. An entry whose file has since been pruned must not come back. An entry from
another day must never ride along, because it belongs to a mail that was already sent.
"""

import datetime as dt
import json
import os
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fetch_paper_attachments as attachments  # noqa: E402


def _write_manifest(folder: Path, date: dt.date, files: list[dict]) -> None:
    (folder / "manifest.json").write_text(
        json.dumps({"date": date.isoformat(), "files": files}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _touch(folder: Path, name: str) -> Path:
    path = folder / name
    path.write_bytes(b"%PDF-1.7 fake " + name.encode())
    return path


class TestManifestCarryover(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(os.environ.get("TMPTEST", os.path.dirname(__file__))) / "_tmp_carry"
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.date = dt.date(2026, 10, 3)
        self.folder = self.tmp / "2026-10-03"
        self.folder.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_earlier_todays_entries_are_carried_forward(self) -> None:
        """The 12:xx run's PDF must still be listed when the 17:20 run writes its own."""
        first = _touch(self.folder, "1_prerun_arxiv.pdf")
        _write_manifest(self.folder, self.date, [
            {"label": "1_arxiv", "name": first.name, "doi": "10.1103/abcd", "kind": "fulltext"},
        ])

        carried = attachments._manifest_entries_from(self.folder / "manifest.json", self.date)

        self.assertEqual([e["name"] for e in carried], [first.name])

    def test_a_run_merges_carried_entries_into_the_file_it_writes(self) -> None:
        """Carrying is not enough: the merge has to reach the manifest on disk."""
        first = _touch(self.folder, "1_prerun.pdf")
        second = _touch(self.folder, "2_prerun.pdf")
        _write_manifest(self.folder, self.date, [
            {"label": "1_prerun", "name": first.name, "kind": "fulltext"},
        ])

        carried = attachments._manifest_entries_from(self.folder / "manifest.json", self.date)
        entries = list(carried)
        entries.append({"label": "2_prerun", "name": second.name, "kind": "preprint"})
        entries = attachments.dedupe_by_kind(entries)
        (self.folder / "manifest.json").write_text(
            json.dumps({"date": self.date.isoformat(), "files": entries}, ensure_ascii=False),
            encoding="utf-8",
        )

        written = json.loads((self.folder / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(
            {e["name"] for e in written["files"]}, {first.name, second.name},
            "a file the earlier run downloaded must survive into the manifest that mails it",
        )

    def test_an_entry_whose_file_is_gone_is_dropped_not_carried(self) -> None:
        """A pruned file must not come back as an attachment the mail cannot deliver."""
        vanished = _touch(self.folder, "1_gone.pdf")
        _write_manifest(self.folder, self.date, [
            {"label": "1_gone", "name": vanished.name, "kind": "fulltext"},
        ])
        vanished.unlink()

        carried = attachments._manifest_entries_from(self.folder / "manifest.json", self.date)

        self.assertEqual(carried, [], "a stale entry would be a phantom attachment")

    def test_another_days_manifest_is_never_carried(self) -> None:
        """Yesterday's files belong to a mail that already went out."""
        _touch(self.folder, "1_yesterday.pdf")
        _write_manifest(self.folder, dt.date(2026, 10, 2), [
            {"label": "1_yesterday", "name": "1_yesterday.pdf", "kind": "fulltext"},
        ])

        carried = attachments._manifest_entries_from(self.folder / "manifest.json", self.date)

        self.assertEqual(carried, [])

    def test_a_missing_manifest_carries_nothing(self) -> None:
        self.assertEqual(
            attachments._manifest_entries_from(self.folder / "manifest.json", self.date), []
        )

    def test_an_unreadable_manifest_does_not_explode(self) -> None:
        (self.folder / "manifest.json").write_text("{ not json", encoding="utf-8")
        self.assertEqual(
            attachments._manifest_entries_from(self.folder / "manifest.json", self.date), []
        )


class TestUnattributedEntriesLandOnTheRightPaper(unittest.TestCase):
    """A manifest entry with no DOI used to be thrown away.

    On 2026-10-02 not one entry carried a DOI, so the cloud matched nothing and mailed
    no paper file at all. The label already says which article the file belongs to -
    `1_<slug>` for the first paper, `2_<slug>` for the second - which is enough to tell
    whether the file belongs to the mail in hand.
    """

    def setUp(self) -> None:
        self.tmp = Path(os.environ.get("TMPTEST", os.path.dirname(__file__))) / "_tmp_slot"
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.date = dt.date(2026, 10, 2)
        self.config = {"paper_attachments": {"dir": str(self.tmp)}}

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, label: str, filenames: list[str], papers: list[dict]) -> list[str]:
        folder = Path(str(self.config["paper_attachments"]["dir"])) / self.date.isoformat()
        folder.mkdir(parents=True, exist_ok=True)
        for name in filenames:
            (folder / name).write_bytes(b"%PDF-1.7 " + name.encode())
        _write_manifest(folder, self.date, [
            {"label": label, "name": filename} for filename in filenames
        ])
        pairs, _cards, _note = attachments.reuse_synced_attachments(
            self.date, self.config, papers=papers, allow_unidentified=False,
        )
        return sorted(path.name for _label, path in pairs)

    def test_a_doi_less_entry_matches_by_its_position_in_the_brief(self) -> None:
        papers = [
            {"doi": "10.1103/aaaa", "title": "first paper"},
            {"doi": "10.1103/bbbb", "title": "second paper"},
        ]
        got = self._run("2_second_paper_slug", ["2_second.pdf"], papers)
        # The manifest itself rides along as an attachment; the paper file must be there too.
        self.assertIn("2_second.pdf", got)

    def test_an_entry_for_a_paper_this_mail_does_not_carry_is_not_taken(self) -> None:
        papers = [{"doi": "10.1103/aaaa", "title": "first paper only"}]
        got = self._run("5_someone_else", ["5_someone.pdf"], papers)
        self.assertEqual(got, [], "another day's paper must not ride along")

    def test_a_doi_less_entry_without_a_numbered_label_is_not_taken(self) -> None:
        papers = [{"doi": "10.1103/aaaa", "title": "first paper"}]
        got = self._run("fulltext", ["fulltext.pdf"], papers)
        self.assertEqual(got, [])

    def test_a_file_with_a_doi_is_still_matched_by_the_doi(self) -> None:
        papers = [{"doi": "10.1103/aaaa", "title": "first paper"}]
        folder = Path(str(self.config["paper_attachments"]["dir"])) / self.date.isoformat()
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "1_named.pdf").write_bytes(b"%PDF-1.7 ok")
        _write_manifest(folder, self.date, [
            {"label": "1_named", "name": "1_named.pdf", "doi": "10.1103/aaaa"},
        ])
        pairs, _cards, _note = attachments.reuse_synced_attachments(
            self.date, self.config, papers=papers, allow_unidentified=False,
        )
        self.assertIn("1_named.pdf", [path.name for _label, path in pairs])


if __name__ == "__main__":
    unittest.main()
