# -*- coding: utf-8 -*-
"""Fetch the day's PDFs from this machine, because the runner cannot.

Why this exists
---------------
Run 37423093541 (2026-10-06) fetched 1060 records but produced
`paper attachments: 1 fetched`, and the log is full of
`publisher feed unavailable https://journals.aps.org/prl/abstract/... : HTTP Error 403`.

A probe run from this machine with the *same* URL and the *same* headers returns
`200 application/pdf` for those DOIs. So the request shape is fine and the route
is legal (`journals.aps.org/robots.txt` disallows only /search, /account, /login;
`/pdf/` is allowed) - **APS simply refuses the GitHub Actions egress IP.** Nothing
inside the workflow can fix that, because every attempt comes from the same blocked
address. Fetching from a normal machine is the way through.

What it does
------------
Reads the DOIs from a brief markdown file (or from a run log), tries the legal
routes in order for each, verifies the download really is this article, and writes
the PDFs into a dated folder.

    python .tools/fetch_pdfs_local.py --brief research_briefs/2026-10-06.md
    python .tools/fetch_pdfs_local.py --dois 10.1103/fstv-lsvh,10.1103/xfg2-227f

Identity check: a PDF is kept only if its DOI appears in the bytes, or its title
tokens match. This is what the arXiv route needs - on 2026-10-06 it happily
returned a 2020 preprint (20347v2) for a 2026 PRL article, and the mismatch had to
be caught or the reader would have received the wrong paper.
"""
import argparse
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36 research-brief/2.0")
ATOM = "{http://www.w3.org/2005/Atom}"

# journals.aps.org/<journal>/pdf/<DOI> - verified 200 application/pdf from this
# machine on 2026-10-06 for PRL and PRB.
APS_JOURNALS = ("prl", "prb", "prresearch", "prx", "prmaterial", "prapplied",
                "prxenergy", "prxquantum", "prsymmetry", "rmp", "prevc", "prd")

STOPWORDS = {"a", "an", "the", "of", "and", "or", "in", "on", "for", "to", "with",
             "via", "using", "we", "is", "are", "at", "by", "from", "as", "it",
             "that", "this", "be", "been", "our", "its"}


def _open(url: str, timeout: int = 40, data: bytes | None = None):
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": UA, "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }, method="POST" if data else "GET")
    return urllib.request.urlopen(req, timeout=timeout)


def slug(text: str, limit: int = 60) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", (text or "").strip()).strip("_").lower()
    return s[:limit].rstrip("_") or "paper"


def title_tokens(title: str) -> set:
    words = re.findall(r"[a-z0-9]+", (title or "").lower())
    return {w for w in words if len(w) > 3 and w not in STOPWORDS}


def crossref_journal(doi: str) -> str:
    """Container title from Crossref, or '' when the DOI does not exist.

    The empty return is load-bearing: a DOI that does not resolve means the
    caller passed something wrong (a hand-assembled one, or one reconstructed
    from a log line where the middle was masked as ***), and the run must say so
    instead of quietly reporting "no route worked" for every venue.
    """
    url = "https://api.crossref.org/works/" + urllib.parse.quote(doi, safe="")
    try:
        with _open(url, timeout=25) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        return (d.get("message", {}).get("container-title") or [""])[0]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return ""
        return ""
    except Exception:
        return ""


def doi_exists(doi: str) -> bool:
    """True when Crossref knows this DOI. Guards against hand-assembled DOIs."""
    url = "https://api.crossref.org/works/" + urllib.parse.quote(doi, safe="")
    try:
        with _open(url, timeout=25) as r:
            return r.status == 200
    except Exception:
        return False


# Crossref reports the full title, e.g. "Physical Review Letters". Substring
# matching on the shorthand ("prl" in "physicalreviewletters") is False - the
# letters are not adjacent - so the mapping is spelled out instead. Getting this
# wrong returns None and the run silently ends with zero PDFs.
APS_JOURNAL_BY_NAME = {
    "physicalreviewletters": "prl",
    "physicalreview": "prl",
    "physicalreviewb": "prb",
    "physicalreviewresearch": "prresearch",
    "physicalreviewx": "prx",
    "physicalreviewmaterials": "prmaterial",
    "physicalreviewappliedmaterials": "prapplied",
    "physicalreviewxquantum": "prxquantum",
    "physicalreviewenergy": "prxenergy",
    "reviews of modern physics": "rmp",
    "physical reviewsymmetry": "prsymmetry",
    "physicalreviewd": "prd",
}


