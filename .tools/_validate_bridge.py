"""Validate the local-fetch -> manifest -> cloud-reuse bridge using 2 real APS DOIs.

Downloads nothing to the repo and sends no email; it just proves that a PDF fetched on
this machine and described by our manifest.json is picked up by reuse_synced_attachments
(the function the cloud calls to attach locally-fetched poster PDFs).
"""
import sys, json, datetime as dt, hashlib, os
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import repo_config as rc  # noqa: E402

REPO = rc.project_root()
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / ".tools"))
import fetch_pdfs_local as fl  # noqa: E402
from fetch_paper_attachments import reuse_synced_attachments  # noqa: E402

ATT_DIR = REPO / "research_briefs" / "attachments"
date = dt.datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
out = ATT_DIR / date
out.mkdir(parents=True, exist_ok=True)

# Two real APS DOIs known to resolve and to be APS-blocked in the cloud.
dois = [
    ("10.1103/fstv-lsvh", "Altermagnetic spin-splitting"),
    ("10.1103/xfg2-227f", "Nodal-line semimetal symmetries"),
]
files = []
for i, (doi, title) in enumerate(dois):
    venue = fl.crossref_journal(doi)
    path, note = fl.try_routes(doi, title, venue, out)
    assert path, f"fetch failed for {doi}: {note}"
    print(f"  fetched {doi} -> {path.name} ({path.stat().st_size//1024} KB)")
    files.append({
        "label": f"{i+1}_x",
        "name": path.name,
        "doi": doi,
        "kind": "fulltext",
        "bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    })
manifest = {"date": date, "files": files}
(out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

config = {"paper_attachments": {"enabled": True, "dir": str(ATT_DIR)}}
papers = [
    {"doi": dois[0][0], "venue": "Physical Review Letters"},
    {"doi": dois[1][0], "venue": "Physical Review Research"},
]
pairs, cards, note = reuse_synced_attachments(dt.date.fromisoformat(date), config, "", papers=papers)
print("reused pairs:", len(pairs))
for label, path in pairs:
    print("   ", path.name)
pdf_pairs = [(label, p) for label, p in pairs if p.name.lower().endswith(".pdf")]
assert len(pdf_pairs) == 2, f"BRIDGE FAILED: reuse returned {len(pdf_pairs)} PDFs, expected 2"
reused_names = {p.name for _l, p in pdf_pairs}
expected_names = {f["name"] for f in files}
assert expected_names <= reused_names, f"BRIDGE FAILED: reused {reused_names} missing {expected_names - reused_names}"
print("BRIDGE OK - cloud would attach both poster PDFs")
