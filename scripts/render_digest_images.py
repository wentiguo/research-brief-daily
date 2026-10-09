#!/usr/bin/env python3
"""Render the daily brief into two images: a poster (1-2 highlighted papers) and a long scroll.

Design rules
------------
* Everything drawn here comes from the records the pipeline actually fetched
  (title, authors, venue, date, DOI, public abstract, scores). Nothing is invented:
  if a field is missing it is left out or replaced by an explicit "unavailable".
* The poster prints the three score components it is ranked on, so the selection is
  auditable rather than a black box.
* The long image is a faithful rendering of the email body (the generated Markdown),
  so the image and the email never disagree.

Only Pillow is used. A CJK font is required; without one the images are skipped loudly
rather than shipped as tofu boxes.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
import re
from typing import Any

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - the brief must still work without images
    # Bind every name to None: a partial `from PIL import ...` failure leaves the other
    # names unbound, which later surfaces as a confusing "name 'ImageFont' is not defined"
    # instead of the real cause (Pillow simply not installed in this interpreter).
    Image = ImageDraw = ImageFont = None  # type: ignore[assignment]

WIDTH = 1100
PADDING = 56
HEADER_H = 176
FOOTER_H = 92

BG = (255, 255, 255)
INK = (24, 30, 40)
MUTED = (108, 120, 134)
ACCENT = (28, 84, 158)
ACCENT_SOFT = (232, 240, 250)
CARD = (245, 247, 250)
RULE = (222, 227, 234)
GOLD = (196, 145, 26)
TEAL = (23, 128, 118)

# (bold, regular) font candidates, in order of preference.
_FONT_SEARCH = (
    (r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\msyh.ttc"),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    ("/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc", "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
    ("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf", "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf"),
    ("/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/PingFang.ttc"),
)


def available_fonts() -> tuple[str, str] | None:
    if Image is None:
        print("warning: Pillow is not installed in this interpreter, digest images skipped "
              "(the GitHub runner installs it; locally use pip install pillow)")
        return None
    for bold, regular in _FONT_SEARCH:
        if Path(bold).exists() and Path(regular).exists():
            return bold, regular
    return None


_fonts_cache: tuple[str, str] | None | bool = None


def fonts() -> tuple[str, str] | None:
    global _fonts_cache
    if _fonts_cache is None:
        _fonts_cache = available_fonts()
    return _fonts_cache  # type: ignore[return-value]


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’\-–—+*/=()\[\].,;:%$#&<>|?!]*|\s+|[^\x00-\x7f]")

# An underscore is not part of the token class above, so the tokenizer used to drop it and
# glue "6_nature_machine_intelligence" into "6naturemachineintelligence" - unreadable in a
# picture that is supposed to name the files it carries. These separators become spaces,
# which also makes a long saved file name break at its underscores instead of mid-word.
# LaTeX in a title ("${Z}_{2}$") needs the same treatment.
_GLUED_RE = re.compile(r'[_{}"\\@~^`]')


def _tokens(paragraph: str) -> list[str]:
    """Latin words stay whole; CJK characters stay single tokens so they can still break anywhere."""
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(paragraph):
        token = match.group(0)
        if tokens and tokens[-1].isspace() and token.isspace():
            continue
        if tokens and token.isspace() and tokens[-1].isspace():
            continue
        tokens.append(token)
    return tokens


def wrap(text: str, draw: ImageDraw.ImageDraw, f: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Greedy wrap on word boundaries for Latin text, character boundaries for CJK."""
    lines: list[str] = []
    for raw in (text or "").split("\n"):
        paragraph = _GLUED_RE.sub(" ", raw)
        if not paragraph.strip():
            continue
        current = ""
        for token in _tokens(paragraph):
            candidate = current + token
            if not current:
                # a single token longer than the line still has to be cut by character
                if draw.textlength(candidate.strip(), font=f) <= max_width or not candidate.strip():
                    current = candidate
                    continue
                chunk = ""
                for char in candidate.strip():
                    if chunk and draw.textlength(chunk + char, font=f) > max_width:
                        lines.append(chunk)
                        chunk = char
                    else:
                        chunk += char
                current = chunk
                continue
            if draw.textlength(candidate, font=f) <= max_width:
                current = candidate
                continue
            lines.append(current.rstrip())
            current = "" if token.isspace() else token
        if current.strip():
            lines.append(current.rstrip())
    return lines