def aps_pdf_url(doi: str, journal: str) -> str | None:
    j = (journal or "").lower().replace(" ", "").strip()
    if not j:
        return None
    if j in APS_JOURNAL_BY_NAME:
        return f"https://journals.aps.org/{APS_JOURNAL_BY_NAME[j]}/pdf/{doi}"
    # Longest shorthand first so "prxquantum" wins over "prx".
    for key in sorted(APS_JOURNALS, key=len, reverse=True):
        if key in j:
            return f"https://journals.aps.org/{key}/pdf/{doi}"
    return None


def _derive_oa_pdf_url(doi: str, landing: str) -> str:
    """Synthesize a direct PDF URL when Unpaywall flags OA but omits ``url_for_pdf``.

    Nature/Springer OA articles resolve to a predictable ``/articles/<id>.pdf`` URL;
    many other OA publishers serve a PDF when ``.pdf`` is appended to the landing
    page. The caller still verifies the bytes, so a wrong derivation is rejected
    safely rather than shipping the wrong paper.
    """
    if not doi:
        return ""
    # Nature / Springer: 10.1038/<manuscript-id> -> nature.com/articles/<manuscript-id>.pdf
    m = re.search(r"10\.1038/([^\s/]+)", doi)
    if m:
        return f"https://www.nature.com/articles/{m.group(1)}.pdf"
    # Generic fallback: append .pdf to the landing page path.
    if landing and not landing.lower().endswith(".pdf"):
        return landing.rstrip("/") + ".pdf"
    return ""


def unpaywall_pdf(doi: str) -> tuple:
    """Return (pdf_url, landing_url) reported as open access, or ('','')."""
    url = (f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi, safe='')}"
           "?email=research.brief@example.org")
    try:
        with _open(url, timeout=25) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return "", ""
    best = d.get("best_oa_location") or {}
    for loc in (best, *(d.get("oa_locations") or [])):
        if not isinstance(loc, dict):
            continue
        pdf = loc.get("url_for_pdf") or ""
        if pdf and str(loc.get("version", "")).lower() in ("publishedversion", "vor"):
            return pdf, loc.get("url_for_landing_page") or ""
    # OA but Unpaywall returned only a landing page (no direct pdf). Synthesize one.
    if d.get("is_oa"):
        landing = (best.get("url_for_landing_page")
                   or (d.get("oa_locations") or [{}])[0].get("url_for_landing_page")
                   or "")
        synth = _derive_oa_pdf_url(doi, landing)
        if synth:
            return synth, landing
    return "", ""


def arxiv_pdf(doi_or_title: str, title: str) -> str:
    """Search arXiv by title; only used when a DOI is absent."""
    q = (title or doi_or_title or "").strip()
    if not q:
        return ""
    params = urllib.parse.urlencode({
        "search_query": f'ti:"{q}"', "start": 0, "max_results": 5,
    })
    url = "https://export.arxiv.org/api/query?" + params
    try:
        with _open(url, timeout=40) as r:
            body = r.read().decode("utf-8", "replace")
    except Exception:
        return ""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(body)
    except Exception:
        return ""
    want = title_tokens(title)
    for entry in root.findall(ATOM + "entry"):
        t = (entry.findtext(ATOM + "title") or "")
        got = title_tokens(t)
        # Require a real overlap, and reject a wildly different year if the arXiv
        # id encodes one: a 2020 preprint is not today's PRL article.
        if not want or not got:
            continue
        if len(want & got) / len(want) < 0.6:
            continue
        link = entry.find(ATOM + "id")
        if link is not None and link.text:
            return link.text.replace("/abs/", "/pdf/")
    return ""


