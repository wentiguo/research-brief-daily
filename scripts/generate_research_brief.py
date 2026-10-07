
#!/usr/bin/env python3
"""Generate and optionally email a personalized research brief.

Dependency-free by design: this script uses only the Python standard library so
it can run reliably in GitHub Actions without package installation.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
from email.message import EmailMessage
import html
import json
import os
from pathlib import Path
import re
import shutil
import smtplib
import ssl
import sys
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from zoneinfo import ZoneInfo

try:
    from publisher_feeds import (
        fetch_publisher_feeds as _fetch_publisher_feeds,
        complete_truncated_abstracts as _complete_truncated_abstracts,
        MIN_USEFUL_ABSTRACT_CHARS,
    )
except ImportError:  # pragma: no cover - keeps the script runnable standalone
    def _fetch_publisher_feeds(config: dict[str, Any], days_back: int, *, contact: str,
                               health: list[dict[str, str]] | None = None) -> list[dict[str, Any]]:
        return []

    MIN_USEFUL_ABSTRACT_CHARS = 320

    def _complete_truncated_abstracts(papers: list[dict[str, Any]], config: dict[str, Any], *,
                                      contact: str, max_pages: int = 40) -> int:
        return 0

try:
    from deepseek_client import enabled as deepseek_enabled, innovation_summary, paper_digest, research_design_ideas
except ImportError:
    def deepseek_enabled() -> bool:
        return False

    def innovation_summary(paper: dict[str, Any]) -> str:
        return ""

    def paper_digest(paper: dict[str, Any], *, style: str = "brief") -> str:
        return ""

    def research_design_ideas(paper: dict[str, Any]) -> str:
        return ""

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "automation" / "research_brief_config.json"
HISTORY_PATH = ROOT / "automation" / "recommended_history.json"
# Two schedulers drive the same job (the GitHub cron at 00:00 UTC and the local
# 08:00 Asia/Shanghai automation), so the same day can reach send_email twice.
# The ledger makes "one mail per day per kind" a property of the code, not of luck.
MAIL_LEDGER_PATH = ROOT / "automation" / "mail_ledger.json"
BRIEF_DIR = ROOT / "research_briefs"
ARCHIVE_DIR = ROOT / "reference_push_archive"
APP_NAME = "research-brief-actions/1.0"

# Upper bound on how long a 429 Retry-After is honored. Some APIs answer with a value
# measured in hours; waiting that long would stall the whole run for one source.
RATE_LIMIT_MAX_WAIT_SECONDS = 30.0



CONFIG_EXAMPLE = ROOT / "automation" / "research_brief_config.example.json"


def load_config() -> dict[str, Any]:
    """Read the user's config, materialising it from the shipped example on first run.

    A fresh clone has no `automation/research_brief_config.json` yet. Copying the
    example keeps `python scripts/generate_research_brief.py --dry-run` and the test
    suite usable straight after clone, instead of failing with a bare FileNotFoundError
    that says nothing about what to do. The copied file is a normal working config:
    edit it, or better, re-run `python scripts/configure_project.py` which asks for
    every value interactively.
    """
    if not CONFIG_PATH.is_file():
        if CONFIG_EXAMPLE.is_file():
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(CONFIG_EXAMPLE, CONFIG_PATH)
            print(f"[config] created {CONFIG_PATH.relative_to(ROOT)} "
                  f"from the example template -- edit it, or run "
                  f"`python scripts/configure_project.py` to answer the setup questions.")
        else:
            raise FileNotFoundError(
                f"Config not found: {CONFIG_PATH}\n"
                f"Copy {CONFIG_EXAMPLE.name} to automation/research_brief_config.json, "
                f"or run `python scripts/configure_project.py`."
            )
    with CONFIG_PATH.open("r", encoding="utf-8-sig") as f:
        return json.load(f)


def contact_email(config: dict[str, Any]) -> str:
    return os.getenv("RESEARCH_BRIEF_CONTACT_EMAIL") or config.get("contact_email") or config.get("recipient_email", "researcher@example.com")


def user_agent(config: dict[str, Any]) -> str:
    return f"{APP_NAME} (mailto:{contact_email(config)})"


def normalize_text(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(normalize_text(v) for v in value)
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def http_json(url: str, config: dict[str, Any], *, timeout: int = 25, retries: int = 3) -> dict[str, Any] | None:
    last_error: Exception | None = None
    for attempt in range(1, max(retries, 1) + 1):
        req = urllib.request.Request(url, headers={"User-Agent": user_agent(config)})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            last_error = exc
            # 429 is a rate limit, not a bad request: honor Retry-After and back off.
            if exc.code != 429 or attempt >= retries:
                break
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            try:
                wait = float(retry_after) if retry_after else 0.0
            except (TypeError, ValueError):
                wait = 0.0
            # A Retry-After measured in hours means "come back much later". Waiting a capped
            # few seconds would be pointless, so the query is abandoned immediately and the
            # other sources still deliver the brief.
            if wait > RATE_LIMIT_MAX_WAIT_SECONDS:
                print(f"note: {urllib.parse.urlparse(url).netloc} asked to wait {wait:.0f}s; "
                      f"skipping this source for this run", file=sys.stderr)
                break
            wait = min(max(wait, 2.0 * attempt), RATE_LIMIT_MAX_WAIT_SECONDS)
            print(f"note: rate limited by {urllib.parse.urlparse(url).netloc}; retrying in {wait:.1f}s", file=sys.stderr)
            time.sleep(wait)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            break
    print(f"warning: failed to fetch {url}: {last_error}", file=sys.stderr)
    return None


def http_xml(url: str, config: dict[str, Any], *, timeout: int = 25) -> ET.Element | None:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent(config)})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return ET.fromstring(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ET.ParseError) as exc:
        print(f"warning: failed to fetch {url}: {exc}", file=sys.stderr)
        return None


def first_crossref_abstract(config: dict[str, Any], paper: dict[str, Any]) -> str:
    doi = normalize_text(paper.get("doi")).replace("https://doi.org/", "")
    if doi:
        data = http_json("https://api.crossref.org/works/" + urllib.parse.quote(doi), config)
        abstract = normalize_text((data or {}).get("message", {}).get("abstract"))
        if abstract:
            return abstract
    title = normalize_text(paper.get("title"))
    if not title:
        return ""
    params = {
        "query.bibliographic": title,
        "rows": "1",
        "select": "DOI,title,abstract",
    }
    data = http_json("https://api.crossref.org/works?" + urllib.parse.urlencode(params), config)
    for item in (data or {}).get("message", {}).get("items", []):
        if title_key(normalize_text(item.get("title"))) == title_key(title):
            return normalize_text(item.get("abstract"))
    return ""


def first_openalex_abstract(config: dict[str, Any], paper: dict[str, Any]) -> str:
    doi = normalize_text(paper.get("doi")).replace("https://doi.org/", "")
    if doi:
        params = {"filter": f"doi:{doi}", "per-page": "1", "mailto": contact_email(config)}
    else:
        title = normalize_text(paper.get("title"))
        if not title:
            return ""
        params = {"search": title, "per-page": "1", "mailto": contact_email(config)}
    data = http_json("https://api.openalex.org/works?" + urllib.parse.urlencode(params), config)
    for item in (data or {}).get("results", []):
        return inverted_index_to_text(item.get("abstract_inverted_index"))
    return ""


def first_semanticscholar_abstract(config: dict[str, Any], paper: dict[str, Any]) -> str:
    """Last-resort abstract recovery via the Semantic Scholar Graph API (DOI lookup).

    Coverage is partial (Semantic Scholar lags the publisher by days for brand-new
    APS articles), so this is a supplement to the publisher ToC feeds, not a
    replacement for them.
    """
    doi = normalize_text(paper.get("doi")).replace("https://doi.org/", "")
    if not doi:
        return ""
    req = urllib.request.Request(
        f"https://api.semanticscholar.org/graph/v1/paper/DOI:{urllib.parse.quote(doi)}?fields=abstract",
        headers={"User-Agent": user_agent(config)},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
        return ""
    return normalize_text(data.get("abstract"))


def enrich_priority_abstracts(papers: list[dict[str, Any]], config: dict[str, Any]) -> None:
    for paper in papers:
        if paper.get("abstract") or not is_priority_journal(paper.get("venue", "")):
            continue
        abstract = (
            first_openalex_abstract(config, paper)
            or first_crossref_abstract(config, paper)
            or first_semanticscholar_abstract(config, paper)
        )
        if abstract:
            paper["abstract"] = abstract
            paper["abstract_source"] = "metadata fallback"
        time.sleep(0.12)


def date_from_parts(parts: Any) -> str:
    try:
        date_parts = parts["date-parts"][0]
        year = int(date_parts[0])
        month = int(date_parts[1]) if len(date_parts) > 1 else 1
        day = int(date_parts[2]) if len(date_parts) > 2 else 1
        return dt.date(year, month, day).isoformat()
    except Exception:
        return ""


def title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower())[:160]


def paper_history_id(paper: dict[str, Any]) -> str:
    doi = normalize_text(paper.get("doi")).lower().replace("https://doi.org/", "")
    if doi:
        return f"doi:{doi}"
    return f"title:{title_key(paper.get('title', ''))}"


def load_history() -> dict[str, Any]:
    if not HISTORY_PATH.exists():
        return {"version": 1, "sent_papers": []}
    try:
        with HISTORY_PATH.open("r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "sent_papers": []}
    data.setdefault("version", 1)
    data.setdefault("sent_papers", [])
    return data


def history_ids(history: dict[str, Any]) -> set[str]:
    return {
        normalize_text(item.get("id"))
        for item in history.get("sent_papers", [])
        if normalize_text(item.get("id"))
    }


def filter_seen_papers(papers: list[dict[str, Any]], history: dict[str, Any], *,
                       run_date: dt.date) -> list[dict[str, Any]]:
    """Drop papers that were already mailed, and keep the ones mailed *today*.

    Only another day's mailing disqualifies a paper. Anything sent on `run_date` is
    exactly what a re-run of that same day is meant to put back in front of the reader,
    and history records the day it was sent, so the two are told apart by `brief_date`.
    Filtering by "ever sent" instead made the second run of a day come out with
    `今日筛出 0 篇候选论文` and a mail that listed nothing - a re-send that failed
    silently by succeeding at the wrong thing. The ledger is what stops a duplicate
    mail; this filter only decides what the mail is about.
    """
    sent_before = history_ids(history)
    sent_today = {entry.get("id") for entry in history.get("sent_papers", [])
                  if entry.get("brief_date") == run_date.isoformat()}
    kept: list[dict[str, Any]] = []
    for paper in papers:
        paper_id = paper_history_id(paper)
        if paper_id not in sent_before or paper_id in sent_today:
            kept.append(paper)
    return kept


def update_history(history: dict[str, Any], papers: list[dict[str, Any]], run_date: dt.date) -> None:
    seen = history_ids(history)
    entries = list(history.get("sent_papers", []))
    sent_at = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()
    for paper in papers:
        paper_id = paper_history_id(paper)
        if not paper_id or paper_id in seen:
            continue
        seen.add(paper_id)
        entries.append({
            "id": paper_id,
            "title": paper.get("title", ""),
            "doi": paper.get("doi", ""),
            "venue": paper.get("venue", ""),
            "published": paper.get("published", ""),
            "brief_date": run_date.isoformat(),
            "sent_at": sent_at,
        })
    history["sent_papers"] = entries[-1500:]
    HISTORY_PATH.write_text(json.dumps(history, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def authors_crossref(author_list: list[dict[str, Any]]) -> str:
    names: list[str] = []
    for author in author_list[:8]:
        name = normalize_text(f"{author.get('given', '')} {author.get('family', '')}")
        if name:
            names.append(name)
    if len(author_list) > 8:
        names.append("et al.")
    return ", ".join(names) or "Unknown"


def affiliations_crossref(author_list: list[dict[str, Any]]) -> str:
    affs: list[str] = []
    for author in author_list:
        for aff in author.get("affiliation", []) or []:
            name = normalize_text(aff.get("name"))
            if name and name not in affs:
                affs.append(name)
    return "; ".join(affs[:6]) or "Metadata unavailable"


def authors_openalex(authorships: list[dict[str, Any]]) -> str:
    names = [normalize_text(a.get("author", {}).get("display_name")) for a in authorships[:8]]
    names = [n for n in names if n]
    if len(authorships) > 8:
        names.append("et al.")
    return ", ".join(names) or "Unknown"


def affiliations_openalex(authorships: list[dict[str, Any]]) -> str:
    affs: list[str] = []
    for authorship in authorships:
        for institution in authorship.get("institutions", []) or []:
            name = normalize_text(institution.get("display_name"))
            if name and name not in affs:
                affs.append(name)
    return "; ".join(affs[:6]) or "Metadata unavailable"


def inverted_index_to_text(index: dict[str, list[int]] | None) -> str:
    if not index:
        return ""
    words: list[tuple[int, str]] = []
    for word, positions in index.items():
        for position in positions:
            words.append((position, word))
    return normalize_text(" ".join(word for _, word in sorted(words)))


def publisher_feeds_enabled(config: dict[str, Any]) -> bool:
    block = config.get("publisher_feeds") or {}
    return bool(block.get("enabled", True)) and bool(block.get("supersedes_journal_queries", True))


def fetch_crossref(config: dict[str, Any], days_back: int) -> list[dict[str, Any]]:
    papers: list[dict[str, Any]] = []
    since = (dt.datetime.now(dt.UTC).date() - dt.timedelta(days=days_back)).isoformat()
    queries = list(config.get("query_templates", []))
    if not publisher_feeds_enabled(config):
        # The publisher ToC feeds already cover the configured journals precisely; querying
        # Crossref with a bare journal name is a blunt substitute for that and only burns
        # rate limit, so it is used solely when the feeds are off.
        queries += list(config.get("journals", []))
    for query in queries:
        params = {
            "query.bibliographic": query,
            "filter": f"from-pub-date:{since},type:journal-article",
            "sort": "published",
            "order": "desc",
            "rows": "20",
            "select": "DOI,title,author,container-title,published-print,published-online,indexed,URL,abstract,subject,publisher",
        }
        data = http_json("https://api.crossref.org/works?" + urllib.parse.urlencode(params), config)
        for item in (data or {}).get("message", {}).get("items", []):
            title = normalize_text(item.get("title"))
            if not title:
                continue
            papers.append({
                "title": title,
                "authors": authors_crossref(item.get("author", []) or []),
                "affiliations": affiliations_crossref(item.get("author", []) or []),
                "venue": normalize_text(item.get("container-title")) or normalize_text(item.get("publisher")),
                "source": "Crossref",
                "published": date_from_parts(item.get("published-online")) or date_from_parts(item.get("published-print")) or date_from_parts(item.get("indexed")),
                "doi": normalize_text(item.get("DOI")),
                "url": normalize_text(item.get("URL")),
                "abstract": normalize_text(item.get("abstract")),
                "subjects": ", ".join(item.get("subject", [])[:8]) if isinstance(item.get("subject"), list) else "",
            })
        time.sleep(0.15)
    return papers


def fetch_openalex(config: dict[str, Any], days_back: int) -> list[dict[str, Any]]:
    papers: list[dict[str, Any]] = []
    since = (dt.datetime.now(dt.UTC).date() - dt.timedelta(days=days_back)).isoformat()
    queries = list(config.get("query_templates", []))
    if not publisher_feeds_enabled(config):
        queries += [f'"{j}"' for j in config.get("journals", [])]
    for query in queries:
        params = {
            "search": query,
            "filter": f"from_publication_date:{since},type:article",
            "sort": "publication_date:desc",
            "per-page": "20",
            "mailto": contact_email(config),
        }
        data = http_json("https://api.openalex.org/works?" + urllib.parse.urlencode(params), config)
        for item in (data or {}).get("results", []):
            title = normalize_text(item.get("title") or item.get("display_name"))
            if not title:
                continue
            primary = item.get("primary_location") or {}
            source = primary.get("source") or {}
            open_access = item.get("open_access") or {}
            best_oa = item.get("best_oa_location") or {}
            papers.append({
                "title": title,
                "authors": authors_openalex(item.get("authorships", []) or []),
                "affiliations": affiliations_openalex(item.get("authorships", []) or []),
                "venue": normalize_text(source.get("display_name")),
                "source": "OpenAlex",
                "published": normalize_text(item.get("publication_date")),
                "doi": normalize_text((item.get("doi") or "").replace("https://doi.org/", "")),
                "url": normalize_text(primary.get("landing_page_url") or item.get("id")),
                "abstract": inverted_index_to_text(item.get("abstract_inverted_index")),
                "subjects": ", ".join(c.get("display_name", "") for c in item.get("concepts", [])[:8]),
                "open_access": open_access.get("is_oa") if isinstance(open_access.get("is_oa"), bool) else None,
                "oa_pdf_url": normalize_text(best_oa.get("pdf_url") or ""),
            })
        # OpenAlex rate-limits aggressively; keep a wider gap than the other sources.
        time.sleep(0.35)
    return papers


def pubmed_date(article: ET.Element) -> str:
    article_date = article.find(".//ArticleDate")
    if article_date is not None:
        year = normalize_text(article_date.findtext("Year"))
        month = normalize_text(article_date.findtext("Month")).zfill(2)
        day = normalize_text(article_date.findtext("Day")).zfill(2)
        if year and month and day:
            return f"{year}-{month}-{day}"

    pub_date = article.find(".//JournalIssue/PubDate")
    if pub_date is None:
        return ""
    year = normalize_text(pub_date.findtext("Year"))
    month = normalize_text(pub_date.findtext("Month"))
    day = normalize_text(pub_date.findtext("Day"))
    if not year:
        medline_date = normalize_text(pub_date.findtext("MedlineDate"))
        match = re.search(r"\b(19|20)\d{2}\b", medline_date)
        year = match.group(0) if match else ""
    month_map = {name: f"{idx:02d}" for idx, name in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1
    )}
    month = month_map.get(month[:3].title(), month.zfill(2) if month.isdigit() else "01")
    day = day.zfill(2) if day.isdigit() else "01"
    return f"{year}-{month}-{day}" if year else ""


def fetch_pubmed(config: dict[str, Any], days_back: int) -> list[dict[str, Any]]:
    """Collect public PubMed metadata; full text is never required."""
    papers: list[dict[str, Any]] = []
    today = dt.datetime.now(dt.UTC).date()
    start = today - dt.timedelta(days=days_back)
    for query in config.get("query_templates", []):
        term = f"({query}) AND ({start:%Y/%m/%d}:{today:%Y/%m/%d}[pdat])"
        params = {"db": "pubmed", "retmode": "json", "retmax": "20", "sort": "pub date", "term": term}
        data = http_json("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?" + urllib.parse.urlencode(params), config)
        ids = (data or {}).get("esearchresult", {}).get("idlist", [])
        if not ids:
            continue
        xml_params = {"db": "pubmed", "retmode": "xml", "id": ",".join(ids)}
        root = http_xml("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?" + urllib.parse.urlencode(xml_params), config)
        if root is None:
            continue
        for article in root.findall(".//PubmedArticle"):
            citation = article.find("./MedlineCitation")
            article_node = citation.find("./Article") if citation is not None else None
            if article_node is None:
                continue
            title = normalize_text("".join(article_node.findtext("ArticleTitle", default="")))
            if not title:
                continue
            author_names: list[str] = []
            for author in article_node.findall("./AuthorList/Author")[:8]:
                name = normalize_text(f"{author.findtext('ForeName', default='')} {author.findtext('LastName', default='')}")
                if name:
                    author_names.append(name)
            if len(article_node.findall("./AuthorList/Author")) > 8:
                author_names.append("et al.")
            abstract = normalize_text(" ".join("".join(node.itertext()) for node in article_node.findall("./Abstract/AbstractText")))
            doi = ""
            for identifier in article.findall("./PubmedData/ArticleIdList/ArticleId"):
                if identifier.attrib.get("IdType") == "doi":
                    doi = normalize_text(identifier.text)
                    break
            pmid = normalize_text(citation.findtext("PMID")) if citation is not None else ""
            journal = normalize_text(article_node.findtext("./Journal/Title"))
            papers.append({
                "title": title,
                "authors": ", ".join(author_names) or "Unknown",
                "affiliations": "Metadata unavailable",
                "venue": journal or "PubMed-indexed journal",
                "source": "PubMed",
                "published": pubmed_date(article),
                "doi": doi,
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else (f"https://doi.org/{doi}" if doi else ""),
                "abstract": abstract,
                "abstract_source": "PubMed" if abstract else "",
                "subjects": normalize_text(citation.findtext("./MeshHeadingList/MeshHeading/DescriptorName")) if citation is not None else "",
            })
        time.sleep(0.34)
    return papers


def fetch_arxiv(config: dict[str, Any], days_back: int) -> list[dict[str, Any]]:
    papers: list[dict[str, Any]] = []
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=days_back)
    start = since.strftime("%Y%m%d%H%M")
    end = dt.datetime.now(dt.UTC).strftime("%Y%m%d%H%M")
    ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    categories = [
        "cond-mat.mtrl-sci",
        "cond-mat.mes-hall",
        "cond-mat.str-el",
        "cond-mat.other",
        "physics.comp-ph",
        "cs.LG",
    ]
    for query in config.get("query_templates", []):
        text_query = " AND ".join(f'all:"{part.strip()}"' for part in query.split()[:6] if part.strip())
        category_query = " OR ".join(f"cat:{cat}" for cat in categories)
        search_query = f"({text_query}) AND ({category_query}) AND submittedDate:[{start} TO {end}]"
        params = {
            "search_query": search_query,
            "start": "0",
            "max_results": "20",
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": user_agent(config)})
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                root = ET.fromstring(resp.read())
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ET.ParseError) as exc:
            print(f"warning: failed to fetch {url}: {exc}", file=sys.stderr)
            continue
        for entry in root.findall("atom:entry", ns):
            title = normalize_text(entry.findtext("atom:title", default="", namespaces=ns))
            if not title:
                continue
            authors = [
                normalize_text(author.findtext("atom:name", default="", namespaces=ns))
                for author in entry.findall("atom:author", ns)
            ]
            doi = ""
            doi_node = entry.find("arxiv:doi", ns)
            if doi_node is not None and doi_node.text:
                doi = normalize_text(doi_node.text)
            categories_found = [
                category.attrib.get("term", "")
                for category in entry.findall("atom:category", ns)
                if category.attrib.get("term")
            ]
            papers.append({
                "title": title,
                "authors": ", ".join(a for a in authors if a) or "Unknown",
                "affiliations": "Metadata unavailable",
                "venue": "arXiv",
                "source": "arXiv",
                "published": normalize_text(entry.findtext("atom:published", default="", namespaces=ns))[:10],
                "doi": doi,
                "url": normalize_text(entry.findtext("atom:id", default="", namespaces=ns)),
                "abstract": normalize_text(entry.findtext("atom:summary", default="", namespaces=ns)),
                "subjects": ", ".join(categories_found),
            })
        time.sleep(3.1)
    return papers


def fetch_ieee(config: dict[str, Any]) -> list[dict[str, Any]]:
    api_key = os.getenv("IEEE_XPLORE_API_KEY")
    if not api_key:
        return []
    papers: list[dict[str, Any]] = []
    for query in config.get("query_templates", []):
        params = {
            "apikey": api_key,
            "format": "json",
            "max_records": "20",
            "sort_order": "desc",
            "sort_field": "publication_year",
            "querytext": query,
        }
        data = http_json("https://ieeexploreapi.ieee.org/api/v1/search/articles?" + urllib.parse.urlencode(params), config)
        for item in (data or {}).get("articles", []):
            papers.append({
                "title": normalize_text(item.get("title")),
                "authors": normalize_text(item.get("authors", {}).get("authors", [])),
                "affiliations": "Metadata unavailable",
                "venue": normalize_text(item.get("publication_title")),
                "source": "IEEE Xplore",
                "published": normalize_text(item.get("publication_year")),
                "doi": normalize_text(item.get("doi")),
                "url": normalize_text(item.get("html_url") or item.get("pdf_url")),
                "abstract": normalize_text(item.get("abstract")),
                "subjects": normalize_text(item.get("index_terms")),
            })
        time.sleep(0.25)
    return [p for p in papers if p.get("title")]


def dedupe(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for paper in papers:
        key = paper.get("doi") or title_key(paper.get("title", ""))
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(paper)
    return out


_ABSTRACT_LABEL_RE = re.compile(r"^\s*(?:abstract|a\s*b\s*s\s*t\s*r\s*a\s*c\s*t)\b[\s:.\-–—]*", re.I)
# OpenAlex occasionally points best_oa_location at a reference-list-only PDF.
_NON_ARTICLE_PDF_RE = re.compile(r"(?i)(_reference|-references?|/references?|_refs?|supplement|_si|_supporting)[^/]*\.pdf$")


def tidy_abstract(text: str) -> str:
    """Drop the leading 'Abstract' label some sources prepend to the abstract body."""
    cleaned = normalize_text(text)
    previous = None
    while previous != cleaned:
        previous = cleaned
        cleaned = _ABSTRACT_LABEL_RE.sub("", cleaned).strip()
    return cleaned


def tidy_abstracts(papers: list[dict[str, Any]]) -> None:
    for paper in papers:
        if paper.get("abstract"):
            paper["abstract"] = tidy_abstract(paper["abstract"])


def paper_text(paper: dict[str, Any]) -> str:
    return " ".join(paper.get(k, "") for k in ("title", "abstract", "venue", "subjects")).lower()


def is_journal_match(venue: str, journal: str) -> bool:
    venue_norm = re.sub(r"\s+", " ", venue.lower()).strip()
    journal_norm = re.sub(r"\s+", " ", journal.lower()).strip()
    if not venue_norm or not journal_norm:
        return False
    # "npj ..." is a separate Nature-family imprint, not a sub-title of Nature: matching it
    # against "Nature" used to promote every npj journal to the top-tier bucket.
    if journal_norm == "nature":
        return venue_norm == "nature" or venue_norm.startswith("nature ")
    if journal_norm == "science":
        return venue_norm == "science" or venue_norm.startswith("science ")
    return venue_norm == journal_norm or venue_norm.startswith(journal_norm + " ")


def is_priority_journal(venue: str) -> bool:
    venue_norm = re.sub(r"\s+", " ", venue.lower()).strip()
    return (
        venue_norm.startswith("physical review ")
        or venue_norm.startswith("prx ")
        or venue_norm == "reviews of modern physics"
        or is_journal_match(venue, "Nature")
        or is_journal_match(venue, "Science")
    )


def score_paper(paper: dict[str, Any], config: dict[str, Any]) -> tuple[float, list[str]]:
    profile = config.get("research_profile", {})
    haystack = paper_text(paper)
    score = 0.0
    reasons: list[str] = []
    priority_topics = profile.get("priority_topics", [])
    for idx, keyword in enumerate(priority_topics):
        if keyword.lower() in haystack:
            score += max(2.0, 4.0 - idx * 0.18)
            reasons.append(keyword)
    for keyword in profile.get("core_topics", []):
        if keyword.lower() in haystack:
            score += 2.2
            reasons.append(keyword)
    for keyword in profile.get("method_keywords", []):
        if keyword.lower() in haystack:
            score += 1.3
            reasons.append(keyword)
    for keyword in profile.get("objective_keywords", []):
        if keyword.lower() in haystack:
            score += 1.0
            reasons.append(keyword)
    venue = paper.get("venue", "")
    for journal in config.get("journals", []):
        if is_journal_match(venue, journal):
            score += 3.0
            reasons.append(journal)
            break
    ml_bonus, ml_tags = ml_theory_bonus(paper, config)
    score += ml_bonus
    reasons.extend(ml_tags)
    experiment_penalty, exp_tags = theory_preference(paper, config)
    score += experiment_penalty
    reasons.extend(exp_tags)
    return round(score, 2), sorted(set(reasons), key=str.lower)[:12]


def publication_priority(paper: dict[str, Any]) -> int:
    venue = paper.get("venue", "")
    source = paper.get("source", "")
    if is_priority_journal(venue):
        return 0
    if source.lower() == "arxiv" or venue.lower() == "arxiv":
        return 2
    return 1


# Journals whose publisher policy is fully open access.  Journal-level fact, used only
# when no per-article open-access flag is available from the source metadata.
OPEN_ACCESS_VENUE_PREFIXES = (
    "nature communications",
    "communications physics",
    "communications materials",
    "science advances",
    "scientific reports",
    "npj ",
    "physical review x",
    "physical review research",
    "physical review physics education research",
    "prx quantum",
    "prx energy",
    "new journal of physics",
    "advanced science",
)


def is_subscription_journal(paper: dict[str, Any]) -> bool:
    """True when the paper comes from a subscription (non-open-access) journal.

    A per-article open-access flag from the source metadata always wins; the
    journal-policy inference below is only the fallback.
    """
    if paper.get("open_access") is True:
        return False
    if paper.get("source", "").lower() == "arxiv" or paper.get("venue", "").lower() == "arxiv":
        return False

    venue_raw = (paper.get("venue") or "").strip()
    venue = re.sub(r"\s+", " ", venue_raw.lower())
    if not venue:
        return False
    if any(venue.startswith(prefix) for prefix in OPEN_ACCESS_VENUE_PREFIXES):
        return False

    if paper.get("open_access") is False:
        return True

    # Family-based inference for the publishers this brief tracks.
    if venue.startswith("physical review ") or venue.startswith("prx ") or venue.startswith("prx quantum"):
        return True
    if venue == "reviews of modern physics":
        return True
    if is_journal_match(venue_raw, "Nature") or is_journal_match(venue_raw, "Science"):
        return True
    return False


def zone_badge(venue: str, config: dict[str, Any]) -> str:
    """Audited CAS zone as plain text, so the email body and the long image agree with the poster."""
    record = journal_zone(venue, config) or {}
    zone = record.get("zone")
    if zone in (1, 2):
        return f"{zone} 区 · {record.get('category', '')}"
    if is_preprint({"venue": venue}):
        return "arXiv 预印本 · 未发表"
    # A tier entry without a zone record is a gap in the audited table, not a
    # permission to publish an empty cell: the tier itself is enough to state the zone,
    # and the category is left visibly unclaimed rather than invented (see
    # AllowlistConsistencyTests.test_every_tiered_journal_reaches_a_visible_zone).
    tier = journal_tier(venue, config)
    if tier in (1, 2):
        return f"{tier} 区 · 大类未建档"
    return ""


def source_badge(paper: dict[str, Any]) -> str:
    if is_priority_journal(paper.get("venue", "")):
        return "顶刊已发表（订阅刊）" if is_subscription_journal(paper) else "顶刊已发表"
    if paper.get("source", "").lower() == "arxiv" or paper.get("venue", "").lower() == "arxiv":
        return "arXiv预印本"
    return "已发表/开放元数据"


def access_badge(paper: dict[str, Any]) -> str:
    """Full-text availability, stated explicitly so a closed-access row is never read as a download."""
    if paper.get("source", "").lower() == "arxiv" or paper.get("venue", "").lower() == "arxiv":
        return "开放获取（arXiv 全文可下载）"
    if paper.get("open_access") is True:
        # "可下载 PDF" was a promise the run could not keep: the Nature family redesigned
        # its site and now serves HTML at the old `.pdf` path, so an open-access row
        # pointed at a full text the reader could read but not download. State what is
        # true - the publisher's page is free - and leave the download to the routes that
        # actually answered.
        return "开放获取（出版商页面可免费读全文；PDF 是否能直接取到见附件清单）"
    if is_subscription_journal(paper):
        return "订阅刊：仅公开题录与摘要，无全文下载权限"
    return "未标注开放状态，仅元数据"


def abstract_badge(paper: dict[str, Any]) -> str:
    if paper.get("abstract_source") == "publisher page":
        return "完整摘要来自出版商官网公开摘要页"
    if paper.get("abstract_source") == "publisher feed":
        if is_subscription_journal(paper):
            return "摘要来自出版商官网（订阅刊公开摘要）"
        return "摘要来自出版商官网"
    if paper.get("abstract_source") == "metadata fallback":
        return "摘要已通过元数据兜底补全"
    if paper.get("abstract"):
        return "摘要可用"
    return "摘要不可用（订阅刊未公开摘要）"


def pdf_link(paper: dict[str, Any]) -> str:
    url = paper.get("url", "")
    if "arxiv.org/abs/" in url:
        return url.replace("/abs/", "/pdf/")
    if "arxiv.org/pdf/" in url:
        return url
    # A closed-access article never gets a direct PDF link; its abstract page is used instead.
    if is_subscription_journal(paper) or not url:
        return ""
    oa_pdf = paper.get("oa_pdf_url") or ""
    if oa_pdf and not _NON_ARTICLE_PDF_RE.search(oa_pdf) and not _NON_ARTICLE_PDF_RE.search(
        urllib.parse.urlparse(oa_pdf).path
    ):
        return oa_pdf
    if url.lower().endswith(".pdf") and not _NON_ARTICLE_PDF_RE.search(url):
        return url
    return ""


def publication_mix(papers: list[dict[str, Any]]) -> tuple[int, int, int]:
    top = sum(1 for paper in papers if publication_priority(paper) == 0)
    published = sum(1 for paper in papers if publication_priority(paper) == 1)
    arxiv = sum(1 for paper in papers if publication_priority(paper) == 2)
    return top, published, arxiv


def published_rank(paper: dict[str, Any]) -> int:
    published = paper.get("published", "")
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", published)
    if not match:
        return 0
    try:
        date = dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return 0
    return date.toordinal()


def parsed_publication_date(paper: dict[str, Any]) -> dt.date | None:
    published = normalize_text(paper.get("published"))
    # Allow 1- or 2-digit month/day: a Crossref date-parts join can produce
    # "2026-10-6", and requiring two digits there made the day default to 1
    # (October 1st), dropping the paper as stale. Normalization now zero-pads,
    # but the parser stays lenient as a second line of defence.
    match = re.match(r"(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?", published)
    if not match:
        return None
    year = int(match.group(1))
    month = int(match.group(2) or 1)
    day = int(match.group(3) or 1)
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def filter_recent_publications(papers: list[dict[str, Any]], days_back: int, config: dict[str, Any], run_date: dt.date) -> list[dict[str, Any]]:
    max_age = int(config.get("published_max_age_days", days_back))
    try:
        tolerance = int(config.get("published_window_tolerance_days", 1) or 1)
    except (TypeError, ValueError):
        tolerance = 1
    # `tolerance` widens the lower bound by a few days to absorb publisher-feed
    # publication-date lag (a paper dated 1-2 days before the run is still "fresh").
    # It must mirror the tolerance applied at the feed level so the two windows agree.
    cutoff = run_date - dt.timedelta(days=max_age + tolerance)
    fresh: list[dict[str, Any]] = []
    for paper in papers:
        pub_date = parsed_publication_date(paper)
        if not pub_date:
            fresh.append(paper)
            continue
        if paper.get("source", "").lower() == "arxiv" or paper.get("venue", "").lower() == "arxiv":
            fresh.append(paper)
            continue
        if cutoff <= pub_date <= run_date:
            fresh.append(paper)
    return fresh


def paper_topic(paper: dict[str, Any], config: dict[str, Any]) -> str:
    text = paper_text(paper)
    for group in config.get("research_profile", {}).get("recommendation_groups", []):
        terms = group.get("terms", [])
        context_terms = group.get("context_terms", [])
        if terms_match(text, terms) and (not context_terms or terms_match(text, context_terms)):
            return normalize_text(group.get("name")) or "未分类"
    if is_priority_journal(paper.get("venue", "")):
        return "顶刊已发表精选"
    return "未分类"


def rank_papers(papers: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    for paper in papers:
        score, reasons = score_paper(paper, config)
        paper["score"] = score
        paper["reasons"] = reasons
        paper["publication_priority"] = publication_priority(paper)
    return sorted(
        papers,
        key=lambda p: (p.get("publication_priority", 2), -float(p.get("score", 0)), -published_rank(p)),
    )


def has_any(text: str, terms: list[str]) -> bool:
    return any(term.lower() in text for term in terms)


def strict_interest_terms(config: dict[str, Any]) -> list[str]:
    profile = config.get("research_profile", {})
    terms: list[str] = []
    terms.extend(profile.get("priority_topics", []))
    terms.extend(profile.get("core_topics", []))
    for group in profile.get("recommendation_groups", []):
        terms.extend(group.get("terms", []))
        terms.extend(group.get("context_terms", []))
    return [term for term in sorted(set(terms), key=str.lower) if len(term) >= 5]


# ===========================================================================
# Topic gate -- which papers count as on-topic.
#
# DEFAULT SCOPE (generic condensed matter / materials science) is a starting point
# only. Every list below can be replaced per deployment from
# automation/research_brief_config.json -> "topic_gate":
#
#   "topic_gate": {
#     "direct_signals":    ["your specific states / materials / phenomena", ...],
#     "weak_signals":      ["cross-field homonyms needing an anchor", ...],
#     "matter_anchors":    ["host-system words that make a weak hit legit", ...],
#     "exclude_wave_physics": true,
#     "wave_signals":      ["photonic", "phononic", ...],
#     "wave_matter_anchors": ["electron", "first-principles", ...]
#   }
#
# Matching semantics (these are the subtle part -- do not "simplify" them):
#
#   DIRECT phrases name a specific state/material in the user's areas and pass on
#   their own. WEAK words are cross-field homonyms ("topological" in biology,
#   "magnetic" in imaging, "insulator" in electronics-adjacent engineering) and
#   pass ONLY together with a matter-side anchor, so those off-topic papers do not
#   slip in.
#
#   CRITICAL: the weak-signal words themselves must NOT appear in matter_anchors.
#   Otherwise a weak hit always finds itself as its own anchor and the rule
#   degrades into "pass everything". That bug silently disabled the gate once.
#
#   Method words ("machine learning", "active learning", "inverse design") are NOT
#   a direction on their own -- ML applied to one of the topics is in scope because
#   the topic phrase carries the gate.
#
# No abstract? A subscribed letter (APS supplies none to Crossref/OpenAlex) is
# judged on its title alone, so a title-only letter still has a chance.
# ===========================================================================

# Generic defaults. Override via config["topic_gate"] (see scripts/configure_project.py).
_DEFAULT_TOPIC_SIGNAL_DIRECT = (
    "topological insulator", "topological crystalline insulator", "topological semimetal",
    "magnetic topological insulator", "topological material", "higher-order topological",
    "dirac semimetal", "weyl semimetal", "nodal-line semimetal", "nodal semimetal",
    "quantum spin hall", "quantum anomalous hall", "quantum valley hall",
    "chern insulator", "axion insulator", "topological hall", "anomalous hall",
    "spin hall", "valley hall", "quantum hall", "berry curvature", "berry phase",
    "chern number", "topological invariant", "band topology", "topological phase",
    "kagome", "dirac fermion", "nodal line", "majorana", "spin-orbit coupling",
    "topological superconductor", "superconductor", "superconductivity",
    "unconventional superconductor", "multiferroic", "magnetoelectric", "ferroelectric",
    "ferroelastic", "ferroic", "magnetoelectric coupling",
    "spintronic", "spin texture", "skyrmion", "magnetic skyrmion",
    "antiferromagnet", "antiferromagnetic", "ferrimagnet", "ferromagnet",
    "magnon", "spin wave", "exchange coupling", "spin-split", "spin splitting",
)

_DEFAULT_TOPIC_SIGNAL_WEAK = (
    "topological", "topology", "magnetic", "magnetism", "spin",
    "ferro", "superconduct", "semimetal", "insulator", "pyroelectric",
)

# Matter-side anchors. A weak signal sitting in an electronic / magnetic / lattice
# system is in scope; the same word in optics / biology / engineering is not.
_DEFAULT_TOPIC_MATTER_ANCHOR = (
    "material", "materials", "crystal", "crystalline", "metal", "electron",
    "electronic", "band", "fermion", "weyl", "dirac", "lattice", "compound",
    "heterostructure", "thin film", "monolayer", "bilayer", "moire", "phase",
    "superconductor", "condensed matter", "density functional", "first-principles",
    "tight-binding", "hamiltonian", "berry", "chern", "hall", "kagome",
    "antiferro", "multiferro", "battery", "electrode", "cathode", "anode",
    "electrolyte", "capacitor",
)

# Topological photonics / topological acoustics study how waves propagate in
# engineered structures. That is wave physics, not electronic band topology.
# The line is drawn by the matter anchors below.
_DEFAULT_WAVE_SIGNAL_PHRASES = (
    "photonic", "photonics", "photonic crystal", "photon crystal", "polariton",
    "polaritonic", "optical", "optics", "phonon", "phononic", "surface acoustic wave",
    "acoustic wave", "sound wave", "waveguide", "microring", "microcavity", "cavity qed",
    "laser", "optical fiber", "optical fibre", "light transport",
    "electromagnetic wave", "wave physics",
)

_DEFAULT_WAVE_MATTER_ANCHOR_PHRASES = (
    "electron", "electronic", "fermion", "fermionic", "band structure",
    "electronic band", "band topology", "spin-orbit coupling", "spin orbit coupling",
    "first-principles", "density functional", "tight-binding", "tight binding",
    "hamiltonian", "hopping", "lattice model", "crystal structure", "crystal field",
    "crystallographic", "unit cell", "superlattice", "thin film", "heterostructure",
    "magnetic structure", "magnetic moment", "magnetocrystalline", "weyl", "dirac",
    "arpes", "magnetotransport", "hall effect", "neutron scattering",
    "x-ray diffraction", "x-ray magnetic circular dichroism",
    "antiferromagnet", "ferromagnet", "multiferroic",
    "magnetoelectric", "ferroelectric", "berry curvature", "chern number",
    "spin splitting", "spin texture", "anomalous hall", "spin nernst",
    "magnetoresistance", "phonon angular momentum", "magnon",
)


def _gate_terms(config: dict[str, Any], key: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
    """Read one term list from config["topic_gate"], falling back to the shipped default.

    Accepts a list of strings; an empty or absent key means "use the default".
    Anything non-string inside the list is ignored rather than crashing a daily run.
    """
    gate = (config or {}).get("topic_gate") or {}
    raw = gate.get(key)
    if not isinstance(raw, list) or not raw:
        return fallback
    cleaned = tuple(
        str(item).strip().lower()
        for item in raw
        if isinstance(item, (str, int, float)) and str(item).strip()
    )
    return cleaned or fallback


def topic_signal_pass(text: str, config: dict[str, Any] | None = None) -> bool:
    """True when the text carries a configured research signal.

    DIRECT phrases pass on their own; WEAK words pass only with a matter-side anchor.
    """
    text = (text or "").lower()
    if any(term in text for term in _gate_terms(config, "direct_signals",
                                                _DEFAULT_TOPIC_SIGNAL_DIRECT)):
        return True
    weak = _gate_terms(config, "weak_signals", _DEFAULT_TOPIC_SIGNAL_WEAK)
    if not any(term in text for term in weak):
        return False
    return any(term in text for term in _gate_terms(config, "matter_anchors",
                                                   _DEFAULT_TOPIC_MATTER_ANCHOR))


def is_domain_match(paper: dict[str, Any], config: dict[str, Any]) -> bool:
    """Universal topic gate. The journal allowlist is enforced separately by
    is_allowed_venue, so this only decides whether a paper is on-topic.

    A subscribed letter often carries no abstract (APS supplies none to Crossref /
    OpenAlex), so when the abstract is missing we judge on the title alone - a title is
    short enough that it does not drown in boilerplate.
    """
    text = paper_text(paper)
    if topic_signal_pass(text, config):
        return True
    if not paper.get("abstract"):
        return topic_signal_pass((paper.get("title") or "").lower(), config)
    return False


def is_wave_physics(paper: dict[str, Any], config: dict[str, Any] | None = None) -> bool:
    """True when a paper belongs to topological optics / acoustics rather than to the
    configured condensed-matter line. Wave physics is excluded outright, not used as a
    last-resort fallback. Set topic_gate.exclude_wave_physics=false to disable.
    """
    gate = (config or {}).get("topic_gate") or {}
    if gate.get("exclude_wave_physics") is False:
        return False
    text = paper_text(paper)
    if not any(term in text for term in _gate_terms(
            config, "wave_signals", _DEFAULT_WAVE_SIGNAL_PHRASES)):
        return False
    return not any(term in text for term in _gate_terms(
        config, "wave_matter_anchors", _DEFAULT_WAVE_MATTER_ANCHOR_PHRASES))


def _norm_venue(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").lower()).strip()


def is_preprint(paper: dict[str, Any]) -> bool:
    venue = _norm_venue(paper.get("venue", ""))
    return venue == "arxiv" or venue.startswith("arxiv ")


def journal_zone(venue: str, config: dict[str, Any]) -> dict[str, Any] | None:
    """Audited CAS-zone record for a venue: {"zone": 1|2, "category": str} or None."""
    policy = config.get("journal_policy") or {}
    if not venue or is_preprint({"venue": venue}):
        return None
    for journal, record in (policy.get("journal_zones") or {}).items():
        if is_journal_match(venue, journal):
            return record
    return None


def _journal_exact_match(venue: str, journal: str) -> bool:
    return _norm_venue(venue) == _norm_venue(journal)


def journal_tier(venue: str, config: dict[str, Any]) -> int:
    """Which tracked tier this venue belongs to: 1 = 1 区/顶刊, 2 = 2 区, 0 = 不在白名单.

    Exact names are resolved across BOTH tiers before any prefix match is tried.
    Without that two-pass order, "Nature" (tier1) would claim "Nature Nanotechnology"
    by prefix before tier2 was ever consulted, silently promoting every Nature sub-journal
    to the top tier.
    """
    policy = config.get("journal_policy") or {}
    if not venue or is_preprint({"venue": venue}):
        return 0
    for pattern in policy.get("exclude", []):
        if _norm_venue(pattern) and _norm_venue(pattern) in _norm_venue(venue):
            return 0
    zone = journal_zone(venue, config)
    if zone and int(zone.get("zone", 0)) in (1, 2):
        return int(zone["zone"])
    # Pass 1: exact name match, either tier.
    for index, key in ((1, "tier1"), (2, "tier2")):
        for journal in policy.get(key, []):
            if _journal_exact_match(venue, journal):
                return index
    # Pass 2: only now allow a prefix match ("physical review b" for "Physical Review B").
    for index, key in ((1, "tier1"), (2, "tier2")):
        for journal in policy.get(key, []):
            if is_journal_match(venue, journal):
                return index
    return 0


def is_allowed_venue(paper: dict[str, Any], config: dict[str, Any]) -> bool:
    """Journal allowlist gate: only 1 区 / 2 区 (or an allowed preprint) is pushed."""
    policy = config.get("journal_policy") or {}
    venue = (paper.get("venue") or "").strip()
    if not venue:
        return not policy.get("allowlist_only", True)
    if is_preprint(paper):
        return bool(policy.get("allow_preprints", True))
    if journal_tier(venue, config):
        return True
    return not policy.get("allowlist_only", True)


def ml_theory_bonus(paper: dict[str, Any], config: dict[str, Any]) -> tuple[float, list[str]]:
    """Reward machine-learning work that is also theoretical/computational.

    ML-only papers (data-heavy, no theory signal) are deliberately weighted lowest:
    the brief prefers ML work that models physics (DFT / tight-binding / ML potentials /
    symmetry analysis) over ML used only as a black-box classifier.
    """
    pref = config.get("ml_theory_preference") or {}
    if not pref.get("enabled", True):
        return 0.0, []
    text = paper_text(paper)
    ml = [t for t in pref.get("ml_terms", []) if t.lower() in text]
    theory = [t for t in pref.get("theory_terms", []) if t.lower() in text]
    if ml and theory:
        bonus = float(pref.get("and_bonus", 4.0))
        tags = ["ML + 理论/计算"]
    elif theory:
        bonus = float(pref.get("theory_only_bonus", 1.5))
        tags = ["理论/计算"]
    elif ml:
        bonus = float(pref.get("ml_only_bonus", 0.4))
        tags = ["ML（偏应用）"]
    else:
        return 0.0, []
    if not paper.get("abstract") and ml:
        bonus -= float(pref.get("ml_penalty_no_abstract", 1.0))
    return round(bonus, 2), tags


# Evidence that something was measured in a lab rather than derived or simulated. Only
# the phrases that are unambiguous on their own; weaker phrasing is not checked, and an
# abstract saying neither lab nor model is treated as neutral rather than as an experiment.
_EXPERIMENT_SIGNAL_TERMS = (
    "experimentally", "in the experiment", "we measured", "we grew",
    "scanning tunneling microscopy", "angle-resolved photoemission",
    "x-ray diffraction", "neutron scattering", "magneto-optical",
    "x-ray magnetic circular dichroism", "magnetic force microscopy",
    "piezoelectric force microscopy", "pump-probe", "photoemission spectroscopy",
    "transport measurement", "magnetotransport measurement", "superconducting quantum interference",
    "x-ray photoelectron", "transmission electron microscopy", "optically induced",
)


def theory_preference(paper: dict[str, Any], config: dict[str, Any]) -> tuple[float, list[str]]:
    """Theory/computation first, as the user asked for a computational line.

    A paper whose only evidence is a lab measurement carries no model, no Hamiltonian
    and no first-principles workflow; it is pushed after the theory work rather than
    being ranked alongside it. A paper showing neither is neutral - absence of the
    theory vocabulary is not evidence of an experiment.
    """
    pref = config.get("theory_preference") or {}
    if not pref.get("enabled", True):
        return 0.0, []
    text = paper_text(paper)
    theory_terms = [str(t).lower() for t in (config.get("evaluation_model", {}).get("theory_terms") or [])]
    if any(term in text for term in theory_terms):
        return 0.0, []
    hits = [term for term in _EXPERIMENT_SIGNAL_TERMS if term in text]
    if not hits:
        return 0.0, []
    return round(float(pref.get("experiment_penalty", -4.0)), 2), [f"实验为主-{hits[0]}"]

def heuristic_innovation(paper: dict[str, Any], config: dict[str, Any]) -> float:
    """Transparent keyword-level innovation proxy, used only when no LLM is available.

    Deliberately conservative: it reads novelty wording and theory depth out of the
    *public abstract only*. It is a ranking heuristic, not a claim about the paper.
    """
    abstract = normalize_text(paper.get("abstract"))
    if not abstract:
        return 3.0
    head = abstract[:900].lower()
    novelty_markers = (
        "we propose", "propose a", "novel", "first observation", "first observation of",
        "unprecedented", "breakthrough", "discovery of", "new mechanism", "beyond",
        "predicts", "predicted", "design rule", "universal", "exact solution",
        "emergent", "robust", "scaling law", "landau",
    )
    hits = [m for m in novelty_markers if m in head]
    score = 3.2 + min(2.6, 0.45 * len(hits))
    if any(k in head for k in ("theory", "analytic", "model", "hamiltonian", "tight-binding", "dft", "first-principles")):
        score += 1.2
    if journal_tier(paper.get("venue", ""), config) == 1:
        score += 0.8
    return max(1.0, min(10.0, round(score, 2)))


def evaluation_dimensions(config: dict[str, Any]) -> dict[str, float]:
    """Weighted evaluation model used for ranking and for the printed score breakdown."""
    model = config.get("evaluation_model") or {}
    weights = dict(model.get("weights") or {})
    for key, default in (("relevance", 36), ("theory", 20), ("journal", 18),
                         ("innovation", 18), ("rigor", 8)):
        weights.setdefault(key, default)
    return weights


def heuristic_theory_depth(paper: dict[str, Any], config: dict[str, Any]) -> float:
    """How concrete and reproducible the theoretical/computational content is (0-10).

    Reads the public abstract only. A paper that states a model, a Hamiltonian, a
    method and quantitative results scores high; a paper that only describes an
    experimental observation scores low because it offers nothing to re-use.
    """
    abstract = normalize_text(paper.get("abstract"))
    if not abstract:
        return 2.0
    head = abstract[:1200].lower()
    model = config.get("evaluation_model") or {}
    terms = [t.lower() for t in (model.get("theory_terms") or [])]
    if not terms:
        terms = ["dft", "first-principles", "tight-binding", "hamiltonian", "berry curvature",
                 "topological invariant", "monte carlo", "landau", "scaling law", "exact solution",
                 "group theory", "effective hamiltonian", "machine learning potential"]
    hits = [t for t in terms if t in head]
    score = 2.2 + min(3.4, 0.55 * len(hits))
    if any(k in head for k in ("we develop", "we construct", "we derive", "we propose a model",
                               "analytically", "closed form", "we solve", "model hamiltonian",
                               "lattice model", "effective model")):
        score += 1.6
    if any(k in head for k in ("accuracy", "benchmark", "mev", "we predict", "predicts that",
                               "predicted", "quantitative")):
        score += 0.8
    if any(k in head for k in ("we synthesized", "we fabricated", "in the experiment")) and not hits:
        score -= 1.2
    return max(1.0, min(10.0, round(score, 2)))


def heuristic_rigor(paper: dict[str, Any], config: dict[str, Any]) -> float:
    """Methodological rigour / reproducibility signals present in the public abstract (0-10)."""
    abstract = normalize_text(paper.get("abstract"))
    if not abstract:
        return 2.0
    head = abstract[:1200].lower()
    model = config.get("evaluation_model") or {}
    terms = [t.lower() for t in (model.get("rigor_terms") or [])]
    if not terms:
        terms = ["cross-validat", "in agreement with", "consistent with", "validation", "reproducib",
                 "benchmark", "we confirm", "testable", "open data", "code available",
                 "supplementary material", "systematic scan", "parameter sweep", "agreement with"]
    hits = [t for t in terms if t in head]
    score = 2.0 + min(3.0, 0.5 * len(hits))
    if any(k in head for k in ("confirms", "validates", "independent", "we verify", "we check")):
        score += 1.0
    tier = journal_tier(paper.get("venue", ""), config)
    if tier == 1:
        score += 0.6
    elif tier == 2:
        score += 0.3
    return max(1.0, min(10.0, round(score, 2)))


def recommendation_score(paper: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Five-dimension recommendation score used to pick the poster papers.

    relevance    = topical fit (0-10), from topic/method scoring plus the
                   ML x theory-computational preference.
    theory       = theoretical/computational depth (0-10): is a reusable model,
                   method or quantitative result actually given?
    journal      = venue tier (1区 10 / 2区 7.2 / 预印本 6 / 其他 4).
    innovation   = breakthrough degree (1-10), LLM-assessed when a key is configured
                   and a conservative keyword proxy otherwise.
    rigor        = methodological rigour / reproducibility signals (0-10).

    weights live in ``config["evaluation_model"]["weights"]`` and default to
    relevance 36 / theory 20 / journal 18 / innovation 18 / rigor 8, i.e. topical
    fit dominates while journal tier and innovation carry exactly the same, lower
    weight. Every sub-score is kept on the paper record so the ranking is auditable.
    """
    weights = evaluation_dimensions(config)
    total_weight = sum(weights.values()) or 1.0
    raw = float(paper.get("score", 0) or 0)
    relevance = min(10.0, raw / 3.0)
    tier = journal_tier(paper.get("venue", ""), config)
    if is_preprint(paper):
        journal = 6.0
    elif tier == 1:
        journal = 10.0
    elif tier == 2:
        journal = 7.2
    else:
        journal = 4.0
    innovation = float(paper.get("innovation_score") or heuristic_innovation(paper, config))
    theory = float(paper.get("theory_score") or heuristic_theory_depth(paper, config))
    rigor = float(paper.get("rigor_score") or heuristic_rigor(paper, config))
    total = (weights["relevance"] * relevance
             + weights["theory"] * theory
             + weights["journal"] * journal
             + weights["innovation"] * innovation
             + weights["rigor"] * rigor) / total_weight
    return {
        "relevance": round(relevance, 2),
        "theory": round(theory, 2),
        "journal": round(journal, 2),
        "innovation": round(innovation, 2),
        "rigor": round(rigor, 2),
        "total": round(total, 2),
        "parts": {
            "relevance": "文章符合度（与理论计算方向的契合）",
            "theory": "理论计算深度（模型/方法是否可复现）",
            "journal": "期刊水平（中科院分区）",
            "innovation": "创新性/突破性",
            "rigor": "方法严谨与可复现性",
        },
    }


