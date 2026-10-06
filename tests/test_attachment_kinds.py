"""回归锁：一篇论文只能随信带一份全文，不能每条合法通道各带一份。

历史 bug：`legal_routes` 会列出同一篇论文的每一处合法全文（Unpaywall OA PDF、arXiv
预印本、OpenAlex 副本、出版商官方端点），每一条都返回自己的字节。旧的去重只比字节
（prune_duplicate_files 按 sha256）和文件名（produced），于是 arXiv 那份 PDF 和出版商
那份 PDF 同时进目录、同时进 manifest、同时进邮件——读者收到同一篇论文的两份"全文"。
2026-10-03 实测目录里就有三组：PRL radiowave、PRX topological mixed states、
Nature Materials 各两套。
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import fetch_paper_attachments as fpa  # noqa: E402

DATE = dt.date(2026, 10, 3)
ARXIV = "https://arxiv.org/pdf/2606.09755v2"
# `aps_pdf_candidates` walks the APS shortnames, so the publisher's endpoint for this
# PRL paper is journals.aps.org/prl/pdf/<DOI> - not the link.aps.org form that
# `publisher_pdf_urls` hands out for open-access APS articles.
APSL = "https://journals.aps.org/prl/pdf/10.1103/PhysRevLett.135.09755"
TITLE = "Radiowave induced resistance oscillations in a topological bilayer"


class _Env(unittest.TestCase):
    """A dated attachment folder with the network switched out for a disk."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="kind-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # The run logs the citation card as a path relative to the working directory, so
        # the fake run has to happen inside one; the repository root is the only cwd the
        # real workflow ever uses.
        self._cwd = Path.cwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, self._cwd)
        self.config = {"paper_attachments": {"enabled": True, "dir": "research_briefs/attachments",
                                             "max_file_mb": 15, "max_files_per_paper": 4}}
        self.paper = {"doi": "10.1103/PhysRevLett.135.09755", "title": TITLE,
                      "venue": "Physical Review Letters"}

    def _run(self, routes, *, about=True):
        """Drive one paper through the real loop with _save stubbed to a real write."""
        out_dir = Path("research_briefs/attachments") / DATE.isoformat()

        def _save(url, target, max_mb):          # noqa: ANN001, ANN202 - stand-in
            target.write_bytes(b"%PDF-1.4 stub " + url.encode())
            return True

        with mock.patch.object(fpa, "unpaywall_locations", return_value={}), \
             mock.patch.object(fpa, "legal_routes", return_value=routes), \
             mock.patch.object(fpa, "_file_is_about", return_value=about), \
             mock.patch.object(fpa.time, "sleep"), \
             mock.patch.object(fpa, "_save", side_effect=_save):
            saved, cards, _note = fpa.fetch_paper_attachments([self.paper], DATE, self.config)
        return saved, out_dir


class TestOneFullTextPerPaper(_Env):
    def test_two_fulltext_routes_keep_only_the_better_one(self) -> None:
        # The publisher's own PDF (tier 3) is listed before the arXiv preprint (tier 4):
        # 2026-10-04 起版本规则改为「有出版版就用出版版，arXiv 只兜底」，之前 arXiv 在
        # tier 2、出版商端点在 tier 4，于是 PRL/PRX 这类文章发给读者的是预印本。
        saved, out_dir = self._run([(3, APSL, "出版商官方 PDF 端点（robots 允许）"),
                                    (4, ARXIV, "arXiv 公开预印本")])
        names = sorted(path.name for _label, path in saved)
        self.assertEqual(len(names), 1, f"one article, one full text: {names}")
        self.assertIn("physrevlett", names[0].lower()), names
        self.assertEqual(len(list(out_dir.glob("*.pdf"))), 1)
        self.assertEqual(len([n for n in names if "2606_09755" in n]), 0,
                         "the lower-priority preprint must not reach the reader")

    def test_a_failed_high_priority_route_leaves_the_kind_untaken(self) -> None:
        """A route that delivers nothing must not block the one behind it."""
        out_dir = Path(self.tmp / "att") / DATE.isoformat()

        def _save_dead(url, target, max_mb):     # noqa: ANN001, ANN202 - stand-in
            target.write_bytes(b"%PDF-1.4 stub " + url.encode())
            return url != APSL                    # the publisher route 403s, the preprint works

        with mock.patch.object(fpa, "unpaywall_locations", return_value={}), \
             mock.patch.object(fpa, "legal_routes",
                               return_value=[(3, APSL, "出版商官方 PDF 端点（robots 允许）"),
                                             (4, ARXIV, "arXiv 公开预印本")]), \
             mock.patch.object(fpa, "_file_is_about", return_value=True), \
             mock.patch.object(fpa.time, "sleep"), \
             mock.patch.object(fpa, "_save", side_effect=_save_dead):
            saved, _cards, _note = fpa.fetch_paper_attachments([self.paper], DATE, self.config)
        names = sorted(path.name for _label, path in saved)
        self.assertEqual(len(names), 1, names)
        self.assertIn("2606_09755", names[0]), names

    def test_fulltext_and_its_own_supplement_both_travel(self) -> None:
        supp = "https://static.example.org/41563_2026_2768_moesm2_esm.xlsx"
        saved, _out = self._run([(2, ARXIV, "arXiv 公开预印本"),
                                 (6, supp, "出版商公开补充材料（直链）")])
        names = sorted(path.name for _label, path in saved)
        self.assertEqual(len(names), 2, f"the article plus its supplement: {names}")


