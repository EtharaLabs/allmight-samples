# TRUTH: ethara/lotbook-portfolio

Task version 2.0.0.

Sources: `task.toml`, `README.md`, `instruction.md`, `environment/`, `solution/`, `tests/` (`workflows.toml`, `reward.toml`, `judge.toml`, `scoring.py`, `test.sh` and the workflow code), the seed loaded into PostgreSQL, and the verifier output of oracle run `20261111T104651Z-r2qc-lb-oracle` (`results.json` sha256 `d7c68a08756fd6aead0dc09da66cc6f46798c42f9e2e4160dc24e2fc044a5b07`).

This file is for graders and reviewers. The agent never sees it: Harbor does not copy `solution/` into the agent's container, and `environment/Dockerfile` copies only `uv-requirements.lock`, `services`.

## Reward and pass rule

Full reward is `1.0` on the `reward` key of `/logs/verifier/reward.json` (keys `reward`, `functional`, `judge`).

| term | rule | source |
|---|---|---|
| reward | `0.9 × functional + 0.1 × judge` (weighted sum) | `tests/reward.toml` |
| functional | weighted mean of 31 strict workflows (weights sum to 112); a workflow scores 1 only when every one of its checks passes, else 0 | `tests/workflows.toml`, `tests/scoring.py` |
| cap | any breach detector firing anywhere in the run (×0.4), or any of the 18 critical workflows failing (×0.4), caps every workflow, so the reward is at most 0.46; a critical workflow that fails only because the verifier's time budget ran out scores 0 without the cap | `tests/scoring.py` |
| judge | weighted mean of 10 binary criteria, each judged by `anthropic/claude-fable-5-1` (reasoning effort max, 3 samples) from the checkpoint evidence under `/logs/verifier/ux/` | `tests/judge.toml`, `tests/judge-reference.md` |
| invalid run | `test.sh` writes a zero reward first; a verifier, service or judge failure leaves that zero and writes `/logs/verifier/evaluation-invalid.json` | `tests/test.sh` |

## Golden trajectory

| step | what happens | source |
|---|---|---|
| S1 | the environment starts PostgreSQL seeded with the existing records and the SMTP mail sink, and writes `/etc/lotbook/services.env` | `environment/services/up.sh` |
| S2 | `solve.sh` copies `solution/app/` into `/app`, makes a copy-based virtual environment, and installs `requirements.txt` offline from the 4 wheels in `solution/wheels/` | `solution/solve.sh` |
| S3 | the verifier starts `/app/start.sh` (`exec ./.venv/bin/python -u app.py`) on a fresh copy of the database; in the oracle run `/healthz` answered 200 after 1 s | `solution/app/start.sh`, `startup.json` |
| S4 | the verifier runs the 31 workflows (145 checks) and the judge; the oracle passed every check, fired no breach detector, and scored functional 1.0, judge 1.0, reward 1.0 | `results.json`, `reward.json` |

Reference stack (`solution/app/README.md`): Python 3.12 standard library HTTP server (`http.server.ThreadingHTTPServer`) with psycopg 3 for PostgreSQL, server-rendered HTML, and `smtplib` for email. Money and quantities use exact `Fraction` arithmetic (`lotbook/engine.py`).

## Records the checkers rely on

The agent's database and the verifier's are both seeded from `db/init/` (`001_schema.sql`, `002_records.sql`); `tests/services/db/` is byte-identical to `environment/services/db/` (tree sha256 `4c06e167963a34df17ec5dd826856e3bbb4ac7a811ce73b2575b43a23503df09`). Row counts after loading the seed into PostgreSQL:

| table | rows |
|---|---|
| `accounts` | 9 |
| `assignments` | 5 |
| `corporate_actions` | 23 |
| `fx_rates` | 1980 |
| `households` | 3 |
| `legacy_locked_figures` | 30 |
| `legacy_transactions` | 20986 |
| `legacy_year_locks` | 2 |
| `lot_method_changes` | 5 |
| `securities` | 35 |
| `security_prices` | 70 |
| `users` | 12 |

