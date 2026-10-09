#!/usr/bin/env python3
"""Fetch the poster papers' full text and supplementary material.

Only *legal* sources are used, so the brief can never hand the reader a pirated copy:

1. Unpaywall - open-access publisher PDF or landing page for the DOI.
2. arXiv - the author's own public preprint.
3. The publisher's landing page, scraped only for openly served supplementary files
   (Springer/Nature MediaObjects, Elsevier mmc files, ACS supplemental pages, ...).

When nothing can be fetched (the usual case for pay-walled APS / Nature / Science /
ACS articles published in the last days), each paper still gets a citation card with
the permanent DOI link, the verified open-access and preprint links, and the legal
route to the full text. That is reported honestly in the mail instead of faking a file.

Dependency-free: standard library only.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import http.client
from pathlib import Path
from typing import Any

# arXiv throttles hard and, from some networks, stops answering at all: four unanswered
# queries cost 3.5 minutes per paper. Once the API has failed this way the rest of the run
# skips it instead of re-discovering the outage paper by paper.
ARXIV_UNAVAILABLE = False
# A single slow arXiv response used to flip ARXIV_UNAVAILABLE and retire the whole
# channel for the rest of the run. Around 2026-10-07 the Actions runner started
# hitting socket timeouts on export.arxiv.org, so one stalled query killed the
# only cloud route that could deliver a published article's preprint - every
# brief since then shipped zero PDFs. We now tolerate a few consecutive failures
# (reset on any success) and only retire the channel once it has clearly given
# up, so an intermittent stall no longer costs every paper its PDF.
ARXIV_FAIL_STREAK = 0
ARXIV_FAIL_THRESHOLD = 2

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36 research-brief/2.0")
ATOM = "{http://www.w3.org/2005/Atom}"
UNPAYWALL_MAILBOX_FOR = "research.brief@example.org"

# APS serves the publisher's own PDF from /<journal>/pdf/<DOI>. Checked 2026-10-02:
# five real APS DOIs answered 200 application/pdf with byte counts identical to the
# copies the desktop browser route produced, a made-up DOI answered 404, and
# journals.aps.org/robots.txt disallows only /search, /account and /login (the
# Cloudflare-managed prefix allows `User-agent: *` and names only the AI crawlers),
# so this is a legal route rather than a mirror.
APS_JOURNALS = ("prb", "prresearch", "prl", "prx", "prmaterial", "prapplied",
                "prxenergy", "prsymmetry", "prevc")


def _open(url: str, *, timeout: int = 30, data: bytes | None = None, referer: str | None = None):
    headers = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    return urllib.request.urlopen(request, timeout=timeout)


def _slug(text: str, limit: int = 48) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", (text or "").strip()).strip("_").lower()
    return (slug[:limit].rstrip("_") or "paper")


_OA_CACHE: dict[str, dict[str, Any]] = {}


def unpaywall_locations(doi: str) -> dict[str, Any]:
    """Open-access locations reported by Unpaywall (publisher PDF, repository, ...)."""
    if doi in _OA_CACHE:
        return _OA_CACHE[doi]
    result: dict[str, Any] = {"available": False, "is_oa": False, "pdf": "", "landing": "", "source": ""}
    if not doi:
        return result
    url = f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi, safe='')}?email={UNPAYWALL_MAILBOX_FOR}"
    for attempt in range(2):
        try:
            with _open(url, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            break
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt == 1:
                print(f"warning: unpaywall lookup failed for {doi}: {exc}")
                return result
            time.sleep(2)
    result["available"] = True
    result["is_oa"] = bool(payload.get("is_oa"))
    best = payload.get("best_oa_location") or {}
    for location in (best, *(payload.get("oa_locations") or [])):
        if not isinstance(location, dict):
            continue
        if location.get("url_for_pdf") and not result["pdf"]:
            result["pdf"] = location["url_for_pdf"]
            result["landing"] = location.get("url", "")
            result["source"] = location.get("host_type", "") or "open access"
            break
        if location.get("url") and not result["landing"] and result["is_oa"]:
            result["landing"] = location["url"]
            result["source"] = location.get("host_type", "") or "open access"
    if result["is_oa"] and not result["pdf"] and result["landing"]:
        result["pdf"] = result["landing"]
    _OA_CACHE[doi] = result
    return result


def _clean_title(title: str) -> str:
    """Strip the LaTeX a machine-readable feed keeps in physics titles (${\\mathcal{T}}$ ...)."""
    text = re.sub(r"\$\s*\\(?:mathr?m|mathrm|text|mathcal|mathbf|mathsf|bm|it|bf|rm)\s*\{([^{}]*)\}\s*\$",
                  r"\1", title)
    text = re.sub(r"\$[^$]*\$", " ", text)
    text = re.sub(r"\\[a-zA-Z]+\s*(\{[^}]*\})?", " ", text)
    text = re.sub(r"[{}]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _arxiv_query_with_retries(query: str) -> "ET.Element | None":
    """One arXiv query, retried for 429 backoff; bumps the fail streak on any other error.

    A transient timeout no longer retires the channel on the spot - only a run of
    ARXIV_FAIL_THRESHOLD consecutive failures does. Bounds the blast radius when
    arXiv is genuinely unreachable while still letting an intermittent stall recover.
    """
    global ARXIV_UNAVAILABLE, ARXIV_FAIL_STREAK
    for attempt in range(3):
        try:
            url = "http://export.arxiv.org/api/query?" + urllib.parse.urlencode(
                {"search_query": query, "max_results": 5})
            with _open(url, timeout=30) as resp:
                return ET.fromstring(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            ARXIV_FAIL_STREAK += 1
            print(f"warning: arXiv lookup failed ({query[:40]}): {exc}")
            break
        except (urllib.error.URLError, TimeoutError, ET.ParseError) as exc:
            ARXIV_FAIL_STREAK += 1
            print(f"warning: arXiv lookup failed ({query[:40]}): {exc}")
            break
    if ARXIV_FAIL_STREAK >= ARXIV_FAIL_THRESHOLD:
        ARXIV_UNAVAILABLE = True
        print(f"warning: arXiv channel retired for the rest of this run "
              f"(>= {ARXIV_FAIL_THRESHOLD} consecutive failures)")
    return None


def arxiv_pdf(title: str, doi: str) -> dict[str, str]:
    """Author's public preprint on arXiv, matched by DOI or by a LaTeX-cleaned title."""
    global ARXIV_FAIL_STREAK
    result: dict[str, str] = {"url": "", "verified": ""}
    if ARXIV_UNAVAILABLE:
        result["skipped"] = "本次 arXiv 接口不可达，已整体跳过该通道"
        return result
    queries: list[str] = []
    if doi:
        queries.append(f'all:"{doi}"')
    clean = _clean_title(title or "")
    for probe in (clean[:180], clean[:110], clean[:70]):
        if probe and probe not in queries:
            queries.append(f'ti:"{probe}"')
    for query in queries:
        root = _arxiv_query_with_retries(query)
        if root is None:
            continue
        for entry in root.findall(ATOM + "entry"):
            entry_id = (entry.findtext(ATOM + "id") or "").strip()
            if not entry_id:
                continue
            entry_doi = (entry.findtext("{http://arxiv.org/schemas/atom}doi") or "").strip().lower()
            if doi and entry_doi == doi.lower():
                result = {"url": entry_id.replace("/abs/", "/pdf/"), "verified": "arXiv DOI 精确匹配"}
                ARXIV_FAIL_STREAK = 0
                return result
            entry_title = re.sub(r"\s+", " ", (entry.findtext(ATOM + "title") or "")).strip().lower()
            probe = re.sub(r"[^a-z0-9]", "", (title or "").lower())[:60]
            if probe and probe in re.sub(r"[^a-z0-9]", "", entry_title):
                pdf = entry_id.replace("/abs/", "/pdf/")
                result = {"url": pdf, "verified": "arXiv 题名匹配"}
                ARXIV_FAIL_STREAK = 0
                return result
    return result