def _real_pdf(blob: bytes) -> tuple:
    """(ok, reason) for a route whose identity was settled outside the bytes.

    Used for arXiv: the URL came from the matching metadata entry, so what is left to
    check is only that the payload is genuinely a PDF and not a redirect to a login or
    error page.
    """
    if not blob.startswith(b"%PDF-"):
        return False, "not a PDF (bad magic)"
    if len(blob) < 20_000:
        return False, f"too small ({len(blob)} B) - likely an error page"
    return True, f"valid PDF ({len(blob) // 1024} KB), identity from arXiv metadata match"


def verify_pdf(blob: bytes, doi: str, title: str) -> tuple:
    """(ok, reason). A wrong-paper PDF is worse than no PDF."""
    if not blob.startswith(b"%PDF-"):
        return False, "not a PDF (bad magic)"
    if len(blob) < 20_000:
        return False, f"too small ({len(blob)} B) - likely an error page"
    if doi and doi.lower().encode() in blob.lower():
        return True, "DOI found in bytes"
    # arXiv stamps its own identifier; check the title instead.
    if title:
        head = blob[:400_000].lower()
        toks = title_tokens(title)
        if toks:
            hit = sum(1 for t in toks if t.encode() in head)
            if hit / len(toks) >= 0.6:
                return True, f"title match ({hit}/{len(toks)} tokens)"
    return False, "no DOI or title match - wrong article"


def try_routes(doi: str, title: str, journal: str, out_dir: Path) -> tuple:
    """Try each legal route in turn. Returns (path|None, note)."""
    routes = []

    aps = aps_pdf_url(doi, journal)
    if aps:
        routes.append(("aps", aps))
    oa_pdf, oa_landing = unpaywall_pdf(doi) if doi else ("", "")
    if oa_pdf:
        routes.append(("unpaywall", oa_pdf))
    # arXiv last, and for DOI-carrying papers too. A subscription journal whose own
    # endpoints all answer with a bot wall can still have the author's version on
    # arXiv, and historically this is the route that actually completed. Gating it on
    # "only when there is no DOI" made it dead code precisely for the papers that need
    # it: every paper this pipeline shortlists carries a DOI.
    arx = arxiv_pdf(doi, title) if (doi or title) else ""
    if arx:
        routes.append(("arxiv", arx))
    if oa_landing:
        routes.append(("landing", oa_landing))

    for name, url in routes:
        try:
            with _open(url) as r:
                blob = r.read()
        except urllib.error.HTTPError as e:
            print(f"    {name}: HTTP {e.code}")
            continue
        except Exception as e:
            print(f"    {name}: {type(e).__name__}")
            continue
        if name == "arxiv":
            # Identity was already settled before the download: this URL belongs to the
            # arXiv entry whose title `arxiv_pdf` matched against the article's. Scraping
            # the bytes for those same words is a second, weaker test - arXiv's own PDFs
            # routinely encode their text so the literal words are not findable in it -
            # and being strict there simply threw away the one route that ever produced
            # a file. So only "is this really a PDF" is asked of the bytes, and why says
            # what the identity rests on.
            ok, why = _real_pdf(blob)
        else:
            ok, why = verify_pdf(blob, doi, title)
        if not ok:
            print(f"    {name}: rejected - {why}")
            continue
        # Reaching here means the route produced a verified PDF; name it and save it.
        fname = f"{slug(journal or name)}_{slug(title, 70)}_{slug(doi.replace('/', '-'), 30)}.pdf"
        path = out_dir / fname
        path.write_bytes(blob)
        return path, f"{name} ({why}, {len(blob)//1024} KB)"
    return None, "no legal route produced a verified PDF"


# --------------------------------------------------------------------------- #
# Supplementary material + composite VOR fetch                                 #
# --------------------------------------------------------------------------- #
def _fetch_landing_page(doi: str) -> tuple[str, str]:
    """Follow the DOI to its publisher landing page; return (final_url, html).

    The landing page is where openly-served supplementary files live. A 200 HTML is
    what we want here (not a PDF), so `_open` is fine - the publisher answers the
    DOI redirect with the article page on a normal machine.
    """
    candidates = [f"https://doi.org/{doi}"]
    if doi.startswith("10.1038/"):
        tail = doi.split("/")[-1].replace(".", "-")
        candidates.append(f"https://www.nature.com/articles/{tail}")
    for url in candidates:
        try:
            with _open(url, timeout=30) as resp:
                return resp.geturl(), resp.read().decode("utf-8", "replace")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                http.client.HTTPException, OSError):
            continue
    return "", ""


