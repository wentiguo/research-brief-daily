"""The keyword search that walks journals instead of walking a table of contents.

The publisher feeds are tables of contents: a fixed list of titles, and whatever that
publisher chose to make public today. They can answer "what is new in PRL" but never
"what in the last two days is about your topic, wherever it appeared". OpenAlex can,
through a documented API that needs no key, verified 2026-10-04.

Two things about it are easy to get wrong and both are invisible until the reader
complains. Its abstract arrives as an inverted index and is empty if you do not rebuild
it, and an entry with no abstract degrades into a title-only row that can never pass the
strict-interest filter - so a broken rebuild looks exactly like "nothing new today".
And its `doi` field is a resolver URL, which has to come back as a bare DOI or the
citation card and the attachment lookup both key off a string that never matches.
"""

import json
import os
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import publisher_feeds as pf  # noqa: E402


def _work(title: str, *, date: str, doi: str, venue: str = "Physical Review B",
           abstract: dict | None = None, oa=None) -> dict:
    return {
        "id": "https://openalex.org/W" + doi[-6:],
        "doi": doi,
        "title": title,
        "publication_date": date,
        "primary_location": {"source": {"display_name": venue}},
        "authorships": [{"author": {"display_name": "A. Researcher"}}],
        "open_access": {"is_oa": oa},
        "concepts": [{"display_name": "condensed matter physics"}],
        **({"abstract_inverted_index": abstract} if abstract is not None else {}),
    }


class TestOpenAlexAbstractRebuild(unittest.TestCase):
    """An inverted index is a word-to-position map; the abstract has to be reassembled."""

    def test_the_words_go_back_into_order(self) -> None:
        index = {"magnet": [0], "splits": [1], "the": [2], "bands": [3], "of": [4],
                 "MoO": [5], "under": [6], "strain": [7]}
        self.assertEqual(
            pf._openalex_abstract({"abstract_inverted_index": index}),
            "magnet splits the bands of MoO under strain",
        )

    def test_a_repeated_word_sits_at_every_position_it_occups(self) -> None:
        index = {"the": [0, 3], "band": [1], "gap": [2]}
        self.assertEqual(pf._openalex_abstract({"abstract_inverted_index": index}), "the band gap the")

    def test_a_work_without_an_index_yields_no_abstract(self) -> None:
        self.assertEqual(pf._openalex_abstract({}), "")
        self.assertEqual(pf._openalex_abstract({"abstract_inverted_index": {}}), "")
        self.assertEqual(pf._openalex_abstract({"abstract_inverted_index": None}), "")

    def test_a_non_integer_slot_is_skipped_not_crashed_on(self) -> None:
        index = {"strain": [0], "switches": ["x"]}
        self.assertEqual(pf._openalex_abstract({"abstract_inverted_index": index}), "strain")