def openalex_oa(doi: str) -> list[dict[str, str]]:
    """OpenAlex registry of open copies: repositories, author self-archives, publisher OA."""
    found: list[dict[str, str]] = []
    if not doi:
        return found
    url = ("https://api.openalex.org/works/doi:" + urllib.parse.quote(doi, safe="")
           + f"?mailto={UNPAYWALL_MAILBOX_FOR}")
    try:
        with _open(url, timeout=40) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, OSError) as exc:
        print(f"warning: OpenAlex lookup failed for {doi}: {exc}")
        return found
    for host_type in ("best_oa_location",):
        location = payload.get(host_type)
        if isinstance(location, dict) and location.get("pdf_url"):
            found.append({"url": location["pdf_url"], "source": "OpenAlex 最佳开放副本",
                          "landing": location.get("landing_page_url", "")})
    for location in payload.get("locations") or []:
        if not isinstance(location, dict):
            continue
        if location.get("pdf_url"):
            found.append({"url": location["pdf_url"], "source": "OpenAlex 登记开放副本",
                          "landing": location.get("landing_page_url", "")})
    return found


def semantic_scholar_oa(doi: str) -> dict[str, str]:
    """Semantic Scholar's own open-access PDF record (often a repository copy)."""
    result: dict[str, str] = {"url": "", "source": ""}
    if not doi:
        return result
    url = ("https://api.semanticscholar.org/graph/v1/paper/DOI:"
           + urllib.parse.quote(doi, safe="") + "?fields=openAccessPdf,externalIds")
    try:
        with _open(url, timeout=40) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, OSError):
        return result
    pdf = (payload.get("openAccessPdf") or {}) if isinstance(payload.get("openAccessPdf"), dict) else {}
    if pdf.get("url"):
        result = {"url": pdf["url"], "source": "Semantic Scholar 开放全文"}
    return result


def doaj_article(doi: str) -> list[dict[str, str]]:
    """DOAJ entry for fully open journals, PDF link first."""
    found: list[dict[str, str]] = []
    if not doi:
        return found
    try:
        with _open(f"https://doaj.org/api/search/articles/{urllib.parse.quote(doi, safe='')}",
                   timeout=40) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, OSError):
        return found
    for entry in payload.get("results") or []:
        bib = (entry.get("bibjson") or {})
        for link in bib.get("link") or []:
            if (link.get("content_type") or "").lower() == "fulltext":
                found.append({"url": link["url"], "source": "DOAJ 开放全文"})
    return found


def europepmc_oa(doi: str) -> list[dict[str, str]]:
    """Europe PMC open copy (mostly life science, harmless for the others)."""
    found: list[dict[str, str]] = []
    if not doi:
        return found
    query = urllib.parse.quote(f'DOI:"{doi}"', safe="")
    try:
        with _open(f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query={query}"
                   f"&format=json&resultType=core", timeout=40) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, OSError):
        return found
    for hit in payload.get("resultList", {}).get("result") or []:
        for link in hit.get("fullTextUrlList", {}).get("fullTextUrl") or []:
            if "pdf" in (link.get("documentStyle") or "").lower() and link.get("url"):
                found.append({"url": link["url"], "source": "Europe PMC 开放全文"})
    return found


def abstract_if_empty(doi: str, title: str) -> dict[str, str]:
    """Crossref record: it sometimes carries an open PDF link in the `link` field."""
    result: dict[str, str] = {"url": "", "source": ""}
    if not doi:
        return result
    try:
        with _open(f"https://api.crossref.org/works/{urllib.parse.quote(doi, safe='')}",
                   timeout=40) as resp:
            payload = (json.loads(resp.read().decode("utf-8", "replace")).get("message") or {})
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, OSError):
        return result
    for link in payload.get("link") or []:
        if "pdf" in (link.get("content-type") or "").lower() or "pdf" in (link.get("contentType") or "").lower():
            result = {"url": link.get("URL", ""), "source": "Crossref 登记 PDF"}
            return result
    return result


def _landing_page(doi: str) -> tuple[str, str]:
    """Open the DOI landing page (Nature articles get their direct article URL)."""
    for candidate in (f"https://doi.org/{doi}",
                      f"https://www.nature.com/articles/{doi.split('/')[-1].replace('.', '-')}"
                      if doi.startswith("10.1038/") else ""):
        if not candidate:
            continue
        try:
            with _open(candidate, timeout=30) as resp:
                return resp.geturl(), resp.read().decode("utf-8", "replace")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                http.client.HTTPException, OSError):
            continue
    return "", ""


_SUPPLEMENT_FILE_RE = re.compile(
    r"\.(?:pdf|zip|rar|7z|tar|gz|xlsx?|docx?|pptx?|csv|ods|png|jpe?g|tiff?)(?:\?|#|$)"
    r"|/doi/supplemental/",
    re.I,
)


def landing_supplements(doi: str) -> tuple[str, list[str]]:
    """Fetch the publisher landing page and collect openly served supplementary links."""
    candidates: list[str] = []
    landing, page = _landing_cached(doi)
    if not page:
        print(f"warning: landing page unreachable for {doi}")
        return "", []
    for raw in re.findall(r'href="([^"]+)"', page):
        low = raw.lower()
        if any(token in low for token in ("supplement", "supp", "mediaobjects", "mmc", "esm", "attachment")):
            # The keyword above also fires on `support.nature.com/support/home`, since
            # "supp" is a substring of "support". A supplement has to be a file, so the
            # keyword only narrows the scan down; without this check a support page was
            # offered as a supplementary file and then reported as "not a file".
            if not _SUPPLEMENT_FILE_RE.search(low):
                continue
            if raw.startswith("#") or raw.endswith("#supplemental"):
                continue
            if raw.startswith("http://"):
                raw = "https://" + raw[7:]
            if raw.startswith("//"):
                raw = "https:" + raw
            elif raw.startswith("/"):
                raw = f"https://{urllib.parse.urlparse(landing).netloc}{raw}"
            if "pubs.acs.org/doi/supplemental" in raw or "/doi/supplemental/" in raw:
                raw = raw.split("?")[0]
            if raw not in candidates:
                candidates.append(raw)
    return landing, candidates


_LANDING_CACHE: dict[str, tuple[str, str]] = {}


def _landing_cached(doi: str) -> tuple[str, str]:
    """Landing pages are HTML-heavy, so one request serves every route that needs it."""
    if doi not in _LANDING_CACHE:
        _LANDING_CACHE[doi] = _landing_page(doi)
    return _LANDING_CACHE[doi]


def landing_pdf_links(doi: str) -> tuple[str, list[str]]:
    """Publisher landing page PDF links - the shortest route for openly published work."""
    landing, page = _landing_cached(doi)
    if not page:
        return "", []
    if "cookies_not_supported" in landing or re.search(r"(?i)captcha|just a moment", page):
        # A page that answered but holds nothing is a silently degraded one: the request
        # did not fail, so nothing else in the run complains, and the only symptom was a
        # citation card that had one route and said the paper could not be fetched.
        print(f"warning: landing page for {doi} served no PDF links "
              f"({len(page)} bytes, cookie/anti-bot notice: {'cookies_not_supported' in landing})")
    found: list[str] = []
    for raw in re.findall(r'href="([^"]+\.pdf[^"]*)"', page, re.I):
        if raw.startswith("http://"):
            raw = "https://" + raw[7:]
        elif raw.startswith("//"):
            raw = "https:" + raw
        elif raw.startswith("/"):
            raw = f"https://{urllib.parse.urlparse(landing).netloc}{raw}"
        if raw not in found and _is_article_pdf(raw):
            found.append(raw)
    return landing, found