def evaluation_note(config: dict[str, Any]) -> str:
    """Explain the five-dimension ranking in the mail body (the poster prints no score)."""
    model = config.get("evaluation_model") or {}
    weights = evaluation_dimensions(config)
    rows = "\n".join(f"- **{label}**（{weights[key]}%）：{help_text}"
                     for key, label, help_text in (
                         ("relevance", "文章符合度", "与理论计算/凝聚态方向的契合程度"),
                         ("theory", "理论计算深度", "是否给出可复现的模型、方法与定量结果"),
                         ("journal", "期刊水平", "中科院《期刊分区表》2025-03 升级版分区"),
                         ("innovation", "创新性/突破性", "新机制、新判据、新效应；无 LLM key 时为保守关键词代理"),
                         ("rigor", "方法严谨与可复现性", "交叉验证、可检验预言、参数扫描完备性、公开数据/代码")))
    return (f"海报推介不展示分数，排序按五个可审计维度加权（总分 100）：\n\n{rows}\n\n"
            "其中文章符合度权重最高，期刊水平与创新性权重相同且低于符合度。\n"
            "海报的中文精读只依据出版商公开摘要生成，每条都附英文原文证据并在落地前做逐字溯源校验，"
            "校验不通过的条目直接丢弃，不做无出处的表述。")


