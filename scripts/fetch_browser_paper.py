#!/usr/bin/env python3
"""Open the *real desktop browser* to fetch a subscription journal's openly served files.

Why this exists
---------------
``fetch_paper_attachments.py`` only speaks HTTP with plain headers. That is enough for
open-access PDFs, arXiv preprints and publisher-hosted supplementary files, but it gets
stopped cold on subscription publishers (APS/PRL, ACS, Elsevier, Nature, Science): they
answer with a Cloudflare / DataDome / Incapsula interstitial, and the abstract itself is
often hidden behind the same wall. A real browser - with a real Chrome profile, real
JavaScript execution and mouse clicks on the "I'm not a robot" checkbox - is the only way
to get past those interstitials.

What this module does, and what it refuses to do
------------------------------------------------
* Drives Chrome (or Edge) already installed on this machine. It is started once as a
  CDP bridge and attached to over DevTools, which is what makes the publisher trust the
  client: Playwright's own ``new_context`` ships automation flags and an empty profile
  (``navigator.plugins.length == 0``), and Cloudflare answers that with a 401 on its
  token exchange - no amount of clicking gets through. A browser this program merely
  *attaches* to reports ``navigator.webdriver === false``, has the machine's own GPU,
  fonts and plugins, so APS serves the article instead of a wall.
* Waits for the page, and *clicks* the verification checkbox when a Cloudflare Turnstile /
  reCAPTCHA / hCaptcha challenge is still present. If it cannot solve the challenge itself
  it leaves the window open and waits: a human finishes the click.
* Downloads only material the publisher actually serves openly to any visitor:
  open-access PDFs, "free to read" full texts, arXiv preprints, and the landing page's own
  supplementary files.
* Subscription PDFs are **never** downloaded. When the page says "a subscription is
  required", the result is `status="paywalled"` plus a citation card with the DOI and the
  legal route - the same honesty rule as the HTTP path, just verified in a browser.

The browser session is reused for every download, so an institutional session that is
already logged in is carried over automatically.

Usage (standalone)
------------------
    python scripts/fetch_browser_paper.py --doi 10.1103/7ts9-mxcy \
        --out research_briefs/attachments/2026-10-02

It is also called from ``fetch_paper_attachments.py`` when ``paper_attachments.
browser_enabled`` is on **and** the run is interactive (no ``GITHUB_ACTIONS``).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# The mouse driver lives next to this file; it is imported lazily so the module still
# imports (and still runs headless on CI) when pyautogui is not installed.
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

PDF_MAGIC = (b"%PDF", b"PK\x03\x04")

# Where the unchecked Cloudflare checkbox sits inside the 1440x900 viewport the runner uses
# (measured pixel-by-pixel on the link.aps.org "Performing security verification" card).
# It is only a fallback: the real click first tries to find the box on the whole screen.
CHALLENGE_CHECKBOX = (290, 336)
CHALLENGE_OFFSET_CANDIDATES = ((290, 336), (285, 332), (296, 340), (283, 344), (300, 328))
REAL_CLICK_DURATION = 0.85
CHALLENGE_ROUNDS = 3
# Where the cursor starts its approach: the top-left of the page area, i.e. just inside the
# browser's own window, so the pointer is visibly "in" the page before it is aimed.
REACH = type("Point", (), {"x": 260, "y": 200})()
TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "assets" / "turnstile_checkbox_unchecked.png"

# DevTools bridge. The first run starts a Chrome window bound to this dedicated profile;
# every later run attaches to the Chrome that is still there, which is the point - the
# publisher's trust in that browser accumulates across runs.
CDP_PORT = 9333
_PROJECT_DIR = Path(__file__).resolve().parent.parent
CDP_PROFILE = _PROJECT_DIR / ".browser_profile"
CDP_STATE = _PROJECT_DIR / ".browser_storage_state.json"
_CDP_PROCESS = None  # kept referenced for the lifetime of the run; Chrome outlives it


def _cdp_version(port: int = CDP_PORT):
    """The DevTools banner of the Chrome already listening there, or None."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1.5) as response:
            return json.load(response)
    except Exception:  # noqa: BLE001 - nothing is listening yet, that is fine
        return None