def _save(url: str, dest: Path, max_mb: int) -> bool:
    """Stream a file to disk, tolerating a truncated body so a partial PDF is still usable."""
    url = url.replace("http://", "https://", 1)
    chunks: list[bytes] = []
    total = 0
    ctype = ""
    try:
        with _open(url, timeout=90) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            # The server is about to tell us the real size: honour it instead of streaming
            # past the cap. A Nature supplementary PDF declares 21 MB against a 15 MB cap,
            # and cutting it at the cap yields a file that opens to a wall of cut-off pages -
            # the same kind of truncated body this pipeline once shipped and later deleted.
            declared = (resp.headers.get("Content-Length") or "").strip()
            if declared.isdigit() and int(declared) > max_mb * 1_000_000:
                print(f"   download skipped ({dest.name} declares {int(declared) // 1_000_000} MB, "
                      f"over the {max_mb} MB cap - skipped whole rather than truncated)")
                return False
            while True:
                try:
                    chunk = resp.read(262144)
                except (http.client.HTTPException, OSError):
                    break
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > max_mb * 1_000_000:
                    break
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            http.client.HTTPException, OSError) as exc:
        print(f"   download skipped ({type(exc).__name__})")
        return False
    data = b"".join(chunks)
    magic = data[:4]
    looks_right = magic in (b"%PDF", b"PK") or (magic[:2] == b"%P" and b"DF" in data[:8])
    if ctype and not ctype.startswith(("application/pdf", "application/zip",
                                       "application/x-zip-compressed", "application/octet-stream")) and not looks_right:
        print(f"   download skipped (content-type {ctype or 'unknown'})")
        return False
    if len(data) < 4096:
        print("   download skipped (too small to be a real file)")
        return False
    dest.write_bytes(data)
    print(f"   saved {dest.name} ({len(data) // 1024} KB) from {urllib.parse.urlparse(url).netloc}")
    return True


def _citation_card(paper: dict[str, Any], index: int, oa: dict[str, Any],
                   arxiv: dict[str, str], landing: str, supplements: list[str],
                   downloaded: list[str], note: str, routes_text: list[str]) -> str:
    # Three states, not two. `is_oa` is False both when Unpaywall positively found no
    # open copy and when the lookup failed - a DOI published hours ago is routinely not
    # indexed yet. Collapsing those into one `False` made the card state "否（订阅期刊，
    # 无开放全文）" about a fully open-access journal, and the mail body said the opposite
    # on the same line a few pixels above ("开放获取（可下载 PDF）"). Only a lookup that
    # actually answered may be quoted as a finding.
    if oa.get("is_oa"):
        oa_line = "是（Unpaywall 登记有开放副本）"
    elif oa.get("available"):
        oa_line = "否（Unpaywall 查询过，未登记任何开放副本）"
    else:
        oa_line = "未知（Unpaywall 尚未索引该 DOI，不代表无开放副本；以出版商页面为准）"
    lines = [
        f"[{index}] {paper.get('title') or '题名未提供'}",
        f"期刊/来源：{paper.get('venue') or '未知'}   日期：{paper.get('published') or '未知'}",
        f"作者：{(paper.get('authors') or '未提供')[:180]}",
        f"DOI：{paper.get('doi') or '未提供'}",
        f"DOI 永久链接：https://doi.org/{paper.get('doi') or ''}",
        f"出版商页面：{landing or '（未抓取到可公开访问的落地页）'}",
        f"开放获取：{oa_line}",
    ]
    if oa.get("landing"):
        lines.append(f"开放获取入口：{oa['landing']}")
    if arxiv.get("url"):
        lines.append(f"arXiv 预印本：{arxiv['url']}（{arxiv.get('verified', '')}）")
    lines.append("")
    lines.append("尝试过的合法获取通道：")
    lines.extend(routes_text[:10] if routes_text else ["  · 无（DOI 缺失或全部通道失败）"])
    if supplements:
        lines.append("补充材料候选链接：" + "；".join(supplements[:4]))
    lines.append("")
    lines.append("已下载的全文/补充材料：" + ("；".join(downloaded) if downloaded else "无"))
    lines.append(f"说明：{note or '本文件仅含题录与合法获取链接。'}")
    if not downloaded:
        doi_url = f"https://doi.org/{paper.get('doi') or ''}"
        # Same three states as the open-access line above: recommending a paid subscription
        # route to a paper Unpaywall says is open is advice that sends the reader the wrong way.
        if oa.get("is_oa"):
            lines.append("获取全文的合法途径：该文为开放获取，直接打开 "
                         f"{doi_url} / 出版商页面即可免费阅读与保存全文（PDF 需浏览器下载）。")
        elif oa.get("available"):
            lines.append("获取全文的合法途径：该文无开放副本，通过所在机构图书馆 / 校园网"
                         f"（已购该刊订阅）打开 {doi_url} 即可下载出版商版全文 PDF 与补充材料。")
        else:
            lines.append("获取全文的合法途径：先打开 "
                         f"{doi_url} 确认是否有开放副本（Unpaywall 尚未索引不代表没有）；"
                         "若无，再走所在机构图书馆 / 校园网已购该刊订阅的通道。")
    else:
        lines.append("开放获取入口：https://doi.org/" + (paper.get("doi") or ""))
    lines.append("")
    return "\n".join(lines)


# Route tiers, best first. The split that matters is version-of-record versus preprint:
# a published article carries the publisher's own file (the version of record, "VOR"),
# and the arXiv preprint is only the fallback for a paper that has nothing else.
_PRIO_UNPAYWALL = 1      # publisher-hosted open-access PDF found through Unpaywall
_PRIO_PUBLISHER_OA = 2   # publisher's own open-access direct PDF link
_PRIO_PUBLISHER_VOR = 3  # publisher's own PDF endpoint (APS /journal/pdf/DOI) and
                         # landing-page PDF links - the version of record itself
_PRIO_PREPRINT = 4       # arXiv preprint
_PRIO_OA_COPY = 5        # OpenAlex / Semantic Scholar / DOAJ mirror
_PRIO_REPOSITORY = 6     # Europe PMC and similar repositories
_PRIO_SUPPLEMENT = 7     # openly served supplementary material
_VOR_MAX_PRIO = _PRIO_PUBLISHER_VOR


def _route_rank(route: tuple[int, str, str]) -> tuple[int, int, str]:
    """Sort key that puts every publisher copy ahead of every preprint.

    Tiers alone would rely on the order `add()` happens to be called in: an APS paper
    used to be offered the preprint at tier 2 and its own PDF at tier 4, so the
    preprint silently won. The second key makes the rule explicit - anything the
    publisher hosts (tiers 1-3) outranks anything a preprint server hosts - and it
    survives a future refactor that reorders the calls below.
    """
    priority, _url, source = route
    return (priority, 0 if priority <= _VOR_MAX_PRIO else 1, source)