def assess_innovation(papers: list[dict[str, Any]], config: dict[str, Any], *, limit: int = 8) -> None:
    """Ask DeepSeek to rate breakthrough degree for the strongest candidates.

    Only papers that already have a public abstract are sent; the request is pure
    metadatum + abstract, never full text. If the call fails or the key is missing the
    papers simply keep their conservative heuristic score.
    """
    if not deepseek_enabled():
        print("innovation scoring: DeepSeek key absent, using keyword-level proxy")
        return
    candidates = [p for p in papers if p.get("abstract")][:limit]
    if not candidates:
        return
    payload = "\n".join(
        f"- DOI: {p.get('doi', '')}\n  Venue: {p.get('venue', '')}\n  Title: {p.get('title', '')}\n  Abstract: {normalize_text(p.get('abstract'))[:1400]}"
        for p in candidates
    )
    system = (
        "You rate research breakthrough degree for condensed-matter / materials scientists. "
        "You receive only title, venue and public abstract. Judge novelty and conceptual "
        "breakthrough on a 1-10 scale against the field's usual standards. Respond with "
        "strict JSON: {\"items\": [{\"doi\": \"...\", \"innovation\": 7.5, \"breakthrough_note\": \"<=50 chars\"}]}."
    )
    user = f"Rate each entry.\n{payload}"
    from deepseek_client import chat_text  # local import: the script stays runnable without it
    answer = chat_text(system, user, max_tokens=1200, temperature=0.2)
    if not answer:
        print("innovation scoring: DeepSeek returned nothing, using keyword-level proxy")
        return
    match = re.search(r"\{.*\}", answer, re.S)
    if not match:
        print("innovation scoring: DeepSeek answer was not JSON, using keyword-level proxy")
        return
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        print("innovation scoring: DeepSeek JSON unparsable, using keyword-level proxy")
        return
    for item in parsed.get("items", []):
        target = next((p for p in candidates if (p.get("doi") or "").lower() == str(item.get("doi", "")).lower()), None)
        if not target:
            continue
        try:
            value = float(item.get("innovation"))
        except (TypeError, ValueError):
            continue
        target["innovation_score"] = max(1.0, min(10.0, value))
        note = normalize_text(item.get("breakthrough_note", ""))
        if note:
            target["breakthrough_note"] = note
    print(f"innovation scoring: DeepSeek scored {sum(1 for p in candidates if p.get('innovation_score'))}/{len(candidates)} candidates")