class TestOpenAlexKeywordSearch(unittest.TestCase):
    """The channel has to reach OpenAlex, normalise what comes back, and stay switched off
    when the config says so."""

    def setUp(self) -> None:
        self.tmp = Path(os.environ.get("TMPTEST", os.path.dirname(__file__))) / "_tmp_openalex"
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.real = pf.http_bytes
        self.calls: list[str] = []
        self.cutoff = "2026-10-02"

    def tearDown(self) -> None:
        pf.http_bytes = self.real
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _serve(self, works: list[dict]) -> None:
        payload = json.dumps({"results": works}).encode("utf-8")

        def fake(url: str, *, user_agent: str = "", timeout: int = 30, retries: int = 2,
                 backoff_seconds: float = 2.0, cache_ttl_seconds: int = 0):
            self.calls.append(url)
            return payload, "fresh"

        pf.http_bytes = fake

    def _config(self, block: dict) -> dict:
        return {"publisher_feeds": {"openalex_keyword_search": block},
                "research_profile": {"priority_topics": ["magnetism", "multiferroic"]}}

    def test_it_queries_the_documented_endpoint_for_every_term(self) -> None:
        self._serve([_work("A study", date="2026-10-03", doi="https://doi.org/10.1103/abcd1234")])
        pf.fetch_openalex_keyword_search(
            self._config({"enabled": True, "per_page": 10, "terms": ["magnetism", "multiferroic"]}),
            cutoff=self.cutoff, user_agent="brief (mailto:a@b.c)",
        )
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all("api.openalex.org/works" in url for url in self.calls))
        self.assertTrue(all("type:article" in url and "from_publication_date:" in url
                            for url in self.calls))

    def test_the_resolver_url_comes_back_as_a_bare_doi(self) -> None:
        self._serve([_work("A study", date="2026-10-03", doi="https://doi.org/10.1103/abcd1234")])
        papers = pf.fetch_openalex_keyword_search(
            self._config({"enabled": True, "terms": ["magnetism"]}),
            cutoff=self.cutoff, user_agent="brief (mailto:a@b.c)",
        )
        self.assertTrue(papers)
        self.assertEqual(papers[0]["doi"], "10.1103/abcd1234")
        self.assertEqual(papers[0]["url"], "https://doi.org/10.1103/abcd1234")

    def test_the_abstract_is_taken_from_the_inverted_index(self) -> None:
        self._serve([_work("A study", date="2026-10-03", doi="10.1/x",
                           abstract={"we": [0], "find": [1], "a": [2], "gap": [3]})])
        papers = pf.fetch_openalex_keyword_search(
            self._config({"enabled": True, "terms": ["magnetism"]}),
            cutoff=self.cutoff, user_agent="brief (mailto:a@b.c)",
        )
        self.assertEqual(papers[0]["abstract"], "we find a gap")
        self.assertEqual(papers[0]["abstract_source"], "OpenAlex")

    def test_an_item_outside_the_window_is_dropped(self) -> None:
        self._serve([_work("Old", date="2026-09-01", doi="10.1/old")])
        papers = pf.fetch_openalex_keyword_search(
            self._config({"enabled": True, "terms": ["magnetism"]}),
            cutoff=self.cutoff, user_agent="brief (mailto:a@b.c)",
        )
        self.assertEqual(papers, [])

    def test_one_work_hit_by_two_terms_is_listed_once(self) -> None:
        work = _work("A study", date="2026-10-03", doi="10.1/dup")
        self._serve([work])
        papers = pf.fetch_openalex_keyword_search(
            self._config({"enabled": True, "terms": ["magnetism", "multiferroic"]}),
            cutoff=self.cutoff, user_agent="brief (mailto:a@b.c)",
        )
        self.assertEqual([p["doi"] for p in papers], ["10.1/dup"])

    def test_the_venue_and_oa_flag_survive_the_trip(self) -> None:
        self._serve([_work("A study", date="2026-10-03", doi="10.1/x",
                           venue="Science Advances", oa=True)])
        papers = pf.fetch_openalex_keyword_search(
            self._config({"enabled": True, "terms": ["magnetism"]}),
            cutoff=self.cutoff, user_agent="brief (mailto:a@b.c)",
        )
        self.assertEqual(papers[0]["venue"], "Science Advances")
        self.assertIs(papers[0]["open_access"], True)
        self.assertEqual((papers[0]["authors"] or "").strip(), "A. Researcher".strip() or "")

    def test_disabled_never_touches_the_network(self) -> None:
        self._serve([])
        papers = pf.fetch_openalex_keyword_search(
            self._config({"enabled": False, "terms": ["magnetism"]}),
            cutoff=self.cutoff, user_agent="brief (mailto:a@b.c)",
        )
        self.assertEqual(papers, [])
        self.assertEqual(self.calls, [])

    def test_no_terms_never_touches_the_network(self) -> None:
        self._serve([])
        papers = pf.fetch_openalex_keyword_search(
            {"publisher_feeds": {"openalex_keyword_search": {"enabled": True}},
             "research_profile": {}},
            cutoff=self.cutoff, user_agent="brief (mailto:a@b.c)",
        )
        self.assertEqual(papers, [])
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
