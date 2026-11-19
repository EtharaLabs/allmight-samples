# Lotbook

A self-hosted household tax-lot tracker built with Python 3.12, the Python standard library HTTP server, PostgreSQL through libpq, and SMTP. Pages use server-rendered HTML and responsive CSS. No external assets or network package downloads are required.

## Start

Export the deployment's variables, then run `./start.sh`. It runs in the foreground on `0.0.0.0:$PORT`. The script uses the bundled Linux ARM64 Python runtime, standard library, loader, libpq, and shared libraries. It does not install or build anything. The bundled runtime matches this workspace's architecture.

The application reads these environment variables at runtime:

- `DATABASE_URL`: PostgreSQL connection URI.
- `PORT`: application listening port.
- `PUBLIC_BASE_URL`: trusted origin used in password-reset emails.
- `SMTP_HOST`, `SMTP_PORT`: SMTP service connection.
- `MAIL_FROM`: sender address.
- `RESET_LINK_TTL_SECONDS`: reset-link lifetime.
- `IMPORT_MAX_BYTES`: maximum statement file content size.

Open `/password-reset` to set a user's first password, then sign in. Household access follows the existing users and assignments. `/healthz` checks a fresh database connection on each request.

## Data and calculations

Startup adds the `lb_*` tables and indexes idempotently and materializes the legacy locked-year snapshots. Original records are preserved. All persistent application state lives in PostgreSQL, including hashed credentials and reset tokens, hashed server session identifiers, appended transactions, imports, frozen reports, and the email outbox.

The calculation engine uses exact rational arithmetic for quantities, prices, currency conversion, allocation, and per-share comparisons. It rounds cents at the specified allocation points. It replays complete household histories, including future replacement acquisitions, corporate actions, IRA treatment, lot-method changes, splits, and transfers.

Mutations use database transactions and a PostgreSQL advisory lock to serialize history updates, reference deduplication, and tax-year locking. Locked exports are stored verbatim. Adjustments reconcile current historical figures with frozen figures and previously frozen adjustments. The email worker drains the persistent outbox after committed locks.

## Verification

`./runtime/bin/python -m unittest -v test_engine` runs isolated calculation tests based on the worked examples. With the deployment environment exported, `./runtime/bin/python -m unittest -v test_auth` verifies reset and session behavior against the database with mocked SMTP delivery and restores its authentication test state. `./runtime/bin/python smoke_test.py` checks the running application's pages, exports, role restrictions, and CSRF protection with temporary sessions.

`test_import.py` checks atomic statement validation, duplicate conflicts, simultaneous uploads, and escaped text against the running app. `test_locks.py` checks simultaneous locks, frozen exports, adjustment carry-forward, and email contents with captured delivery. These integration tests clean up their temporary application records. Run integration tests sequentially against an idle development installation.

Application modules: `app.py` contains HTTP routes, forms, authentication, imports, and mail delivery; `engine.py` contains calculations and exports; `db.py` contains the libpq adapter and schema additions.
