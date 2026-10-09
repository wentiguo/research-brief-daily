"""回归锁：简报要说人话，而且要说对。

2026-10-04 那封邮件里，读者发现两个问题，两个都不是笔误而是方法错：

1. 头条论文（Nature Communications，铁电向列相液晶体相介相建模）的「创新点总结」写的是
   「本文提出针对…的建模方法，可能填补了…空白；具体模型形式与假设需阅读全文确认」。
   根因：`complete_truncated_abstracts` 只给 `feed_source == "aps feed"` 的论文补全摘要，
   Nature 那篇从来没进过补全；落地页上摆着 1670 字符真摘要，喂给 DeepSeek 的却是
   96 字符的出版商引文行。摘要都没有，自然只剩下「需要读全文」这类空话。

2. 同一封邮件里，正文写「全文获取：开放获取（可下载 PDF）」，题录卡写
   「开放获取：否（订阅期刊，无开放全文）」。根因：正文按刊级 OA 事实判断（Nature
   Communications 全 OA，判断无误），题录卡按 Unpaywall 的单点结果判断，而 Unpaywall
   对发表当天的 DOI 返回 404（尚未索引）——「查不到」被当成「确认没有」，于是凭空
   断言这是一篇订阅刊论文。

这两条锁住的是「宁可标注不知道，也不要编一个听起来确定的结论」。
"""

from __future__ import annotations

import datetime as dt
import unittest
from unittest import mock

import inspect  # noqa: E402
from pathlib import Path  # noqa: E402
import sys  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import deepseek_client as dsc  # noqa: E402
import fetch_paper_attachments as fpa  # noqa: E402
import generate_research_brief as grb  # noqa: E402
import publisher_feeds as pf  # noqa: E402

# The Nature article that reached the mailbox with a citation line instead of an abstract.
NATURE_ABSTRACT = (
    "Ferroelectric nematic liquid crystals (FNLCs) are emerging materials that combine "
    "orientational order with spontaneous macroscopic polarization. They exhibit "
    "distinctive properties, such as polar domain formation, field-tunable textures, and "
    "strong electro-elastic couplings. However, due to the complexity of the underlying "
    "mechanisms, the bulk structure of the emergent mesophases is not yet fully understood, "
    "and a generalized description of all observed phases is still lacking. Here, we "
    "demonstrate the bulk mesophases of FNLCs as stabilized by a combination of "
    "flexoelectricity, elasticity, bulk ordering, and polarization-nematic couplings."
)
# What the feed actually carried for that paper in the 2026-10-04 run.
CITATION_LINE = "Nature Communications, Published online: 03 October 2026; doi:10.1038/s41467-026-78039-1"


