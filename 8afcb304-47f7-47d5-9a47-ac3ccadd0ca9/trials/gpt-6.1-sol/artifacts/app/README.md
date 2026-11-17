# Harborview Physio

A Python 3.12 application using the standard library HTTP server, PostgreSQL through a bundled libpq client, SMTP, and the supplied Stripe-compatible payment service. Server-rendered responsive pages use no external assets.

Run `./start.sh` with the service environment exported. The executable starts the application in the foreground on `0.0.0.0:$PORT`. It uses the workspace virtual environment, bundled shared libraries, and time-zone database. Startup applies additive, idempotent schema changes. Nothing is downloaded or built at startup.

Environment variables read at runtime: `DATABASE_URL`, `PORT`, `PUBLIC_BASE_URL`, `SMTP_HOST`, `SMTP_PORT`, `MAIL_FROM`, `PAYMENTS_API_URL`, `PAYMENTS_WEBHOOK_SIGNING_FILE`, `RESET_LINK_TTL_SECONDS`, `HOLD_TTL_SECONDS`, `OFFER_TTL_SECONDS`, `MIN_NOTICE_MINUTES`, and `BOOKING_HORIZON_DAYS`.

Existing records are preserved. Additional `hv_` tables persist authentication, sessions, payment state, waitlist entries, event deduplication, reminders and outgoing messages. A background worker expires holds and offers, retries provider refunds and sends queued email. Booking writes are serialized within the process. Run one application process; its HTTP server serves concurrent requests with threads.

Verification: `.venv/bin/python -m unittest test_accounts test_availability` runs rollback-isolated account, schedule, buffer, fee, and DST checks. `.venv/bin/python verify.py` runs an HTTP smoke test against `PUBLIC_BASE_URL`; it creates a clearly named test patient and releases its test hold. Integration checks also exercised provider-hosted card payment, signed confirmation, rescheduling, cancellation/refund, waitlist offers, staff pages, reminders, CSV and application restart. No browser rendering engine is installed in this environment, so viewport rendering was not automated.

Concurrency verification sent four simultaneous requests for one slot: exactly one returned 303 with a Checkout Session, and three returned 409. The winning test hold was released afterwards.