def legal_routes(paper: dict[str, Any]) -> list[tuple[int, str, str]]:
    """Every legal place this paper's full text could live in, best first.

    Returns (priority, url, source) triples. Priorities: 1 Unpaywall OA PDF, 2 the
    publisher's open-access direct link, 3 the publisher's own PDF endpoint and
    landing-page full text (all three are the published version of record), 4 arXiv
    preprints, 5 open-access mirrors, 6 repositories, 7 openly served supplements.
    Subscription PDFs are never on this list.
    """
    doi = paper.get("doi", "") or ""
    title = paper.get("title", "") or ""
    routes: list[tuple[int, str, str]] = []

    def add(priority: int, url: str, source: str) -> None:
        if url and all(url != existing[1] for existing in routes):
            routes.append((priority, url, source))

    oa = unpaywall_locations(doi)
    if oa.get("pdf"):
        add(_PRIO_UNPAYWALL, oa["pdf"], "Unpaywall 开放获取")
    if oa.get("is_oa"):
        # The publisher's own open-access PDF route: the authoritative copy of an OA paper.
        for url in publisher_pdf_urls(doi):
            add(_PRIO_PUBLISHER_OA, url, "出版商开放获取 PDF 直链")
    # The landing page is reached for every paper, not only for OA ones: it is how an
    # open-access journal's own PDF endpoint gets discovered when Unpaywall has not
    # indexed the article yet (they routinely lag a day or more for very fresh DOIs,
    # and that gap used to leave the citation card asserting the paper was paywalled
    # when the publisher was serving it openly the whole time).
    landing_links = [url for url in landing_pdf_links(doi)[1][:4] if _is_article_pdf(url)][:3]
    for url in landing_links:
        add(_PRIO_PUBLISHER_VOR, url, "出版商落地页 PDF 链接")
    # The publisher's own PDF endpoint comes before the preprint: for an APS article
    # these are PRL / PRX / PRB / PRD papers, and the reader wants the published file.
    # Candidates that answer HTML for a subscription DOI are rejected by `_save`, and a
    # rejected candidate does not occupy the slot, so the preprint still fills in.
    for url in aps_pdf_candidates(doi):
        add(_PRIO_PUBLISHER_VOR, url, "出版商官方 PDF 端点（robots 允许）")
    arxiv = arxiv_pdf(title, doi)
    if arxiv.get("url") and not (paper.get("venue") or "").lower().startswith("arxiv"):
        add(_PRIO_PREPRINT, arxiv["url"], "arXiv 公开预印本")
    for item in openalex_oa(doi):
        add(_PRIO_OA_COPY, item.get("url", ""), item.get("source") or "OpenAlex 开放副本")
    for item in (semantic_scholar_oa(doi), abstract_if_empty(doi, title)):
        if item.get("url"):
            add(_PRIO_OA_COPY, item["url"], item.get("source") or "开放副本登记")
    for item in doaj_article(doi):
        add(_PRIO_OA_COPY, item.get("url", ""), "DOAJ 开放全文")
    for item in europepmc_oa(doi):
        add(_PRIO_REPOSITORY, item.get("url", ""), "Europe PMC 开放全文")
    _, supplements = landing_supplements(doi)
    for url in supplements[:3]:
        add(_PRIO_SUPPLEMENT, url, "出版商公开补充材料（落地页扫描）")
    for url in supplement_candidates(doi):
        add(_PRIO_SUPPLEMENT, url, "出版商公开补充材料（直链）")
    routes.sort(key=_route_rank)
    return routes


def _is_article_pdf(url: str) -> bool:
    """True for a link that can only be the article itself.

    A landing page puts every PDF-looking href on the page, and most of them are not the
    paper:APS lists `_reference.pdf`, Nature lists `..._MOESM1_ESM.pdf` (supplementary,
    30+ MB) and the reference list. Asking for those costs a request that `_save` then
    rejects for being HTML, and the rejection is logged as if the publisher had refused -
    which is how a run once ended up claiming the only route it had tried was
    "supplementary material", when the article's own PDF link was sitting three lines up.
    """
    path = urllib.parse.urlparse(url).path.lower()
    if not path.endswith(".pdf"):
        return False
    if not all(token not in path for token in
               ("_reference", "/references/", "figure", "fig-",
                "video", "captions", "poster", "dataset")):
        return False
    # The redesigned nature.com no longer serves the article at this address. Measured
    # 2026-10-04 on a fresh Nature Communications paper, offering the landing page's
    # cookies to the PDF endpoint first: still `200` with `text/html`, so the file never
    # arrives and the reader is told the paper is paywalled when it is open access. The
    # only MediaObjects files that do arrive are the supplements; see the warning inside
    # `landing_pdf_links` for the degraded page this leaves behind.
    return not re.search(r"(?i)/articles/[^/]+\.pdf$", path)


def publisher_pdf_urls(doi: str) -> list[str]:
    """Well-known open-access PDF routes of the big publishers.

    Only ever used for a paper Unpaywall marks as open access: the same URL points at a
    pay-wall for a subscription article, and those are never requested.
    """
    if not doi:
        return []
    found: list[str] = []
    tail = doi.split("/")[-1] if "/" in doi else ""
    if doi.startswith("10.1103/"):
        # APS serves open-access PDFs straight from link.aps.org, which has nobot wall of
        # its own; for a subscription DOI the very same URL answers with an HTML shop page,
        # and `_save` would reject it anyway.
        found.append(f"https://link.aps.org/pdf/{doi}")
    # The Nature family used to be served the same way, with
    # `nature.com/articles/<suffix>.pdf` appended here. Measured 2026-10-04 on a fresh
    # Nature Communications article: the URL answers 200 with `Content-Type: text/html`,
    # because the redesigned site no longer serves a file at that path - the landing page
    # carries only the supplementary PDFs. The candidate was therefore removed rather than
    # left in: it could only ever be rejected, and its presence is what made the run look
    # like it had tried the publisher and failed. Nature's own routes now come from
    # `landing_pdf_links`, which reads them off the page.
    unique: list[str] = []
    for url in found:
        if url not in unique:
            unique.append(url)
    return unique


def aps_pdf_candidates(doi: str) -> list[str]:
    """The publisher's own PDF endpoints for an APS article, one per journal shortname.

    The APS DOI suffix does not carry the journal, so the shortnames are tried in order
    and the first one that answers a real PDF wins. Candidates that answer HTML cost a
    few tens of kilobytes and are rejected by `_save`, which is why the caller can
    simply walk the whole list.
    """
    if not doi.startswith("10.1103/"):
        return []
    return [f"https://journals.aps.org/{journal}/pdf/{doi}" for journal in APS_JOURNALS]


def supplement_candidates(doi: str) -> list[str]:
    """The publishers' own supplementary-material endpoints for the two big physics houses.

    APS answers `link.aps.org/supplemental/<DOI>` with a redirect to the abstract page
    for a subscription article, and Springer answers the `*.mendeley` media object with
    403 without a session, so these are attempts that must fail honestly rather than
    hints that could be sold to the reader as a file.
    """
    if not doi:
        return []
    tail = doi.split("/")[-1] if "/" in doi else ""
    found: list[str] = []
    if doi.startswith("10.1103/"):
        found.append(f"https://link.aps.org/supplemental/{doi}")
    if doi.startswith("10.1038/") and tail:
        found.append(f"https://www.nature.com/articles/{tail.replace('.', '-')}/supplementary")
    return found