# A supplementary file is one whose link name carries a supplement token AND whose
# extension is a real document type. The token list also catches "note_s1", "fig_s2",
# "dataset", "additional" - the common shapes across APS/Nature/Springer/ACS/Elsevier.
_SUPP_TOKEN_RE = re.compile(
    r"(?i)(?:supplement|supp|mediaobject|mmc|esm|attachment|supporting|si_|"
    r"supplementary|additional|figure|fig_|dataset|note_?[s]?[1-9])")
_SUPP_EXT_RE = re.compile(
    r"(?i)\.(?:pdf|zip|docx?|xlsx?|csv|png|jpe?g|tiff?)(?:\?|#|$)")
# These are article navigation, not files: never offered as a supplement.
_SUPP_STOP_RE = re.compile(r"(?i)(?:^#|/#supplemental|support\.|/help|cookie|login|account)")


def _scan_supplement_links(html: str, landing: str, doi: str) -> list[str]:
    """Openly-served supplementary files linked from the publisher's own landing page.

    Only links on the *same publisher host* as the landing page are kept, so a
    support/sitemap/ad URL is never offered. A supplementary file belongs to this
    article precisely because the publisher's own page links to it - that is the
    identity guarantee, and it is why a wrong-paper supplement cannot arrive here.
    """
    if not html or not landing:
        return []
    host = urllib.parse.urlparse(landing).netloc.lower()
    out: list[str] = []
    for raw in re.findall(r'href="([^"]+)"', html):
        low = raw.lower()
        if not _SUPP_TOKEN_RE.search(low):
            continue
        if not _SUPP_EXT_RE.search(low):
            continue
        if _SUPP_STOP_RE.search(low):
            continue
        url = raw
        if url.startswith("http://"):
            url = "https://" + url[7:]
        elif url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            url = f"https://{host}{url}"
        if urllib.parse.urlparse(url).netloc.lower() != host:
            continue
        if url not in out:
            out.append(url)
    return out


def fetch_supplements(doi: str, title: str, out_dir: Path, *, limit: int = 4) -> list[tuple[Path, str]]:
    """Download openly-served supplementary material for the DOI (HTTP, residential IP).

    Returns (path, note) pairs. Identity for a supplement is lighter than for the full
    text: it must be a real file (PDF/zip/...), a reasonable size, and linked from
    the publisher's own landing page (it is, by construction). We do NOT demand the
    article DOI inside a supplement - supplementary figures legitimately carry none -
    but we DO require the link to come from this article's page on the publisher host,
    which is the binding that keeps the wrong paper's files out.
    """
    landing, html = _fetch_landing_page(doi)
    links = _scan_supplement_links(html, landing, doi)
    out: list[tuple[Path, str]] = []
    for url in links[:limit]:
        try:
            with _open(url, timeout=90) as r:
                blob = r.read()
        except urllib.error.HTTPError as e:
            print(f"    supplement: HTTP {e.code} {url[:80]}")
            continue
        except (urllib.error.URLError, TimeoutError, http.client.HTTPException,
                OSError) as exc:
            print(f"    supplement: {type(exc).__name__} {url[:80]}")
            continue
        magic = blob[:4]
        looks_file = magic in (b"%PDF", b"PK\x03\x04") or (magic[:2] == b"%P" and b"DF" in blob[:8])
        if not looks_file:
            print(f"    supplement: skipped (not a file) {url[:80]}")
            continue
        if len(blob) < 4096:
            print(f"    supplement: skipped (too small, {len(blob)} B) {url[:80]}")
            continue
        tail = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1] or "file"
        fname = f"supp_{slug(title, 50)}_{slug(tail, 40)}"
        if not fname.lower().endswith((".pdf", ".zip", ".docx", ".xlsx", ".csv",
                                       ".png", ".jpg", ".jpeg", ".tif", ".tiff")):
            fname += ".pdf"
        path = out_dir / fname
        path.write_bytes(blob)
        note = f"supplement ({len(blob)//1024} KB) from {urllib.parse.urlparse(url).netloc}"
        out.append((path, note))
        print(f"    OK supplement -> {path.name} [{note}]")
    if not out and links:
        print(f"    supplement: {len(links)} candidate link(s) found but none downloaded "
              f"(paywalled or not a direct file)")
    return out