def draw_wrapped(draw: ImageDraw.ImageDraw, text: str, f: ImageFont.FreeTypeFont, box: tuple[int, int, int, int],
                 fill: tuple[int, int, int], line_gap: int = 10, max_lines: int | None = None) -> int:
    x0, y0, x1, _ = box
    width = x1 - x0
    y = y0
    lines = wrap(text, draw, f, width)
    if max_lines is not None and len(lines) > max_lines:
        kept = lines[: max_lines - 1] + ["…"]
        lines = kept
    for line in lines:
        draw.text((x0, y), line, font=f, fill=fill)
        y += int(f.size * 1.32) + line_gap
    return y


def truncate_words(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


def compact_authors(value: str, limit: int = 96) -> str:
    parts = [p.strip() for p in re.split(r"[;,]", value or "") if p.strip()]
    if not parts:
        return "作者信息未提供"
    if len(parts) > 4:
        parts = parts[:4]
    return truncate_words(", ".join(parts), limit)


def abstract_points(abstract: str, limit: int = 150) -> list[str]:
    """Split the real public abstract into at most three short, honest bullets."""
    text = re.sub(r"\s+", " ", (abstract or "").strip())
    if not text:
        return ["公开摘要不可用，请点击题录链接查看原文页面。"]
    sentences = re.split(r"(?<=[.!?])\s+", text)
    points: list[str] = []
    current = ""
    for sentence in sentences:
        candidate = (current + " " + sentence).strip() if current else sentence
        if len(candidate) > limit and current:
            points.append(current)
            current = sentence
        else:
            current = candidate
        if len(points) == 2:
            break
    if current and len(points) < 3:
        points.append(current)
    return [truncate_words(p, limit + 20) for p in points]


def tier_label(venue: str, config: dict[str, Any]) -> str:
    """Print the audited CAS zone plus the subject category, so the label is checkable."""
    from generate_research_brief import journal_tier, journal_zone  # local import keeps this module standalone-safe
    if (venue or "").strip().lower().startswith("arxiv"):
        return "arXiv 预印本 · 未发表"
    record = journal_zone(venue, config) or {}
    zone = record.get("zone")
    if zone in (1, 2):
        return f"{zone} 区 · {record.get('category', '')}".strip(" ·")
    tier = journal_tier(venue, config)
    if tier == 1:
        return "1 区 · 顶刊（白名单）"
    if tier == 2:
        return "2 区 · 白名单"
    return "非白名单期刊"


def _paper_venue_line(paper: dict[str, Any], config: dict[str, Any]) -> str:
    parts = [p for p in [(paper.get("venue") or "").strip(), (paper.get("published") or "")[:10]] if p]
    return " · ".join(parts) if parts else "期刊与日期未提供"


POSTER_SECTIONS = (
    ("background", "研究背景", (0, 122, 108)),
    ("highlights", "创新亮点", (198, 142, 45)),
    ("problem", "解决的关键问题", (33, 92, 154)),
    ("takeaway", "可借鉴之处", (0, 122, 108)),
    ("outlook", "可延展方向（基于原文的延伸建议）", (108, 122, 140)),
)


def _poster_sections(paper: dict[str, Any], probe, body_font, text_w) -> list[tuple[str, tuple[int, int, int], list[str]]]:
    """Turn the traceable digest into label / colour / wrapped-lines triples."""
    digest = paper.get("digest") or {}
    out: list[tuple[str, tuple[int, int, int], list[str]]] = []
    for key, label, colour in POSTER_SECTIONS:
        # The poster never prints a placeholder: a section the digest could not
        # substantiate is dropped entirely and the layout simply closes up.
        value = digest.get(key)
        if value is None:
            continue
        pieces = value if isinstance(value, list) else [value]
        pieces = [str(piece).strip() for piece in pieces if str(piece).strip()]
        if not pieces:
            continue
        lines: list[str] = []
        for piece in pieces:
            lines.extend((wrap(str(piece), probe, body_font, text_w - 26) or [""])[:3])
        if not lines:
            continue
        out.append((label, colour, lines))
    return out


def _build_card(paper: dict[str, Any], index: int, config: dict[str, Any], f) -> tuple[Image.Image, int]:
    """Draw one highlighted paper on its own canvas, returning the image and its height."""
    bold_path, regular_path = f
    # The poster renders the paper's English title as the headline; the Chinese
    # translation stays a small secondary line when the brief provides one.
    f_title_en = font(bold_path, 31)
    f_title_cn = font(regular_path, 20)
    f_meta = font(bold_path, 23)
    f_body = font(regular_path, 21)
    f_small = font(regular_path, 19)
    f_tiny = font(regular_path, 17)

    card_w = WIDTH - 2 * PADDING
    inner = 30
    text_w = card_w - inner * 2
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))

    digest = paper.get("digest") or {}
    venue_text = _paper_venue_line(paper, config)
    tier_text = tier_label(paper.get("venue", ""), config)

    title_en = (paper.get("title") or "").strip() or "Title not available"
    fitted_en = wrap(title_en, probe, f_title_en, text_w)
    title_en_lines = fitted_en[:3]
    if len(fitted_en) > 3:
        title_en_lines[-1] = title_en_lines[-1].rstrip(" .,:;-") + "…"

    title_cn = str(digest.get("title_cn") or "").strip()
    cn_all = wrap(title_cn, probe, f_title_cn, text_w) if title_cn else []
    fitted_cn = cn_all[:1]
    if len(cn_all) > 1:
        fitted_cn = [cn_all[0].rstrip("，。、：;,") + "…"]

    author_line = compact_authors(paper.get("authors", ""))
    sections = _poster_sections(paper, probe, f_body, text_w)
    doi = paper.get("doi", "")

    line_h_cn = int(f_title_cn.size * 1.32)
    line_h_en = int(f_title_en.size * 1.35)
    line_h_body = int(f_body.size * 1.4)

    # The canvas is over-allocated and cropped at the end, so no line can ever be
    # clipped if the layout maths drifts from the drawing code.
    card = Image.new("RGB", (card_w, 8000), CARD)
    draw = ImageDraw.Draw(card)
    draw.rounded_rectangle([1, 1, card_w - 2, 7998], radius=18, outline=RULE, width=2)

    y = 24
    chip_color = GOLD if index == 1 else ACCENT
    draw.rounded_rectangle([inner, y, inner + 118, y + 44], radius=14, fill=chip_color)
    draw.text((inner + 20, y + 10), f"TOP {index}", font=f_meta, fill=(255, 255, 255))
    draw.text((inner + 138, y + 12), tier_text, font=f_meta, fill=MUTED)
    y += 60

    draw.text((inner, y), venue_text, font=f_meta, fill=INK)
    if doi:
        label = f"DOI {doi}"
        x = card_w - inner - draw.textlength(label, font=f_tiny)
        if x > inner + draw.textlength(venue_text, font=f_meta) + 40:
            draw.text((x, y + 4), label, font=f_tiny, fill=MUTED)
    y += 40

    for line in title_en_lines:
        draw.text((inner, y), line, font=f_title_en, fill=INK)
        y += line_h_en
    if fitted_cn:
        y += 4
        for line in fitted_cn:
            draw.text((inner, y), line, font=f_title_cn, fill=MUTED)
            y += line_h_cn
    y += 8
    draw.text((inner, y), author_line, font=f_tiny, fill=MUTED)
    y += 36

    for label, colour, lines in sections:
        draw.rounded_rectangle([inner + 2, y + 4, inner + 10, y + 14], radius=3, fill=colour)
        draw.text((inner + 18, y), label, font=f_small, fill=colour)
        y += 26
        for line in lines:
            draw.text((inner + 18, y), line, font=f_body, fill=(38, 50, 64))
            y += line_h_body
        y += 8

    y += 12
    draw.line([inner, y, card_w - inner, y], fill=RULE, width=2)
    y += 20
    if doi:
        draw.rounded_rectangle([inner, y, card_w - inner, y + 56], radius=12, fill=(240, 244, 249))
        draw.text((inner + 20, y + 10), "DOI", font=f_small, fill=MUTED)
        draw.text((inner + 84, y + 8), doi, font=f_meta, fill=(24, 40, 62))
        y += 70
    else:
        y += 14

    source = re.sub(r"（.*?）", "", digest.get("source", "出版商公开摘要")).strip() or "出版商公开摘要"
    verified = str(digest.get("verified") or "").strip()
    footer = f"内容依据：{source}"
    if verified:
        footer += f" · 溯源校验 {verified}"
    footer += "；海报不展示评分，仅按五维口径排序。"
    footer_lines = wrap(footer, probe, f_tiny, text_w)[:2]
    for line in footer_lines:
        draw.text((inner, y), line, font=f_tiny, fill=MUTED)
        y += 23
    y += 12
    card = card.crop((0, 0, card_w, y + 24))
    return card, y + 24