class TestAbstractCompletionIsNotAnAPSLoophole(unittest.TestCase):
    """摘要补全的门槛看摘要本身，不看它从哪个 feed 来。"""

    def _complete(self, papers, *, http):
        with mock.patch.object(pf, "http_bytes", side_effect=http), \
             mock.patch.object(pf.time, "sleep"):
            return pf.complete_truncated_abstracts(papers, {}, contact="r@example.org")

    @staticmethod
    def _short_page(abstract: str) -> bytes:
        page = ('<html><body><div class="c-article-section__content" id="Abs1-content">'
                f"<p>{abstract}</p></div></body></html>")
        return page.encode()

    def test_a_non_aps_paper_with_a_citation_line_gets_completed(self) -> None:
        """回归场景：Nature 论文只有引文行，也必须被补全。"""
        seen: list[str] = []

        def http(url, *args, **kwargs):
            seen.append(url)
            return self._short_page(NATURE_ABSTRACT), 200

        paper = {"doi": "10.1038/s41467-026-78039-1", "title": "Bulk mesophases",
                 "venue": "Nature Communications", "abstract": CITATION_LINE,
                 "abstract_source": "publisher feed", "feed_source": "Nature feed",
                 "feed_slug": ""}
        completed = self._complete([paper], http=http)
        self.assertEqual(completed, 1)
        self.assertTrue(len(paper["abstract"]) > len(CITATION_LINE))
        self.assertEqual(paper["abstract_source"], "publisher page")
        self.assertFalse(paper["abstract_truncated"], "a completed abstract is no longer truncated")

    def test_a_aps_paper_still_serves_its_own_landing_page(self) -> None:
        """APS 的专属落地页还在，不该因为换了判据而丢掉。"""
        seen: list[str] = []

        def http(url, *args, **kwargs):
            seen.append(url)
            # The generic DOI route first: a DOI that answers with nothing leaves APS to
            # prove the abstract is really there.
            if "journals.aps.org" in url:
                return self._short_page(NATURE_ABSTRACT), 200
            return b"<html></html>", 200

        paper = {"doi": "10.1103/PhysRevLett.135.09755", "title": "T",
                 "venue": "Physical Review Letters", "abstract": "Physics. ",
                 "feed_source": "aps feed", "feed_slug": "prl"}
        self._complete([paper], http=http)
        self.assertTrue(any("journals.aps.org" in url for url in seen))

    def test_a_good_landing_page_ends_the_search_without_the_aps_request(self) -> None:
        """通用落地页已经拿到完整摘要时，APS 页就别再请求一次。"""
        seen: list[str] = []

        def http(url, *args, **kwargs):
            seen.append(url)
            return self._short_page(NATURE_ABSTRACT), 200

        paper = {"doi": "10.1103/PhysRevLett.135.09755", "title": "T",
                 "venue": "Physical Review Letters", "abstract": "Physics. ",
                 "feed_source": "aps feed", "feed_slug": "prl"}
        self._complete([paper], http=http)
        self.assertFalse(any("journals.aps.org" in url for url in seen))

    def test_a_complete_abstract_is_left_alone(self) -> None:
        """摘要已经够长就别再发一次请求。"""
        seen: list[str] = []

        def http(url, *args, **kwargs):
            seen.append(url)
            return self._short_page(NATURE_ABSTRACT), 200

        paper = {"doi": "10.1038/x", "title": "T", "abstract": NATURE_ABSTRACT,
                 "feed_source": "Nature feed"}
        self._complete([paper], http=http)
        self.assertEqual(seen, [], "a complete abstract must not cost a request")

    def test_an_empty_abstract_is_completed(self) -> None:
        """摘要干脆没有时也要补，否则 LLM 只能从题名猜。"""
        paper = {"doi": "10.1038/x", "title": "T", "venue": "Nature Communications"}

        def http(url, *args, **kwargs):
            return self._short_page(NATURE_ABSTRACT), 200

        self.assertEqual(self._complete([paper], http=http), 1)

    def test_a_fresh_nature_page_yields_the_abstract(self) -> None:
        """真实结构（Nature 新版 `Abs1-content`）能被抽出来。"""
        text = pf.extract_landing_abstract(self._short_page(NATURE_ABSTRACT).decode())
        self.assertIn("Ferroelectric nematic liquid crystals", text)
        self.assertGreater(len(text), len(CITATION_LINE))


class TestOpenAccessVerdictIsNotGuesswork(unittest.TestCase):
    """题录卡可以把「查不到」写成「未知」，但不能写成「订阅期刊」。"""

    def _card(self, oa):
        return fpa._citation_card(
            {"title": "Bulk mesophases", "venue": "Nature Communications",
             "doi": "10.1038/s41467-026-78039-1", "authors": "Aditya Vats"},
            1, oa, {"url": ""}, "https://www.nature.com/articles/s41467-026-78039-1",
            [], [], "无", ["  · [x] url → 未取到"],
        )

    def test_unindexed_doi_is_reported_as_unknown_not_as_paywalled(self) -> None:
        """Unpaywall 404（未索引）不等于「该刊是订阅刊」。"""
        card = self._card({"available": False, "is_oa": False})
        self.assertIn("未知", card)
        self.assertNotIn("订阅期刊", card,
                         "an unindexed DOI may not be described as a subscription journal")

    def test_a_confirmed_non_oa_paper_may_say_so(self) -> None:
        card = self._card({"available": True, "is_oa": False})
        self.assertIn("Unpaywall 查询过", card)

    def test_a_confirmed_oa_paper_says_yes(self) -> None:
        card = self._card({"available": True, "is_oa": True})
        self.assertIn("是", card)
        self.assertNotIn("订阅期刊", card)