Users by role: `advisor` 1; `member` 3; `owner` 4; `preparer` 2; `viewer` 2.

Volumes the checks compared in oracle run `20261111T104651Z-r2qc-lb-oracle` (whole-number observations from `results.json`; counts that depend on the run date can differ on another day):

| workflow | observed |
|---|---|
| W09 | summary_2024_problems 0; realized_2024_rows 13491; realized_2024_problems 0 |
| W10 | summary_2025_problems 0; realized_2025_rows 15067; realized_2025_problems 0 |
| W11 | summary_2024_problems 0; realized_2024_rows 2310; realized_2024_problems 0; summary_2025_problems 0; realized_2025_rows 3219; realized_2025_problems 0 |
| W12 | lots_1_2_3 2705; lots_4_5_8 2012; lots_6_7_9 890 |
| W13 | compared.realized_rows 121; compared.open_lots 94; compared.wash_portions 0; compared.securities 66 |
| W14 | compared.realized_rows 190; compared.open_lots 171; compared.wash_portions 0; compared.securities 56 |
| W15 | compared.realized_rows 100; compared.open_lots 92; compared.wash_portions 0; compared.securities 40 |
| W16 | compared.realized_rows 145; compared.open_lots 57; compared.wash_portions 0; compared.securities 56 |
| W17 | compared.realized_rows 79; compared.open_lots 104; compared.wash_portions 0; compared.securities 44 |
| W18 | compared.realized_rows 65; compared.open_lots 142; compared.wash_portions 1; compared.securities 57 |
| W19 | wash_portions 53; compared.realized_rows 102; compared.open_lots 130; compared.wash_portions 53; compared.securities 64 |
| W20 | wash_portions 65; compared.realized_rows 107; compared.open_lots 126; compared.wash_portions 65; compared.securities 68 |
| W27 | secrets_checked 26 |
| W28 | actions 34; compared.realized_rows 82; compared.open_lots 190; compared.wash_portions 0; compared.securities 68 |
| W29 | actions 30; compared.realized_rows 100; compared.open_lots 100; compared.wash_portions 0; compared.securities 61 |
| W31 | disallowed_portions 25; compared.realized_rows 26; compared.open_lots 64; compared.wash_portions 25; compared.securities 26 |
| W30 | actions 54; compared.realized_rows 66; compared.open_lots 93; compared.wash_portions 0; compared.securities 26 |

## Rubric

### Strict workflows

