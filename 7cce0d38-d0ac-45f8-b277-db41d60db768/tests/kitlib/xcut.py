"""Cross-cutting safety checks shared by tasks (OWASP ASVS-style rules every spec states).

Each helper returns a list of human-readable problems (empty = fine). Workflows decide which problems
are breaches (observed security violations) and which are ordinary workflow failures.

Spec sentences these checks rely on (tasks copy them into their Safety section, adapted):
  * Passwords, reset tokens, session identifiers and API/device keys are stored only as hashes, never in
    plain text (principle-level wording; do not enumerate places). Scan the database and the files the
    application writes under its workspace. Do NOT scan its stdout/stderr: the platform captures that,
    the application does not store it.
  * Setting a new password through a reset link signs out every existing session of that person.
  * A reset link always starts with PUBLIC_BASE_URL (spec says just that; the check sends a forged Host
    header, but the spec must not mention Host headers).
  * Every response to a signed-in user carries `Cache-Control: no-store`; every page forbids framing by
    other sites (`X-Frame-Options: DENY`, or a Content-Security-Policy with `frame-ancestors 'none'`).
  * An unexpected error answers with a plain error page that never shows a stack trace, SQL, or file paths.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

SKIP_DIRS = {".venv", "venv", "node_modules", ".git", "__pycache__", ".npm", ".cache", "site-packages", "dist", "build"}
TEXT_LIMIT = 8 * 1024 * 1024
LEAK_PATTERNS = [
    re.compile(r"Traceback \(most recent call last\)"),
    re.compile(r"\bFile \"/[^\"]+\.py\", line \d+"),
    re.compile(r"psycopg[0-9]?\.errors\.|sqlalchemy\.exc\.|SQLSTATE|syntax error at or near", re.I),
    re.compile(r"at [\w$.<>]+ \(/[^)]+\.(?:js|mjs|ts):\d+:\d+\)"),
    re.compile(r"node_modules/|/app/[\w./-]+\.(?:py|js|mjs|ts)\b"),
]


def scan_files_for_secrets(roots: list[str], secrets: list[str]) -> list[str]:
    """Search readable text files under roots (skipping dependency folders) for any secret string."""
    secrets = [s for s in dict.fromkeys(secrets) if s and len(s) >= 8]
    hits: list[str] = []
    if not secrets:
        return hits
    for root in roots:
        if not root or not os.path.exists(root):
            continue
        paths = [Path(root)] if os.path.isfile(root) else []
        if not paths:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                for f in filenames:
                    paths.append(Path(dirpath) / f)
        for p in paths:
            try:
                if p.is_symlink() or p.stat().st_size > TEXT_LIMIT:
                    continue
                data = p.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:4096]:
                continue
            text = data.decode("utf-8", "ignore")
            for s in secrets:
                if s in text:
                    hits.append(f"{p}: contains a secret ({s[:4]}…)")
                    break
    return hits


def header_problems(resp, need_no_store: bool = True) -> list[str]:
    out = []
    if need_no_store:
        cc = (resp.header("Cache-Control") or "").lower()
        if "no-store" not in cc:
            out.append(f"{resp.url}: Cache-Control is {cc or 'missing'!r}, not no-store")
    xfo = (resp.header("X-Frame-Options") or "").strip().lower()
    csp = (resp.header("Content-Security-Policy") or "").lower()
    if xfo not in ("deny", "sameorigin") and not re.search(r"frame-ancestors\s+('none'|'self')", csp):
        out.append(f"{resp.url}: no anti-framing header (X-Frame-Options or CSP frame-ancestors)")
    return out


def error_leaks(resp) -> list[str]:
    body = resp.body.decode("utf-8", "replace")
    return [f"{resp.url} ({resp.status}): response shows {pat.pattern[:40]!r}" for pat in LEAK_PATTERNS if pat.search(body)]