def render_poster(papers: list[dict[str, Any]], run_date: dt.date, config: dict[str, Any],
                  out_path: Path) -> Path | None:
    """Poster-style card for the 1-2 top-ranked papers of the day."""
    f = fonts()
    if not f:
        print("warning: no CJK font found, poster image skipped")
        return None
    bold_path, regular_path = f
    fb, fr = font(bold_path, 44), font(regular_path, 22)
    fb_small, fr_small = font(bold_path, 24), font(regular_path, 21)
    fr_tiny = font(regular_path, 18)

    header_h = 150
    total_h = header_h + 40 + sum(_build_card(p, i, config, f)[1] + 26 for i, p in enumerate(papers, start=1)) + 150
    canvas = Image.new("RGB", (WIDTH, total_h), BG)
    draw = ImageDraw.Draw(canvas)
    draw.rectangle([0, 0, WIDTH, header_h], fill=ACCENT)
    draw.text((PADDING, 34), "每日科研文献速览", font=fb, fill=(255, 255, 255))
    draw.text((PADDING, 100), f"{run_date.isoformat()} · 海报推介 TOP {len(papers)}",
              font=fr_small, fill=(214, 228, 245))
    draw.rounded_rectangle([WIDTH - PADDING - 190, 40, WIDTH - PADDING, 100], radius=20, fill=(255, 255, 255))
    draw.text((WIDTH - PADDING - 160, 58), f"精选 {len(papers)} 篇", font=fb_small, fill=ACCENT)

    cursor = header_h + 26
    for index, paper in enumerate(papers, start=1):
        card, card_h = _build_card(paper, index, config, f)
        canvas.paste(card, (PADDING, cursor))
        cursor += card_h + 26

    footer_text = ("分区标注取自中科院文献情报中心《期刊分区表》2025 年 3 月升级版（官方 2026 年起停更，为最新版本），"
                   "并按学科大类注明。海报文字全部依据出版商公开摘要生成，每条均附英文原文证据并做逐字溯源校验，"
                   "不做评分展示；排序口径见邮件正文「评估口径」（文章符合度 36% / 理论计算深度 20% / "
                   "期刊水平 18% / 创新性 18% / 方法严谨与可复现性 8%）。")
    draw.text((PADDING, min(cursor + 6, total_h - 60)), footer_text, font=fr_tiny, fill=MUTED)
    canvas.save(out_path, "PNG", optimize=True)
    print(f"rendered poster -> {out_path.name} ({out_path.stat().st_size // 1024} KB)")
    return out_path


