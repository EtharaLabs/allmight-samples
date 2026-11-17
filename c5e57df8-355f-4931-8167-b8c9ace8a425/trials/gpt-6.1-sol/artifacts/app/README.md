# Pickpath

Pickpath is a Python 3 application using the standard-library threaded HTTP server, PostgreSQL through the bundled libpq client, server-rendered HTML, SMTP, and Stripe-compatible payment endpoints. Runtime shared libraries are included in `lib/`; no package install or build is required.

Run `./start.sh` with the deployment environment exported. It loads bundled shared libraries, applies idempotent schema additions, imports historical movement instants without modifying existing tables, starts the durable refund/email worker, registers the payment webhook, and serves in the foreground on `0.0.0.0:$PORT`.

Required runtime variables: `DATABASE_URL`, `PORT`, `PUBLIC_BASE_URL`, `SMTP_HOST`, `SMTP_PORT`, `MAIL_FROM`, `PAYMENTS_API_URL`, `PAYMENTS_WEBHOOK_SIGNING_FILE`, `RESET_LINK_TTL_SECONDS`. All are read at runtime. Warehouse dates use `sites.timezone`.

Start at `/password-reset` to set the password of an existing active user. No default password is created. Browser actions use session-bound CSRF tokens. API clients create keys at `/account/api-keys` and use Bearer authentication.

New state is stored in `pp_state` and the full movement ledger in `pp_movements`. Original data is preserved. Transactions serialize mutations to protect stock and action state, while provider refunds carry persistent idempotency keys and retry until acknowledged. A separate database connection on every health request lets the service recover after a database outage.

Verification scripts (with the same environment exported): `python3 tests/check.py`, `python3 tests/auth_check.py`, and `python3 tests/http_check.py` (the last requires a running application). Domain and authentication checks use transactions that roll back their fixtures. The HTTP script temporarily creates and then removes its test sessions.
