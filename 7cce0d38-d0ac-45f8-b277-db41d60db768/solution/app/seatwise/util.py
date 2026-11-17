"""Shared helpers: configuration, time zones and instants, validation, hashing, QR tokens, formatting."""

from __future__ import annotations

import hashlib
import hmac
import html
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, available_timezones

UTC = timezone.utc


# ---- configuration (read from the environment at run time) -----------------------------------------
def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, default)).strip())
    except ValueError:
        return default


def base_url() -> str:
    return env("PUBLIC_BASE_URL").strip().rstrip("/")


def hold_ttl() -> int:
    return env_int("HOLD_TTL_SECONDS", 600)


def offer_ttl() -> int:
    return env_int("WAITLIST_OFFER_TTL_SECONDS", 1800)


def link_ttl() -> int:
    return env_int("ACCOUNT_LINK_TTL_SECONDS", 3600)


def ticket_key() -> bytes:
    return env("TICKET_SIGNING_KEY").encode()


def app_secret() -> bytes:
    """Secret for CSRF tokens, derived from the configured signing key (nothing stored)."""
    return hashlib.sha256(b"seatwise-app-secret:" + ticket_key()).digest()


# ---- escaping / formatting ---------------------------------------------------------------------------
def E(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def dollars(cents: int) -> str:
    cents = int(cents)
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"


# ---- time --------------------------------------------------------------------------------------------
def now() -> datetime:
    return datetime.now(UTC)


def ceil_second(dt: datetime) -> datetime:
    if dt.microsecond:
        return dt.replace(microsecond=0) + timedelta(seconds=1)
    return dt


def floor_second(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def instant(dt: datetime | None) -> str:
    """RFC 3339 UTC to the second, like 2027-06-13T01:00:00Z."""
    if dt is None:
        return ""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@lru_cache(maxsize=64)
def zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


_ZONES: set[str] | None = None


def known_timezone(name: str) -> bool:
    global _ZONES
    if _ZONES is None:
        _ZONES = set(available_timezones())
    return name in _ZONES


def legacy_instant(naive: datetime | None, fold: int, tz: str) -> datetime | None:
    if naive is None:
        return None
    return naive.replace(fold=int(fold or 0), tzinfo=zone(tz)).astimezone(UTC)


_WALL = re.compile(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?")


def parse_wallclock(text: str, tz: str) -> tuple[datetime | None, str]:
    """Organizer-typed wall-clock time in the venue's zone -> (UTC instant, error).

    A time that happens twice (clocks set back) means its first occurrence; a time that does not exist
    (clocks set forward) is refused."""
    s = (text or "").strip()
    m = _WALL.fullmatch(s)
    if not m:
        return None, "Enter the time as YYYY-MM-DDTHH:MM."
    try:
        naive = datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5]), int(m[6] or 0))
    except ValueError:
        return None, "That date or time does not exist."
    z = zone(tz)
    try:
        aware = naive.replace(tzinfo=z, fold=0)
        utc = aware.astimezone(UTC)
        back = utc.astimezone(z).replace(tzinfo=None)
    except (ValueError, OverflowError):
        return None, "That date or time is out of range."
    if back != naive:
        return None, (f"{s} does not exist in {tz}: the clocks skip that time when daylight saving time "
                      "starts. Choose a time outside the skipped hour.")
    return utc, ""


def local_text(dt: datetime | None, tz: str) -> str:
    """Human local time at the venue, e.g. 'Sat 12 Jun 2027, 20:00 CDT'."""
    if dt is None:
        return ""
    loc = dt.astimezone(zone(tz))
    return loc.strftime("%a %d %b %Y, %H:%M ") + (loc.tzname() or "")


def local_input(dt: datetime | None, tz: str) -> str:
    if dt is None:
        return ""
    return dt.astimezone(zone(tz)).strftime("%Y-%m-%dT%H:%M")


def parse_rfc3339(text) -> datetime | None:
    if not isinstance(text, str):
        return None
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(\.\d+)?([Zz]|[+-]\d{2}:\d{2})", text)
    if not m:
        return None
    try:
        base = datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5]), int(m[6]))
        off = m[8]
        if off in ("Z", "z"):
            tzinfo = UTC
        else:
            sign = 1 if off[0] == "+" else -1
            hh, mm = int(off[1:3]), int(off[4:6])
            if hh > 23 or mm > 59:
                return None
            tzinfo = timezone(sign * timedelta(hours=hh, minutes=mm))
        return base.replace(tzinfo=tzinfo).astimezone(UTC)
    except (ValueError, OverflowError):
        return None