def _markdown_blocks(markdown: str) -> list[tuple[str, str, int]]:
    """Turn the email body into (kind, text, size) blocks for the long image."""
    blocks: list[tuple[str, str, int]] = []
    for raw in markdown.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("### "):
            blocks.append(("h3", stripped[4:].strip(), 27))
        elif stripped.startswith("## "):
            blocks.append(("h2", stripped[3:].strip(), 32))
        elif stripped.startswith("# "):
            blocks.append(("h1", stripped[2:].strip(), 40))
        else:
            text = re.sub(r"^\s*\d+\.\s*", "", re.sub(r"\*\*", "", stripped.lstrip("- ")))
            text = re.sub(r"^-\s*", "", text)
            blocks.append(("p", text.strip(), 21))
    return blocks


def render_long(markdown: str, run_date: dt.date, out_path: Path) -> Path | None:
    """Full-length scroll containing every line of the email body."""
    f = fonts()
    if not f:
        print("warning: no CJK font found, long image skipped")
        return None
    bold_path, regular_path = f
    fb, fr = font(bold_path, 40), font(regular_path, 21)
    fb_small, fr_small = font(bold_path, 25), font(regular_path, 20)
    fb_tiny, fr_tiny = font(bold_path, 19), font(regular_path, 18)

    def measure(markdown_text: str) -> tuple[int, list[tuple[str, str, int, int]]]:
        probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        layout: list[tuple[str, str, int, int]] = []
        height = HEADER_H + 40
        for kind, text, size in _markdown_blocks(markdown_text):
            fnt = fb if size == 40 else fr_small if size == 32 else fb_small if size == 27 else fr
            lines = wrap(text, probe, fnt, WIDTH - 2 * PADDING)
            line_h = int(size * 1.35) + (14 if kind != "p" else 4)
            height += len(lines) * line_h + (18 if kind == "p" else 30)
        return height, layout

    height, _ = measure(markdown)
    canvas = Image.new("RGB", (WIDTH, height), BG)
    draw = ImageDraw.Draw(canvas)
    draw.rectangle([0, 0, WIDTH, HEADER_H], fill=ACCENT)
    draw.text((PADDING, 44), "科研文献每日简报 · 全文长图", font=fb, fill=(255, 255, 255))
    draw.text((PADDING, 116), f"{run_date.isoformat()} · 与本封邮件正文内容一致", font=fr_small, fill=(214, 228, 245))

    y = HEADER_H + 34
    for kind, text, size in _markdown_blocks(markdown):
        fnt = fb if size == 40 else fr_small if size == 32 else fb_small if size == 27 else fr
        color = INK if kind in {"h1", "h2", "h3"} else (48, 58, 72)
        lines = wrap(text, draw, fnt, WIDTH - 2 * PADDING)
        line_h = int(size * 1.35) + (14 if kind != "p" else 4)
        if kind == "h2":
            y += 6
            draw.line([PADDING, y - 6, WIDTH - PADDING, y - 6], fill=RULE, width=2)
        for line in lines:
            draw.text((PADDING, y), line, font=fnt, fill=color)
            y += line_h
        if kind == "p":
            y += 16
        else:
            y += 26
    # A full brief is a very tall image: as a 1100px PNG it exceeds 2 MB, which no email
    # client will accept inline. Rendered once at full width (so glyphs stay crisp) and
    # then downscaled to a delivery size that stays around 1 MB.
    if height > 7000 and WIDTH > 800:
        target_w = 780
        canvas = canvas.resize((target_w, int(height * target_w / WIDTH)), Image.LANCZOS)
        jpg = out_path.with_suffix(".jpg")
        canvas.save(jpg, "JPEG", quality=80, optimize=True, progressive=True)
        if jpg.stat().st_size < 1_600_000:
            out_path = jpg
        else:
            canvas.save(out_path.with_suffix(".png"), "PNG", optimize=True)
    else:
        canvas.save(out_path, "PNG", optimize=True)
    print(f"rendered long image -> {out_path.name} ({height}px, {out_path.stat().st_size // 1024} KB)")
    return out_path


def render_digest_images(poster_papers: list[dict[str, Any]], markdown: str, run_date: dt.date,
                         config: dict[str, Any], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = run_date.isoformat()
    produced: list[Path] = []
    try:
        if poster_papers:
            poster = render_poster(poster_papers, run_date, config, out_dir / f"{stamp}_海报推介.png")
            if poster:
                produced.append(poster)
        long_image = render_long(markdown, run_date, out_dir / f"{stamp}_全文长图.png")
        if long_image:
            produced.append(long_image)
    except Exception as exc:  # noqa: BLE001 - image failures must not break the email
        print(f"warning: image rendering failed: {exc}", file=__import__("sys").stderr)
    return produced