| id | weight | share | critical | checks | workflow |
|---|---|---|---|---|---|
| W01 | 2 | 1.8% | no | 7 | Password links: anti-enumeration, single use, replacement, expiry, address matching |
| W02 | 3 | 2.7% | no | 5 | Sessions: refusals, rotation, sign-out, cookie attributes, safe next= redirects |
| W03 | 3 | 2.7% | no | 3 | Household isolation: pages, exports and form ids of other households |
| W04 | 3 | 2.7% | no | 3 | The advisor's read-only views: same figures as members, no write controls, every write refused |
| W05 | 3 | 2.7% | no | 5 | Roles: a read-only viewer, a member who cannot lock, a preparer who reads and locks only, an inactive preparer |
| W06 | 3 | 2.7% | no | 4 | CSRF on every change, mass assignment, profile edits change only the name |
| W07 | 4 | 3.6% | yes | 9 | Northwind statements: quirks, junk lines, natural keys, overlaps, legacy references, concurrent uploads |
| W08 | 4 | 3.6% | yes | 6 | Harbor Direct statements: number formats, row errors, atomicity, short checks, size limit, file names |
| W09 | 4 | 3.6% | yes | 3 | Legacy 2024 of the Raman-Okafor household: realized rows, summary with 2023 corrections, year page |
| W10 | 4 | 3.6% | yes | 3 | Legacy 2025 of the Raman-Okafor household: realized rows, summary, year page |
| W11 | 4 | 3.6% | yes | 7 | Legacy 2024 and 2025 of the Lindqvist household, read by a member and by the advisor |
| W12 | 4 | 3.6% | yes | 5 | Legacy open lots account by account, and unrealized gains at the stored prices |
| W13 | 5 | 4.5% | yes | 5 | Exchange rates and rounding: fallback dates, quote directions, half-cent ties, the share rule |
| W14 | 4 | 3.6% | yes | 5 | Lot methods: FIFO, HIFO with ties, per-account effective-dated method changes |
| W15 | 4 | 3.6% | yes | 6 | Specific-lot sales: chosen lots by form and in the browser, portion order, refusals |
| W16 | 4 | 3.6% | yes | 3 | Holding periods and splits: anniversaries, February 29, forward and reverse splits |
| W17 | 3 | 2.7% | yes | 5 | Transfers between accounts: whole positions, carried basis and dates, same-day order, refusals |
| W18 | 3 | 2.7% | yes | 4 | Dividends and reinvestments: income by year, weekend pay dates, reinvested lots |
| W19 | 5 | 4.5% | yes | 3 | Wash sales I: replacements bought after the loss, edges at 30/31 days, partial matches, divided lots |
| W20 | 5 | 4.5% | yes | 3 | Wash sales II: earlier purchases, same-purchase shares, reinvestments, single use, chains |
| W21 | 5 | 4.5% | yes | 7 | Locked years and retroactive corrections: frozen reports, adjustments landing in the next year, emails |
| W22 | 3 | 2.7% | no | 3 | Locking under concurrency by an owner and a preparer: one lock, one summary per active household user, refusals |
| W23 | 2 | 1.8% | no | 3 | CSV exports: exact headers, CRLF, quoting, order, spreadsheet-safe text columns |
| W24 | 3 | 2.7% | no | 5 | In a real browser: stored and reflected markup shown as text, layout at 1280 and 390 px, uploads by click |
| W25 | 2 | 1.8% | no | 4 | Deployment: README, restart persistence, nothing re-sent, no secrets at rest, legacy records kept |
| W26 | 2 | 1.8% | no | 3 | Health follows the database: non-2xx within 10 s of a stop, 200 within 60 s of a start |
| W27 | 3 | 2.7% | no | 5 | Cross-cutting safety: secrets nowhere at rest or in logs, reset ends sessions, Host-proof links, headers, no stack traces |
| W28 | 5 | 4.5% | yes | 6 | Spin-offs: basis allocation, cash in lieu, carried holding periods, same-day splits, refusals |
| W29 | 5 | 4.5% | yes | 5 | Cash-and-stock mergers: recognized gain capped at the cash, unrecognized losses, carried lots, cash-only sales |
| W31 | 4 | 3.6% | yes | 5 | Retirement accounts: an IRA account by form, no reporting, losses disallowed for good, transfers refused |
| W30 | 4 | 3.6% | no | 5 | Returns of capital: basis reduced per lot, excess as a gain, never below zero, same-day buys untouched |

### Checks

Every check of every workflow, in order, as the oracle run recorded them (each matches a `ctx.step()` in `tests/`). A workflow scores only when all of its checks pass.

**W01** Password links: anti-enumeration, single use, replacement, expiry, address matching

1. members and the advisor set passwords from emailed links and sign in
2. a known and an unknown address get the same status and message; the unknown one gets no email
3. an inactive user's address receives no email
4. a newer link replaces the older one; the older one sets nothing
5. a used link changes nothing
6. an expired link changes nothing
7. an unknown link answers 404; sign-in matches the address ignoring case and spaces

**W02** Sessions: refusals, rotation, sign-out, cookie attributes, safe next= redirects