class TestLandingPageLinksAreFiltered(unittest.TestCase):
    """落地页上绝大多数 .pdf 链接都不是正文。"""

    def test_the_reference_pdf_is_not_an_article_route(self) -> None:
        self.assertFalse(fpa._is_article_pdf("https://www.nature.com/articles/x_reference.pdf"))
        self.assertFalse(fpa._is_article_pdf("https://x.com/figures/fig-1.pdf"))

    def test_the_article_pdf_passes(self) -> None:
        url = ("https://media.springernature.com/original/springer-static/esm/"
               "art%3A10.1038%2Fs41467-026-78039-1/MediaObjects/41467_2026_78039_MOESM1_ESM.pdf")
        self.assertTrue(fpa._is_article_pdf(url))

    def test_the_nature_full_text_address_is_not_offered_as_a_route(self) -> None:
        """带落地页 cookie 实测 2026-10-04 仍返回 text/html；占位只会白花一次请求。"""
        self.assertFalse(fpa._is_article_pdf("https://www.nature.com/articles/s41467-026-77973-4.pdf"))

    def test_the_nature_support_page_is_not_a_supplement(self) -> None:
        """"supp" 是 "support" 的子串，这条支持页曾经被当成补充材料交出去。"""
        page = ('<a href="https://support.nature.com/support/home">Support</a>'
                '<a href="https://media.springernature.com/original/springer-static/esm/'
                'art%3A10.1038%2Fs41467-026-77973-4/MediaObjects/41467_2026_77973_MOESM1_ESM.pdf">'
                'Supplementary file 1</a>')
        with mock.patch.object(fpa, "_landing_cached", return_value=("https://x", page)):
            _, found = fpa.landing_supplements("10.1038/s41467-026-77973-4")
        self.assertEqual(len(found), 1, found)
        self.assertIn("MOESM1_ESM.pdf", found[0])


class TestDeadNaturePdfRouteIsGone(unittest.TestCase):
    """`nature.com/articles/<suffix>.pdf` 在 2026 年新版站点上已经不是 PDF 了。"""

    def test_no_nature_direct_pdf_candidate_is_offered(self) -> None:
        urls = fpa.publisher_pdf_urls("10.1038/s41467-026-78039-1")
        self.assertEqual(urls, [], "the direct Nature PDF link answers HTML; only APS remains")

    def test_aps_open_access_route_is_untouched(self) -> None:
        self.assertEqual(fpa.publisher_pdf_urls("10.1103/PhysRevLett.135.09755"),
                         ["https://link.aps.org/pdf/10.1103/PhysRevLett.135.09755"])


class TestAReSendOfTheSameDayKeepsItsPapers(unittest.TestCase):
    """同一天重跑是把当天内容再送一遍；不该因为「今天发过了」就筛成 0 篇。"""

    def history(self, brief_date, ids):
        return {"sent_papers": [{"id": i, "brief_date": brief_date} for i in ids]}

    def test_papers_sent_today_stay_in_the_pool(self) -> None:
        today = dt.date(2026, 10, 4)
        history = self.history("2026-10-04", ["doi:10.1038/x"])
        kept = grb.filter_seen_papers([{"doi": "10.1038/x", "title": "T"}], history,
                                      run_date=today)
        self.assertEqual(len(kept), 1, "a re-send must not arrive empty")

    def test_papers_sent_on_another_day_are_still_skipped(self) -> None:
        today = dt.date(2026, 10, 4)
        history = self.history("2026-10-03", ["doi:10.1038/x"])
        kept = grb.filter_seen_papers([{"doi": "10.1038/x", "title": "T"}], history,
                                      run_date=today)
        self.assertEqual(kept, [])

    def test_both_days_are_tracked(self) -> None:
        today = dt.date(2026, 10, 4)
        # history ids are keyed `doi:<doi>`, so today's own paper and yesterday's differ
        # only by the day they were sent.
        history = self.history("2026-10-04", ["doi:today"])
        history["sent_papers"].append({"id": "doi:older", "brief_date": "2026-10-03"})
        papers = [{"doi": "today", "title": "TODAY"}, {"doi": "older", "title": "OLDER"}]
        kept = grb.filter_seen_papers(papers, history, run_date=today)
        self.assertEqual([p["title"] for p in kept], ["TODAY"])



class TestOversizedFilesAreNotTruncatedIntoGarbage(unittest.TestCase):
    """截断成半截 PDF 比不下更糟：能打开，但内容是切口页面。"""

    def save(self, headers, body=b"%PDF-1.7 tail"):
        class _Resp:
            def __init__(self): self.headers = headers
            def read(self, n=-1):  # one shot, so the loop exercises once
                return body if n < 0 or n >= len(body) else body
            def close(self): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
        with mock.patch.object(fpa, "_open", return_value=_Resp()) as opener, \
             mock.patch("pathlib.Path.write_bytes") as write:
            ok = fpa._save("https://example.org/x.pdf", Path("x.pdf"), max_mb=15)
        return ok, write

    def test_a_21mb_nature_supplement_is_skipped_not_cut(self) -> None:
        ok, write = self.save({"Content-Type": "application/pdf",
                               "Content-Length": str(21_600_000)})
        self.assertFalse(ok, "a truncated 21 MB file is not a usable attachment")
        write.assert_not_called()

    def test_a_file_inside_the_cap_is_still_saved(self) -> None:
        ok, write = self.save({"Content-Type": "application/pdf",
                               "Content-Length": str(3_000_000)})
        self.assertTrue(ok)
        self.assertEqual(write.call_count, 1)