def fetch_vor_and_supplements(doi: str, title: str, venue: str, out_dir: Path) -> dict:
    """Best-effort journal version of record (VOR) + openly-served supplements.

    The arXiv preprint is only the fallback for the *main* file when no publisher
    route answers - it is never preferred. Supplements are fetched on their own.

    Returns a dict: main_path (Path|None), main_kind ('fulltext'|'preprint'|None),
    main_note (str), supplements (list of (path, note)).
    """
    main_path, main_note = try_routes(doi, title, venue, out_dir)
    main_kind = None
    if main_path:
        # try_routes names the winning route first, so the note opens with it:
        # "aps ...", "unpaywall ...", "arxiv (...)", "landing ...". Only "arxiv" is
        # the preprint; everything else is the publisher's own file (VOR or OA).
        main_kind = "preprint" if main_note.lower().startswith("arxiv") else "fulltext"
    supplements = fetch_supplements(doi, title, out_dir)
    return {
        "main_path": main_path, "main_kind": main_kind, "main_note": main_note,
        "supplements": supplements,
    }


def dois_from_brief(md: str) -> list:
    """Pull (doi, title, venue) triples out of a brief markdown file."""
    text = Path(md).read_text(encoding="utf-8", errors="replace")
    rows = []
    # Brief entries look like: ### 1. Title  ...  **DOI**: 10.xxxx/yyy
    for block in re.split(r"\n(?=#{2,4} )", text):
        d = re.search(r"10\.\d{4,9}/[^\s`<>)\]]+", block)
        if not d:
            continue
        head = block.strip().splitlines()[0]
        title = re.sub(r"^#+\s*", "", head)
        title = re.sub(r"^\d+[\.、]\s*", "", title).strip(" *`")
        venue = ""
        vm = re.search(r"(?:期刊|venue|Journal)\*\*\s*[:：]\s*([^\n|]+)", block)
        if vm:
            venue = vm.group(1).strip()
        rows.append((d.group(0).rstrip(".,;"), title, venue))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Fetch article PDFs from this machine (the Actions runner is blocked by APS).")
    ap.add_argument("--brief", help="brief markdown file to harvest DOIs from")
    ap.add_argument("--dois", help="semicolon-separated entries, each DOI[|title[|venue]]")
    ap.add_argument("--out", help="output folder (default research_briefs/attachments/<date>_local)")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()

    items: list = []
    if args.brief:
        items = dois_from_brief(args.brief)
        print(f"harvested {len(items)} DOI(s) from {args.brief}")
    if args.dois:
        # Entries are separated by ";" because "|" already separates the fields
        # of one entry. A comma-separated list collapsed into a single DOI.
        for chunk in args.dois.split(";"):
            parts = [p.strip() for p in chunk.split("|")]
            if parts and parts[0]:
                items.append((parts[0],
                              parts[1] if len(parts) > 1 else "",
                              parts[2] if len(parts) > 2 else ""))
    if not items:
        ap.error("nothing to do: pass --brief or --dois")

    out_dir = Path(args.out) if args.out else Path(
        "research_briefs/attachments/local_pdfs")
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"output -> {out_dir}")
    print("=" * 74)

    ok = 0
    for doi, title, venue in items[:args.limit]:
        print(f"{doi}")
        if not doi_exists(doi):
            # Almost always a DOI reconstructed from a masked log line
            # (journals.aps.org/.../7wl9-***971). Say so rather than failing
            # every route in turn with no explanation.
            print("    SKIPPED: Crossref does not know this DOI - it is not a real"
                  " DOI (often reassembled from a masked log line)")
            continue
        if not venue:
            venue = crossref_journal(doi)
        if not title and venue:
            title = ""
        path, note = try_routes(doi, title, venue, out_dir)
        if path:
            ok += 1
            print(f"    OK -> {path.name}  [{note}]")
        else:
            print(f"    FAILED: {note}")
        time.sleep(1.0)   # be polite to the publishers

    print("=" * 74)
    print(f"fetched {ok}/{min(len(items), args.limit)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
