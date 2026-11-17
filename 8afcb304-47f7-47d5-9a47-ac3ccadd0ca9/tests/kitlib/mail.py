"""Mail sink client (the kit's SMTP sink: GET /api/v1/messages, GET /api/v1/message/<id>)."""

from __future__ import annotations

import json
import re
import time
import urllib.request


class Mail:
    def __init__(self, http_url: str):
        self.url = http_url.rstrip("/")

    def _get(self, path: str):
        with urllib.request.urlopen(self.url + path, timeout=20) as r:
            return json.loads(r.read().decode())

    _cache: dict = {}

    def _full(self, mid: str) -> dict:
        if mid not in self._cache:
            full = self._get("/api/v1/message/" + str(mid))
            self._cache[mid] = {
                "id": full.get("ID"), "subject": full.get("Subject", ""), "text": self._text_of(full),
                "html": full.get("HTML") or "", "message_id": full.get("MessageID", ""), "created": full.get("Created"),
                "from": str((full.get("From") or {}).get("Address", "")).strip().lower(),
                "envelope_from": str((full.get("Envelope") or {}).get("From", "")).strip().lower(),
            }
        return self._cache[mid]

    @staticmethod
    def _text_of(full: dict) -> str:
        """The email's text. Specs say only what a body states, so an HTML-only email is read through its
        visible text rather than as empty."""
        text = full.get("Text") or ""
        if not text.strip() and full.get("HTML"):
            import html as _html
            from kitlib.web import visible_text
            body = full.get("HTML") or ""
            # The visible text, then every link target: links behind anchor text must stay findable in `text`.
            hrefs = [_html.unescape(h) for h in re.findall(r"""href\s*=\s*["']([^"']+)["']""", body, re.I)]
            text = visible_text(body) + ("\n" + "\n".join(hrefs) if hrefs else "")
        return text

    @staticmethod
    def _recipients(summary: dict) -> list[str]:
        to = [str(r.get("Address", "")).strip().lower() for r in (summary.get("To") or [])]
        env = [str(r).strip().lower() for r in ((summary.get("Envelope") or {}).get("To") or [])]
        return sorted(set(to + env))

    def summaries(self) -> list[dict]:
        """Every message, oldest first, without bodies: id, to, subject."""
        data = self._get("/api/v1/messages")
        return [{"id": m["ID"], "to": self._recipients(m), "subject": m.get("Subject", "")}
                for m in reversed(data.get("messages", []))]

    def all(self) -> list[dict]:
        """Every message, oldest first, with bodies (fetched once per message and cached)."""
        return [{**self._full(s["id"]), "to": s["to"]} for s in self.summaries()]

    def to(self, address: str) -> list[dict]:
        a = address.strip().lower()
        return [{**self._full(s["id"]), "to": s["to"]} for s in self.summaries() if a in s["to"]]

    def wait_for(self, address: str, pred=lambda m: True, count: int = 1, timeout: float = 35.0) -> list[dict]:
        end = time.monotonic() + timeout
        got: list[dict] = []
        while time.monotonic() < end:
            got = [m for m in self.to(address) if pred(m)]
            if len(got) >= count:
                return got
            time.sleep(0.5)
        return got


LINK_RE = re.compile(r"https?://[^\s<>\"']+")


def links(text: str) -> list[str]:
    return [l.rstrip(".,);]") for l in LINK_RE.findall(text or "")]
