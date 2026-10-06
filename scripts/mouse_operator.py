#!/usr/bin/env python3
"""Drive the machine's *real* mouse - the same cursor a person moves.

Playwright's ``page.mouse`` only dispatches browser input events, which is not enough for
sites whose challenge needs a genuine pointer gesture (and cannot be reached from the DOM).
This module moves the actual system cursor with ``pyautogui``: smooth multi-step paths,
slight hand tremor on the way in, a pause before the click, then a real left-button press.

Only two things are exported that matter to callers:

* :func:`move_to` - glide the cursor to an absolute screen position like a human.
* :func:`click`   - glide, hesitate, then press the left button once.

Screen coordinates are the OS's own cursor coordinates. ``pyautogui`` writes them with
``SetCursorPos``, which the platform interprets in **device** pixels, and this display runs at
150% scaling (``devicePixelRatio`` 1.5), so they are not the same numbers the page sees: a
real click at (500,500) here reached the browser as viewport (316,180) (see
:func:`fetch_browser_paper.screen_origin`, which measures the mapping instead of assuming it).
"""

from __future__ import annotations

import random
import time
from pathlib import Path
from typing import Iterable

try:
    import pyautogui
except ImportError:  # pragma: no cover - the browser path degrades without it
    pyautogui = None  # type: ignore[assignment]

# Moving to the corner is pyautogui's emergency stop; nothing here should ever abort a run.
if pyautogui is not None:
    pyautogui.FAILSAFE = False

JITTER = 2.0          # pixels of hand tremor while gliding
TREMOR_STEPS = 3      # small wobbles inserted along the path
PRE_CLICK_PAUSE = 0.35  # seconds the cursor rests before pressing


def available() -> bool:
    """True when the machine really can move a physical cursor."""
    return pyautogui is not None


def position() -> tuple[float, float]:
    """Where the real cursor sits right now."""
    if not available():
        return (0.0, 0.0)
    spot = pyautogui.position()
    return float(spot[0]), float(spot[1])


def _glide(x: float, y: float, duration: float) -> None:
    """Move toward (x, y) along a slightly wobbly curve instead of teleporting.

    ``pyautogui.moveTo`` costs ~10 ms of round-trip per call on this machine, so the path is
    cut into a handful of waypoints rather than dozens of frames: the eye cannot tell, and
    an automated click must not spend four seconds crawling across the screen.
    """
    start = position()
    span = max(abs(x - start[0]), abs(y - start[1]))
    steps = 3 if span < 80 else (5 if span < 400 else 7)
    seg = max(0.01, duration / steps)
    for step in range(1, steps + 1):
        t = step / steps
        # ease-in-out keeps the pointer from arriving like a robot
        eased = t * t * (3 - 2 * t)
        wobble = JITTER if step < steps else 0.0
        px = start[0] + (x - start[0]) * eased + random.uniform(-wobble, wobble)
        py = start[1] + (y - start[1]) * eased + random.uniform(-wobble, wobble)
        pyautogui.moveTo(px, py, duration=seg)
    pyautogui.moveTo(x, y, duration=0.02)


def move_to(x: float, y: float, duration: float = 1.1) -> None:
    """Glide the real cursor to an absolute screen position."""
    if not available():
        raise RuntimeError("pyautogui is not installed - cannot move the real mouse")
    _glide(x, y, duration)
    time.sleep(0.08)


def move_along(points: Iterable[tuple[float, float]], duration: float = 0.9) -> None:
    """Walk the cursor through a path (used to look like browsing before a click)."""
    for point in points:
        move_to(point[0], point[1], duration)


def click(x: float, y: float, duration: float = 1.1) -> None:
    """Real click: glide in, rest on the target, then press once and let go."""
    if not available():
        raise RuntimeError("pyautogui is not installed - cannot click the real mouse")
    move_to(x, y, duration)
    # a human does not press the instant the pointer stops
    time.sleep(PRE_CLICK_PAUSE + random.uniform(0, 0.25))
    pyautogui.click(x, y)


def double_click(x: float, y: float, duration: float = 1.1) -> None:
    """Real double click, used for the occasional "open in new tab" control."""
    if not available():
        raise RuntimeError("pyautogui is not installed - cannot click the real mouse")
    move_to(x, y, duration)
    time.sleep(PRE_CLICK_PAUSE)
    pyautogui.doubleClick(x, y)


def drag_to(x: float, y: float, duration: float = 1.4) -> None:
    """Press, drag, release - the gesture Cloudflare uses for its slider challenges."""
    if not available():
        raise RuntimeError("pyautogui is not installed - cannot drag the real mouse")
    start = position()
    pyautogui.mouseDown()
    steps = 24
    for step in range(1, steps + 1):
        t = step / steps
        eased = t * t * (3 - 2 * t)
        pyautogui.moveTo(start[0] + (x - start[0]) * eased,
                         start[1] + (y - start[1]) * eased,
                         duration=duration / steps)
        time.sleep(0.01)
    time.sleep(0.15)
    pyautogui.mouseUp()


def scroll(amount: int = 300) -> None:
    """Spin the wheel, the way the caller scrolls a control into view before aiming at it.

    Positive rolls up, negative down; ``amount`` is the notch count, not pixels.
    """
    if not available():
        raise RuntimeError("pyautogui is not installed - cannot scroll the real wheel")
    pyautogui.scroll(int(amount))
    # the OS needs a frame or two to hand the event to the window under the cursor
    time.sleep(0.2)


def press(key: str) -> None:
    """Tap a single key down and let go (``press('f5')``, ``press('enter')``)."""
    if not available():
        raise RuntimeError("pyautogui is not installed - cannot press a key")
    pyautogui.press(key)


def hotkey(*keys: str, interval: float = 0.04) -> None:
    """Hold the keys down together, e.g. ``hotkey('ctrl', 'c')``, then release."""
    if not available():
        raise RuntimeError("pyautogui is not installed - cannot press a key")
    pyautogui.hotkey(*keys, interval=interval)


def write(text: str, interval: float = 0.04) -> None:
    """Type text one character at a time, the way a person does."""
    if not available():
        raise RuntimeError("pyautogui is not installed - cannot type")
    pyautogui.write(text, interval=interval)


def wait(seconds: float) -> None:
    """Do nothing, the way a person waiting for a challenge to solve would."""
    time.sleep(max(0.0, seconds))


def screenshot_image():
    """The whole screen as a PIL image, for locating a control before clicking it."""
    if not available():
        return None
    return pyautogui.screenshot()


def screenshot(path: str | Path) -> Path:
    """Grab the real screen (cursor included) so the click can be verified afterwards."""
    if not available():
        raise RuntimeError("pyautogui is not installed - cannot screenshot")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    pyautogui.screenshot(str(target))
    return target


def unlocked() -> bool:
    """Leave the machine's cursor where it is (used right before handing control back)."""
    return available()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Move the machine's real mouse.")
    parser.add_argument("--to", nargs=2, type=float, required=True, help="absolute x y")
    parser.add_argument("--screenshot", default="", help="save the screen before moving")
    args = parser.parse_args()
    if args.screenshot:
        screenshot(args.screenshot)
    print("before:", position())
    move_to(args.to[0], args.to[1])
    print("after:", position())
