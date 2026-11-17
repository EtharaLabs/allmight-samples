# Harborview Physio

Stack: Python 3.12 standard library HTTP server (`http.server.ThreadingHTTPServer`) with psycopg 3
for PostgreSQL, server-rendered HTML, `urllib` for the payment provider, and `smtplib` for email.

`start.sh` runs `.venv/bin/python app.py` in the foreground. On start the app applies its schema
additions idempotently (tables prefixed `hv_`, plus two indexes), registers its webhook endpoint with
the payment provider, starts background workers (hold and offer expiry, refund retries, an email
outbox), and serves HTTP on `0.0.0.0:$PORT`. Dependencies live in `.venv/` inside the workspace
(created with `python -m venv --copies`), so nothing is downloaded at start.

Environment variables read: `DATABASE_URL`, `PORT`, `PUBLIC_BASE_URL`, `SMTP_HOST`, `SMTP_PORT`,
`MAIL_FROM`, `PAYMENTS_API_URL`, `PAYMENTS_WEBHOOK_SIGNING_FILE`, `RESET_LINK_TTL_SECONDS`,
`HOLD_TTL_SECONDS`, `OFFER_TTL_SECONDS`, `MIN_NOTICE_MINUTES`, `BOOKING_HORIZON_DAYS`.