class TestCitationCardDoesNotContradictItself(unittest.TestCase):
    """题录卡的「开放获取」与「说明/获取途径」必须说同一件事。"""

    def card(self, oa):
        return fpa._citation_card(
            {"title": "T", "doi": "10.1038/s41467-026-77973-4", "venue": "Nature Communications"},
            1, oa, {}, "", [], [], "no routes", [])

    def test_an_open_access_paper_is_not_told_it_needs_a_subscription(self) -> None:
        text = self.card({"is_oa": True, "available": True, "landing": ""})
        self.assertIn("开放获取：是", text)
        self.assertNotIn("订阅", text, "开放获取的文章不该被建议走机构订阅")
        self.assertNotIn("受出版商订阅版权限制", text)

    def test_a_paper_with_no_open_copy_may_say_so(self) -> None:
        text = self.card({"is_oa": False, "available": True})
        self.assertIn("开放获取：否", text)
        self.assertIn("机构图书馆", text)

    def test_an_unindexed_doi_is_not_blamed_on_the_paywall(self) -> None:
        """Unpaywall 没索引 ≠ 付费墙；说明里不许把下落不明的文章写成订阅限制。"""
        text = self.card({"is_oa": False})
        self.assertIn("开放获取：未知", text)
        self.assertIn("尚未索引", text)
        note = [line for line in text.splitlines() if line.startswith("说明：")][0]
        self.assertNotIn("版权限制", note)
        self.assertNotIn("订阅", note)


class TestDeepSeekRestatesItsEvidence(unittest.TestCase):
    """解读要标注自己站在什么材料上，摘要级不能冒充全文级。"""

    def test_a_short_abstract_is_labelled_as_such(self) -> None:
        """摘要取到但只有引文行那么长时，标注出来，别让结论扮成读过全文。"""
        with mock.patch.object(grb, "deepseek_enabled", return_value=True), \
             mock.patch.object(grb, "innovation_summary", return_value="问题：x"):
            note = grb.deepseek_note({"title": "T", "venue": "Nature Communications",
                                      "abstract": CITATION_LINE})
        self.assertIn("依据题名与题录", note)
        self.assertEqual(note, "【依据题名与题录撰写】\n问题：x")

    def test_a_full_abstract_carries_no_scope_label(self) -> None:
        with mock.patch.object(grb, "deepseek_enabled", return_value=True), \
             mock.patch.object(grb, "innovation_summary", return_value="问题：x"):
            note = grb.deepseek_note({"title": "T", "venue": "Nature Communications",
                                      "abstract": NATURE_ABSTRACT})
        self.assertNotIn("仅据题名与题录", note)
        self.assertEqual(note, "问题：x")

    def test_no_abstract_at_all_produces_no_note(self) -> None:
        with mock.patch.object(grb, "deepseek_enabled", return_value=True), \
             mock.patch.object(grb, "innovation_summary", return_value="问题：x"):
            self.assertEqual(grb.deepseek_note({"title": "T", "venue": "Nature Communications"}), "")

    def test_the_summary_prompt_demands_substance(self) -> None:
        """prompt 要按四个维度要内容，并且明令禁止空话兜底。"""
        source = inspect.getsource(dsc.innovation_summary)
        for heading in ("问题：", "方法：", "结果：", "意义："):
            self.assertIn(heading, source, f"the summary prompt must ask for 「{heading}」")
        self.assertIn("需阅读全文确认", source,
                      "the phrase is named only so the model can see it is forbidden")

    def test_abstract_floor_is_defined(self) -> None:
        self.assertGreaterEqual(pf.MIN_USEFUL_ABSTRACT_CHARS, 300)
        self.assertEqual(grb.MIN_USEFUL_ABSTRACT_CHARS, pf.MIN_USEFUL_ABSTRACT_CHARS)


if __name__ == "__main__":
    unittest.main()