def extract_archive(path: Path, out_dir: Path, max_children: int = 8,
                    max_mb: float = 6.0) -> list[Path]:
    """Unpack a supplementary zip next to itself and return its files, biggest first.

    Publishers ship supplementary figures as one archive, and a reader who gets only the
    zip has to open it by hand. Only zip is unpacked - no rar/7z in the standard library
    - and every child is size-capped so one figure pack cannot swallow the mail.
    """
    import zipfile

    if path.stat().st_size < 4096 or path.read_bytes()[:2] != b"PK":
        return []                     # a PDF is not an archive; don't pretend otherwise
    children: list[Path] = []
    try:
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if not name.endswith("/")]
            for name in sorted(names, key=lambda n: archive.getinfo(n).file_size, reverse=True)[:max_children]:
                info = archive.getinfo(name)
                if info.file_size > max_mb * 1_000_000:
                    continue
                dest = out_dir / path.stem / name.split("/")[-1]
                if dest.exists():
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as src, dest.open("wb") as dst:
                    remaining = info.file_size
                    while remaining > 0:
                        chunk = src.read(min(262144, remaining))
                        if not chunk:
                            break
                        dst.write(chunk)
                        remaining -= len(chunk)
                if dest.stat().st_size >= 4096:
                    children.append(dest)
    except Exception as exc:  # noqa: BLE001 - an unpack failure must not kill the fetch
        print(f"   archive not unpacked ({type(exc).__name__}: {exc})")
    return children


def _extension_for(url: str) -> str:
    """arXiv and APS serve PDFs from extension-less paths; name the saved file properly."""
    path = urllib.parse.urlparse(url).path.lower()
    if path.endswith((".pdf", ".zip", ".docx", ".xlsx", ".rar")):
        return path.rsplit(".", 1)[-1]
    if path.endswith("/pdf") or "/pdf/" in path or re.search(r"/10\.\d{4,}/[^/]+$", path):
        return "pdf"
    return ""


def routes_ok(got: list[str]) -> bool:
    """True once a real file is on disk - then the browser pass can be skipped."""
    return bool(got)


