"""HTTP client and HTML helpers for verifiers (standard library only).

Client keeps its own cookie jar for one origin and never follows redirects unless asked, so every
status the application answers is visible. `Dom` is a tiny element tree with attribute queries;
`submit_form` finds a form by the data-action of a button inside it, submits every hidden input
carrying data-csrf (the contract's session token) plus the given fields, exactly like a browser.
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


@dataclass
class Node:
    tag: str
    attrs: dict
    parent: "Node | None" = None
    children: list = field(default_factory=list)
    text_parts: list = field(default_factory=list)

    def text(self) -> str:
        out = []
        def walk(n):
            out.extend(n.text_parts_in_order())
        walk(self)
        return re.sub(r"\s+", " ", "".join(out)).strip()

    def text_parts_in_order(self):
        for c in self.children:
            if isinstance(c, str):
                yield c
            elif c.tag not in ("script", "style"):
                yield from c.text_parts_in_order()
                yield " "

    def find_all(self, pred) -> list["Node"]:
        out = []
        for c in self.children:
            if isinstance(c, Node):
                if pred(c):
                    out.append(c)
                out.extend(c.find_all(pred))
        return out

    def attr_all(self, name: str, value: str | None = None) -> list["Node"]:
        return self.find_all(lambda n: name in n.attrs and (value is None or n.attrs[name] == value))

    def attr_one(self, name: str, value: str | None = None) -> "Node | None":
        r = self.attr_all(name, value)
        return r[0] if r else None

    def ancestor(self, tag: str) -> "Node | None":
        n = self.parent
        while n is not None and n.tag != tag:
            n = n.parent
        return n


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {})
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        n = Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur)
        self.cur.children.append(n)
        if tag not in VOID:
            self.cur = n

    def handle_startendtag(self, tag, attrs):
        n = Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur)
        self.cur.children.append(n)

    def handle_endtag(self, tag):
        n = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def parse(html_text: str) -> Node:
    b = _Builder()
    try:
        b.feed(html_text)
        b.close()
    except Exception:
        pass
    return b.root


def visible_text(html_text: str) -> str:
    return parse(html_text).text()


@dataclass
class Response:
    status: int
    headers: list
    body: bytes
    url: str

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def header(self, name: str) -> str | None:
        for k, v in self.headers:
            if k.lower() == name.lower():
                return v
        return None

    def header_all(self, name: str) -> list[str]:
        return [v for k, v in self.headers if k.lower() == name.lower()]

    def json(self):
        return json.loads(self.body.decode("utf-8"))

    @property
    def dom(self) -> Node:
        return parse(self.text)

    @property
    def location(self) -> str | None:
        return self.header("Location")


class Client:
    def __init__(self, base: str, timeout: float = 150.0):  # above every spec time limit (e.g. 120 s runs)
        self.base = base.rstrip("/")
        u = urllib.parse.urlsplit(self.base)
        self.host, self.port = u.hostname, u.port or 80
        self.timeout = timeout
        self.cookies: dict[str, str] = {}
        self.set_cookie_headers: list[str] = []

    def copy(self) -> "Client":
        c = Client(self.base, self.timeout)
        c.cookies = dict(self.cookies)
        return c

    def request(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None,
                follow: bool = False, max_hops: int = 8) -> Response:
        url = path if path.startswith("http") else self.base + path
        for _ in range(max_hops):
            u = urllib.parse.urlsplit(url)
            target = (u.path or "/") + (("?" + u.query) if u.query else "")
            conn = http.client.HTTPConnection(u.hostname or self.host, u.port or self.port, timeout=self.timeout)
            h = {"Host": f"{u.hostname}:{u.port or 80}", "User-Agent": "forge-kit-verifier", "Connection": "close"}
            if self.cookies:
                h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
            h.update(headers or {})
            try:
                conn.request(method, target, body=body, headers=h)
                r = conn.getresponse()
                data = r.read()
                hdrs = r.getheaders()
            finally:
                conn.close()
            for k, v in hdrs:
                if k.lower() == "set-cookie":
                    self.set_cookie_headers.append(v)
                    self._store_cookie(v)
            resp = Response(r.status, hdrs, data, url)
            if follow and r.status in (301, 302, 303, 307, 308) and resp.location:
                url = urllib.parse.urljoin(url, resp.location)
                if r.status in (301, 302, 303):
                    method, body = "GET", None
                    headers = {k: v for k, v in (headers or {}).items() if k.lower() not in ("content-type",)}
                continue
            return resp
        return resp

    def _store_cookie(self, header: str) -> None:
        first = header.split(";", 1)[0]
        if "=" not in first:
            return
        k, v = first.split("=", 1)
        k, v = k.strip(), v.strip()
        attrs = header.lower()
        if v == "" or "max-age=0" in attrs or "expires=thu, 01 jan 1970" in attrs:
            self.cookies.pop(k, None)
        else:
            self.cookies[k] = v

    def get(self, path: str, follow: bool = False, **kw) -> Response:
        return self.request("GET", path, follow=follow, **kw)

    def post_form(self, path: str, fields: dict | list, follow: bool = False, headers: dict | None = None) -> Response:
        body = urllib.parse.urlencode(fields, doseq=True).encode()
        h = {"Content-Type": "application/x-www-form-urlencoded"}
        h.update(headers or {})
        return self.request("POST", path, body=body, headers=h, follow=follow)

    def post_json(self, path: str, obj, headers: dict | None = None) -> Response:
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        return self.request("POST", path, body=json.dumps(obj).encode(), headers=h)


class FormNotFound(AssertionError):
    pass


def find_form(dom: Node, action_marker: str) -> Node:
    for btn in dom.attr_all("data-action", action_marker):
        form = btn.ancestor("form")
        if form is not None:
            return form
    raise FormNotFound(f"no form with a data-action={action_marker!r} button")


def form_fields(form: Node, include_hidden: bool = True) -> list[tuple[str, str]]:
    out = []
    for inp in form.find_all(lambda n: n.tag == "input"):
        if "data-csrf" in inp.attrs and inp.attrs.get("name"):
            out.append((inp.attrs["name"], inp.attrs.get("value", "")))
        elif include_hidden and inp.attrs.get("type", "").lower() == "hidden" and inp.attrs.get("name"):
            out.append((inp.attrs["name"], inp.attrs.get("value", "")))
    return out


def submit_form(client: Client, page_path: str, action_marker: str, fields: dict, follow: bool = False,
                page: Response | None = None, drop_csrf: bool = False, override: dict | None = None) -> Response:
    """GET the page (unless given), find the form, submit csrf + hidden fields + `fields`."""
    page = page or client.get(page_path, follow=True)
    form = find_form(page.dom, action_marker)
    action = form.attrs.get("action") or page.url
    action = urllib.parse.urljoin(page.url, action)
    method = (form.attrs.get("method") or "get").upper()
    base = [(k, v) for k, v in form_fields(form) if k not in fields]
    if drop_csrf:
        csrf_names = {i.attrs.get("name") for i in form.find_all(lambda n: n.tag == "input" and "data-csrf" in n.attrs)}
        base = [(k, v) for k, v in base if k not in csrf_names]
    payload = base + [(k, v) for k, v in fields.items()]
    if override:
        payload = [(k, v) for k, v in payload if k not in override] + list(override.items())
    if method == "GET":
        q = urllib.parse.urlencode(payload)
        return client.get(action + ("&" if "?" in action else "?") + q, follow=follow)
    return client.post_form(action, payload, follow=follow)


def csrf_from(page: Response) -> list[tuple[str, str]]:
    return [(i.attrs.get("name", ""), i.attrs.get("value", "")) for i in
            page.dom.find_all(lambda n: n.tag == "input" and "data-csrf" in n.attrs)]


def wait_port(host: str, port: int, timeout: float) -> bool:
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.25)
    return False
