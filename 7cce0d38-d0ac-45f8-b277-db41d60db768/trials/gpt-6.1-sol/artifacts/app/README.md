# Seatwise

Reserved-seat ticketing for independent venues. Built with Python 3.12's standard-library threaded HTTP server, PostgreSQL, SMTP, and the provided Stripe-compatible payment service. The responsive browser interface includes promoter management, seat selection, checkout, tickets, transfers, refunds, waiting lists, door scanning, reporting and CSV export.

Run `./start.sh` with the deployment environment exported. It starts the app in the foreground on `0.0.0.0:$PORT`. Startup does not download, install or build anything. Python, its standard library, time-zone data, the PostgreSQL client and shared libraries are bundled in the workspace. Use the same OS/CPU architecture as the supplied environment.

The app reads these environment variables at runtime:

- `DATABASE_URL`, `PORT`, `PUBLIC_BASE_URL`
- `SMTP_HOST`, `SMTP_PORT`, `MAIL_FROM`
- `PAYMENTS_API_URL`, `PAYMENTS_WEBHOOK_SIGNING_FILE`
- `HOLD_TTL_SECONDS`, `WAITLIST_OFFER_TTL_SECONDS`, `ACCOUNT_LINK_TTL_SECONDS`
- `TICKET_SIGNING_KEY`

`PAYMENTS_WEBHOOK_SIGNING_FILE` names a file containing the provider's webhook secret. Keep `TICKET_SIGNING_KEY` stable across restarts to preserve signing continuity.

Startup creates `seatwise_state` idempotently. The original tables and rows remain intact; normalized application views and subsequent changes live in that additional PostgreSQL table. Pending work, sessions, ticket tokens, refund idempotency keys and email delivery records persist there. One application process serializes state changes; run one instance per database. A worker processes expiry, offers, refunds and queued email every two seconds. `/healthz` checks PostgreSQL directly with a bounded timeout.

Existing organization members establish their password through `/password-reset`. New attendees use `/signup` and follow the emailed verification link.

To run the isolated integration checks, export the same environment and run:

```sh
LD_LIBRARY_PATH="$PWD/runtime/lib" PYTHONHOME="$PWD/runtime/python" PYTHONTZPATH="$PWD/runtime/zoneinfo" .venv/bin/python check.py
```

The checks read application state, then perform their mutations only in memory. Payment calls are stubbed in these checks; the running application uses the configured payment provider.

Run the integration checks with the same environment exported and the bundled runtime:

```sh
LD_LIBRARY_PATH="$PWD/runtime/lib" PYTHONHOME="$PWD/runtime/python" \
PYTHONTZPATH="$PWD/runtime/zoneinfo" .venv/bin/python check.py
```

These checks cover account verification/reset behavior, session invalidation, organization permissions, CSRF, venue/event creation, DST interpretation, seat holds, promos, all four pricing examples, signed payment/refund webhooks and duplicate deliveries, transfers, offline scan ordering, waiting-list offers, dashboards and CSV output.