def fetch_paper_attachments(papers: list[dict[str, Any]], run_date: dt.date, config: dict[str, Any]) -> tuple[list[tuple[str, Path]], list[Path], str]:
    """Download what can legally be downloaded; always return a citation manifest.

    Returns (paper files, citation cards, card text). The card is a .txt list of routes,
    not a paper, and stays out of the paper list so the mail's size budget and the
    "attached N papers" line count what was actually fetched.
    """
    options = config.get("paper_attachments") or {}
    if not options.get("enabled", True) or not papers:
        return [], [], ""
    out_dir = Path(str(options.get("dir", "research_briefs/attachments"))) / run_date.isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    max_mb = float(options.get("max_file_mb", 15))
    max_files = int(options.get("max_files_per_paper", 4))

    max_papers = int(options.get("max_papers", 8))
    browser_papers = int(options.get("browser_max_papers", 3))
    browser_wait = int(options.get("browser_wait_seconds", 60))
    targets = papers[:max_papers]
    browser_allowed = not os.environ.get("GITHUB_ACTIONS")
    browser_used = 0
    profile_path = Path(str(options.get("browser_profile", ".browser_storage_state.json")))

    # (paper label, path): the label lets the mail budget talk about *this* article when
    # it trims, instead of dropping files by name and losing track of whose they were.
    saved: list[tuple[str, Path]] = []
    manifest_lines: list[str] = [f"文献全文与补充材料附件清单 | {run_date.isoformat()}", "=" * 68,
                                  f"本次遍历关注期刊范围内的 {len(targets)} 篇入选文献，逐篇尝试合法获取通道。", ""]
    for index, paper in enumerate(targets, start=1):
        doi = paper.get("doi", "")
        label = f"{index}_{_slug(paper.get('venue') or 'paper')}_{_slug(paper.get('title') or '')}"
        got: list[str] = []
        produced: set[str] = set()      # names already written for this paper: no second pass
        taken: set[str] = set()         # kinds already carried by this paper: no second copy
        provenance: dict[str, dict[str, str]] = {}
        routes_text: list[str] = []
        note = ""
        oa = unpaywall_locations(doi)
        # The note used to be one sentence about subscription copyright, printed whether or
        # not the paper was open access - so an open-access Nature paper arrived with a card
        # saying "开放获取：是" two lines above and "受订阅版权限制" below it.
        if oa.get("is_oa"):
            note = ("本文件仅含题录与合法获取链接；该文为开放获取，出版商未向本脚本直连提供"
                    "全文 PDF（已实测：nature.com 全文地址与 Springer 静态路径即便带上落地页 "
                    "cookie 也返回 text/html），请在浏览器中打开出版商页面自行保存全文。")
        elif oa.get("available"):
            note = "本文件仅含题录与合法获取链接；Unpaywall 未登记该文有开放副本，全文通常需机构订阅。"
        else:
            note = ("本文件仅含题录与合法获取链接；Unpaywall 尚未索引该 DOI（新上线文献常见），"
                    "不代表无开放副本，能否免费获取请以出版商页面为准。")
        try:
            routes = legal_routes(paper)
            if not routes:
                routes_text.append("  · 未找到任何合法开放副本（无 OA、无预印本、无公开补充材料）"
                                   + ("；arXiv 通道本次未查询" if ARXIV_UNAVAILABLE else ""))
            for priority, url, source in routes:
                if len(got) >= max_files:
                    break
                tail_name = _slug(urllib.parse.urlparse(url).path.rsplit("/", 1)[-1], 40)
                extension = _extension_for(url)
                stem = f"{label}_{tail_name}" if tail_name and not tail_name.endswith(("_", "pdf")) else ""
                target = out_dir / (f"{stem}.{extension}" if stem and extension
                                    else f"{label}_文件{len(got) + 1}.pdf")
                if target.name in produced:
                    routes_text.append(f"  · [{source}] {url} → 与上一条通道同一文件，未重复下载")
                    continue
                ok = _save(url, target, max_mb)
                if ok and not _file_is_about(target, url, doi, paper.get("title", "")):
                    # A landing page links to anything, and the scan that collects
                    # "supplementary" links treats each of them as ours. One Nature page
                    # hands over a 14 MB DOE validation report of unrelated climate
                    # products; downloading it and mailing it under this article's name is
                    # exactly what the citation cards exist to prevent. So the bytes have
                    # to carry the DOI - or the URL already did - before we accept them.
                    ok = False
                    try:
                        target.unlink()
                        print(f"discarded {target.name}: not this article (no DOI or title match)")
                    except OSError as exc:  # noqa: BLE001
                        print(f"warning: could not discard {target.name} ({exc})")
                    routes_text.append(f"  · [{source}] {url} → 链接指向的并非本文（文件内既无本文 DOI 也无本文标题词），已丢弃")
                kind = "supplement" if _is_supplement(target) else "fulltext"
                if ok and kind in taken:
                    # One article, one copy. `legal_routes` lists every legal place the
                    # full text could live in - Unpaywall's OA PDF, the arXiv preprint,
                    # an OpenAlex copy, the publisher's own endpoint - and each of them
                    # serves its own bytes, so the dated folder, and the mail that walks
                    # it, used to hold two "full texts" of one article: the arXiv PDF
                    # next to the publisher's. Only the copy from the best-priority route
                    # is kept now, and the card says which route answered instead of
                    # sending the same paper twice. A route that failed to deliver never
                    # occupies its kind, so a lower-priority one can still take over.
                    try:
                        target.unlink()
                        print(f"discarded {target.name}: already have this article's {kind}")
                    except OSError as exc:  # noqa: BLE001
                        print(f"warning: could not discard {target.name} ({exc})")
                    routes_text.append(f"  · [{source}] {url} → 与已取得的"
                                       f"{'补充材料' if kind == 'supplement' else '全文'}是同一篇文献"
                                       f"（更高优先级通道已取到），未重复发送")
                    provenance.pop(target.name, None)
                    continue
                provenance[target.name] = {"source": source, "doi": doi, "kind": kind}
                routes_text.append(f"  · [{source}] {url}"
                                   + ("→ 已取得全文/补充材料" if ok else "→ 未取到（受限或非文件）"))
                if ok:
                    got.append(target.name)
                    produced.add(target.name)
                    taken.add(kind)
                    # A supplementary zip only helps the reader once its figures are loose.
                    for child in extract_archive(target, out_dir):
                        got.append(child.name)
                        provenance[child.name] = {"source": f"{source}（压缩包内取件）", "doi": doi,
                                                  "kind": "supplement"}
        except Exception as exc:  # noqa: BLE001 - attachment hunting must never break the run
            print(f"warning: attachment fetch failed for {doi}: {exc}")

        # Second pass through the real desktop browser: it reaches the publisher's own
        # landing page (Cloudflare included) and picks up files plain HTTP never sees.
        # Only non-OA papers need it, and only on a machine with a desktop window.
        if browser_allowed and browser_used < browser_papers and not routes_ok(got):
            browser_used += 1
            try:
                from fetch_browser_paper import browser_fetch, _citation_note
                # Open-access papers additionally get the publisher's own PDF route, handed to
                # the browser session that already passed the verification wall.
                oa_urls = publisher_pdf_urls(doi) if oa.get("is_oa") else []
                for url in oa_urls[:1]:
                    routes_text.append(f"  · [出版商开放获取 PDF 直链] {url} → 交给浏览器会话尝试")
                probe = browser_fetch(doi, out_dir, title=paper.get("title", ""), max_mb=max_mb,
                                      wait_seconds=25, keep_open=browser_wait,
                                      oa_pdf_urls=oa_urls, profile=profile_path)
                routes_text.append(f"  · [本机浏览器] {doi} → {probe.get('status') or '无文件'}")
                if probe.get("verification") or probe.get("notes"):
                    note += "\n" + _citation_note(probe, doi, str(paper.get("title") or ""))
                for name in probe.get("files") or []:
                    if (out_dir / name).exists() and name not in got:
                        got.append(name)
                        saved.append((label, out_dir / name))
            except Exception as exc:  # noqa: BLE001 - the browser is optional
                print(f"   browser pass skipped ({type(exc).__name__})")

        known = {path for _label, path in saved}
        for name in got:
            path = out_dir / name
            if path not in known:
                saved.append((label, path))
        if got:
            note = ("全文/补充材料已通过合法开放通道下载（出版商开放获取、作者公开预印本或"
                    "出版商公开补充材料；订阅版全文未下载）。")
        oa = unpaywall_locations(doi)
        arxiv = {"url": next((u for _, u, s in routes if "arXiv" in s), ""), "verified": ""}
        landing, supplements = landing_supplements(doi)
        manifest_lines.append(_citation_card(paper, index, oa, arxiv, landing, supplements, got, note, routes_text))
        manifest_lines.append("-" * 68)
        print(f"attachment pass {index}: {doi or 'no-doi'} -> {len(got)} files")
        time.sleep(1)

    # Prune first, then describe what is left. The manifest is the hand-over to the cloud
    # mail, and it may only name files that still exist - a duplicate deleted one line
    # earlier would otherwise be declared here and then arrive as an empty attachment.
    saved = prune_duplicate_files(out_dir, saved)

    manifest_json = out_dir / "manifest.json"
    # An earlier run on the same day has already written to this very file, and the mail
    # reads that file. Whatever it listed has to survive into this manifest, or those
    # files - an arXiv full text among them - land on disk and never reach the reader.
    carried: list[dict[str, Any]] = _manifest_entries_from(manifest_json, run_date)
    entries: list[dict[str, Any]] = list(carried)
    seen_paths: set[str] = set()
    # A carried entry names the same file if this run reached it again through another
    # route; listing it twice would promise the mail one file twice.
    claimed_names = {str(e.get("name", "")) for e in entries}
    for label, path in saved:
        if _attachment_priority(path) < 0:
            continue
        if path.name in claimed_names:
            continue
        # Two routes can share one saved name (`link.aps.org/supplemental/10.1103/x`
        # and `journals.aps.org/prb/pdf/10.1103/x` both end in `x`), so `saved` may name
        # a file that is already listed. The manifest describes the mail; one entry.
        key = path.resolve().as_posix()
        if key in seen_paths:
            continue
        seen_paths.add(key)
        entry: dict[str, Any] = {"label": label, "name": path.name}
        entry.update(provenance.get(path.name, {}))
        if path.exists():
            entry["bytes"] = path.stat().st_size
            entry["sha256"] = _content_digest(path)
        entries.append(entry)
    entries = dedupe_by_kind(entries)
    manifest_json.write_text(json.dumps({"date": run_date.isoformat(), "files": entries},
                                        ensure_ascii=False, indent=2), encoding="utf-8")

    manifest_path = out_dir / f"{run_date.isoformat()}_题录与获取指引.txt"
    manifest_path.write_text("\n".join(manifest_lines), encoding="utf-8")
    # The citation card travels with the mail, but it is not a paper file: filing it into
    # `saved` made `article_files` at least one long, so the log read
    # "attached 1 paper files" on the day every attachment pass had fetched 0 files, and
    # the card quietly consumed a slice of the mail's size budget (trim keeps its first
    # item whatever it costs). Papers and cards are two lists now.
    cards = [manifest_path]
    # A log line must never be what breaks a run. `relative_to('.')` raises for any path
    # outside the working directory, so a `paper_attachments.dir` configured with an
    # absolute path would crash the last statement of the pass instead of reporting it.
    shown = manifest_path.relative_to(Path.cwd()) if manifest_path.is_absolute() else manifest_path
    print(f"paper attachments: {len(saved)} fetched, {len(papers)} citation cards -> {shown}")
    return saved, cards, "\n".join(manifest_lines)


def _distinctive_title_words(title: str) -> set[str]:
    """The long words of a title that a single unrelated document will not happen to hold."""
    generic = {"article", "manuscript", "physical", "review", "letters", "theory", "system",
               "model", "models", "state", "states", "matter", "quantum", "approach",
               "method", "methods", "study", "studies", "evidence", "science", "research",
               "published", "properties", "observed", "measurements", "rules", "based",
               "analysis", "abstract", "introduction", "conclusions"}
    return {word for word in re.findall(r"[a-z]{10,}", (title or "").lower())
            if word not in generic}


