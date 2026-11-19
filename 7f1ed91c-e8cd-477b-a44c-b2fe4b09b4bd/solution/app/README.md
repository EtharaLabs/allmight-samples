# Lotbook

Stack: Python 3.12 standard library HTTP server (`http.server.ThreadingHTTPServer`) with psycopg 3 for
PostgreSQL, server-rendered HTML, and `smtplib` for email. Money and quantities use exact `Fraction`
arithmetic (`lotbook/engine.py`).

`start.sh` runs `.venv/bin/python app.py` in the foreground. On start the app applies its schema additions
idempotently (tables prefixed `lb_`, plus the `lb_txn_id` sequence that numbers new transactions above every
legacy id), starts a background outbox sender for email, and serves HTTP on `0.0.0.0:$PORT`. Dependencies
live in `.venv/` inside the workspace (created with `python -m venv --copies`), so nothing is downloaded at
start.

Environment variables read: `DATABASE_URL`, `PORT`, `PUBLIC_BASE_URL`, `SMTP_HOST`, `SMTP_PORT`, `MAIL_FROM`,
`RESET_LINK_TTL_SECONDS`, `IMPORT_MAX_BYTES`.