class TestPublishedVersionBeatsPreprint(unittest.TestCase):
    """版本规则：出版版在位时用出版版，arXiv 预印本只是兜底。

    这条不变量原先靠 add() 的调用顺序和一个分散的数字表维持——APS 论文在 tier 4、
    arXiv 在 tier 2，结果发货的是预印本。现在排序键 `_route_rank` 显式带了一层
    「出版版 vs 预印本」，所以调用顺序怎么改都不会翻过来。
    """

    DOI = "10.1103/PhysRevLett.135.09755"
    ARXIV_URL = "https://arxiv.org/pdf/2606.09755v2"
    APS_URL = "https://journals.aps.org/prl/pdf/10.1103/PhysRevLett.135.09755"

    def _routes(self):
        with mock.patch.object(fpa, "unpaywall_locations", return_value={}), \
             mock.patch.object(fpa, "arxiv_pdf",
                               return_value={"url": self.ARXIV_URL, "title": "2606.09755"}), \
             mock.patch.object(fpa, "openalex_oa", return_value=[]), \
             mock.patch.object(fpa, "semantic_scholar_oa", return_value={}), \
             mock.patch.object(fpa, "abstract_if_empty", return_value={}), \
             mock.patch.object(fpa, "doaj_article", return_value=[]), \
             mock.patch.object(fpa, "europepmc_oa", return_value=[]), \
             mock.patch.object(fpa, "landing_pdf_links", return_value=("", [])), \
             mock.patch.object(fpa, "landing_supplements", return_value=("", [])):
            return fpa.legal_routes({"doi": self.DOI, "title": TITLE,
                                     "venue": "Physical Review Letters"})

    def test_the_publisher_endpoint_ranks_ahead_of_the_preprint(self) -> None:
        routes = self._routes()
        tiers = {url: priority for priority, url, _src in routes}
        self.assertIn(self.APS_URL, tiers)
        self.assertIn(self.ARXIV_URL, tiers)
        self.assertLess(tiers[self.APS_URL], tiers[self.ARXIV_URL])

    def test_no_preprint_outranks_a_publisher_copy(self) -> None:
        tiers = [(p, url) for p, url, _src in self._routes()]
        first_preprint = next((p for p, url in tiers if "arxiv.org" in url), None)
        last_publisher = max((p for p, url in tiers
                              if p <= fpa._PRIO_PUBLISHER_VOR and "arxiv.org" not in url),
                             default=None)
        self.assertNotEqual(first_preprint, None, "the fixture must offer a preprint")
        self.assertLess(last_publisher, first_preprint,
                        "every publisher copy must sit ahead of every preprint")

    def test_the_rank_key_encodes_the_rule_not_the_tier_numbers(self) -> None:
        rank = fpa._route_rank
        preprint = (fpa._PRIO_PREPRINT, self.ARXIV_URL, "arXiv 公开预印本")
        publisher = (fpa._PRIO_PUBLISHER_VOR, self.APS_URL, "出版商官方 PDF 端点")
        self.assertLess(rank(publisher), rank(preprint))
        # The second key is what makes the rule survive a reordering of the calls in
        # legal_routes: the flag, not the tier number, decides.
        self.assertEqual((rank(publisher)[1], rank(preprint)[1]), (0, 1))


class TestDedupeByKind(unittest.TestCase):
    """The manifest must never claim two full texts of one article."""

    def test_the_second_fulltext_entry_is_dropped(self) -> None:
        entries = [{"name": "a_arxiv.pdf", "kind": "fulltext", "doi": "10.1/x"},
                   {"name": "b_publisher.pdf", "kind": "fulltext", "doi": "10.1/x"},
                   {"name": "c_esm.xlsx", "kind": "supplement", "doi": "10.1/x"}]
        kept = fpa.dedupe_by_kind(entries)
        self.assertEqual([e["name"] for e in kept], ["a_arxiv.pdf", "c_esm.xlsx"])

    def test_an_entry_without_a_kind_survives(self) -> None:
        kept = fpa.dedupe_by_kind([{"name": "card.txt"}, {"name": "card.txt", "kind": "fulltext"}])
        self.assertEqual([e["name"] for e in kept], ["card.txt", "card.txt"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