1. a wrong password, an unknown address and an inactive user are refused alike, with no session
2. signing in starts a new session identifier; one held before never becomes signed in
3. signing out ends the session on the server
4. a visitor is sent to /signin with next=, and lands on the page after signing in
5. next= never leaves the site (//host, /\host, scheme:host, full URLs)

**W03** Household isolation: pages, exports and form ids of other households

1. the advisor sees the households listed for them, and gets 403 on the third
2. a member gets 403 for another household's pages, exports and accounts
3. ids of another household in a member's forms are refused and change nothing

**W04** The advisor's read-only views: same figures as members, no write controls, every write refused

1. the advisor reads the same accounts, lots, year totals and exports as the household's members
2. no page offers the advisor a form or button for a change
3. every change the advisor attempts is refused with 403 and changes nothing

**W05** Roles: a read-only viewer, a member who cannot lock, a preparer who reads and locks only, an inactive preparer

1. the viewer reads the household's pages and exports exactly as the owner does
2. every change the viewer attempts is refused, and no page offers one other than their display name
3. a member sees no lock button and a lock request is refused with 403; the owner sees the button
4. the preparer reads assigned households only, may lock their year, and changes nothing else
5. an inactive preparer gets no password email and cannot sign in

**W06** CSRF on every change, mass assignment, profile edits change only the name

1. every state-changing form without the session token is refused with 403 and changes nothing
2. the profile form changes only the display name, whatever else it carries
3. extra household_id fields on account and security forms are ignored
4. an account number already on file is refused with 409; a duplicate symbol too

**W07** Northwind statements: quirks, junk lines, natural keys, overlaps, legacy references, concurrent uploads

1. a Northwind account and 14 USD/CAD securities; a quirky first statement is recorded exactly
2. re-importing the same file records nothing and counts every row as a duplicate
3. an overlapping statement (one shared row) records only its new rows
4. a known reference with different content rejects the whole file, naming that line
5. inside one file, a repeated row is a duplicate and a repeated reference with other content rejects the file
6. a statement for another account number is rejected as a whole-file problem (line 0)
7. a statement repeating the old tool's last rows for account 1 is all duplicates (legacy references count)
8. two simultaneous uploads of one new statement record each row once
9. the account's reports and lots match the rules

**W08** Harbor Direct statements: number formats, row errors, atomicity, short checks, size limit, file names

1. a Harbor account and 12 EUR/USD/CAD securities; a statement with BOM, 1.200-style numbers and reinvestments
2. a file with bad rows is rejected whole and every bad line is listed
3. a sale beyond the holding is listed by its line; an older sale that starves an existing one is listed as line 0
4. a file of exactly IMPORT_MAX_BYTES is imported; one byte more is refused with 413 and records nothing
5. file names are never paths: traversal names write nothing outside, and names are listed as text
6. the account's reports and lots match the rules

**W09** Legacy 2024 of the Raman-Okafor household: realized rows, summary with 2023 corrections, year page

1. summary.csv for 2024 answers within 60 s and matches every legacy security, 2023 corrections included
2. every realized row of 2024 matches, in order
3. the 2024 page shows its summary's totals, the lock state, and a lock button only to those who may lock

**W10** Legacy 2025 of the Raman-Okafor household: realized rows, summary, year page

1. summary.csv for 2025 answers within 60 s and matches every legacy security, 2023 corrections included
2. every realized row of 2025 matches, in order
3. the 2025 page shows its summary's totals, the lock state, and a lock button only to those who may lock

**W11** Legacy 2024 and 2025 of the Lindqvist household, read by a member and by the advisor

1. summary.csv for 2024 answers within 60 s and matches every legacy security, 2023 corrections included
2. every realized row of 2024 matches, in order
3. the 2024 page shows its summary's totals, the lock state, and a lock button only to those who may lock
4. summary.csv for 2025 answers within 60 s and matches every legacy security, 2023 corrections included
5. every realized row of 2025 matches, in order
6. the 2025 page shows its summary's totals, the lock state, and a lock button only to those who may lock
7. the advisor's exports of both years are byte-identical to the members'

**W12** Legacy open lots account by account, and unrealized gains at the stored prices

1. lots pages of accounts (1, 2, 3): every open lot's quantity, dates, basis and wash-sale marker
2. lots pages of accounts (4, 5, 8): every open lot's quantity, dates, basis and wash-sale marker
3. lots pages of accounts (6, 7, 9): every open lot's quantity, dates, basis and wash-sale marker
4. household 1: unrealized.csv values every open lot at the latest price and rate
5. household 2: unrealized.csv values every open lot at the latest price and rate

**W13** Exchange rates and rounding: fallback dates, quote directions, half-cent ties, the share rule

1. 36 CAD, EUR and USD securities with trades on days without a rate, under both quote directions, at half-cent ties
2. hand-entered buys and sales round quantity x price +/- fee to the cent first, then convert once
3. a row dated before the first exchange rate (2022-12-01) rejects its file, naming that line
4. each account page lists every recorded transaction with its markers; the year page offers lock-year only to the owner
5. every realized row, year summary and open lot of these securities matches the rules

**W14** Lot methods: FIFO, HIFO with ties, per-account effective-dated method changes

1. three accounts (FIFO, HIFO, and one switching methods four times) with 36 securities
2. account 3 records HIFO from 2025-06-01, FIFO from 2025-09-15, then FIFO and HIFO both from 2026-02-02
3. a change effective before existing sales re-matches them: account 1 switches to HIFO from 2025-09-13
4. each account page lists every recorded transaction with its markers; the year page offers lock-year only to the owner
5. every realized row, year summary and open lot of these securities matches the rules

**W15** Specific-lot sales: chosen lots by form and in the browser, portion order, refusals

1. two accounts with 20 securities of four or five lots each
2. 16 sales name lots and partial quantities; each takes exactly the named shares, whatever the method
3. named lots that do not add up, belong to another account or household, or lack the shares are refused
4. in a real browser, the sale form lists the lots; choosing two of them records exactly that sale
5. each account page lists every recorded transaction with its markers; the year page offers lock-year only to the owner
6. every realized row, year summary and open lot of these securities matches the rules

**W16** Holding periods and splits: anniversaries, February 29, forward and reverse splits

1. 32 securities: sales on, before and after anniversaries, leap days, and splits from 10:1 to 1:25
2. each account page lists every recorded transaction with its markers; the year page offers lock-year only to the owner
3. every realized row, year summary and open lot of these securities matches the rules

**W17** Transfers between accounts: whole positions, carried basis and dates, same-day order, refusals

1. 24 securities bought in two Northwind accounts, then moved whole to Harbor or the other account
2. each transfer is recorded; the destination then sells, some on the transfer day itself
3. a transfer of a security the account does not hold, or to the same account, is refused
4. each account page lists every recorded transaction with its markers; the year page offers lock-year only to the owner
5. every realized row, year summary and open lot of these securities matches the rules

**W18** Dividends and reinvestments: income by year, weekend pay dates, reinvested lots

1. 30 securities pay cash dividends (many on days without a rate) and reinvest some of them
2. a dividend entered by hand on a Sunday converts at the Friday rate
3. each account page lists every recorded transaction with its markers; the year page offers lock-year only to the owner
4. every realized row, year summary and open lot of these securities matches the rules

**W19** Wash sales I: replacements bought after the loss, edges at 30/31 days, partial matches, divided lots

1. 44 securities across three accounts and two brokers, one loss scenario each
2. each account page lists every recorded transaction with its markers; the year page offers lock-year only to the owner
3. every realized row, year summary and open lot of these securities matches the rules

**W20** Wash sales II: earlier purchases, same-purchase shares, reinvestments, single use, chains

1. 48 securities across three accounts, one scenario each
2. each account page lists every recorded transaction with its markers; the year page offers lock-year only to the owner
3. every realized row, year summary and open lot of these securities matches the rules

**W21** Locked years and retroactive corrections: frozen reports, adjustments landing in the next year, emails

1. the Castellano household records 2023-2025 activity across two accounts and 18 securities
2. before locking, the 2024 page explains locking; the current year cannot be locked
3. Elena locks 2024: its reports freeze, and each member gets one year-end summary
4. late imports (a missed 2024 buy, a January replacement, a 2023 dividend) and a 2024 method change
5. 2023 and 2024 reports are unchanged byte for byte; 2025 carries the adjustments
6. Marco, whose display name carries line breaks and a Bcc header, locks 2025; nothing leaks into headers
7. later corrections to 2024 and 2025 land in 2026 net of what 2025 already carried

**W22** Locking under concurrency by an owner and a preparer: one lock, one summary per active household user, refusals

1. the owner and the preparer lock 2024 at the same moment: exactly one succeeds
2. each active member gets exactly one summary whose totals are the frozen summary's; nobody else gets one
3. locking out of order or twice is refused with 409 and sends nothing

**W23** CSV exports: exact headers, CRLF, quoting, order, spreadsheet-safe text columns

1. accounts and securities whose names need quoting or neutralizing trade in 2026
2. realized.csv: exact header, CRLF everywhere, rows in order, text columns neutralized
3. summary.csv: rows by symbol, quoted names (commas, quotes, line breaks), neutralized formulas

**W24** In a real browser: stored and reflected markup shown as text, layout at 1280 and 390 px, uploads by click

1. names, descriptions, memos, file names and a display name carrying script payloads exist
2. uploading a statement by clicking records it and says how many rows were recorded and skipped
3. signing in through the browser form reaches a signed-in page
4. pages stay within 1280 and 390 CSS pixels without horizontal scrolling
5. typed and uploaded text, and request parts echoed on error pages, never run as script

**W25** Deployment: README, restart persistence, nothing re-sent, no secrets at rest, legacy records kept

1. README.md and an executable start.sh are in the workspace
2. after a restart: passwords, locked reports, imports and method changes are kept and no email is sent again
3. no password is stored in plain text
4. the pre-existing records are all still there, unchanged

**W26** Health follows the database: non-2xx within 10 s of a stop, 200 within 60 s of a start

1. /healthz answers 200 while the database is up
2. within 10 s of the database stopping, /healthz answers a non-2xx status within 5 s
3. within 60 s of the database starting again, /healthz answers 200 without a restart

**W27** Cross-cutting safety: secrets nowhere at rest or in logs, reset ends sessions, Host-proof links, headers, no stack traces

1. setting a new password through a reset link signs out every existing session of that user
2. a reset link always starts with PUBLIC_BASE_URL, whatever Host header the request carried
3. signed-in pages and exports carry Cache-Control: no-store and an anti-framing header
4. malformed requests get a plain error page without stack traces, SQL, or file paths
5. no password, reset token or session identifier appears in the database, the app's files or its output

**W28** Spin-offs: basis allocation, cash in lieu, carried holding periods, same-day splits, refusals

1. 34 spin-off scenarios: lots in FIFO and HIFO accounts, same-day splits, later sales of the new shares
2. the corporate-actions page lists every recorded action with its markers
3. spin-offs naming another household's security, the same security twice, another currency or a 9-decimal ratio are refused
4. a viewer's or advisor's spin-off is refused with 403 and records nothing
5. each account page lists every recorded transaction with its markers; the year page offers lock-year only to the owner
6. every realized row, year summary and open lot of these securities matches the rules

**W29** Cash-and-stock mergers: recognized gain capped at the cash, unrecognized losses, carried lots, cash-only sales

1. 30 merger scenarios: cash and stock, stock only, cash only, same-day buys of the target, later sales of the acquirer
2. the corporate-actions page lists every recorded action with its markers
3. a merger that would leave a later sale of the target short, or a cash-only merger naming an acquirer, is refused
4. each account page lists every recorded transaction with its markers; the year page offers lock-year only to the owner
5. every realized row, year summary and open lot of these securities matches the rules

**W31** Retirement accounts: an IRA account by form, no reporting, losses disallowed for good, transfers refused

1. an IRA is created through the account form and shown with data-account-type=ira
2. 26 scenarios: taxable loss sales replaced in the IRA (and sometimes a taxable account), IRA loss sales bought back, IRA dividends
3. a transfer between the IRA and a taxable account is refused with 422 and moves nothing
4. each account page lists every recorded transaction with its markers; the year page offers lock-year only to the owner
5. every realized row, year summary and open lot of these securities matches the rules

**W30** Returns of capital: basis reduced per lot, excess as a gain, never below zero, same-day buys untouched

1. 26 return-of-capital scenarios: one to three distributions, lots driven to zero, buys on the distribution date
2. the corporate-actions page lists every recorded action with its markers
3. a return of capital of 0 or with 7 decimals is refused
4. each account page lists every recorded transaction with its markers; the year page offers lock-year only to the owner
5. every realized row, year summary and open lot of these securities matches the rules

### Breach detectors

Any detector that fires caps every workflow at ×0.4. Each row lists where the detector is raised and what it reports (`…` marks a value filled in at run time).

| code | raised in | fires when |
|---|---|---|
| X1 | W03, W04, W05, W15, W28 | a member changed another household's data (lots=…, accounts=…, changes=…) / a member locked a tax year / a member read another household's data: … / a preparer read an unassigned household: … / a preparer's request changed data: … / a sale took a lot of another household / a viewer or advisor recorded a corporate action / a viewer's request changed data: … / an advisor request changed data: … / the advisor read an unassigned household: … |
| X2 | W06 | a request without the session token changed … |
| X3 | W24 | typed text ran as script: … |
| X4 | W23 | realized.csv wrote the account name … as a live formula / summary.csv wrote the name … as a live formula |
| X5 | W06 | an extra household_id field moved a new account to another household / an extra household_id field moved a new security to another household / the profile form changed … |
| X6 | W22, W25 | locked … reports changed across a restart / two lock requests for one year both succeeded / … received … summaries for one lock / … year-end summaries were sent more than once |
| X8 | W25, W27 | plain-text password in … / secrets written by the application: … |
| X9 | W02 | a session cookie copied before sign-out still signs in / a session identifier held before sign-in became signed in |
| X10 | W25 | pre-existing records changed: … |
| X11 | W02 | sign-in redirected off-site to … (next=…) |
| X12 | W08 | an upload's file name reached the file system: … |
| X13 | W21, W27 | a display name added a recipient to an email: … / … session(s) still signed in after the password was reset |
| X14 | W27 | reset link built from the request's Host header: … |

### Judged experience criteria

`anthropic/claude-fable-5-1` answers each criterion yes or no from the evidence files named for it, following `tests/judge-reference.md`; a checkpoint that was never reached fails its criterion. The oracle run scored every criterion yes.

| id | requirement | weight | evidence (written by) | criterion |
|---|---|---|---|---|
| J01 | R12.1 | 1 | `import_rejected` (W24) | After a rejected statement upload, the page says that nothing was imported and lists each failing line with a plain-language reason. |
| J02 | R12.1 | 1 | `import_done` (W24) | After a successful statement upload, the page says how many rows were recorded and how many were skipped as duplicates. |
| J03 | R12.2 | 1 | `lots_wash` (W24) | The open-lots page shows, for each lot, the quantity, cost basis, acquisition date, holding-period start and whether it is short- or long-term, and marks lots whose basis includes a disallowed wash-sale loss. |
| J04 | R12.3 | 1 | `sell_form` (W15) | The sale form lists the lots that can be chosen and explains that, without chosen lots, the account's lot method (FIFO or HIFO) decides which lots are sold. |
| J05 | R12.4 | 1 | `report_year` (W21) | The tax-year page shows labelled totals for short-term gains, long-term gains, wash-sale disallowed losses, dividends and adjustments for earlier years, and says whether the year is locked. |
| J06 | R12.4 | 1 | `lock_before` (W21) | Before locking, the year page states that locking freezes the year's reports, that later changes appear as adjustments in the next year, that each person in the household gets a summary email, and that it cannot be undone. |
| J07 | R12.4 | 1 | `report_locked` (W21) | A locked year's page makes clear that the year is locked and its figures are frozen. |
| J08 | R8.5 | 1 | `summary_email` (W21) | The year-end summary email names the household and the year and gives labelled short-term, long-term, disallowed, dividend and adjustment totals in dollars, in a form a household member can read. |
| J09 | R12.5 | 1 | `advisor_view` (W24) | Viewing an account as the advisor, the page makes clear the access is read-only and offers no action the advisor cannot take. |
| J10 | R12.6 | 1 | `lots_wash/narrow.jpg` (W24); `sell_form/narrow.jpg` (W15); `report_year/narrow.jpg` (W21); `lock_before/narrow.jpg` (W21) | At 390 pixels wide, the key facts and the primary action on these pages stay readable and reachable. |

## Controls

| control | run | reward | functional | judge |
|---|---|---|---|---|
| oracle (`solution/solve.sh`) | `20261111T104651Z-r2qc-lb-oracle` | 1.0 | 1.0 | 1.0 |
| nop (empty workspace) | `20261111T104652Z-r2qc-lb-nop` | 0.0 | 0.0 | 0.0 |

## Interface contract literals

The routes of `instruction.md`'s interface contract and every `data-action` and `data-*` marker it names. The workflows drive and read the application through these routes and markers (as well as the form fields, exports and emails the instruction specifies), so a submission that renames one fails the checks that rely on it.

| path | methods |
|---|---|
| `/password-reset` | GET, POST |
| `/password-reset/<token>` | GET, POST |
| `/signin` | GET, POST |
| `/signout` | POST |
| `/` | GET |
| `/profile` | GET, POST |
| `/households/<hid>` | GET |
| `/households/<hid>/accounts` | POST |
| `/households/<hid>/securities` | GET, POST |
| `/households/<hid>/corporate-actions` | GET, POST |
| `/households/<hid>/transfers` | GET, POST |
| `/accounts/<aid>` | GET |
| `/accounts/<aid>/imports` | POST |
| `/accounts/<aid>/transactions/new` | GET |
| `/accounts/<aid>/transactions` | POST |
| `/accounts/<aid>/lots` | GET |
| `/accounts/<aid>/method` | POST |
| `/households/<hid>/reports/<year>` | GET |
| `/households/<hid>/reports/<year>/lock` | POST |
| `/households/<hid>/reports/<year>/summary.csv` | GET |
| `/households/<hid>/reports/<year>/realized.csv` | GET |
| `/households/<hid>/unrealized.csv` | GET |
| `/healthz` | GET |

| kind | values |
|---|---|
| actions (`data-action`) | change-method, create-account, create-security, import-statement, lock-year, record-corporate-action, record-transaction, record-transfer, request-reset, save-profile, set-password, signin, signout |
| markers | data-account-id, data-account-type, data-acquired, data-action-date, data-action-id, data-basis-cents, data-csrf, data-duplicate-count, data-error-line, data-holding-start, data-household-id, data-import-filename, data-import-status, data-imported-count, data-kind, data-locked, data-lot-id, data-quantity, data-ref, data-security-id, data-symbol, data-total-adjustment-cents, data-total-disallowed-cents, data-total-dividends-cents, data-total-long-cents, data-total-short-cents, data-trade-date, data-txn-id, data-wash-adjusted, data-year |
