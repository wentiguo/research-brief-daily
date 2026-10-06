"""回归锁：附件去重必须删掉所有副本，包括本次抓取自己列了两遍的那些。

历史 bug：一份 PDF 经两条通道各存一份（出版商端点 + 预印本），两份都在 `saved` 里时
互相"保护"，结果一个都不删，目录里留下 29 个文件而只有 18 份不同内容。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fetch_paper_attachments import prune_duplicate_files  # noqa: E402


class TestPruneDuplicates(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="prune-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.out_dir = self.tmp / "attachments" / "2026-10-02"
        self.out_dir.mkdir(parents=True)

    def _write(self, name: str, payload: bytes) -> Path:
        path = self.out_dir / name
        path.write_bytes(payload)
        return path

    def test_every_copy_but_the_first_is_deleted(self) -> None:
        self.setUp()
        first = self._write("b_copy.pdf", b"identical" * 500)
        second = self._write("a_copy.pdf", b"identical" * 500)
        third = self._write("c.pdf", b"different" * 500)
        # Both duplicates are listed as fetched: that is exactly the case that used to
        # protect each other and leave both on disk.
        kept = prune_duplicate_files(self.out_dir, [("paper", first), ("paper", second),
                                                    ("paper", third)])
        survivors = sorted(path.name for _label, path in kept)
        # The rule is the lexicographically first name, so a_copy wins over b_copy even
        # though b_copy was the one the fetch pass touched first.
        assert survivors == ["a_copy.pdf", "c.pdf"], survivors
        assert not first.exists(), "b_copy.pdf sorts later, so it is the copy that goes"
        assert second.exists() and third.exists()

    def test_files_not_in_the_fetched_set_are_still_culled(self) -> None:
        self.setUp()
        keeper = self._write("1_publisher.pdf", b"pdf-bytes" * 900)
        leftover = self._write("browser_saved_1全文.pdf", b"pdf-bytes" * 900)
        unrelated = self._write("other.pdf", b"other-bytes")
        kept = prune_duplicate_files(self.out_dir, [("paper", keeper), ("paper", unrelated)])
        assert not leftover.exists(), "a stale copy of a file still on the list must not survive"
        assert sorted(path.name for _label, path in kept) == ["1_publisher.pdf", "other.pdf"]

    def test_the_citation_card_survives(self) -> None:
        self.setUp()
        card = self._write("2026-10-02_题录与获取指引.txt", b"citation card")
        payload = b"pdf-bytes" * 400
        body = self._write("1_paper.pdf", payload)
        kept = prune_duplicate_files(self.out_dir, [("paper", body)])
        # The card is appended to the mail list after the prune, so what matters here is
        # that pruning never eats it off disk.
        assert card.exists(), "the citation card must survive a prune"
        assert sorted(path.name for _label, path in kept) == ["1_paper.pdf"]


if __name__ == "__main__":
    unittest.main(verbosity=2)