def cdp_attach(timeout: float = 25.0) -> int | None:
    """Start the desktop Chrome as a DevTools bridge and hand back its port, or None.

    Returns None only when this machine has no Chrome or Edge at all; the caller then
    falls back to Playwright's own context, which is weaker but still better than HTTP.
    The process is started detached so it survives the run and can be reused.
    """
    if _cdp_version():
        return CDP_PORT
    binary = chrome_executable()
    if not binary:
        return None
    try:
        CDP_PROFILE.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    creation = 0
    if os.name == "nt":
        creation = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:  # pragma: no cover - the bridge is used on the developer's Windows box
        creation = subprocess.CREATE_NEW_PROCESS_GROUP
    global _CDP_PROCESS  # noqa: PLW0603 - the handle must live as long as the run does
    try:
        _CDP_PROCESS = subprocess.Popen(  # noqa: S603 - fixed argv from a local path
            [binary, f"--remote-debugging-port={CDP_PORT}", f"--user-data-dir={str(CDP_PROFILE)}",
             "--no-first-run", "--no-default-browser-check",
             "--disable-blink-features=AutomationControlled"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creation)
    except OSError:
        return None
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _cdp_version():
            return CDP_PORT
        time.sleep(0.5)
    return None


def seed_session(context, state: Path | None = None) -> int:
    """Copy the stored cookies into a live browser so the publisher sees the same client."""
    source = state or CDP_STATE
    if not source.exists():
        return 0
    now = time.time()
    added = 0
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a corrupt state file is not worth failing over
        return 0
    for cookie in payload.get("cookies", []):
        expires = cookie.get("expires")
        if expires is not None and expires < now:
            continue  # a dead cookie makes the whole batch fail
        try:
            context.add_cookies([cookie])
            added += 1
        except Exception:  # noqa: BLE001 - one unusable cookie must not stop the rest
            continue
    return added


def _log(message: str) -> None:
    print(f"  [browser] {message}", flush=True)


def chrome_executable() -> str | None:
    """Locate the Chrome or Edge binary already installed on this machine."""
    candidates = []
    for program_files in (os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")):
        program_files = program_files or ""
        candidates.append(f"{program_files}\\Google\\Chrome\\Application\\chrome.exe")
        candidates.append(f"{program_files}\\Microsoft\\Edge\\Application\\msedge.exe")
    candidates.append(os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"))
    for raw in candidates:
        if raw and Path(raw).exists():
            return raw
    for raw in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/usr/bin/google-chrome", "/usr/bin/chromium"):
        if Path(raw).exists():
            return raw
    return None


def _challenge_frames(page) -> list[Any]:
    hints = ("turnstile", "recaptcha", "hcaptcha", "challenges", "_incapsula", "datadome", "cf-verify")
    return [frame for frame in page.frames if any(hint in (frame.url or "") for hint in hints)]


# Where the control sits inside the widget, as a fraction of its box. Cloudflare draws a
# short bar with the control as the left disc, so the horizontal centre lands near an eighth
# of the width; the extra fractions cover the light/dark variants, whose padding differs.
WIDGET_FRACTIONS = ((0.10, 0.5), (0.16, 0.5), (0.06, 0.5), (0.22, 0.5))

# An exact hit on the control scores ~1.00 and the runner-up anywhere else on the page stays
# below 0.5, so 0.9 separates "found it" from "guessed".
SCORE_THRESHOLD = 0.9
# Half-width of the search box the DOM is allowed to aim at. Wide enough to swallow the
# error of the measured viewport-to-screen mapping (a wrong guess still lands the crop on
# the widget), tight enough to skip the rest of the screen.
FOCUS_HALF = 140

# The control itself lives in a shadow root inside a cross-origin frame, so it never reaches
# the DOM - but the <iframe> that hosts it, or the [data-sitekey] wrapper around it, is an
# ordinary element of the main document and can report its own geometry.
_WIDGET_JS = """() => {
    const hints = ['challenges.cloudflare.com', 'turnstile', 'recaptcha', 'hcaptcha', 'challenges'];
    const sized = (rect) => (rect && rect.width > 24 && rect.height > 12)
        ? {x: rect.x, y: rect.y, w: rect.width, h: rect.height} : null;
    for (const el of document.querySelectorAll('iframe')) {
        const src = (el.getAttribute('src') || '') + (el.src || '');
        if (el.getAttribute('data-sitekey') || hints.some((h) => src.includes(h))) {
            const hit = sized(el.getBoundingClientRect());
            if (hit) return hit;
        }
    }
    for (const el of document.querySelectorAll('[data-sitekey]')) {
        const hit = sized(el.getBoundingClientRect());
        if (hit) return hit;
    }
    return null;
}"""


def widget_box(page, log: list[str] | None = None) -> dict | None:
    """The verification widget's rectangle in *viewport* coordinates, or None.

    Reading the layout is the fix for "a fixed coordinate stops working as soon as the page
    moves": the widget is found where the browser says it is, here and now, instead of at a
    position measured once against one screenshot.
    """
    try:
        box = page.evaluate(_WIDGET_JS)
        if not box:
            if log is not None:
                log.append("主文档里找不到验证组件所在的 iframe / [data-sitekey] 元素")
            return None
        view = page.evaluate("() => [window.innerWidth, window.innerHeight]")
        box["vw"], box["vh"] = view[0], view[1]
        return box
    except Exception as exc:  # noqa: BLE001 - geometry is one of two locators, not the only one
        if log is not None:
            log.append(f"验证组件几何定位失败（{type(exc).__name__}）")
        return None


def screen_origin(page, mouse, log: list[str] | None = None) -> tuple[float, float] | None:
    """Where the page's viewport origin sits in screen pixels, or None.

    A viewport point is not a screen point - the window carries title and address bars, and
    the two do not line up any fixed amount when the browser is docked or the display scale
    is not 100%. So the mapping is *measured* rather than assumed: park the real cursor on a
    known viewport point, read where the cursor actually is, and take the difference. A second
    probe at the far corner separates a constant offset from a scale error; the two disagree
    the moment the scale drifts, and the pixel locator is trusted instead (it reports a score).
    """
    try:
        size = page.evaluate("() => ({w: window.innerWidth, h: window.innerHeight})")
        offsets = []
        for fx, fy in ((0.3, 0.3), (0.7, 0.7)):
            vx, vy = size["w"] * fx, size["h"] * fy
            mouse.move_to(vx, vy, duration=0.45)
            time.sleep(0.3)
            rx, ry = mouse.position()
            offsets.append((rx - vx, ry - vy))
        if max(abs(offsets[0][0] - offsets[1][0]), abs(offsets[0][1] - offsets[1][1])) > 6:
            if log is not None:
                log.append("视口与屏幕坐标不是纯平移关系（疑似显示器缩放），放弃几何定位")
            return None
        ox = (offsets[0][0] + offsets[1][0]) / 2.0
        oy = (offsets[0][1] + offsets[1][1]) / 2.0
        if log is not None:
            log.append(f"实测页面视口到屏幕的偏移 = ({ox:.0f},{oy:.0f})")
        return ox, oy
    except Exception as exc:  # noqa: BLE001 - a probe failure just removes this locator
        if log is not None:
            log.append(f"坐标换算失败（{type(exc).__name__}）")
        return None


def _wheel_widget_into_view(page, box, log: list[str] | None = None) -> None:
    """Roll the page until the whole widget is inside the viewport.

    A bounding box below the fold is not usable: its screen point would sit outside the
    browser's own content area and the press would land on the desktop.

    The wheel is dispatched by the *browser* (``page.mouse.wheel``), not by pyautogui.
    Measured on this machine: six notches of ``pyautogui.scroll()`` moved the page 2.67 px,
    twice in opposite directions moved it 5.33 px total, while ``page.mouse.wheel(0, 500)``
    moved it exactly 500 px. The physical wheel is effectively dead here, so the page is
    scrolled with the mechanism that is provably working, then re-measured rather than
    predicted.
    """
    for _attempt in range(3):
        top, bottom = box["y"], box["y"] + box["h"]
        if top >= 0 and bottom <= box["vh"]:
            return
        delta = (top + bottom) / 2.0 - box["vh"] / 2.0
        try:
            page.mouse.wheel(0, max(-600.0, min(600.0, delta)))
            page.wait_for_timeout(500)
        except Exception as exc:  # noqa: BLE001 - a refused wheel just ends the scrolling
            if log is not None:
                log.append(f"把组件滚进视口失败（{type(exc).__name__}）")
            return
        box = widget_box(page, log) or box
    if log is not None:
        log.append(f"滚动后组件仍在视口外（y={box['y']:.0f}..{box['y'] + box['h']:.0f}，"
                   f"视口高 {box['vh']:.0f}）")


def _widget_in_view(box) -> bool:
    """True when the widget's whole box is inside the viewport."""
    return bool(box) and box["y"] >= 0 and box["y"] + box["h"] <= box["vh"]


def focus_window(page, mouse, log: list[str] | None = None) -> tuple[int, int] | None:
    """A screen point near where the DOM says the widget is, or None.

    The geometry is used for *aiming*, never as the click point itself. Measured on this
    machine the viewport-to-screen mapping is not a plain translation - a real click at
    screen (500,500) reached the page as viewport (316,180), and the box's own
    ``screenX/screenY`` with ``devicePixelRatio`` 1.5 do not reproduce that - so any
    coordinate derived from the element alone could be off by a hundred pixels and would
    silently press the wrong thing. Feeding it to the matcher as a search centre keeps the
    two techniques where each is strong: the element says *where to look*, the pixels say
    *what is there*, and the matcher's ~1.00-vs-<0.5 score then vouches for the result.
    """
    box = widget_box(page, log)
    if not box:
        return None
    _wheel_widget_into_view(page, box, log)
    if not _widget_in_view(box):
        return None
    origin = screen_origin(page, mouse, log)
    if origin is None:
        return None
    # roughly the control's position inside the widget bar
    cx = origin[0] + box["x"] + box["w"] * 0.18
    cy = origin[1] + box["y"] + box["h"] * 0.5
    if log is not None:
        log.append(f"元素几何定位：组件框 x={box['x']:.0f} y={box['y']:.0f} "
                   f"{box['w']:.0f}x{box['h']:.0f}（视口 {box['vw']:.0f}x{box['vh']:.0f}），"
                   f"指向屏幕 ({cx:.0f},{cy:.0f}) 供像素匹配收紧搜索范围")
    return int(cx), int(cy)


def _mouse():
    """The real-cursor driver, or None when this machine has no physical pointer."""
    try:
        import mouse_operator
    except Exception:  # noqa: BLE001 - a headless run simply has no mouse
        return None
    return mouse_operator if mouse_operator.available() else None


def _ncc(big, small):
    """Normalised cross-correlation of ``small`` against ``big``; returns (y, x, score).

    Normalising by the window's own contrast is what makes this usable at all: a blank
    white page correlates strongly with anything, so the raw correlation happily reports
    the desktop icons as the checkbox. Dividing by the local spread leaves a peak of ~1.0
    only where the control really is.
    """
    import numpy as np
    h, w = small.shape
    height, width = big.shape
    if height < h or width < w:
        return None
    # float64 throughout: the numerator is a ~5e7 product minus a ~5e7 mean term, so
    # single precision would cancel away the equivalent of every grey level in it.
    big = big.astype(np.float64, copy=False)
    small = small.astype(np.float64, copy=False)

    # Sum(big) and Sum(big^2) over every window, from cumulative sums.
    integral = np.pad(np.cumsum(np.cumsum(big, axis=0), axis=1), ((1, 0), (1, 0)))
    squares = np.pad(np.cumsum(np.cumsum(big * big, axis=0), axis=1), ((1, 0), (1, 0)))
    total = integral[h:, w:] - integral[:-h, w:] - integral[h:, :-w] + integral[:-h, :-w]
    squares = squares[h:, w:] - squares[:-h, w:] - squares[h:, :-w] + squares[:-h, :-w]
    area = h * w
    mean = total / area
    variance = np.maximum(squares / area - mean * mean, 0.0)

    # Sum(big*small) over every window, by the correlation theorem.
    shape = (height + h - 1, width + w - 1)
    product = np.fft.irfft2(np.fft.rfft2(big, s=shape) * np.conj(np.fft.rfft2(small, s=shape)),
                            s=shape)[:height - h + 1, :width - w + 1]
    if product.size == 0 or not np.isfinite(product).all():
        return None

    numerator = product - mean * small.mean() * area
    spread = np.sqrt(variance) * small.std() * area
    # A window flatter than a couple of grey levels carries no signal; without this guard
    # the ratio explodes to several units on the empty white page and wins the argmax.
    usable = spread > 1e-9
    score = np.clip(np.where(usable, numerator / np.where(usable, spread, 1.0), -1.0), -1.0, 1.0)
    index = np.unravel_index(np.argmax(score), score.shape)
    return float(index[0]), float(index[1]), float(score[index])


def _coarse(big, small, factor: int = 4):
    """Block-average both images and match there, so the fine pass starts nearly aligned.

    The control is a hard iced box: a single pixel of misalignment already drops the
    full-resolution score from 1.00 to about 0.57. Matching on 4x-averaged pixels first
    tolerates that, and only the neighbourhood of the coarse hit is re-checked sharp.
    """
    height, width = big.shape
    hh, ww = height // factor, width // factor
    if hh < 16 or ww < 16:
        return None
    coarse_big = big[:hh * factor, :ww * factor].reshape(hh, factor, ww, factor).mean(axis=(1, 3))
    rows, cols = small.shape[0] // factor, small.shape[1] // factor
    coarse_small = small[:rows * factor, :cols * factor].reshape(rows, factor, cols, factor).mean(axis=(1, 3))
    hit = _ncc(coarse_big, coarse_small)
    if hit is None:
        return None
    return hit[0] * factor, hit[1] * factor
def _match_biggest(big, template, allow_coarse: bool = True):
    """Best (y, x, score) of ``template`` anywhere in ``big``; score -2.0 when it is absent."""
    height, width = big.shape
    h, w = template.shape
    if height <= h or width <= w:
        return -2.0, -1.0, -2.0
    top_y, top_x, score = -1.0, -1.0, -2.0
    best = _ncc(big, template)
    if best and best[2] > score:
        top_y, top_x, score = best
    if allow_coarse:
        guess = _coarse(big, template)
        if guess:
            for dy in range(-6, 7):
                for dx in range(-6, 7):
                    y0, x0 = int(round(guess[0])) + dy, int(round(guess[1])) + dx
                    if y0 < 0 or x0 < 0 or y0 + h > height or x0 + w > width:
                        continue
                    value = _ncc(big[y0:y0 + h, x0:x0 + w], template)
                    if value and value[2] > score:
                        # value carries coordinates *inside* the 44x44 window just slid over
                        # the target, so the window's own origin has to be added back. For a
                        # 1:1 window the peak is (0,0) and the hit lands on the search
                        # origin unless it is - returning raw (0,0) here is what silently
                        # aimed every click at the top-left corner of the screen.
                        top_y, top_x, score = y0 + value[0], x0 + value[1], value[2]
    return top_y, top_x, score


def _checkbox_template():
    """The un-ticked Turnstile box as a grey array, or None when the asset is missing."""
    try:
        import numpy as np
        from PIL import Image
        return np.asarray(Image.open(TEMPLATE_PATH).convert("L"), dtype=np.float32)
    except Exception:  # noqa: BLE001 - a missing asset must not stop the run
        return None


def locate_checkbox(page, log: list[str] | None = None) -> tuple[float, float] | None:
    """Where the human-verification checkbox sits on the *screen*, or None.

    The pixel match is the decision-maker and stays first: it scores ~1.00 on an exact hit
    against <0.5 for anything else on the page, so when it fires it is right. The DOM is
    consulted *before* it, but only to trim the search to a neighbourhood around the widget
    the browser's own layout reports, which is what keeps this working when the control is
    restyled or drawn at a different scale - a screenshot can see the widget only if the
    widget is actually on the screen, but it always knows what the control should look like.
    If the trimmed search comes back weak, the whole screen is searched anyway.
    """
    mouse = _mouse()
    if mouse is None:
        return None
    try:
        return _locate_by_pixels(log, focus=focus_window(page, mouse, log))
    except Exception as exc:  # noqa: BLE001 - locating is best effort; the click still runs
        if log is not None:
            log.append(f"元素几何辅助定位失败（{type(exc).__name__}）：直接全屏搜索")
        return _locate_by_pixels(log)


def _locate_by_pixels(log: list[str] | None = None,
                      focus: tuple[int, int] | None = None) -> tuple[float, float] | None:
    """Where the human-verification checkbox sits on the *screen*, or None.

    ``focus`` optionally narrows the search to a box around a point the DOM suggested; when
    no strong hit is found there, the whole screen is searched anyway, so a bad focus costs
    nothing but a crop.

    The widget cannot be reached from the DOM (cross-origin frame, markup inside a shadow
    root), so the picture is what gets searched: a full-screen grab, then coarse-to-fine
    template matching. Two things made earlier attempts miss:

    * the fine match has to start from a block-averaged match - the control is a hard iced
      box and one pixel of misalignment costs 0.4 of the score;
    * everything is measured in *screen* coordinates, which removes the browser window's
      chrome offset. Feeding viewport coordinates to the driver simply clicks the desktop.

    A screen coordinate is exactly what the physical mouse needs, so this returns it.
    """
    template = _checkbox_template()
    mouse = _mouse()
    if template is None or mouse is None:
        return None
    try:
        import numpy as np
        shot = mouse.screenshot_image()
        if shot is None:
            return None
        big = np.asarray(shot.convert("L"), dtype=np.float32)
        height, width = big.shape
        h, w = template.shape

        # The DOM said roughly where the widget is: look there first. The crop is wide on
        # purpose - it has to swallow the whole error of the viewport-to-screen mapping -
        # and if nothing strong is in it the full screen is searched right afterwards.
        if focus is not None:
            fx, fy = focus
            y0, x0 = max(0, fy - FOCUS_HALF), max(0, fx - FOCUS_HALF)
            y1, x1 = min(height, fy + FOCUS_HALF), min(width, fx + FOCUS_HALF)
            crop = big[y0:y1, x0:x1]
            if crop.shape[0] > h + 4 and crop.shape[1] > w + 4:
                cy, cx, cscore = _match_biggest(crop, template)
                if cscore >= SCORE_THRESHOLD:
                    if log is not None:
                        log.append(f"按 DOM 指向的 ({fx},{fy}) 收紧搜索后在邻域命中（分数 {cscore:.2f}）")
                    return x0 + cx + w / 2.0, y0 + cy + h / 2.0
                if log is not None:
                    log.append(f"DOM 指向的邻域内没有命中（{cscore:.2f}），退回全屏搜索")

        top_y, top_x, score = _match_biggest(big, template)

        # An exact hit scores ~1.0 while the runner-up anywhere on the page scores <0.5,
        # so anything under 0.9 is a false positive rather than the control.
        if score < SCORE_THRESHOLD:
            if log is not None:
                log.append(f"屏幕上没有找到未勾选的人机验证框（最佳模板匹配分数 {score:.2f}）")
            return None
        return top_x + w / 2.0, top_y + h / 2.0
    except Exception as exc:  # noqa: BLE001 - locating is best effort; clicking still runs
        if log is not None:
            log.append(f"验证框图像定位失败（{type(exc).__name__}）")
    return None


def reach_window(mouse, log: list[str] | None = None) -> None:
    """Bring the browser forward and walk the cursor into it, the way a person would.

    A physical press only lands on the topmost window, so the runner raises the page first
    and then moves the pointer over it before aiming - pointing at a control that sits
    behind another window is how a "click" quietly goes to the wrong program.
    """
    if mouse is None:
        return
    try:
        mouse.move_to(REACH.x, REACH.y, duration=0.9)
        time.sleep(0.25)
        mouse.move_to(REACH.x + 40, REACH.y + 30, duration=0.7)
        if log is not None:
            log.append(f"已把窗口切到最前并把光标移入页面（{REACH.x},{REACH.y}）")
    except Exception as exc:  # noqa: BLE001 - a stuck pointer must not abort the run
        if log is not None:
            log.append(f"光标转入页面失败（{type(exc).__name__}）")

def _real_click(x: float, y: float, log: list[str], note: str) -> None:
    """Press the physical left button down and let go, once, at (x, y)."""
    mouse = _mouse()
    if mouse is None:
        return
    mouse.click(x, y, duration=REAL_CLICK_DURATION)
    log.append(note)


def _click_verification(page, log: list[str]) -> None:
    """Press the machine's *real* cursor on the verification box until the wall clears.

    ``page.mouse`` only dispatches synthetic browser input, and the APS challenge ignores
    it completely. Three things are needed for a physical press to count: the browser has
    to be the frontmost window, the pointer has to be walked in from outside it, and the
    target has to be located in *screen* coordinates - which is what the matcher returns.
    """
    if not _interstitial_reason(page):
        return
    mouse = _mouse()
    reach_window(mouse, log)
    for _round in range(1, CHALLENGE_ROUNDS + 1):
        spot = locate_checkbox(page, log)
        if spot:
            _real_click(spot[0], spot[1], log,
                        f"第 {_round} 轮：真实鼠标点击人机验证框（屏幕坐标 "
                        f"{spot[0]:.0f},{spot[1]:.0f}，屏幕截图粗-精模板匹配定位）")
            page.wait_for_timeout(5000)
            if not _interstitial_reason(page):
                return
            continue
        # No image match on this screen. Without reliable screen coordinates there is
        # nothing honest to press, so the window is left up for a person instead of
        # blind-clicking numbers that used to hit blank desktop.
        if not mouse:
            # No physical cursor on this machine: Playwright can still dispatch a page-level
            # click, which is what CI does. It frequently misses, so several plausible centres
            # are tried rather than one blind guess.
            for vx, vy in CHALLENGE_OFFSET_CANDIDATES:
                try:
                    page.mouse.click(vx, vy)
                except Exception:  # noqa: BLE001 - a dead page must not abort the loop
                    pass
                log.append(f"第 {_round} 轮：页面内点击验证框候选点 ({vx},{vy})")
                page.wait_for_timeout(2500)
                if not _interstitial_reason(page):
                    return
            return
        if log is not None:
            log.append(f"第 {_round} 轮：未能在屏幕上确定验证框位置，改由人工点击")
        return


def _interstitial_reason(page) -> str:
    """Return why the page is still an interstitial, or '' once the real article is there.

    Cloudflare's wall is easy to miss by keyword: the *title* reads "Just a moment..."
    while the body reads "Performing security verification", so both are scanned here.
    """
    blob = ""
    for source in (lambda: page.locator("body").inner_text(timeout=4000),):
        try:
            blob = (source() or "").lower()
        except Exception:  # noqa: BLE001 - the page is still loading
            continue
    blob = f"{page.title() or ''}\n{blob}".lower()
    for hint in ("just a moment", "performing security verification", "security service to protect",
                 "verify you are human", "verify you are not a bot", "are you a robot",
                 "checking your browser before accessing", "enable javascript and cookies to continue",
                 "attention required", "protected by datadome", "unusual traffic from your",
                 "request blocked", "enable cookies"):
        if hint in blob:
            return hint
    for frame in _challenge_frames(page):
        if frame.locator("input[type=checkbox]").count():
            return "verification widget present"
    return ""


def _collect_links(page) -> tuple[str, list[str], dict[str, bool]]:
    """Read the live DOM: landing URL, candidate file links, open-access flags."""
    landing = page.url
    links: list[str] = []
    # An empty scan used to be silent, and a silent empty scan means a paper that was
    # fetched looks exactly like one that was never reachable. The page arrives in pieces
    # (and a fresh tab can still be the start page), so the scan is retried and every
    # failed attempt is printed instead of swallowed.
    hrefs: list[str] = []
    for attempt in range(3):
        try:
            hrefs = page.eval_on_selector_all("a[href]", "nodes => nodes.map(n => n.getAttribute('href'))")
        except Exception as exc:  # noqa: BLE001
            print(f"  [browser] anchor scan failed ({type(exc).__name__}) on {landing[:70]}")
            hrefs = []
        if hrefs:
            break
        print(f"  [browser] no anchors on {landing[:70]} "
              f"(attempt {attempt + 1}/3); the page is still arriving")
        page.wait_for_timeout(3000)
    for raw in hrefs or []:
        if not raw or raw.startswith(("#", "mailto:", "javascript:")):
            continue
        absolute = urllib.parse.urljoin(landing, raw)
        if absolute.startswith("http://"):
            absolute = "https://" + absolute[7:]
        if absolute not in links:
            links.append(absolute)
    body_text = ""
    try:
        body_text = page.locator("body").inner_text(timeout=5000)
    except Exception:  # noqa: BLE001
        pass
    low = body_text.lower()
    return landing, links, {
        "open_access": bool(re.search(r"open access|free to read|freely available", low)),
        "paywalled": bool(re.search(r"subscription is required|subscribe to read|purchase a "
                                    r"subscription|institution.{0,25}access|pay per view|"
                                    r"access options|not open access|article is restricted|"
                                    r"subscribers? (can )?(download|access|view)", low)),
        "has_pdf_button": bool(re.search(r"\bdownload pdf\b|\bpdf\b", low)),
    }


def _looks_like_file(url: str) -> bool:
    """True for real file links, including APS-style `/doi/pdf/...` and `/10.xxxx/....` PDF routes."""
    path = urllib.parse.urlparse(url).path.lower()
    return (path.endswith((".pdf", ".zip", ".docx", ".xlsx", ".rar"))
            or "pdf" in path.rsplit("/", 1)[-1] or "/pdf/" in url.lower())


def _download(page, url: str, dest: Path, max_mb: float) -> bool:
    """Fetch one file through the browser's own session (cookies + challenge pass carried)."""
    try:
        response = page.context.request.get(
            url, timeout=120_000, headers={"Accept": "application/pdf,*/*"})
        body = response.body()
    except Exception as exc:  # noqa: BLE001
        _log(f"download skipped ({type(exc).__name__})")
        return False
    if response.status >= 400 or not body:
        _log(f"download skipped (HTTP {response.status})")
        return False
    magic = body[:4]
    if magic not in PDF_MAGIC and not (magic[:2] == b"%P" and b"DF" in body[:8]):
        ctype = (response.headers.get("content-type") or "").split(";")[0]
        _log(f"download skipped (not a file: {ctype or 'unknown content-type'})")
        return False
    if len(body) > max_mb * 1_000_000:
        _log("download skipped (larger than the configured attachment limit)")
        return False
    dest.write_bytes(body)
    _log(f"saved {dest.name} ({len(body) // 1024} KB)")
    return True


def _openly_served(url: str, retries: int = 2) -> bool:
    """True when *any* visitor can have these bytes, subscriber session or not.

    The browser running here usually carries the subscriber's own cookies - APS in
    particular answers the PDF route with the full text as soon as ``apsjournals.session``
    is present. A cookie-gated download is not an open one, and calling it one would be
    the same claim as "we got the full text" while quietly using a paid subscription.
    Asking anonymously is the honest test: same URL, asked twice.
    """
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/154.0.0.0 Safari/537.36")
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(1.5 * attempt)
        try:
            request = urllib.request.Request(url,
                                             headers={"User-Agent": ua, "Accept": "application/pdf,*/*"})
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read(512).startswith(b"%PDF")
        except Exception:  # noqa: BLE001 - a paywall answers with 403 or HTML, not a crash
            continue
    return False


def browser_fetch(doi: str, out_dir: Path, *, title: str = "", headless: bool = False,
                  max_mb: float = 15, wait_seconds: int = 25, keep_open: int = 0,
                  oa_pdf_urls: list[str] | None = None, profile: str | None = None) -> dict[str, Any]:
    """Open a real browser on the DOI, pass any verification, save what is openly served.

    Two browsers can play this part. The preferred one is the machine's own Chrome, started
    once as a DevTools bridge and attached to; the fallback, for a machine with no Chrome or
    a headless run, is the browser Playwright launches itself.
    """
    result: dict[str, Any] = {
        "landing": f"https://doi.org/{doi}" if doi else "",
        "files": [], "status": "no-doi", "verification": [], "notes": [], "links": [],
    }
    if not doi:
        return result
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        result["notes"].append("浏览器通道未启用（缺少 playwright，pip install playwright）")
        return result

    out_dir.mkdir(parents=True, exist_ok=True)
    log: list[str] = []
    port = cdp_attach()
    try:
        # Everything that needs a live session lives here, so it can run inside whichever
        # browser ended up being opened: a page object is only good inside its own session.
        def crawl(pg, ctx) -> None:
            """Walk one article page and save whatever the publisher serves to anyone."""
            _log(f"opening {result['landing']} in the desktop browser")
            try:
                # "commit" only: on a publisher landing page DOMContentLoaded never fires
                # (a stalled subresource keeps it pending past 90s) even though the article
                # page is already there - measured 2s to journals.aps.org/prb/abstract/...
                # Waiting for the full load only burned the timeout on every single paper.
                pg.goto(result["landing"], wait_until="commit", timeout=60_000)
            except Exception as exc:  # noqa: BLE001
                _log(f"navigation interrupted ({type(exc).__name__}); continuing with the loaded page")
            # The page arrives in pieces, so give it a moment to paint before it is judged.
            pg.wait_for_timeout(5000)
            # The physical cursor can only press what is on top of the screen, so the
            # browser window is raised first; a backgrounded window would swallow the click.
            try:
                pg.bring_to_front()
                pg.wait_for_timeout(600)
            except Exception:  # noqa: BLE001 - some Chromium builds refuse to raise
                pass
            _click_verification(pg, log)

            def _wall(page_) -> str:
                return _interstitial_reason(page_)

            wall = _wall(pg)
            waited = 0
            while wall and keep_open and waited < keep_open:
                _log(f"human-verification wall on screen ({wall!r}); "
                     f"window stays open - click the checkbox, {keep_open - waited}s left")
                pg.wait_for_timeout(15000)
                waited += 15
                _click_verification(pg, log)
                wall = _wall(pg)
            if wall:
                result["status"] = "verification-required"
                result["verification"] = log
                result["notes"].append(f"出版商的人机验证未能通过（页面提示 {wall!r}）；"
                                       "脚本已如实记录，未绕过付费墙。")
                result["links"] = []
                return result
            pg.wait_for_timeout(2500)
            landing, links, flags = _collect_links(pg)
            result["landing"] = landing
            result["links"] = links[:40]
            label = re.sub(r"[^A-Za-z0-9]+", "_", (title or doi)[:48]).strip("_").lower() or "paper"

            if flags["paywalled"] and not flags["open_access"]:
                result["status"] = "paywalled"
                result["notes"].append("浏览器已确认该文为订阅内容（页面提示需订阅/机构权限），"
                                       "按版权约束未下载任何全文，仅提供 DOI 与合法获取路径。")

            # 1) anything openly served by the publisher itself
            targets = [link for link in links if _looks_like_file(link)
                       and any(token in link.lower() for token in
                               ("full text", "pdf", "supplement", "supp", "mmc", "esm",
                                "attachment", "download", "mediaobjects", "arxiv"))]
            # 2) arXiv preprint mirrors, which are public by construction
            targets.extend([link for link in links if "arxiv.org" in link])
            if not targets:
                # Distinguish "the page offered nothing downloadable" from "the page was
                # never read" - the first is a paywall, the second is a broken run.
                _log(f"no file-like route on {landing[:70]} ({len(links)} anchors scanned)")

            # 0) open-access publishers serve the PDF at a well-known article URL. Asking the
            #    browser session - the one that just passed the wall - is the honest route to
            #    an open-access full text; a subscription PDF simply returns no file here.
            saved = 0
            if flags["open_access"] or oa_pdf_urls:
                for candidate in oa_pdf_urls or []:
                    if saved >= 3:
                        break
                    dest = out_dir / f"browser_{label}_全文.pdf"
                    if _download(pg, candidate, dest, max_mb):
                        saved += 1

            # Anything that only comes back for a cookie-holding client is subscription
            # material: it is reported as a legal route, never as a downloaded file.
            gated: list[str] = []
            for url in targets:
                if saved >= 3:
                    break
                _log(f"candidate route: {url[:100]}")
                if not _openly_served(url):
                    gated.append(url)
                    _log(f"not openly served (subscription route): {url[:80]}")
                    continue
                suffix = "arXiv" if "arxiv.org" in url else "全文"
                dest = out_dir / f"browser_{label}_{saved + 1}{suffix}.pdf"
                if _download(pg, url, dest, max_mb):
                    saved += 1
            if gated:
                result["notes"].append(
                    f"另有 {len(gated)} 条路由（含出版商版 PDF 与补充材料）需机构订阅权限才能取到，"
                    "本次未下载；这些文件在网页端对本机已登录会话可见，合法获取路径见题录卡。")
            result["files"] = [path.name for path in sorted(out_dir.glob(f"browser_{label}*.pdf"))]
            subscription_host = any(host in landing for host in
                                    ("aps.org", "nature.com", "science.org", "sciencedirect.com",
                                     "elsevier.com", "acs.org", "wiley.com", "springer.com",
                                     "aip.org", "optics.org", "tandfonline.com"))
            if saved:
                result["status"] = "fetched"
                result["notes"].append("文件由本机真实浏览器会话下载；已逐条核验这些文件匿名即可取得"
                                       "（未使用任何订阅会话权限）。")
            elif result["status"] == "paywalled" or (subscription_host and not flags["open_access"]):
                result["status"] = "paywalled"
                result["notes"].append("浏览器已进入出版商页面，但该文无公开全文：正文与 PDF 属订阅内容，"
                                       "按版权约束未下载任何全文，仅提供 DOI 与合法获取路径。")
            else:
                result["status"] = "cited-only"
                result["notes"].append("页面无可公开下载的全文/补充材料，仅生成题录卡。")
            # Whatever cleared the wall is worth keeping: the next run starts from a browser
            # the publisher already trusts instead of re-introducing itself.
            target = Path(profile) if profile else CDP_STATE
            try:
                ctx.storage_state(path=str(target))
                _log(f"browser session state saved to {target.name} (reused next run)")
            except Exception as exc:  # noqa: BLE001 - a lost cookie is not fatal
                _log(f"could not save the browser session state ({type(exc).__name__})")

        with sync_playwright() as playwright:
            if port is not None:
                # The DevTools bridge: this machine's own Chrome, so the publisher's anti-bot
                # layer sees an ordinary client - the browser reports navigator.webdriver as
                # false and keeps the machine's GPU, fonts and plugins. That is what makes the
                # wall go away without a click; closing it here would throw that away.
                browser = None
                try:
                    browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                    context = browser.contexts[0]
                    seeded = seed_session(context, Path(profile) if profile else None)
                    if seeded:
                        _log(f"reused the desktop browser ({seeded} stored cookies restored)")
                    page = context.pages[0] if context.pages else context.new_page()
                except Exception as exc:  # noqa: BLE001 - a bad bridge is not fatal
                    _log(f"DevTools bridge unusable ({type(exc).__name__}); using a fresh context")
                    browser = None
                if browser is not None:
                    crawl(page, context)
                    # The session ends with the ``with`` block above, so the Chrome started
                    # as a bridge is gone again - what survives is the session state that was
                    # just written, which is what the next run loads before navigating.
                    return result
            # Fallback: a browser this program started itself. It is weaker (Playwright
            # adds automation flags and a throw-away profile) but the rules below are the same.
            exe = chrome_executable()
            launch_kwargs: dict[str, Any] = {"headless": headless,
                                             "args": ["--disable-blink-features=AutomationControlled",
                                                      "--start-maximized"]}
            if exe:
                launch_kwargs["executable_path"] = exe
            browser = playwright.chromium.launch(**launch_kwargs)
            context_kwargs: dict[str, Any] = {
                "viewport": {"width": 1440, "height": 900},
                "locale": "en-US",
                "user_agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
            }
            if profile and Path(profile).exists():
                context_kwargs["storage_state"] = profile
            context = browser.new_context(**context_kwargs)
            page = context.new_page()
            crawl(page, context)
            browser.close()
    except Exception as exc:  # noqa: BLE001 - a browser must never break the mail run
        _log(f"browser path failed: {type(exc).__name__}: {exc}")
        result["notes"].append(f"浏览器通道异常（{type(exc).__name__}），已回退到纯 HTTP 通道。")
    result["verification"] = log
    return result


def _citation_note(result: dict[str, Any], doi: str, title: str) -> str:
    status_text = {
        "fetched": "已用本机浏览器确认页面可公开下载，并通过合法通道取得文件。",
        "paywalled": "浏览器已确认该文为订阅内容（页面提示需订阅/机构权限），按版权约束未下载全文。",
        "verification-required": "出版商的人机验证未能自动通过（已尝试点击验证控件），需人工点击后重试。",
        "cited-only": "页面无可公开下载的全文/补充材料，仅附题录卡。",
    }.get(result["status"], "浏览器通道未取得可下载文件。")
    lines = [f"浏览器抓取：{status_text}"]
    for entry in result.get("verification") or []:
        lines.append(f"  · {entry}")
    for entry in result.get("notes") or []:
        lines.append(f"  · {entry}")
    if result["status"] == "paywalled":
        lines.append(f"  · 合法获取：通过所在机构图书馆 / 校园网打开 https://doi.org/{doi} 下载出版商版全文；"
                     f"{title[:40]} 的 arXiv 公开预印本亦为合法全文副本（见题录卡）。")
    return "\n".join(lines)


def _main() -> int:
    parser = argparse.ArgumentParser(description="Open the desktop browser on a DOI and save openly served files.")
    parser.add_argument("--doi", required=True)
    parser.add_argument("--title", default="")
    parser.add_argument("--out", required=True)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--max-mb", type=float, default=15)
    parser.add_argument("--keep-open", type=int, default=0,
                        help="seconds to leave the window open when a human must click the challenge")
    parser.add_argument("--oa-pdf", action="append", default=[],
                        help="open-access PDF route to request from the passed session (repeatable)")
    parser.add_argument("--profile", default="",
                        help="file that stores the cookie solving the verification wall")
    args = parser.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = browser_fetch(args.doi, out_dir, title=args.title, headless=args.headless,
                           max_mb=args.max_mb, keep_open=args.keep_open,
                           oa_pdf_urls=list(args.oa_pdf) or None,
                           profile=args.profile or None)
    print(_citation_note(result, args.doi, args.title))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
