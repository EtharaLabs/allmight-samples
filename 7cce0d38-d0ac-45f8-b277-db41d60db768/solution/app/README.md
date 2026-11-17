# Seatwise

Reserved-seat ticketing for small venues: organizations, venues and seat maps, events with presales and
sales windows in venue time zones, seat holds, promo codes, Stripe-compatible checkout confirmed by signed
webhooks, refunds and cancellation, transfers with signed QR tokens, door check-in and a scan API, waiting
lists, dashboards, attendee export and email.

## Stack

- Python 3.12 standard library HTTP server (`http.server.ThreadingHTTPServer`, HTTP/1.1), server-rendered
  HTML with no JavaScript.
- PostgreSQL through psycopg 3 (the only third-party package, vendored in `.venv/`).
- Email through `smtplib`; payments through the provider's HTTP API with `urllib`.

## Running

`start.sh` runs `./.venv/bin/python -u app.py` in the foreground. On start the app:

1. applies its schema additions idempotently under an advisory lock (new tables are prefixed `sw_`; the
   old platform's tables and rows are never changed, except `memberships` rows when an owner changes a
   role or removes a member) and, the first time only, copies the old platform's orders and tickets into
   its working tables with their recorded amounts and times;
2. starts background threads: the email outbox sender, hold/offer expiry and waiting-list offers,
   provider session expiry, refund sending with retries under a stable `Idempotency-Key`, and webhook
   endpoint registration;
3. serves HTTP on `0.0.0.0:$PORT`.

The `.venv/` was created with `python -m venv --copies` and holds every dependency, so nothing is
downloaded or built at start.

## Environment variables

`DATABASE_URL`, `PORT`, `PUBLIC_BASE_URL`, `SMTP_HOST`, `SMTP_PORT`, `MAIL_FROM`, `PAYMENTS_API_URL`,
`PAYMENTS_WEBHOOK_SIGNING_FILE`, `HOLD_TTL_SECONDS`, `WAITLIST_OFFER_TTL_SECONDS`,
`ACCOUNT_LINK_TTL_SECONDS`, `TICKET_SIGNING_KEY`.

## Notes

- Money is integer cents throughout (`seatwise/money.py`: half-up rounding and largest-remainder
  allocation with seat-order ties).
- Passwords and scanner device keys are stored as salted scrypt hashes; session ids and account/invite
  link tokens only as SHA-256 hashes. Emails that carry such links are sent directly and never queued;
  QR tokens are HMAC-signed and derived when needed, never stored.
- Scan API: `POST /api/scan` answers one result object (also repeated as `"results": [...]`); `POST /api/scan/batch` answers
  `{"results": [...]}` with one result per row in order.
