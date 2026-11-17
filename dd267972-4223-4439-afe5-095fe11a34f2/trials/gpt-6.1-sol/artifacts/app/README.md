# Shiftbook

Shiftbook uses Python 3's standard library HTTP server, PostgreSQL through a bundled libpq client, and SMTP. The Python interpreter, standard library, shared libraries, and the three location timezone files are bundled in this directory; no package download, installation, or build occurs at startup.

Run `./start.sh` with the service variables exported. It starts the server in the foreground on `0.0.0.0:$PORT`, applies additive, idempotent schema additions, and starts the persistent email outbox workers. The bundled runtime targets Linux on aarch64. Application data, credentials, approval overrides, payroll snapshots, and email delivery records are stored in PostgreSQL. Legacy records are read without modification, except authorized display-name changes.

Runtime environment variables: `DATABASE_URL`, `PORT`, `PUBLIC_BASE_URL`, `SMTP_HOST`, `SMTP_PORT`, `MAIL_FROM`, `RESET_LINK_TTL_SECONDS`, and `LOGIN_LOCKOUT_SECONDS`.

Staff set their initial passwords through `/password-reset`. Payroll administrators can add staff, append rates, provision or revoke clocks, and create payroll runs. The bearer punch API is `/api/punches`. Browser forms carry session-bound CSRF tokens. The health endpoint independently checks the database on each request.

Payroll rounds UTC punch instants before splitting worked intervals at local midnight. It applies daily and weekly categories chronologically and calculates weighted premiums with exact rational arithmetic. Draft snapshots and finalized allocation ledgers support held weeks and subsequent adjustments. Mutating requests serialize through a PostgreSQL advisory transaction lock; run finalization validates the version and recomputes its contents before locking it.

Verification: `PYTHONHOME="$PWD/runtime" LD_LIBRARY_PATH="$PWD/lib" runtime/bin/python3 -m unittest test_payroll` checks payroll boundaries, weighted rates, meals, DST, rounding, and exceptions. With the service variables exported, `PYTHONHOME="$PWD/runtime" LD_LIBRARY_PATH="$PWD/lib" runtime/bin/python3 test_http.py` exercises browser-form HTTP flows and concurrency using temporary fixture rows that it removes afterward; stop the production application before running that integration test.
 The optional `test_outbox.py` uses the same runtime environment and verifies delivery of 1,500 queued messages to an in-memory test sink within 30 seconds.
