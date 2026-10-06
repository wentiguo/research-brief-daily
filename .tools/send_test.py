"""Send the already-generated 2026-10-06 brief through the real send path.

Reads email credentials from the environment and calls the exact same `send_email`
used by the daily GitHub Actions workflow, so a successful send proves the push
path is wired correctly. Reuses research_briefs/2026-10-06.md plus the poster
attachments and digest images produced by the earlier run; no re-fetch of feeds.
"""
import os
import sys
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import generate_research_brief as grb  # noqa: E402

run_date = dt.date(2026, 10, 6)
cfg = grb.load_config()

brief_dir = ROOT / "research_briefs"
markdown = (brief_dir / f"{run_date.isoformat()}.md").read_text(encoding="utf-8")

attach_dir = brief_dir / "attachments" / run_date.isoformat()
attachments = [p for p in attach_dir.rglob("*") if p.is_file()] if attach_dir.exists() else []

img_dir = brief_dir / "images"
images = list(img_dir.glob(f"{run_date.isoformat()}*")) if img_dir.exists() else []

print(f"recipient resolved to: {grb.resolve_recipient(cfg)}")
print(f"attachments: {len(attachments)} file(s); digest images: {len(images)} file(s)")

provider = grb.send_email(
    markdown, cfg, run_date,
    attachments=[*attachments, *images],
    force=True,  # bypass the local ledger so this explicit test always goes out
)
print(f"RESULT: sent via {provider}")