# ---- validation --------------------------------------------------------------------------------------
def has_control(s: str) -> bool:
    return any(ord(ch) < 0x20 for ch in s)


def valid_name(s: str, limit: int = 100) -> bool:
    """1..limit characters, nothing below U+0020, not only blanks."""
    return isinstance(s, str) and 1 <= len(s) <= limit and not has_control(s) and s.strip() != ""


def norm_email(s: str | None) -> str:
    return (s or "").strip().lower()


_EMAIL = re.compile(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+")


def valid_email(s: str) -> bool:
    return bool(s) and len(s) <= 254 and _EMAIL.fullmatch(s) is not None


def parse_int(s, lo: int | None = None, hi: int | None = None) -> int | None:
    s = (s or "").strip() if isinstance(s, str) else ""
    if not re.fullmatch(r"\d{1,12}", s):
        return None
    v = int(s)
    if lo is not None and v < lo:
        return None
    if hi is not None and v > hi:
        return None
    return v


CODE_RE = re.compile(r"[A-Z0-9]{4,20}")
SECTION_RE = re.compile(r"[A-Z0-9]{1,8}")


# ---- hashing -----------------------------------------------------------------------------------------
def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def hash_secret(secret: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(secret.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    return "scrypt$" + salt.hex() + "$" + dk.hex()


def check_secret(secret: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        _, salt, dk = stored.split("$")
        got = hashlib.scrypt(secret.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1)
        return hmac.compare_digest(got.hex(), dk)
    except Exception:
        return False


_DUMMY = None


def dummy_check(secret: str) -> None:
    """Spend about the same time as a real check (unknown address)."""
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = hash_secret("dummy-password-x")
    check_secret(secret, _DUMMY)


def csrf_for(sid_hash: str) -> str:
    return hmac.new(app_secret(), b"csrf:" + sid_hash.encode(), hashlib.sha256).hexdigest()[:40]


# ---- QR tokens ---------------------------------------------------------------------------------------
def _sig(msg: str) -> str:
    return hmac.new(ticket_key(), msg.encode(), hashlib.sha256).hexdigest()[:32]


def qr_token(ticket_id: int, version: int, barcode: str | None = None) -> str:
    """Version 0 is an old-platform ticket's barcode; later versions are HMAC-signed by this app."""
    if version == 0 and barcode:
        return barcode
    return f"SW.{ticket_id}.{version}.{_sig(f'qr:{ticket_id}.{version}')}"


_QR = re.compile(r"SW\.([1-9]\d{0,17})\.([1-9]\d{0,6})\.([0-9a-f]{32})")


def parse_qr(token: str) -> tuple[int, int] | None:
    """-> (ticket id, version) when the token carries this application's valid signature."""
    m = _QR.fullmatch(token or "")
    if not m:
        return None
    tid, ver = int(m[1]), int(m[2])
    if not hmac.compare_digest(qr_token(tid, ver), token):
        return None
    return tid, ver


def offer_token(offer_id: int) -> str:
    return f"{offer_id}-{_sig(f'offer:{offer_id}')}"


def parse_offer_token(token: str) -> int | None:
    m = re.fullmatch(r"([1-9]\d{0,9})-([0-9a-f]{32})", token or "")
    if not m:
        return None
    oid = int(m[1])
    return oid if hmac.compare_digest(offer_token(oid), token) else None


# ---- seats -------------------------------------------------------------------------------------------
def seat_label(code: str, row: str, num: int) -> str:
    return f"{code}-{row}-{num}"


def seat_key(code: str, row: str, num: int) -> tuple:
    return (code, row, int(num))


def label_key(label: str) -> tuple:
    sec, row, num = label.rsplit("-", 2)
    return (sec, row, int(num))


def safe_next(nxt: str | None, default: str = "/tickets") -> str:
    """R2.7: only a path on this site: a single leading '/' not followed by '/' or '\\', no scheme/host."""
    if not nxt or not isinstance(nxt, str):
        return default
    if not nxt.startswith("/") or nxt[1:2] in ("/", "\\"):
        return default
    if any(ord(c) < 0x21 or ord(c) > 0x7E for c in nxt):
        return default
    return nxt


def log_exc(where: str) -> None:
    """Log an error without its message or any request data (no secrets, bodies or links in logs)."""
    import sys
    import traceback
    et, _, tb = sys.exc_info()
    frames = traceback.extract_tb(tb) if tb else []
    loc = "; ".join(f"{os.path.basename(f.filename)}:{f.lineno} {f.name}" for f in frames[-5:])
    try:
        sys.stderr.write(f"seatwise: {where} failed: {et.__name__ if et else 'error'} at {loc}\n")
    except Exception:
        pass