def _file_is_about(path: Path, url: str, doi: str, title: str = "") -> bool:
    """True when the bytes served for this route really are the article we asked for.

    The landing-page scan collects every link on a publisher page and the page puts a
    cited government report right next to the article PDF, so "supplementary" links can
    lead anywhere. A file is accepted when the DOI is in the URL that served it (the
    publisher's own endpoint) or somewhere in its head (the publisher's own PDF, or the
    preprint that prints the DOI on page one). Both sides are stripped of punctuation so
    a DOI broken across lines as "10.1103/\\nStw8-9mld" still matches.

    A DOI alone is not enough, and it is not always there. An arXiv PDF prints neither the
    DOI in its URL nor the DOI on page one, so rejecting on the DOI alone throws away the
    whole preprint route - and accepting on a single long word is how a government report
    about downscaled climate products would pass as a climate paper. So the second
    criterion is the article title: at least two of its distinctive words (ten letters or
    more, generic titles like "physical review" excluded) must be in the file. A preprint
    of the right paper says "topological signatures"; the wrong one does not.

    A paper without a DOI cannot be checked at all; its routes were curated by name
    anyway, so it is accepted - and loudly, because a silent pass here is how a wrong
    document would get mailed.
    """
    if not doi:
        print(f"warning: {path.name} accepted without a DOI to check it against")
        return True
    needle = re.sub(r"[^0-9a-z]", "", doi.lower())
    if needle and needle in re.sub(r"[^0-9a-z]", "", (url or "").lower()):
        return True
    try:
        with path.open("rb") as handle:
            head = handle.read(1 << 20)
    except OSError as exc:  # noqa: BLE001
        print(f"warning: could not read {path.name} to check its identity ({exc})")
        return False
    # The DOI in a PDF is stored as bytes and may be broken across lines, so the haystack
    # is stripped down to digits and letters on both sides before it is compared.
    if needle and needle.encode() in re.sub(rb"[^0-9a-z]", b"", head.lower()):
        return True
    wanted = _distinctive_title_words(title)
    if not wanted:
        return False
    # Both sets are compared as text: the file is bytes, the title is str, and intersecting
    # the two raw would quietly compare nothing against nothing.
    head_text = re.sub(r"[^a-z]+", " ", head.decode("ascii", "ignore").lower())
    found = set(re.findall(r"[a-z]{10,}", head_text))
    return len(wanted & found) >= 2


def _attachment_priority(path: Path) -> int:
    """Order what survives the size budget: the citation card, then the publisher's own
    open-access PDF, then preprints and supplementary files."""
    name = path.name.lower()
    if "题录" in name or "manifest" in name:
        return 0
    if "arxiv" in name:
        return 2
    if name.endswith(".pdf"):
        return 1
    return 3