def _clip_text(text: str, limit: int) -> str:
    """Hard cap on a printed bullet so the poster layout stays predictable."""
    text = normalize_text(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip(" ,;:。") + "…"


# Shortest evidence span, in words, that may support a printed bullet. Four words
# is enough to pin a claim ("spin orbit coupling is included") and short enough to
# survive the elision case above without letting a stub through.
MIN_QUOTE_WORDS = 4


def _quote_abstract(quote: str, abstract: str) -> bool:
    """True only when the evidence quote occurs verbatim in the publisher's abstract.

    This is the anti-hallucination gate: a bullet is printed only if the English
    phrase that supports it really appears in the text the publisher published.
    """
    if not quote or not abstract:
        return False

    def norm(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()

    q, a = norm(quote), norm(abstract)
    # Measure the evidence in *words*, not characters. The gate used to test
    # len(q) < 6 on the normalised string, which a two-word quote like "we study"
    # slips past ("we study" is 8 characters) - so a two-word fragment was enough to
    # get a bullet printed. A quote has to carry at least MIN_QUOTE_WORDS real words
    # to be evidence for anything.
    words = q.split()
    if len(words) < MIN_QUOTE_WORDS:
        return False
    if q in a:
        return True
    # A model often stitches two real fragments into one quote ("...gap... the
    # authors find..." with elisions or a clause boundary in between). Demanding one
    # unbroken match threw away the whole - correctly written, genuinely grounded -
    # Chinese bullet over that, and the poster then degraded to printing the English
    # abstract, which is exactly what the reader does not want. Accept the quote when
    # a sufficiently long *contiguous* run of its words does occur verbatim: the
    # evidence standard is unchanged (some span of the quote is still literally in the
    # abstract), only its tolerance for elision changes.
    min_run = max(MIN_QUOTE_WORDS, 4)
    for run in range(len(words), min_run - 1, -1):
        for start in range(0, len(words) - run + 1):
            if " ".join(words[start:start + run]) in a:
                return True
    return False


def _fallback_digest(paper: dict[str, Any]) -> dict[str, Any]:
    """No-LLM degradation: quote the publisher's abstract instead of inventing text."""
    abstract = normalize_text(paper.get("abstract"))
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", abstract) if s.strip()] if abstract else []
    def first(pred) -> str:
        for sentence in sentences:
            if pred(sentence):
                return sentence
        return ""
    background = first(lambda s: True) or "公开摘要不可用。"
    highlights = [first(lambda s: re.search(r"\b(we (propose|show|demonstrate|report|find)|we present)\b", s, re.I))] 
    highlights = [h for h in highlights if h][:2]
    return {
        "title_cn": "",
        "background": background,
        "highlights": highlights or [abstract[:220] + ("…" if len(abstract) > 220 else "")],
        "problem": first(lambda s: re.search(r"\b(however|although|yet|but|problem|challenge)\b", s, re.I)) or background,
        "takeaway": first(lambda s: re.search(r"\b(we (develop|construct|derive|provide)|method|approach|model)\b", s, re.I)) or "",
        "outlook": first(lambda s: re.search(r"\b(future|further|these results suggest|we anticipate|open)\b", s, re.I)) or "",
        "verified": "0/0（未启用 LLM，直接摘录原文）",
        "source": "出版商公开摘要原文摘录（未启用 LLM 精读）",
    }


def assess_poster_digests(papers: list[dict[str, Any]], config: dict[str, Any]) -> None:
    """Attach an auditable Chinese digest to the poster papers.

    The poster prints no scores any more, only prose, so every sentence has to be
    traceable to the public abstract. The model is asked to attach an English
    evidence quote to each bullet; anything whose quote is not verbatim in the
    abstract is discarded rather than shown unverified.
    """
    for paper in papers:
        if paper.get("digest"):
            continue
        abstract = normalize_text(paper.get("abstract"))
        if not abstract:
            paper["digest"] = {**_fallback_digest(paper), "verified": "公开摘要不可用"}
            continue
        if not deepseek_enabled():
            paper["digest"] = _fallback_digest(paper)
            continue

        payload = (f"Title: {paper.get('title', '')}\nVenue: {paper.get('venue', '')}\n"
                   f"Keywords matched: {', '.join(paper.get('reasons', [])[:8])}\n"
                   f"Abstract: {abstract[:2200]}")
        system = (
            "你是严谨的凝聚态物理/材料理论研究者，为同行写文献海报精读。你只会收到题名、匹配关键词和出版商公开摘要，"
            "没有全文。你的唯一义务是：说出来的每一句都能在摘要里找到出处。"
        )
        user = f"""请基于下面这段出版商公开摘要，产出海报用的中文精读，字段含义如下：

- background 研究背景：这个方向已知什么、为什么还要做
- problem 解决的关键问题：本文具体地回答/解决了什么
- highlights 创新亮点：2-3 条，本文相对已有工作新在哪里
- takeaway 可借鉴之处：对做第一性原理/理论计算的同行，方法、模型或思路上能直接借用的部分
- outlook 可延展方向：基于本文方法与体系的合理延伸方向，必须写成明确的展望口吻（如“可进一步把该模型推广到…”“可检验的是…”），不得预测具体数值

硬性规则：
1. 只使用给定的摘要原文，禁止引入摘要之外的任何信息（禁止补充结果、数据、图号、团队、机构、应用前景）。
2. 每个字段都要给 quote：2-8 个英文单词，逐字摘自上面的摘要原文（大小写、标点与原文一致）。
3. 摘要支撑不住的字段，直接省略该字段（text 留空、不进入输出），绝不写“摘要未提供”“摘要未披露”之类的占位语，也绝不编造；每条保留的字段都必须带逐字 quote。
4. 中文用词标准专业（第一性原理/密度泛函理论、紧束缚模型、对称性分析、拓扑非平凡、序参量、相变标度律等），不要营销化、不要夸张、不要抒情。
5. 每个 text 控制在 60-110 个汉字，简洁。

输出严格 JSON，不要代码块、不要解释：
{{"title_cn":"中文题名","background":{{"text":"","quote":""}},"problem":{{"text":"","quote":""}},"highlights":[{{"text":"","quote":""}},{{"text":"","quote":""}}],"takeaway":{{"text":"","quote":""}},"outlook":{{"text":"","quote":""}}}}

{payload}"""
        try:
            from deepseek_client import chat_text  # local import: keeps this module standalone-safe
            answer = chat_text(system, user, max_tokens=1600, temperature=0.2)
        except Exception as exc:  # noqa: BLE001 - a digest failure must not kill the run
            print(f"warning: poster digest failed for {paper.get('doi', '?')}: {exc}")
            paper["digest"] = _fallback_digest(paper)
            continue
        if not answer:
            paper["digest"] = _fallback_digest(paper)
            continue
        match = re.search(r"\{.*\}|\[.*\]", answer, re.S)
        parsed = None
        if match:
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError:
                parsed = None
        if not parsed:
            print(f"warning: poster digest JSON unparsable for {paper.get('doi', '?')}, falling back to abstract quotes")
            paper["digest"] = _fallback_digest(paper)
            continue

        def cell(key: str) -> str | None:
            item = parsed.get(key) or {}
            text = normalize_text(item.get("text", "")) if isinstance(item, dict) else normalize_text(item)
            quote = (item.get("quote", "") if isinstance(item, dict) else "") or ""
            if not text:
                return None
            if not _quote_abstract(quote, abstract):
                return None
            return _clip_text(text, 130)

        highlights: list[str] = []
        total = 0
        verified = 0
        for item in parsed.get("highlights") or []:
            if not isinstance(item, dict):
                continue
            text = normalize_text(item.get("text", ""))
            if not text:
                continue
            total += 1
            if _quote_abstract(item.get("quote", ""), abstract):
                verified += 1
                text = _clip_text(text, 130)
                if text not in highlights:
                    highlights.append(text)

        # If verification rejects everything the model returned, degrade to the abstract
        # instead of printing an unverified poster.
        if verified == 0 and total > 0:
            print(f"warning: poster digest quotes unverifiable for {paper.get('doi', '?')}, using abstract quotes")
            paper["digest"] = _fallback_digest(paper)
            continue

        digest = {
            "title_cn": _clip_text(parsed.get("title_cn", "") or paper.get("title", ""), 90),
            "background": cell("background"),
            "problem": cell("problem"),
            "takeaway": cell("takeaway"),
            "outlook": cell("outlook"),
            "highlights": highlights,
            "verified": f"{verified}/{total + 3} 条已逐字溯源到公开摘要",
            "source": "出版商公开摘要（LLM 精读 + 逐条英文原文溯源校验）",
        }
        # A field the abstract cannot support is left *empty on purpose*: the poster
        # renderer skips sections with no content instead of printing a placeholder
        # sentence the reader would have to take on faith.
        paper["digest"] = digest
        print(f"poster digest: {paper.get('doi', '?')} | traced {verified}/{total} bullets to the abstract")


def resolve_attachment_targets(poster_papers: list[dict[str, Any]],
                               selected_for_history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Which papers get a full-text hunt: the poster papers, nothing else.

    Both arguments are non-empty in a real run (`selected_for_history` falls back to
    `ranked[:max_papers]`), so an earlier guard of the shape
    `poster_papers if not selected_for_history else selected_for_history` resolved to
    the second branch on every run and eight papers were downloaded instead of the two
    the poster advertises. The poster list is authoritative; it only degrades when the
    run produced no poster at all.
    """
    if poster_papers:
        return list(poster_papers)
    return list(selected_for_history[:1])


def pick_poster_papers(papers: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    """Two posters, biased to the user's priority journals' highly-relevant published work.

    The user (2026-10-06) asked that the two posters prefer **highly-relevant** articles
    from a fixed shortlist of venues - Nature, Science, Nature Physics, Science Advances,
    PRL, PRX, Nature Communications - and that published work always beats a preprint
    ("only fall back to preprints if there really are none").

    So the candidate order is: (1) a paper from a priority journal *and* above the
    high-relevance threshold ranks first, (2) then any other published paper by tier,
    (3) then preprints. The two slots are filled from that order while keeping the two
    posters in *different* journals where possible - a preference, not a hard rule: if
    relaxing it is the only way to reach two posters, the same journal is allowed.
    """
    wanted = int(((config.get("digest_images") or {}).get("poster_papers", 2)))
    priority_journals = list((config.get("digest_images") or {}).get("poster_priority_journals", []))
    high_threshold = float((config.get("digest_images") or {}).get("poster_high_relevance_threshold", 8.0))

    for paper in papers:
        paper["recommendation"] = recommendation_score(paper, config)

    def rel(paper: dict[str, Any]) -> float:
        return float((paper.get("recommendation") or {}).get("total", 0))

    def tier(paper: dict[str, Any]) -> int:
        return journal_tier(paper.get("venue", ""), config)

    def is_priority(paper: dict[str, Any]) -> bool:
        venue = paper.get("venue", "")
        return any(is_journal_match(venue, j) for j in priority_journals)

    def same_journal(a: dict[str, Any], b: dict[str, Any]) -> bool:
        return bool(a.get("venue")) and is_journal_match(a.get("venue", ""), b.get("venue", ""))

    if not papers:
        return []

    published = [p for p in papers if not is_preprint(p)]
    preprints = sorted((p for p in papers if is_preprint(p)), key=lambda p: -rel(p))

    # Order: priority-journal + high-relevance first, then by tier (1区 before 2区), then
    # by relevance. NOTE: journal_tier returns 1 for 顶刊/1区 (best) and 2 for 2区, so the
    # secondary key must be +tier (ascending) - a -tier here would pick the WORSE journal
    # first, which is the opposite of "by tier".
    def key(paper: dict[str, Any]) -> tuple:
        return (0 if (is_priority(paper) and rel(paper) >= high_threshold) else 1,
                tier(paper), -rel(paper))
    ordered = sorted(published, key=key)

    picked: list[dict[str, Any]] = []
    # Pass 1 - keep the two posters in different journals.
    for paper in ordered:
        if len(picked) >= wanted:
            break
        if any(same_journal(paper, q) for q in picked):
            continue
        picked.append(paper)
    # Pass 2 - if the distinct-journal rule left us short, fill from the rest.
    if len(picked) < wanted:
        for paper in ordered:
            if paper in picked:
                continue
            if len(picked) >= wanted:
                break
            picked.append(paper)
    # Pass 3 - only if published work could not fill both slots, use preprints.
    if len(picked) < wanted:
        for paper in preprints:
            if paper in picked:
                continue
            if len(picked) >= wanted:
                break
            picked.append(paper)

    return picked[:wanted]


# ---------------------------------------------------------------------------
# Local fetch bridge.
#
# APS blocks the Actions runner's egress IP (HTTP 403 on the publisher PDF route),
# so the cloud run cannot download APS poster PDFs itself. A local run on a normal
# machine can. The hand-over is three steps:
#   1. the cloud writes the two poster DOIs to a wishlist and publishes it;
#   2. a local automation fetches those PDFs and pushes them into
#      research_briefs/attachments/<date>/ with a manifest;
#   3. the cloud refreshes that folder from origin/main and reuses the files.
# Everything below is the cloud side of that bridge.
# ---------------------------------------------------------------------------

def _git(*args: str, timeout: int = 120) -> "subprocess.CompletedProcess":
    import subprocess
    return subprocess.run(["git", *args], capture_output=True, text=True, timeout=timeout)


def _git_commit_push(paths: list[str], message: str) -> bool:
    """Commit and push specific paths; return True when something was pushed.

    Best-effort only: a failure here must never break the run. Mirrors the existing
    ledger commit step (git pull --rebase --autostash origin main && push HEAD:main).
    """
    try:
        _git("config", "user.name", "github-actions[bot]")
        _git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
        _git("add", *paths)
        res = _git("commit", "-m", message)
        if res.returncode != 0:
            return False  # nothing to commit
        _git("pull", "--rebase", "--autostash", "origin", "main")
        push = _git("push", "origin", "HEAD:main")
        return push.returncode == 0
    except Exception as exc:  # noqa: BLE001 - bridge is a convenience, never fatal
        print(f"warning: could not publish {paths}: {exc}", file=sys.stderr)
        return False


def _git_refresh_attachments(run_date: dt.date) -> None:
    """Bring the locally-pushed poster files into the dated folder, without touching the
    cloud's own uncommitted fetches.

    `git checkout origin/main -- <folder>` would *delete* any file the cloud already
    fetched and has not committed (the cloud run only commits the ledger, never the
    attachments folder), so we instead extract only the files the local automation
    committed - the manifest and the poster PDFs named in it - straight out of the
    origin/main blob. Everything the cloud fetched stays put; reuse_synced_attachments
    then reads the refreshed manifest and picks up the new files.
    """
    folder = f"research_briefs/attachments/{run_date.isoformat()}"
    try:
        _git("fetch", "origin", "main", timeout=90)
        man = _git_bytes(f"origin/main:{folder}/manifest.json")
        if man is None:
            return
        import json
        try:
            payload = json.loads(man.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            return
        if payload.get("date") != run_date.isoformat():
            return
        out_dir = Path(folder)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "manifest.json").write_bytes(man)
        for entry in payload.get("files", []):
            name = entry.get("name", "")
            if not name:
                continue
            blob = _git_bytes(f"origin/main:{folder}/{name}")
            if blob is not None:
                (out_dir / name).write_bytes(blob)
    except Exception as exc:  # noqa: BLE001
        print(f"warning: attachment refresh failed: {exc}", file=sys.stderr)


def _git_bytes(ref_path: str) -> bytes | None:
    """Return the raw bytes of a blob at a git ref path, or None if it does not exist."""
    import subprocess
    try:
        proc = subprocess.run(["git", "show", ref_path], capture_output=True, timeout=90)
    except Exception as exc:  # noqa: BLE001
        print(f"warning: git show {ref_path} failed: {exc}", file=sys.stderr)
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    return proc.stdout


def _wait_and_reuse(run_date: dt.date, config: dict[str, Any], note: str, *,
                    papers: list[Any], poster_papers: list[dict[str, Any]]) -> tuple[list[tuple[str, Any]], str]:
    """Reuse locally-fetched poster PDFs, polling for them when the cloud could not fetch.

    Returns (synced paper-file pairs, updated note). The cloud attempt has already run;
    if it produced files for every poster we are done. Otherwise the missing poster PDFs
    are almost certainly APS-blocked, so we wait (up to a bounded window) for the local
    machine to fetch and push them, refreshing the folder from origin/main and re-trying
    reuse each round. If the local machine is off or late, we give up gracefully and the
    mail goes out brief-only rather than stalling the pipeline.
    """
    import time
    from fetch_paper_attachments import reuse_synced_attachments as _reuse

    # Quick try first - a local run may already have pushed for today.
    _git_refresh_attachments(run_date)
    pairs, _cards, note = _reuse(run_date, config, note, papers=papers)
    if pairs:
        return pairs, note

    waited = 0
    max_wait = int((config.get("paper_attachments") or {}).get("local_pdf_wait_seconds", 1800))
    interval = 300
    while waited < max_wait:
        time.sleep(interval)
        waited += interval
        _git_refresh_attachments(run_date)
        pairs, _cards, note = _reuse(run_date, config, note, papers=papers)
        if pairs:
            return pairs, note
        print(f"attachment poll: still waiting for locally-fetched poster PDFs ({waited}s)")
    print("attachment poll: local fetch window elapsed; sending brief without poster PDFs")
    return [], note


def google_scholar_link(title: str) -> str:
    return "https://scholar.google.com/scholar?" + urllib.parse.urlencode({"q": title})


def compact_list(value: str, sep: str = ",", limit: int = 4) -> str:
    parts = [p.strip() for p in value.split(sep) if p.strip()]
    if not parts:
        return "Unknown"
    if len(parts) <= limit:
        return ", ".join(parts)
    return ", ".join(parts[:limit]) + ", et al."


def score_label(score: float) -> str:
    if score >= 8:
        return "high"
    if score >= 4:
        return "medium"
    return "low"


def focus_note(paper: dict[str, Any], config: dict[str, Any]) -> str:
    text = paper_text(paper)
    profile = config.get("research_profile", {})
    notes: list[str] = []
    for keyword in profile.get("core_topics", [])[:8]:
        if keyword.lower() in text:
            notes.append(f"matches {keyword}")
    for keyword in profile.get("method_keywords", [])[:8]:
        if keyword.lower() in text:
            notes.append(f"uses or mentions {keyword}")
    for keyword in profile.get("objective_keywords", [])[:8]:
        if keyword.lower() in text:
            notes.append(f"touches {keyword}")
    if not notes:
        notes.append("overlaps with your profile through title, venue, or concepts")
    return "; ".join(notes[:3]) + "."


def action_note(score: float) -> str:
    if score >= 8:
        return "Read today: inspect the problem formulation, assumptions, and evaluation setup."
    if score >= 4:
        return "Save and skim: check abstract, system model, and baselines before deep reading."
    return "Low priority: keep only if the title directly supports current writing."


def abstract_note(paper: dict[str, Any], *, language: str) -> str:
    abstract = normalize_text(paper.get("abstract"))
    if not abstract:
        return "摘要元数据不可用，建议打开链接查看原文页面。"
    limit = 260 if language.lower().startswith("zh") else 340
    if len(abstract) <= limit:
        return abstract
    return abstract[:limit].rsplit(" ", 1)[0] + "..."


def deepseek_note(paper: dict[str, Any]) -> str:
    """The one-sentence-per-paragraph read of the paper, labelled with what it rests on.

    The label is not decoration. A summary written from a 96-character citation line is
    not the same claim as one written from the 1670-character abstract, and the reader
    has a right to know which he is holding before he decides what to open.
    """
    if not deepseek_enabled():
        return ""
    abstract = (paper.get("abstract") or "").strip()
    if not abstract:
        return ""
    scope = "" if len(abstract) >= MIN_USEFUL_ABSTRACT_CHARS else "【依据题名与题录撰写】\n"
    if is_priority_journal(paper.get("venue", "")):
        return scope + innovation_summary(paper)
    return scope + paper_digest(paper, style="brief")


def terms_match(text: str, terms: list[str]) -> bool:
    return any(term.lower() in text for term in terms)


def group_recommendations(papers: list[dict[str, Any]], config: dict[str, Any]) -> tuple[list[tuple[str, list[dict[str, Any]]]], list[dict[str, Any]]]:
    groups = config.get("research_profile", {}).get("recommendation_groups", [])
    if not groups:
        selected = papers[: int(config.get("max_papers", 8))]
        return [], selected

    base_limit = int(config.get("papers_per_direction", 5))
    extra_limit = int(config.get("extra_papers_per_direction", 0))
    excellent_threshold = float(config.get("excellent_score_threshold", 9.0))
    grouped: list[tuple[str, list[dict[str, Any]]]] = []
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()

    featured = [
        paper for paper in papers
        if publication_priority(paper) == 0 and paper.get("abstract")
    ][:base_limit + extra_limit]
    if featured:
        grouped.append(("顶刊/已发表精选", featured))
        for paper in featured:
            key = paper_history_id(paper)
            if key and key not in seen:
                seen.add(key)
                unique.append(paper)

    def _take(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        selected = [p for p in candidates if paper_history_id(p) not in seen][:base_limit]
        extras = [
            p for p in candidates[base_limit:]
            if float(p.get("score", 0)) >= excellent_threshold and paper_history_id(p) not in seen
        ][:extra_limit]
        chosen = selected + extras
        for paper in chosen:
            key = paper_history_id(paper)
            if key and key not in seen:
                seen.add(key)
                unique.append(paper)
        return chosen

    # Direction groups claim their papers before the featured list does. Filling
    # "priority-journal selected" first meant every priority-journal paper that carried
    # an abstract landed there, so the four direction groups kept reporting
    # "no sufficiently relevant candidates" while the whole day's output sat in the
    # featured section - that is the section the user actually reads for direction.
    for group in groups:
        name = normalize_text(group.get("name")) or "方向"
        terms = group.get("terms", [])
        context_terms = group.get("context_terms", [])
        candidates: list[dict[str, Any]] = []
        for paper in papers:
            text = paper_text(paper)
            if not terms_match(text, terms):
                continue
            if context_terms and not terms_match(text, context_terms):
                continue
            candidates.append(paper)
        grouped.append((name, _take(candidates)))

    featured = [
        paper for paper in papers
        if publication_priority(paper) == 0 and paper.get("abstract")
        and paper_history_id(paper) not in seen
    ]
    if featured:
        grouped.append(("顶刊/已发表精选", _take(featured)))
    return grouped, unique


def make_markdown(papers: list[dict[str, Any]], config: dict[str, Any], run_date: dt.date) -> str:
    max_papers = int(config.get("max_papers", 8))
    grouped, selected = group_recommendations(papers, config)
    selected = selected[:max_papers] if selected else papers[:max_papers]
    detailed = selected[:5]
    remaining = selected[5:]
    high = sum(1 for p in selected if p.get("score", 0) >= 8)
    medium = sum(1 for p in selected if 4 <= p.get("score", 0) < 8)
    top_count, published_count, arxiv_count = publication_mix(selected)
    language = config.get("language", "en")

    if language.lower().startswith("zh"):
        title = f"# 科研简报 | {run_date.isoformat()}"
        summary = f"今日筛出 {len(selected)} 篇候选论文：顶刊已发表 {top_count} 篇，其他已发表/开放元数据 {published_count} 篇，arXiv 预印本 {arxiv_count} 篇；高相关 {high} 篇，中等相关 {medium} 篇。"
        overview = "## 今日概览"
        priority = "## 分方向推荐"
        details = "## 精读清单"
        rest = "## 其余候选"
        advice = "## 今日建议"
        why = "为什么值得看"
        action = "建议动作"
        final_notes = [
            "优先阅读高相关论文；中等相关论文先看摘要、问题定义和实验设置。",
            "如果候选过少，请放宽 query_templates；如果跑题太多，请收紧 domain_keywords。",
            "BibTeX 已同步生成，可导入 Zotero、EndNote 或其他文献管理器。",
        ]
    else:
        title = f"# Research Brief | {run_date.isoformat()}"
        summary = f"Selected {len(selected)} candidate papers: {top_count} priority-journal published, {published_count} other published/open metadata, {arxiv_count} arXiv preprints; {high} high relevance, {medium} medium relevance."
        overview = "## Overview"
        priority = "## Today's Priority"
        details = "## Reading List"
        rest = "## Other Candidates"
        advice = "## Suggested Actions"
        why = "Why it matters"
        action = "Action"
        final_notes = [
            "Read high-relevance papers first; skim medium-relevance papers for problem formulation and experiments.",
            "If there are too few papers, broaden query_templates; if results drift, tighten domain_keywords.",
            "BibTeX files are generated for Zotero, EndNote, or other reference managers.",
        ]

    lines: list[str] = [title, "", summary]
    if selected:
        top = selected[0]
        lines.append(f"Top pick: {top['title']} ({top.get('venue') or 'Unknown'}, {top.get('published') or 'date unknown'}).")
    else:
        lines.append("No relevant papers were found in this run.")
    lines.append("")
    lines.append(overview)
    lines.append(f"- Source mix: 顶刊已发表 {top_count}; 其他已发表/开放元数据 {published_count}; arXiv {arxiv_count}.")
    lines.append("- Ranking rule: priority-journal published papers first, other published papers second, arXiv preprints after published articles.")
    lines.append("- Abstract rule: top-journal papers are included when abstract metadata can be found, even if full text is inaccessible.")
    lines.append("")

    if grouped:
        lines.append(priority)
        for group_name, group_papers in grouped:
            lines.append(f"### {group_name}")
            if not group_papers:
                lines.append("- 近几天未筛到足够高相关候选。")
                lines.append("")
                continue
            for idx, paper in enumerate(group_papers, 1):
                score = float(paper.get("score", 0))
                reasons = ", ".join(paper.get("reasons", [])) or "semantic match"
                url = paper.get("url") or google_scholar_link(paper["title"])
                pdf = pdf_link(paper)
                lines.append(f"{idx}. **{paper['title']}**")
                zone = zone_badge(paper.get("venue", ""), config)
                venue_line = (f"{paper.get('venue') or 'Unknown'}"
                              + (f"（{zone}）" if zone else ""))
                lines.append(f"   - Source: {source_badge(paper)} | {venue_line} | {paper.get('published') or 'Unknown'}")
                lines.append(f"   - Authors: {compact_list(paper.get('authors', 'Unknown'))}")
                lines.append(f"   - DOI: {paper.get('doi') or 'N/A'}")
                lines.append(f"   - Link: {url}")
                if pdf:
                    lines.append(f"   - PDF: {pdf}")
                lines.append(f"   - 全文获取: {access_badge(paper)}")
                lines.append(f"   - Abstract: {abstract_badge(paper)}")
                lines.append(f"   - Relevance: {score_label(score)}, {score:.1f}; matched: {reasons}")
                lines.append(f"   - 摘要要点: {abstract_note(paper, language=language)}")
                note = deepseek_note(paper)
                if note:
                    note_label = "DeepSeek创新点总结" if is_priority_journal(paper.get("venue", "")) else "DeepSeek解读"
                    lines.append(f"   - {note_label}: {note}")
            lines.append("")
    elif selected:
        lines.append(priority)
        for idx, paper in enumerate(selected[:3], 1):
            score = float(paper.get("score", 0))
            lines.append(f"{idx}. **{paper['title']}** - {score_label(score)}, {score:.1f}; {paper.get('venue') or 'Unknown'}.")
        lines.append("")

        lines.append(details)
        for idx, paper in enumerate(detailed, 1):
            score = float(paper.get("score", 0))
            reasons = ", ".join(paper.get("reasons", [])) or "semantic match"
            url = paper.get("url") or google_scholar_link(paper["title"])
            pdf = pdf_link(paper)
            lines.append(f"### {idx}. {paper['title']}")
            lines.append(f"- Source: {source_badge(paper)}")
            zone = zone_badge(paper.get("venue", ""), config)
            venue_line = (paper.get("venue") or "Unknown")
            if zone:
                venue_line += f"（{zone}）"
            lines.append(f"- Venue: {venue_line} ({paper.get('source')})")
            lines.append(f"- Authors: {compact_list(paper.get('authors', 'Unknown'))}")
            lines.append(f"- Affiliations: {compact_list(paper.get('affiliations', 'Metadata unavailable'), sep=';', limit=2)}")
            lines.append(f"- Date: {paper.get('published') or 'Unknown'}")
            lines.append(f"- DOI: {paper.get('doi') or 'N/A'}")
            lines.append(f"- Link: {url}")
            if pdf:
                lines.append(f"- PDF: {pdf}")
            lines.append(f"- Scholar: {google_scholar_link(paper['title'])}")
            lines.append(f"- 全文获取: {access_badge(paper)}")
            lines.append(f"- Abstract: {abstract_badge(paper)}")
            lines.append(f"- Relevance: {score_label(score)}, {score:.1f}; matched: {reasons}")
            lines.append(f"- 摘要要点: {abstract_note(paper, language=language)}")
            note = deepseek_note(paper)
            if note:
                note_label = "DeepSeek创新点总结" if is_priority_journal(paper.get("venue", "")) else "DeepSeek解读"
                lines.append(f"- {note_label}: {note}")
            lines.append(f"**{why}:** {focus_note(paper, config)}")
            lines.append(f"**{action}:** {action_note(score)}")
            lines.append("")

        if remaining:
            lines.append(rest)
            for idx, paper in enumerate(remaining, len(detailed) + 1):
                score = float(paper.get("score", 0))
                url = paper.get("url") or google_scholar_link(paper["title"])
                lines.append(f"- {idx}. {paper['title']} | {paper.get('venue') or 'Unknown'} | {score_label(score)} {score:.1f} | {url}")
            lines.append("")

    lines.append(advice)
    for note in final_notes:
        lines.append(f"- {note}")
    return "\n".join(lines)

def inline_html(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    return re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', escaped)


def markdown_to_html(markdown: str) -> str:
    html_lines = [
        "<html><body style=\"margin:0;background:#f6f7f9;color:#1f2933;font-family:Arial,'Microsoft YaHei',sans-serif;\">",
        "<div style=\"max-width:760px;margin:0 auto;padding:24px 16px;\">",
        "<div style=\"background:#ffffff;border:1px solid #e5e7eb;border-radius:8px;padding:24px;\">",
    ]
    list_stack: list[str] = []

    def close_lists() -> None:
        while list_stack:
            html_lines.append(f"</{list_stack.pop()}>")

    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line:
            close_lists()
            html_lines.append("<div style=\"height:8px\"></div>")
            continue
        if line.startswith("# "):
            close_lists()
            html_lines.append(f"<h1 style=\"font-size:24px;line-height:1.3;margin:0 0 12px;color:#111827;\">{inline_html(line[2:])}</h1>")
        elif line.startswith("## "):
            close_lists()
            html_lines.append(f"<h2 style=\"font-size:18px;line-height:1.35;margin:24px 0 10px;color:#0f766e;border-bottom:1px solid #e5e7eb;padding-bottom:6px;\">{inline_html(line[3:])}</h2>")
        elif line.startswith("### "):
            close_lists()
            html_lines.append(f"<h3 style=\"font-size:15px;line-height:1.45;margin:18px 0 8px;color:#111827;\">{inline_html(line[4:])}</h3>")
        elif re.match(r"\d+\. ", line):
            if not list_stack or list_stack[-1] != "ol":
                close_lists()
                list_stack.append("ol")
                html_lines.append("<ol style=\"margin:6px 0 14px 22px;padding:0;\">")
            item = re.sub(r"^\d+\. ", "", line)
            html_lines.append(f"<li style=\"margin:7px 0;line-height:1.55;\">{inline_html(item)}</li>")
        elif line.startswith("- "):
            if not list_stack or list_stack[-1] != "ul":
                close_lists()
                list_stack.append("ul")
                html_lines.append("<ul style=\"margin:6px 0 14px 20px;padding:0;\">")
            html_lines.append(f"<li style=\"margin:6px 0;line-height:1.55;\">{inline_html(line[2:])}</li>")
        else:
            close_lists()
            html_lines.append(f"<p style=\"font-size:14px;line-height:1.7;margin:8px 0;\">{inline_html(line)}</p>")

    close_lists()
    html_lines.append("</div>")
    html_lines.append("<p style=\"font-size:12px;color:#6b7280;margin:12px 4px 0;\">Generated by Research Brief Actions.</p>")
    html_lines.append("</div></body></html>")
    return "\n".join(html_lines)


def bibtex_key(paper: dict[str, Any]) -> str:
    first_author = paper.get("authors", "paper").split(",")[0].split()[-1].lower()
    year = re.search(r"\d{4}", paper.get("published", ""))
    first_title_word = re.sub(r"[^A-Za-z0-9]", "", paper.get("title", "paper").split()[0]).lower()
    return f"{first_author}{year.group(0) if year else 'nd'}{first_title_word}"


def make_bibtex(papers: list[dict[str, Any]], max_papers: int) -> str:
    entries: list[str] = []
    for paper in papers[:max_papers]:
        year_match = re.search(r"\d{4}", paper.get("published", ""))
        fields = {
            "title": paper.get("title", ""),
            "author": paper.get("authors", "").replace(", ", " and "),
            "journal": paper.get("venue", ""),
            "year": year_match.group(0) if year_match else "",
            "doi": paper.get("doi", ""),
            "url": paper.get("url", ""),
        }
        body = ",\n".join(f"  {k} = {{{v}}}" for k, v in fields.items() if v)
        entries.append(f"@article{{{bibtex_key(paper)},\n{body}\n}}")
    return "\n\n".join(entries) + ("\n" if entries else "")


def safe_filename(value: str, *, max_len: int = 120) -> str:
    text = normalize_text(value)
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return (text[:max_len].strip(" .") or "untitled")


def week_range(run_date: dt.date) -> tuple[dt.date, dt.date]:
    start = run_date - dt.timedelta(days=run_date.weekday())
    return start, start + dt.timedelta(days=6)


def compact_date(value: dt.date) -> str:
    return value.strftime("%Y%m%d")


def weekly_archive_dir(run_date: dt.date) -> Path:
    start, end = week_range(run_date)
    return ARCHIVE_DIR / f"{compact_date(start)}-{compact_date(end)}"


def daily_archive_dir(run_date: dt.date) -> Path:
    return weekly_archive_dir(run_date) / compact_date(run_date)


def paper_file_stem(paper: dict[str, Any], config: dict[str, Any]) -> str:
    topic = paper_topic(paper, config)
    venue = paper.get("venue") or paper.get("source") or "Unknown"
    published = paper.get("published") or "unknown-date"
    return safe_filename(f"{topic}-{venue}-{published}-{paper.get('title', '')}", max_len=180)


# Publishers commonly return 403 to a bot-style User-Agent even for their open-access
# PDFs. A standard browser User-Agent is used *only* for PDFs of articles that are already
# open access; closed-access articles are never downloaded.
PDF_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def download_pdf(paper: dict[str, Any], config: dict[str, Any], out_dir: Path) -> str:
    url = pdf_link(paper)
    if not url:
        return ""
    out_path = out_dir / f"{paper_file_stem(paper, config)}.pdf"
    if out_path.exists() and out_path.stat().st_size > 0:
        return str(out_path)
    headers = {"User-Agent": user_agent(config)}
    if "arxiv.org" not in url:
        headers = {"User-Agent": PDF_USER_AGENT, "Accept": "application/pdf,*/*"}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            content_type = resp.headers.get("Content-Type", "")
            data = resp.read(30 * 1024 * 1024)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        print(f"warning: failed to download PDF for {paper.get('title', 'unknown')}: {exc}", file=sys.stderr)
        return ""
    if b"%PDF" not in data[:1024] and "pdf" not in content_type.lower():
        print(f"warning: skipped non-PDF response for {paper.get('title', 'unknown')}", file=sys.stderr)
        return ""
    out_path.write_bytes(data)
    return str(out_path)


def fallback_research_designs(paper: dict[str, Any], config: dict[str, Any]) -> str:
    topic = paper_topic(paper, config)
    venue = paper.get("venue") or paper.get("source") or "Unknown"
    return "\n".join([
        f"1. 围绕“{topic}”复现实验/计算问题定义：整理 {venue} 文章的材料体系、关键物理量与对照基线，建立可复核的最小数据表。",
        "2. 设计同族材料或相近结构的可迁移验证：比较能带、磁序/极化态、输运或拓扑指标，检验结论是否只依赖特定样品。",
        "3. 结合机器学习或高通量筛选构造候选扩展：用摘要中的关键词限定特征空间，并以第一性原理或公开实验数据做小规模验证。"
    ])


def ensure_research_designs(papers: list[dict[str, Any]], config: dict[str, Any]) -> None:
    for paper in papers:
        if paper.get("research_designs"):
            continue
        note = research_design_ideas(paper) if deepseek_enabled() else ""
        paper["research_designs"] = normalize_text(note).replace(" 2.", "\n2.").replace(" 3.", "\n3.") or fallback_research_designs(paper, config)
        time.sleep(0.15 if deepseek_enabled() else 0)


def prepare_archive_metadata(papers: list[dict[str, Any]], config: dict[str, Any]) -> None:
    for paper in papers:
        paper["topic"] = paper_topic(paper, config)
        if not paper.get("deepseek_note"):
            paper["deepseek_note"] = deepseek_note(paper) if deepseek_enabled() else ""
        if not paper.get("deepseek_note"):
            paper["deepseek_note"] = abstract_note(paper, language=config.get("language", "zh-CN"))
    ensure_research_designs(papers, config)


def archive_daily_papers(papers: list[dict[str, Any]], config: dict[str, Any], run_date: dt.date) -> Path:
    out_dir = daily_archive_dir(run_date)
    out_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for paper in papers:
        pdf_path = download_pdf(paper, config, out_dir)
        record = dict(paper)
        record["topic"] = paper_topic(paper, config)
        record["pdf_path"] = pdf_path
        record["collected_date"] = run_date.isoformat()
        records.append(record)
    (out_dir / "papers.json").write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return out_dir


def xml_escape(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def xlsx_col_name(index: int) -> str:
    name = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name


def write_simple_xlsx(path: Path, rows: list[list[Any]], *, sheet_name: str = "Weekly Summary") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet_rows: list[str] = []
    for r_idx, row in enumerate(rows, 1):
        cells: list[str] = []
        for c_idx, value in enumerate(row, 1):
            ref = f"{xlsx_col_name(c_idx - 1)}{r_idx}"
            style = ' s="1"' if r_idx == 1 else ""
            cells.append(f'<c r="{ref}" t="inlineStr"{style}><is><t>{xml_escape(value)}</t></is></c>')
        sheet_rows.append(f'<row r="{r_idx}">{"".join(cells)}</row>')
    last_col = xlsx_col_name(max(len(rows[0]) - 1, 0)) if rows else "A"
    dimension = f"A1:{last_col}{max(len(rows), 1)}"
    worksheet = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<dimension ref="{dimension}"/><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>
<cols>{''.join(f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>' for i, w in enumerate([12,16,20,12,50,30,24,44,44,18,34,22,32,12,28,70,80], 1))}</cols>
<sheetData>{"".join(sheet_rows)}</sheetData><autoFilter ref="{dimension}"/></worksheet>'''
    workbook = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="{xml_escape(sheet_name)}" sheetId="1" r:id="rId1"/></sheets></workbook>'''
    styles = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF0F766E"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'''
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>''')
        zf.writestr("_rels/.rels", '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>''')
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>''')
        zf.writestr("xl/worksheets/sheet1.xml", worksheet)
        zf.writestr("xl/styles.xml", styles)


def load_week_records(run_date: dt.date) -> list[dict[str, Any]]:
    week_dir = weekly_archive_dir(run_date)
    records: list[dict[str, Any]] = []
    for path in sorted(week_dir.glob("*/papers.json")):
        try:
            records.extend(json.loads(path.read_text(encoding="utf-8-sig")))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"warning: failed to read archived records {path}: {exc}", file=sys.stderr)
    return records


def weekly_excel_rows(records: list[dict[str, Any]]) -> list[list[Any]]:
    headers = ["收藏日期", "主题", "发表期刊/来源", "发表时间", "标题", "作者", "DOI", "论文链接", "PDF本地路径",
               "开放获取状态", "全文获取", "出版商栏目", "摘要状态", "相关度评分", "匹配关键词",
               "摘要/DeepSeek解读", "三条细分选题和研究设计"]
    rows = [headers]
    for paper in records:
        note = paper.get("deepseek_note") or paper.get("summary") or abstract_note(paper, language="zh-CN")
        oa_state = "开放获取" if paper.get("open_access") is True else ("订阅刊（非开放）" if is_subscription_journal(paper) else "未标注")
        rows.append([
            paper.get("collected_date", ""),
            paper.get("topic", ""),
            paper.get("venue") or paper.get("source") or "",
            paper.get("published", ""),
            paper.get("title", ""),
            paper.get("authors", ""),
            paper.get("doi", ""),
            paper.get("url", ""),
            paper.get("pdf_path", ""),
            oa_state,
            access_badge(paper),
            paper.get("publisher_section", ""),
            abstract_badge(paper),
            paper.get("score", ""),
            ", ".join(paper.get("reasons", [])),
            note,
            paper.get("research_designs", ""),
        ])
    return rows


def generate_weekly_excel(run_date: dt.date) -> Path | None:
    records = load_week_records(run_date)
    if not records:
        return None
    week_dir = weekly_archive_dir(run_date)
    start, end = week_range(run_date)
    xlsx_path = week_dir / f"{compact_date(start)}-{compact_date(end)}_科研文献推送周报.xlsx"
    write_simple_xlsx(xlsx_path, weekly_excel_rows(records))
    return xlsx_path


def email_subject(run_date: dt.date, config: dict[str, Any]) -> str:
    prefix = config.get("email_subject_prefix", "Research Brief")
    return f"{prefix} | {run_date.isoformat()}"


def inline_image_html(markdown: str, images: list[Path] | None) -> str:
    """Insert the rendered poster/long image directly under the brief headline."""
    if not images:
        return markdown_to_html(markdown)
    import base64 as _b64

    blocks: list[str] = []
    for path in images:
        if not path.exists():
            continue
        encoded = _b64.b64encode(path.read_bytes()).decode("ascii")
        caption = "海报推介" if "海报" in path.name else "全文长图"
        blocks.append(
            f'<figure><img src="data:image/png;base64,{encoded}" alt="{caption}" '
            f'style="width:100%;max-width:1100px;height:auto;border:1px solid #dde3ea;border-radius:12px;">'
            f'<figcaption style="color:#6c7886;font-size:13px;">{caption}（同时作为附件发送）</figcaption></figure>'
        )
    return markdown_to_html(markdown).replace("</body>", "".join(blocks) + "</body>")


def load_mail_ledger() -> list[dict[str, Any]]:
    if not MAIL_LEDGER_PATH.exists():
        return []
    try:
        data = json.loads(MAIL_LEDGER_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        # A ledger that cannot be parsed is treated as empty, never as "already sent":
        # losing a duplicate risk is cheap, silently losing a day's mail is not.
        return []
    if isinstance(data, dict):
        data = data.get("sent", [])
    if not isinstance(data, list):
        return []
    return [entry for entry in data if isinstance(entry, dict)]


def mail_already_sent(run_date: dt.date, kind: str = "daily") -> bool:
    key = run_date.isoformat()
    return any(entry.get("date") == key and entry.get("kind") == kind for entry in load_mail_ledger())


def mark_mail_sent(run_date: dt.date, kind: str, provider: str) -> None:
    entries = [entry for entry in load_mail_ledger()
               if not (entry.get("date") == run_date.isoformat() and entry.get("kind") == kind)]
    entries.append({"date": run_date.isoformat(), "kind": kind, "provider": provider,
                    "at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")})
    MAIL_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    MAIL_LEDGER_PATH.write_text(
        json.dumps({"sent": entries[-200:]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def send_email(markdown: str, config: dict[str, Any], run_date: dt.date, *, subject: str | None = None,
               attachments: list[Path] | None = None, inline_images: list[Path] | None = None,
               kind: str = "daily", force: bool = False) -> str | None:
    """Mail the brief and record it. Returns the provider, or None when the day/kind
    was already mailed and the run deliberately stayed quiet."""
    if not force and mail_already_sent(run_date, kind):
        print(f"mail skipped: {kind} for {run_date.isoformat()} was already sent today "
              "(re-run with --force-send to send it again)")
        return None
    api_key = os.getenv("SENDGRID_API_KEY")
    from_email = os.getenv("RESEARCH_BRIEF_FROM_EMAIL")
    if api_key and from_email:
        send_email_sendgrid(markdown, config, run_date, api_key, from_email, subject=subject, attachments=attachments, inline_images=inline_images)
        provider = "SendGrid"
    else:
        send_email_smtp(markdown, config, run_date, subject=subject, attachments=attachments, inline_images=inline_images)
        provider = "SMTP"
    mark_mail_sent(run_date, kind, provider)
    return provider


def attachment_payload(path: Path) -> dict[str, str]:
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    suffix = path.suffix.lower()
    content_type = {
        ".pdf": "application/pdf",
        ".zip": "application/zip",
        ".txt": "text/plain; charset=utf-8",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }.get(suffix, "application/octet-stream")
    return {
        "content": data,
        "type": content_type,
        "filename": path.name,
        "disposition": "attachment",
    }


def send_email_sendgrid(markdown: str, config: dict[str, Any], run_date: dt.date, api_key: str, from_email: str, *, subject: str | None = None, attachments: list[Path] | None = None, inline_images: list[Path] | None = None) -> None:
    to_email = resolve_recipient(config)
    payload = {
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": from_email},
        "subject": subject or email_subject(run_date, config),
        "content": [
            {"type": "text/plain", "value": markdown},
            {"type": "text/html", "value": inline_image_html(markdown, inline_images)},
        ],
    }
    if attachments:
        payload["attachments"] = [attachment_payload(path) for path in attachments if path.exists()]
    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"SendGrid returned HTTP {resp.status}")


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


_PLACEHOLDER_EMAIL_RE = re.compile(r"@(example\.(invalid|com|org)|your_|changeme)", re.IGNORECASE)


def resolve_recipient(config: dict[str, Any]) -> str:
    """Recipient for this run, with a hard stop on the shipped placeholder."""
    to_email = os.getenv("SMTP_TO_EMAIL") or os.getenv("RESEARCH_BRIEF_TO_EMAIL") or config.get("recipient_email", "")
    if _PLACEHOLDER_EMAIL_RE.search(to_email or ""):
        raise RuntimeError(
            "Refusing to send: the recipient is still the shipped placeholder "
            f"{config.get('recipient_email', '')!r}. Either set SMTP_TO_EMAIL in the environment, "
            "put a real address in automation/research_brief_config.json -> recipient_email, "
            "or re-run with --no-email to only produce local files."
        )
    if "@" not in (to_email or ""):
        raise RuntimeError(f"Refusing to send: recipient {to_email!r} is not an email address.")
    return to_email


def send_email_smtp(markdown: str, config: dict[str, Any], run_date: dt.date, *, subject: str | None = None, attachments: list[Path] | None = None, inline_images: list[Path] | None = None) -> None:
    to_email = resolve_recipient(config)
    host = os.getenv("SMTP_HOST")
    username = os.getenv("SMTP_USERNAME")
    password = os.getenv("SMTP_PASSWORD")
    if not host or not username or not password:
        raise RuntimeError("Email is not configured. Set either SendGrid secrets or SMTP_HOST + SMTP_USERNAME + SMTP_PASSWORD.")

    port = env_int("SMTP_PORT", 587)
    use_ssl = env_bool("SMTP_USE_SSL", port == 465)
    starttls = env_bool("SMTP_STARTTLS", not use_ssl)
    from_email = os.getenv("SMTP_FROM_EMAIL") or os.getenv("RESEARCH_BRIEF_FROM_EMAIL") or username

    message = EmailMessage()
    message["Subject"] = subject or email_subject(run_date, config)
    message["From"] = from_email
    message["To"] = to_email
    message.set_content(markdown)
    html_body = markdown_to_html(markdown)
    for index, path in enumerate([p for p in (inline_images or []) if p.exists()]):
        # Inline so both images are visible in the client itself, not only as downloads.
        cid = f"digest-image-{index}@research-brief"
        message.add_related(path.read_bytes(), maintype="image", subtype="png", cid=cid, filename=path.name)
        html_body = html_body.replace("</body>", f'<img src="cid:{cid}" style="width:100%;max-width:1100px;height:auto;border:1px solid #dde3ea;border-radius:12px;"><br></body>')
    message.add_alternative(html_body, subtype="html")
    for path in attachments or []:
        if not path.exists():
            continue
        suffix = path.suffix.lower()
        maintype, subtype = {
            ".pdf": ("application", "pdf"),
            ".zip": ("application", "zip"),
            ".txt": ("text", "plain"),
            ".xlsx": ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        }.get(suffix, ("application", "octet-stream"))
        message.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)

    retries = env_int("SMTP_RETRIES", 3)
    context = ssl.create_default_context()
    last_error: Exception | None = None
    print(f"sending email via SMTP host={host} port={port} ssl={use_ssl} starttls={starttls}")
    for attempt in range(1, retries + 1):
        try:
            if use_ssl:
                with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as server:
                    server.login(username, password)
                    server.send_message(message)
            else:
                with smtplib.SMTP(host, port, timeout=30) as server:
                    if starttls:
                        server.starttls(context=context)
                    server.login(username, password)
                    server.send_message(message)
            return
        except (OSError, smtplib.SMTPException) as exc:
            last_error = exc
            if attempt >= retries:
                break
            wait_seconds = 8 * attempt
            print(f"warning: SMTP send attempt {attempt} failed: {exc}; retrying in {wait_seconds}s", file=sys.stderr)
            time.sleep(wait_seconds)
    raise RuntimeError(f"SMTP send failed after {retries} attempts: {last_error}") from last_error


def zotero_library_url() -> tuple[str, str]:
    api_key = os.getenv("ZOTERO_API_KEY")
    user_id = os.getenv("ZOTERO_USER_ID")
    group_id = os.getenv("ZOTERO_GROUP_ID")
    if not api_key:
        raise RuntimeError("ZOTERO_API_KEY must be set to import items to Zotero")
    if group_id:
        return f"https://api.zotero.org/groups/{group_id}", api_key
    if user_id:
        return f"https://api.zotero.org/users/{user_id}", api_key
    raise RuntimeError("ZOTERO_USER_ID or ZOTERO_GROUP_ID must be set to import items to Zotero")


def zotero_headers(api_key: str) -> dict[str, str]:
    return {"Content-Type": "application/json", "Zotero-API-Key": api_key, "Zotero-API-Version": "3"}


def zotero_creators(authors: str) -> list[dict[str, str]]:
    creators: list[dict[str, str]] = []
    for author in [a.strip() for a in authors.split(",") if a.strip()]:
        if author.lower() == "et al.":
            continue
        parts = author.split()
        if len(parts) == 1:
            creators.append({"creatorType": "author", "name": parts[0]})
        else:
            creators.append({"creatorType": "author", "firstName": " ".join(parts[:-1]), "lastName": parts[-1]})
    return creators


def zotero_item_from_paper(paper: dict[str, Any], run_date: dt.date) -> dict[str, Any]:
    item = {
        "itemType": "journalArticle",
        "title": paper.get("title", ""),
        "creators": zotero_creators(paper.get("authors", "")),
        "publicationTitle": paper.get("venue", ""),
        "date": paper.get("published", ""),
        "DOI": paper.get("doi", ""),
        "url": paper.get("url", ""),
        "abstractNote": paper.get("abstract", ""),
        "tags": [{"tag": "research-brief"}, {"tag": f"research-brief-{run_date.isoformat()}"}],
    }
    collection_key = os.getenv("ZOTERO_COLLECTION_KEY")
    if collection_key:
        item["collections"] = [collection_key]
    return {k: v for k, v in item.items() if v}


def zotero_item_exists(paper: dict[str, Any], library_url: str, api_key: str) -> bool:
    query = paper.get("doi") or paper.get("title")
    if not query:
        return False
    params = urllib.parse.urlencode({"format": "json", "itemType": "journalArticle", "limit": "5", "q": query, "qmode": "everything"})
    req = urllib.request.Request(f"{library_url}/items?{params}", headers=zotero_headers(api_key))
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            items = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"warning: failed to check Zotero duplicates for {paper.get('title', 'unknown')}: {exc}", file=sys.stderr)
        return False
    doi = paper.get("doi", "").lower()
    title = title_key(paper.get("title", ""))
    for item in items:
        data = item.get("data", {})
        if doi and normalize_text(data.get("DOI")).lower() == doi:
            return True
        if title and title_key(data.get("title", "")) == title:
            return True
    return False


def import_to_zotero(papers: list[dict[str, Any]], max_papers: int, run_date: dt.date) -> tuple[int, int]:
    library_url, api_key = zotero_library_url()
    imported = 0
    skipped = 0
    for paper in papers[:max_papers]:
        if zotero_item_exists(paper, library_url, api_key):
            skipped += 1
            continue
        req = urllib.request.Request(
            f"{library_url}/items",
            data=json.dumps([zotero_item_from_paper(paper, run_date)]).encode("utf-8"),
            headers=zotero_headers(api_key),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=25) as resp:
            if resp.status >= 300:
                raise RuntimeError(f"Zotero returned HTTP {resp.status}")
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        imported += len(payload.get("successful", {}))
    return imported, skipped


def write_outputs(markdown: str, bibtex: str, run_date: dt.date) -> tuple[Path, Path | None]:
    BRIEF_DIR.mkdir(exist_ok=True)
    md_path = BRIEF_DIR / f"{run_date.isoformat()}.md"
    latest_path = BRIEF_DIR / "latest.md"
    md_path.write_text(markdown, encoding="utf-8")
    latest_path.write_text(markdown, encoding="utf-8")
    # A rendered HTML copy makes the brief directly viewable locally and shareable as a
    # single self-contained file, without needing the email channel.
    html_doc = markdown_to_html(markdown)
    (BRIEF_DIR / f"{run_date.isoformat()}.html").write_text(html_doc, encoding="utf-8")
    (BRIEF_DIR / "latest.html").write_text(html_doc, encoding="utf-8")
    bib_path = None
    if bibtex:
        bib_path = BRIEF_DIR / f"{run_date.isoformat()}.bib"
        bib_path.write_text(bibtex, encoding="utf-8")
        (BRIEF_DIR / "latest.bib").write_text(bibtex, encoding="utf-8")
    return md_path, bib_path


def _is_duplicate_of_kept(path: Path, kept: list[tuple[str, Path]]) -> bool:
    """True when the dropped file is a byte-identical copy of one that does travel.

    Saying "duplicate" for a file the budget merely found too big would misreport the
    reason, and the two cases need different remedies from the reader.
    """
    import hashlib

    def digest(target: Path) -> str:
        if not target.exists():
            return ""
        handle_hash = hashlib.sha256()
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                handle_hash.update(chunk)
        return handle_hash.hexdigest()

    mine = digest(path)
    return bool(mine) and any(digest(other) == mine for _label, other in kept)


def attachment_accounting(run_date: dt.date, article_files: list[Path],
                          kept: list[tuple[str, Path]], dropped: list[tuple[str, Path]],
                          fetched_pdfs: int, mailed_pdfs: int, budget: float) -> str:
    """Plain language about which files travelled and which did not, and why.

    Lives here instead of inside `main` so the mail body and the long image are built from
    one text; a second copy would drift the moment the wording changed.

    The citation card claims every legal full text was downloaded. A file the size budget
    then dropped must not read as delivered, so the difference is spelled out - a duplicate
    and an over-budget file are different promises broken, and the reader needs to know
    which one happened to the file he wanted.
    """
    note = (f"\n\n**随信说明**：邮件附件受体积上限（约 {budget:.0f} MB）约束，"
            f"本次合法取到的 {fetched_pdfs} 份全文/补充材料中随信发出 {mailed_pdfs} 份；"
            f"未随信的文件仍保存在 `research_briefs/attachments/{run_date.isoformat()}/`，"
            "题录与获取指引中逐条列出各自的合法获取链接。")
    for label, path in dropped:
        size = path.stat().st_size / 1_000_000 if path.exists() else 0.0
        reason = ("与已发出的文件逐字节相同" if _is_duplicate_of_kept(path, kept)
                  else f"超出邮件体积上限（单封约 {budget:.0f} MB）")
        note += (f"\n  · {label or '其他附件'} · {path.name}"
                 f"（{size:.1f} MB，{reason}，未随信发出）")
    return note


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days-back", type=int, default=7, help="Search window in days")
    parser.add_argument("--dry-run", action="store_true", help="Do not send email or import to Zotero")
    parser.add_argument("--no-email", action="store_true", help="Do not send email")
    parser.add_argument("--force-send", action="store_true",
                        help="Send even when automation/mail_ledger.json records this day/kind already mailed")
    parser.add_argument("--zotero", action="store_true", help="Import selected papers to Zotero via the Zotero Web API")
    parser.add_argument("--force-weekly", action="store_true",
                        help="Build the weekly workbook even when today is not Sunday")
    args = parser.parse_args()

    config = load_config()
    tz = ZoneInfo(config.get("timezone", "UTC"))
    run_date = dt.datetime.now(tz).date()

    history = load_history()

    papers: list[dict[str, Any]] = []
    # Publisher ToC feeds first: they carry the public abstract of subscription journals
    # (PRL/PRB/Nature/Science), and dedupe() keeps the first record for a given DOI.
    feed_health: list[dict[str, str]] = []
    publisher_papers = _fetch_publisher_feeds(config, args.days_back, contact=contact_email(config), health=feed_health)
    if feed_health:
        failed = [item["feed"] for item in feed_health if item["status"] == "failed"]
        cached = [item["feed"] for item in feed_health if item["status"] in {"cache", "stale-cache"}]
        print(f"publisher feeds: {len(feed_health) - len(failed)}/{len(feed_health)} feeds reached"
              + (f"; served from cache: {len(cached)}" if cached else "")
              + (f"; unreachable: {', '.join(failed)}" if failed else ""))
    if publisher_papers:
        closed = sum(1 for p in publisher_papers if p.get("open_access") is False)
        print(f"publisher feeds contributed {len(publisher_papers)} records "
              f"({closed} from subscription journals, {len(publisher_papers) - closed} open access)")
    papers.extend(publisher_papers)
    papers.extend(fetch_arxiv(config, args.days_back))
    papers.extend(fetch_crossref(config, args.days_back))
    papers.extend(fetch_openalex(config, args.days_back))
    papers.extend(fetch_pubmed(config, args.days_back))
    papers.extend(fetch_ieee(config))
    deduped = dedupe(papers)
    tidy_abstracts(deduped)
    enrich_priority_abstracts(deduped, config)
    recent = filter_recent_publications(deduped, args.days_back, config, run_date)
    skipped_stale = len(deduped) - len(recent)
    if skipped_stale:
        print(f"skipped {skipped_stale} stale published papers outside publication-date window")
    fresh = filter_seen_papers(recent, history, run_date=run_date)
    skipped_seen = len(recent) - len(fresh)
    if skipped_seen:
        print(f"skipped {skipped_seen} previously emailed papers")
    ranked = rank_papers(fresh, config)
    relevant = [p for p in ranked if p.get("score", 0) > 0 and is_domain_match(p, config)]
    # Topological photonics/acoustics drop out of the candidate pool outright. Wave
    # physics is about how waves travel through engineered structures, not electronic
    # band topology, and it used to come back as a "nothing else today" fallback -
    # which it can no longer do.
    wave_dropped = sum(1 for p in relevant if is_wave_physics(p, config))
    if wave_dropped:
        print(f"dropped {wave_dropped} wave-physics papers (topological photonics/acoustics)")
    relevant = [p for p in relevant if not is_wave_physics(p, config)]
    ranked = (
        [p for p in relevant if p.get("score", 0) >= 4]
        or relevant
        or [p for p in ranked if p.get("score", 0) > 0]
    )
    # Journal allowlist gate: no 3rd-tier or untracked journal reaches the brief.
    before_policy = len(ranked)
    ranked = [p for p in ranked if is_allowed_venue(p, config)]
    if len(ranked) != before_policy:
        print(f"journal policy: dropped {before_policy - len(ranked)} papers outside the 1区/2区 allowlist")
    grouped_for_history, selected_for_history = group_recommendations(ranked, config)
    if not selected_for_history:
        selected_for_history = ranked[: int(config.get("max_papers", 8))]
    selected_for_history = selected_for_history[: int(config.get("max_papers", 8))]

    # Publisher feeds truncate APS abstracts; replace them with the complete public
    # abstract from the article landing page for the papers that are actually pushed.
    _complete_truncated_abstracts(selected_for_history, config, contact=contact_email(config))

    # Recommendation ranking and poster selection: relevance 45% / journal 30% / innovation 25%.
    assess_innovation(selected_for_history, config)
    poster_papers = pick_poster_papers(selected_for_history, config)
    assess_poster_digests(poster_papers, config)
    for rank, paper in enumerate(poster_papers, start=1):
        rec = paper.get("recommendation") or {}
        print(f"poster pick {rank}: {paper.get('venue', '?')} | total={rec.get('total')} "
              f"relevance={rec.get('relevance')} theory={rec.get('theory')} journal={rec.get('journal')} "
              f"innovation={rec.get('innovation')} rigor={rec.get('rigor')}")

    # Publish the poster DOIs so the local fetch automation can download the original
    # journal PDFs (the cloud runner is blocked by APS). The wishlist is a fixed name so
    # each run overwrites the previous day's; the local automation always reads the latest.
    wishlist_doc = build_poster_wishlist(poster_papers, run_date)
    try:
        wishlist_path = ROOT / "research_briefs" / "latest_poster_wishlist.json"
        wishlist_path.write_text(
            json.dumps(wishlist_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if _git_commit_push([str(wishlist_path)],
                            f"Publish poster PDF wishlist for {run_date.isoformat()}"):
            print(f"published poster wishlist ({len(wishlist_doc['papers'])} DOI(s)) for local PDF fetch")
        else:
            print("poster wishlist written locally (not published - see warning above)")
    except Exception as exc:  # noqa: BLE001 - publishing the wishlist must never break the run
        print(f"warning: could not publish poster wishlist: {exc}", file=sys.stderr)

    markdown = make_markdown(ranked, config, run_date)
    # The poster no longer prints scores, so the ranking rule is explained in the body.
    eval_note = evaluation_note(config)
    if eval_note:
        markdown = markdown.rstrip() + "\n\n---\n\n## 评估口径\n\n" + eval_note + "\n"
    bibtex = make_bibtex(ranked, int(config.get("max_papers", 8))) if config.get("generate_bibtex") else ""
    md_path, bib_path = write_outputs(markdown, bibtex, run_date)
    print(f"wrote {md_path.relative_to(ROOT)}")
    if bib_path:
        print(f"wrote {bib_path.relative_to(ROOT)}")

    # The pictures are rendered below, once the whole body is known - see that block.
    digest_images: list[Path] = []

    # Full text is hunted for the poster papers only - the papers the brief actually
    # recommends for reading. Walking every selected paper instead used to download
    # eight files for a two-paper poster; see resolve_attachment_targets(). The
    # citation card is always produced.
    article_files: list[tuple[str, Path]] = []
    cards: list[Path] = []
    attachment_note = ""
    attachment_targets = resolve_attachment_targets(poster_papers, selected_for_history)
    if config.get("paper_attachments", {}).get("enabled", True) and attachment_targets:
        try:
            from fetch_paper_attachments import (fetch_paper_attachments as _fetch_attachments,
                                                 trim_attachments as _trim_attachments)
            article_files, cards, attachment_note = _fetch_attachments(attachment_targets, run_date, config)
            # trim_attachments answers with (paper label, path) pairs; the mail needs plain paths.
            # The citation card is deliberately outside this list: it is a .txt route list, and
            # trimming would otherwise spend part of the budget on a file that is already free.
            budget = float((config.get("paper_attachments") or {}).get("mail_budget_mb", 20.0))
            kept, dropped = _trim_attachments(article_files, budget)
            article_files = kept
            # The citation card says the full text was downloaded; a file the size budget then
            # threw away must not read as delivered. Say out loud what actually travelled,
            # and why - a duplicate and an over-budget file are different promises broken.
            fetched_pdfs = sum(1 for _label, path in article_files if path.suffix.lower() == ".pdf")
            mailed_pdfs = sum(1 for _label, path in article_files if path.suffix.lower() == ".pdf")
            if fetched_pdfs > mailed_pdfs or dropped:
                attachment_note += attachment_accounting(run_date, article_files, kept, dropped,
                                                         fetched_pdfs, mailed_pdfs, budget)
        except Exception as exc:  # noqa: BLE001 - attachment hunting must never break the mail
            print(f"warning: paper attachment fetch failed: {exc}", file=sys.stderr)
            article_files, cards = [], []

    # A journal the cloud cannot reach (APS blocks the Actions runner's IP) leaves a poster
    # without its original PDF. When any poster PDF is still missing, wait for the local
    # machine to fetch and push it, then fold those files in. The cloud's own fetched files
    # (e.g. a non-APS poster's PDF) are kept untouched - we only *add* the locally-fetched
    # ones, deduplicated by resolved path.
    poster_count = len(poster_papers)
    have = len(article_files)
    if have < poster_count:
        try:
            synced_files, attachment_note = _wait_and_reuse(
                run_date, config, attachment_note,
                papers=selected_for_history, poster_papers=poster_papers)
            existing = {p.resolve() for _, p in article_files}
            for label, path in synced_files:
                if path.resolve() not in existing:
                    article_files.append((label, path))
                    existing.add(path.resolve())
        except Exception as exc:  # noqa: BLE001 - reuse is a convenience, not a guarantee
            print(f"warning: synced attachment reuse failed ({exc})", file=sys.stderr)

    # 附件这一段本身就是正文的一部分，而它此刻才定稿，所以正文要在它入列之后才算完，
    # 图片也在这之后才画。长图是整封邮件的一张图片：读者在信里读得到的段落，图里就必须
    # 看得到 —— 包括那份没随信寄出、以及为什么没寄的清单。
    if attachment_note:
        markdown = markdown.rstrip() + "\n\n---\n\n## 海报文章附件\n\n" + attachment_note
        try:
            md_path.write_text(markdown, encoding="utf-8")
        except OSError as exc:  # noqa: BLE001 - an unwritable archive must not hide the note
            print(f"warning: could not update {md_path.name} with the attachment note ({exc})",
                  file=sys.stderr)
    if config.get("digest_images", {}).get("enabled", True):
        try:
            from render_digest_images import render_digest_images as _render_digest_images
            out_dir = ROOT / str(config["digest_images"].get("dir", "research_briefs/images"))
            digest_images = _render_digest_images(poster_papers, markdown, run_date, config, out_dir)
        except Exception as exc:  # noqa: BLE001 - image failures must not break the email
            print(f"warning: digest image rendering failed: {exc}", file=sys.stderr)
    else:
        print("digest images disabled by config")

    # Inline images bloat the HTML body; clients start dropping the message past ~1.2 MB.
    # Everything stays attached, only the inline copy is dropped when it would be too big.
    inline_images: list[Path] = []
    inline_bytes = 0
    for path in digest_images:
        inline_bytes += 4 * (path.stat().st_size // 3) + 128
        if inline_bytes <= 1_200_000:
            inline_images.append(path)
        else:
            print(f"inline image skipped (too large for a single email): {path.name}")

    if not args.dry_run:
        # The archive and the weekly workbook are produced independently of email
        # delivery, so a local run with --no-email still yields the same artifacts.
        try:
            prepare_archive_metadata(selected_for_history, config)
            archive_path = archive_daily_papers(selected_for_history, config, run_date)
            print(f"archived daily papers to {archive_path.relative_to(ROOT)}")
        except Exception as exc:
            print(f"warning: archive step failed: {exc}", file=sys.stderr)

        want_weekly = config.get("weekly_excel_enabled", True) and (
            run_date.weekday() == 6 or args.force_weekly
        )
        weekly_xlsx: Path | None = None
        if want_weekly:
            try:
                weekly_xlsx = generate_weekly_excel(run_date)
            except Exception as exc:
                print(f"warning: weekly workbook step failed: {exc}", file=sys.stderr)
            if weekly_xlsx:
                print(f"wrote weekly workbook {weekly_xlsx.relative_to(ROOT)}")
                if not args.no_email:
                    start, end = week_range(run_date)
                    weekly_markdown = f"# 科研文献周报 | {compact_date(start)}-{compact_date(end)}\n\n本周文献推送汇总表见附件，归档目录：`{weekly_archive_dir(run_date)}`。"
                    weekly_subject = f"{config.get('weekly_email_subject_prefix', '每周科研文献汇总')} | {compact_date(start)}-{compact_date(end)}"
                    try:
                        weekly_provider = send_email(weekly_markdown, config, run_date, subject=weekly_subject,
                                                     attachments=[weekly_xlsx], kind="weekly", force=args.force_send)
                        if weekly_provider:
                            print(f"sent weekly Excel via {weekly_provider}: {weekly_xlsx.relative_to(ROOT)}")
                    except Exception as exc:
                        print(f"warning: weekly email failed: {exc}", file=sys.stderr)
            else:
                print("weekly workbook skipped: no archived records for this week yet")
    else:
        print("archive/weekly step skipped (dry run)")

    if not ranked:
        # Genuinely empty day: no paper survived the domain / score / venue gates, so
        # there is nothing to report. Do NOT send a placeholder mail (it would teach the
        # reader to ignore the brief), and do NOT record a send in the ledger, so a later
        # re-run or an explicit force-send can still deliver a real brief for today.
        print("no candidate papers passed the selection gates today; email skipped (empty day)")
    elif not args.dry_run and not args.no_email:
        # `markdown` already carries the attachment section - it was appended before the
        # pictures were drawn, so the long image and the mail show the same body.
        paper_files = [path for _label, path in article_files]
        provider = send_email(markdown, config, run_date,
                              attachments=[*paper_files, *cards, *digest_images],
                              inline_images=inline_images, kind="daily", force=args.force_send)
        if provider is None:
            print("email sending skipped: the daily mail for today went out already")
        else:
            # Say what kind of file went out. Counting the citation card as a paper is how
            # this line once read "attached 1 paper files" on a run where every pass had
            # fetched 0.
            attached_pdfs = sum(1 for path in paper_files if path.suffix.lower() == ".pdf")
            if paper_files or cards:
                print(f"attached {attached_pdfs} paper file(s)"
                      + (f" and {len(cards)} citation card(s)" if cards else "") + " to the mail")
            update_history(history, selected_for_history, run_date)
            print(f"sent email via {provider}")
            print(f"updated {HISTORY_PATH.relative_to(ROOT)} with {len(selected_for_history)} emailed papers")
    else:
        print("email sending skipped")
    if args.dry_run:
        # A dry run is trusted to leave the day's published artefacts alone. It does not:
        # the markdown, the bib and the poster attachments are written for real, so the
        # same date that was already mailed can be silently replaced by a second draft.
        print("note: --dry-run still writes research_briefs/<date>.md, <date>.bib and the "
              "poster attachments; only the email, the archive and the Zotero import were "
              "skipped. Point the run at a separate checkout to compare drafts safely.")

    if args.zotero and not args.dry_run:
        imported, skipped = import_to_zotero(ranked, int(config.get("max_papers", 8)), run_date)
        print(f"zotero import complete: imported {imported}, skipped existing {skipped}")
    elif args.zotero:
        print("zotero import skipped")
    return 0


def build_poster_wishlist(poster_papers: list[dict[str, Any]], run_date: dt.date) -> dict[str, Any]:
    """The JSON the cloud publishes for the local PDF-fetch automation.

    Stamps the cloud's authoritative run date so the local run targets the exact
    folder/date the cloud will poll. The local machine clock can drift (observed +2
    days vs the runner); trusting it would write the poster PDFs into the wrong dated
    folder and the cloud would never find them. Papers without a DOI cannot be fetched
    by DOI, so they are dropped here rather than handed to a fetch that would fail.
    """
    papers = [
        {"doi": (p.get("doi") or "").strip(),
         "title": (p.get("title") or "").strip(),
         "venue": (p.get("venue") or "").strip()}
        for p in poster_papers if p.get("doi")
    ]
    return {"run_date": run_date.isoformat(), "papers": papers}
if __name__ == "__main__":
    raise SystemExit(main())
