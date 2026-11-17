"""Playwright helpers: real-browser sessions, layout checks, XSS beacons, judge evidence capture.

XSS detection is behavioural: every injected payload calls `window.__fk_xss(<tag>)`. The page binding
records the call, so any script the application lets run is observed no matter which sink it reached.
The application's own dialogs (a confirm before a destructive action, say) are accepted, never counted:
only a dialog whose message carries a payload tag is an XSS hit.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

UX_DIR = Path(os.environ.get("FK_UX_DIR", "/logs/verifier/ux"))

XSS_PAYLOADS = [
    '<img src=x onerror="window.__fk_xss&&__fk_xss(\'{tag}-img\')">',
    "<script>window.__fk_xss&&__fk_xss('{tag}-script')</script>",
    "\"><svg onload=\"window.__fk_xss&&__fk_xss('{tag}-svg')\">",
    "'><img src=x onerror=window.__fk_xss&&__fk_xss('{tag}-sq')>",
]


def payload(tag: str, i: int = 0) -> str:
    return XSS_PAYLOADS[i % len(XSS_PAYLOADS)].replace("{tag}", tag)


class Browser:
    def __init__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=True, args=["--no-sandbox"])
        self.xss_hits: list[str] = []

    def close(self):
        try:
            self.browser.close()
        finally:
            self._pw.stop()

    def context(self, width: int = 1280, height: int = 900, cookies: list | None = None):
        ctx = self.browser.new_context(viewport={"width": width, "height": height}, ignore_https_errors=True)
        ctx.expose_function("__fk_xss", lambda tag: self.xss_hits.append(str(tag)))
        if cookies:
            ctx.add_cookies(cookies)
        return ctx

    def page(self, ctx):
        p = ctx.new_page()
        p.on("dialog", lambda d: ((self.xss_hits.append("dialog:" + d.message[:80]) if "__fk" in d.message else None),
                                  d.accept()))
        p.set_default_timeout(60000)  # generous: a check must never be stricter than a time limit the spec states
        return p


def horizontal_overflow(page) -> int:
    """Pixels of horizontal overflow of the document at the current viewport (0 = none)."""
    return int(page.evaluate(
        "() => Math.max(document.documentElement.scrollWidth, document.body ? document.body.scrollWidth : 0)"
        " - window.innerWidth"))


MAX_SHOT_PX = 6000  # JPEG cannot exceed 65535 px; judges read the top of long pages plus focus shots


def _shot(page, path: Path, focus: list[str]) -> None:
    height = int(page.evaluate("() => Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0)"))
    width = (page.viewport_size or {"width": 1280})["width"]
    page.screenshot(path=str(path), full_page=True, type="jpeg", quality=70,
                    clip={"x": 0, "y": 0, "width": width, "height": min(max(height, 1), MAX_SHOT_PX)})
    for i, sel in enumerate(focus):
        try:
            box = page.locator(sel).first.bounding_box(timeout=3000)
        except Exception:
            box = None
        if not box:
            continue
        y0 = max(0, box["y"] + page.evaluate("() => window.scrollY") - 900)
        page.screenshot(path=str(path.with_name(f"{path.stem}-focus{i}.jpg")), full_page=True, type="jpeg", quality=70,
                        clip={"x": 0, "y": y0, "width": width, "height": min(1400, max(1, height - y0))})


def capture(page, checkpoint: str, truth: dict | None = None, narrow: bool = True, focus: list[str] | None = None) -> Path:
    """Judge evidence under UX_DIR/<checkpoint>/: desktop.jpg, narrow.jpg (each clipped to MAX_SHOT_PX), optional
    *-focus<i>.jpg around the given selectors, visible.txt (top of the page plus the text around focus selectors),
    url.txt and truth.json."""
    d = UX_DIR / checkpoint
    d.mkdir(parents=True, exist_ok=True)
    focus = focus or []
    vp = page.viewport_size or {"width": 1280, "height": 900}
    page.set_viewport_size({"width": 1280, "height": 900})
    _shot(page, d / "desktop.jpg", focus)
    if narrow:
        page.set_viewport_size({"width": 390, "height": 844})
        _shot(page, d / "narrow.jpg", focus)
    page.set_viewport_size(vp)
    text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
    text = re.sub(r"\n{3,}", "\n\n", text)
    parts = [text[:12000]]
    for sel in focus:
        try:
            near = page.evaluate(
                "(sel) => { const el = document.querySelector(sel); if (!el) return '';"
                " let n = el; for (let i = 0; i < 4 && n.parentElement; i++) n = n.parentElement; return n.innerText; }", sel)
        except Exception:
            near = ""
        if near:
            parts.append(f"\n--- text around {sel} ---\n" + near[:4000])
    (d / "visible.txt").write_text("".join(parts)[:20000], encoding="utf-8")
    (d / "url.txt").write_text(page.url, encoding="utf-8")
    if truth is not None:
        (d / "truth.json").write_text(json.dumps(truth, indent=1, default=str), encoding="utf-8")
    return d


def capture_text(checkpoint: str, text: str, truth: dict | None = None) -> Path:
    d = UX_DIR / checkpoint
    d.mkdir(parents=True, exist_ok=True)
    (d / "visible.txt").write_text(text[:20000], encoding="utf-8")
    if truth is not None:
        (d / "truth.json").write_text(json.dumps(truth, indent=1, default=str), encoding="utf-8")
    return d


def mark_unreached(checkpoints: list[str]) -> None:
    """A checkpoint the run never reached still gets evidence, so the judge scores it as missing."""
    for c in checkpoints:
        d = UX_DIR / c
        if not (d / "visible.txt").exists():
            d.mkdir(parents=True, exist_ok=True)
            (d / "visible.txt").write_text("CHECKPOINT NOT REACHED: the application never showed this page.\n")