def dedupe_by_kind(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One manifest line per article per kind, whatever is still sitting on disk.

    `prune_duplicate_files` compares bytes, so it cannot see two different renderings of
    the same article - the arXiv PDF next to the publisher's - and the manifest would go
    on describing both as "已取得全文", which is a claim the mail then repeats. Keeping
    the first entry of each kind stops that: the reader gets one full text and the
    article's own supplements, and the citation card, which carries no kind, always
    survives.
    """
    # Grouped per article, not globally. The label opens with the article's position in
    # the brief, so two different papers each with a preprint must both be listed: a
    # global key kept the first preprint and dropped the second one's, and the article
    # whose full text the mail then arrived with no full text at all. An entry with no
    # number in its label is its own group - it belongs to no identified article, which
    # is the state most manifests are actually in, and collapsing those into one group
    # per day threw away eleven of the twelve files of 2026-10-02.
    seen: set[tuple[Any, str]] = set()
    kept: list[dict[str, Any]] = []
    for entry in entries:
        key = (_label_slot(str(entry.get("label", ""))), entry.get("kind") or "")
        if key in seen:
            print(f"manifest: dropped a second {key[1] or 'unlabelled'} entry for {entry.get('name')}")
            continue
        seen.add(key)
        kept.append(entry)
    return kept


def _manifest_entries_from(manifest_json: Path, run_date: dt.date) -> list[dict[str, Any]]:
    """Read the manifest entries an earlier run on this same day already wrote down.

    A run used to write the manifest unconditionally, so a second run on the same day
    erased the first run's list. On 2026-10-03 that is what happened four times in one
    day: the 12:xx runs had fetched five PDFs that sat in the dated folder, and the
    17:20 run's manifest named two files. The five were never mailed, on any of the
    four mails that went out, so the reader was told a dry envelope had been attached
    on exactly the days the attachment pipeline had been busiest. The files stayed on
    disk the whole time - only the list that ships them was being thrown away.

    Two guards keep the merge honest. Same date only: a manifest from another day is
    another run's work and must not ride along under today's citation. And every entry
    is re-checked against the folder before it is carried, so a file an earlier run
    described and a later one has since pruned does not come back as a phantom
    attachment the mail promises and cannot deliver.
    """
    if not manifest_json.exists():
        return []
    try:
        payload = json.loads(manifest_json.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"manifest: could not read the previous manifest ({exc}); not merging it")
        return []
    if payload.get("date") != run_date.isoformat():
        return []
    folder = manifest_json.parent
    claimed: set[str] = set()
    merged: list[dict[str, Any]] = []
    for entry in payload.get("files") or []:
        name = str(entry.get("name", "") or "")
        if not name or name in claimed:
            continue
        if not (folder / name).exists():
            print(f"manifest: dropping stale entry {name} from an earlier run today")
            continue
        merged.append(entry)
        claimed.add(name)
    print(f"manifest: carrying {len(merged)} file(s) from an earlier run on {run_date.isoformat()}")
    return merged


def _is_supplement(path: Path) -> bool:
    """True for a supplementary file, which ranks behind the article's own full text."""
    name = path.name.lower()
    return any(token in name for token in
               ("supplement", "_supp", "supp_", "si_", "mmc", "esm", "supporting", "附件"))


def _content_digest(path: Path) -> str:
    """Hash of the bytes, so a PDF found twice through two channels counts once."""
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalise_doi(value: str) -> str:
    value = value.strip().lower()
    for prefix in ("https://doi.org/", "http://dx.doi.org/", "doi:", "https://"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    return value.strip("/ ")


def _label_slot(label: str) -> int | None:
    """The article's position in the brief, as the label `1_<slug>` / `2_<slug>` carries it.

    Zero-based, to line up with the paper list. A label that does not open with a digit
    names nothing.
    """
    head = re.match(r"(\d+)", str(label or ""))
    return int(head.group(1)) - 1 if head else None


def _paper_doi_set(papers) -> set[str]:
    """The DOIs this mail is actually about, so a synced file can be told to belong or not."""
    dois: set[str] = set()
    for paper in papers or []:
        doi = _normalise_doi(str((paper or {}).get("doi", "") or ""))
        if doi:
            dois.add(doi)
    return dois


def reuse_synced_attachments(
    run_date: dt.date, config: dict[str, Any], note: str = "",
    *, papers: list[Any] | None = None, allow_unidentified: bool = False,
) -> tuple[list[tuple[str, Path]], str]:
    """Pick the files a local run already fetched and pushed into the repository.

    The full text of a subscription journal only exists on a machine that owns the
    publisher's session, so a local run can fetch it while the scheduled cloud run
    cannot. The     local run drops its result into `research_briefs/attachments/<date>/`
    with a manifest, and the mail that goes out from the cloud reads that folder
    back instead of mailing an empty envelope.

    Dates matter: a manifest from another day is another run's work, and mailing it
    under today's citation card would be the exact lie the citation cards exist to
    prevent.

    Returns (paper files, citation cards, note) - the card is not a paper, see
    `fetch_paper_attachments`.
    """
    options = config.get("paper_attachments") or {}
    if not options.get("enabled", True):
        return [], [], note
    out_dir = Path(str(options.get("dir", "research_briefs/attachments"))) / run_date.isoformat()
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.exists():
        return [], [], note
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"warning: attachment manifest unreadable ({exc})")
        return [], [], note
    stamped = payload.get("date")
    if stamped != run_date.isoformat():
        print(f"warning: ignoring synced manifest dated {stamped!r} for run on {run_date.isoformat()}")
        return [], [], note

    pairs: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    seen_digest: set[str] = set()

    def offer(path: Path, label: str = "") -> None:
        """Attach a synced file once; a second copy of the same bytes is not a second file.

        Identity is the hash, not the name: two routes can hand over the same PDF under
        two names, and a mail with that file twice is the promise the manifest made twice.
        """
        if not path.exists():
            return
        if path.resolve() in seen:
            return
        digest = _content_digest(path)
        if digest in seen_digest:
            print(f"pruned duplicate attachment {path.name}")
            return
        seen.add(path.resolve())
        seen_digest.add(digest)
        pairs.append((label, path))

    # A manifest dated today is not automatically a manifest about *today's* papers: a
    # folder can still hold the previous run's files if the local hand-over overwrote only
    # part of it. On 2026-10-03 the cloud run reused 13 files, of which 12 belonged to
    # yesterday's seven papers, and mailed them under today's single citation. So the
    # files are matched against the papers this mail actually carries, by DOI.
    wanted = _paper_doi_set(papers) if papers else set()
    # A manifest entry can sit there without a DOI, when the run that fetched the file
    # had not yet worked out which article it belongs to. Its label carries the
    # article's position in the brief - "1_<slug>", "2_<slug>" - which is enough to
    # settle whether this mail is the right one. Without it every such file was counted
    # as an orphan, so on 2026-10-02, where not one entry carried a DOI, a cloud run
    # matched nothing at all and the mail it sent carried no paper file.
    slot_doi: dict[int, str] = {}
    for index, paper in enumerate(papers or []):
        doi = _normalise_doi(str((paper or {}).get("doi", "") or ""))
        if doi:
            slot_doi[index] = doi
    orphan = 0
    for entry in payload.get("files") or []:
        path = out_dir / str(entry.get("name", ""))
        if wanted:
            entry_doi = _normalise_doi(str(entry.get("doi", "") or ""))
            if entry_doi and entry_doi not in wanted:
                orphan += 1
                continue
            if not entry_doi:
                slot = _label_slot(str(entry.get("label", "")))
                if slot is None or slot_doi.get(slot) not in wanted:
                    if not allow_unidentified:
                        orphan += 1
                        continue
        offer(path, str(entry.get("label", "")))
    if orphan:
        print(f"reuse: skipped {orphan} file(s) that do not belong to the papers in this mail")
    card_path = out_dir / f"{run_date.isoformat()}_题录与获取指引.txt"
    if not pairs and not card_path.exists():
        return [], [], note
    # The manifest is deliberately NOT offered as an attachment any more. It is the
    # bridge's own bookkeeping - which route answered, what hash was verified - so in
    # the reader's mailbox it is a file that names itself "manifest.json" and tells
    # them nothing at all. It was worse than noise: offering it made this function
    # return a non-empty paper list on a day whose real PDFs had not yet arrived, so
    # `_wait_and_reuse` saw its success condition met, stopped polling for the local
    # bridge, and mailed the brief immediately with no full text behind it.
    # The citation card is not in the manifest - the fetch that will run in the cloud
    # writes its own - but a cloud run that fetched nothing still owes the reader the
    # list of articles and the legal routes they came from.
    cards = [card_path] if card_path.exists() else []
    note += ("\n\n**随信说明**：以上文件的抓取发生在本地（本机浏览器会话），"
             f"由本地 run 同步进仓库后由云端这封邮件发出；推送时间 {stamped}。"
             "抓取过程与通道各自记录在 `manifest.json` 中。")
    print(f"reused {len(pairs)} synced attachment files from {out_dir}"
          + (f" and {len(cards)} citation card" if cards else ""))
    return pairs, cards, note


def prune_duplicate_files(out_dir: Path, saved: list[tuple[str, Path]]) -> list[tuple[str, Path]]:
    """Delete the byte-identical copies sitting in the dated folder.

    Two sessions fetching the same paper through different routes write the same bytes
    under two names, and one session can write them twice - the publisher endpoint and
    the preprint both answer for an open-access article. A copy that is in `saved`
    protects its twin, which is why the survivors are chosen globally: for every digest
    keep only the lexicographically first path and delete the rest, including copies
    this very run listed. The folder that gets mailed and archived then reads as one
    file per route hit instead of a pile of copies of the same PDF.
    """
    candidates = [path for _label, path in saved if path.exists()]
    candidates += [path for path in sorted(out_dir.iterdir())
                   if path.is_file() and all(path != known for known in candidates)]

    groups: dict[str, list[Path]] = {}
    for path in candidates:
        groups.setdefault(_content_digest(path), []).append(path)
    survivors = {min(paths).resolve() for paths in groups.values()}

    kept = [(label, path) for label, path in saved
            if path.exists() and path.resolve() in survivors]
    for path in candidates:
        if path.resolve() in survivors:
            continue
        try:
            path.unlink()
            print(f"pruned duplicate attachment {path.name}")
        except OSError as exc:
            print(f"warning: could not prune {path.name} ({exc})")
    return kept



def trim_attachments(pairs: list[tuple[str, Path]], budget_mb: float = 20.0) -> tuple[list[tuple[str, Path]], list[tuple[str, Path]]]:
    """Spend the mail budget across 入选文献 instead of on the first two papers.

    Two things used to waste it. The same PDF arrived twice - the HTTP route and the
    browser route return identical bytes under two different names - and the old keep-order
    walked every PDF of the first papers before the last paper's file had a chance, so the
    browser-fetched full texts were exactly the ones thrown away. Duplicates by content are
    collapsed and the budget is then handed out round-robin, one file per paper per pass.

    Answers with `(kept, dropped)`; the reader of the mail needs the dropped list as much
    as the kept one, otherwise a file the citation card claims was downloaded silently
    never arrives.
    """
    def size_of(path: Path) -> float:
        try:
            return path.stat().st_size / 1_000_000
        except OSError:
            return 0.0

    kept: list[tuple[str, Path]] = []
    dropped: list[tuple[str, Path]] = []
    seen_digest: set[str] = set()
    total = 0.0
    bodies = 0

    def spend(item: tuple[str, Path]) -> bool:
        nonlocal total, bodies
        path = item[1]
        if _attachment_priority(path) < 0:      # the citation card always travels
            kept.append(item)
            return True
        digest = _content_digest(path)
        if digest in seen_digest:
            print(f"attachment dropped (identical bytes already mailed): {path.name}")
            dropped.append(item)
            return False
        need = size_of(path)
        # The first article file goes out even if it alone exceeds the budget: an empty
        # mailing list would leave the citation card claiming full texts that never arrive.
        if total + need <= budget_mb or bodies == 0:
            seen_digest.add(digest)
            kept.append(item)
            total += need
            bodies += 1
            return True
        print(f"attachment dropped (mail size budget): [{item[0]}] {path.name} ({need:.1f} MB)")
        dropped.append(item)
        return False

    manifest = [item for item in pairs if _attachment_priority(item[1]) < 0]
    rest = [item for item in pairs if item not in manifest]
    kept.extend(manifest)

    groups: dict[str, list[tuple[str, Path]]] = {}
    order: list[str] = []
    for item in rest:
        label = item[0] or "其他附件"
        groups.setdefault(label, [])
        if not groups[label]:
            order.append(label)
        groups[label].append(item)
    # Inside one paper the publisher's own full text outranks a supplement.
    for label in order:
        groups[label].sort(key=lambda item: _is_supplement(item[1]))

    pending = {label: list(groups[label]) for label in order}
    while True:
        moved = False
        for label in order:
            bucket = pending[label]
            while bucket:
                item = bucket.pop(0)
                if spend(item):
                    moved = True
                    break   # one file per paper per pass, so no single article eats the mail
        if not moved:
            break
    return kept, dropped
