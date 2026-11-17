# TRUTH: ethara/shiftbook-payroll

Task version 1.0.0.

Sources: `task.toml`, `README.md`, `instruction.md`, `environment/`, `solution/`, `tests/` (`workflows.toml`, `reward.toml`, `judge.toml`, `scoring.py`, `test.sh` and the workflow code), the seed loaded into PostgreSQL, and the verifier output of oracle run `20261111T075750Z-r2qc-sb-oracle` (`results.json` sha256 `4082c3f3f8933d0c3b0156c91bfc7164ccd497181248109022d92705b09356ec`).

This file is for graders and reviewers. The agent never sees it: Harbor does not copy `solution/` into the agent's container, and `environment/Dockerfile` copies only `uv-requirements.lock`, `services`.

## Reward and pass rule

Full reward is `1.0` on the `reward` key of `/logs/verifier/reward.json` (keys `reward`, `functional`, `judge`).

| term | rule | source |
|---|---|---|
| reward | `0.9 × functional + 0.1 × judge` (weighted sum) | `tests/reward.toml` |
| functional | weighted mean of 23 strict workflows (weights sum to 87); a workflow scores 1 only when every one of its checks passes, else 0 | `tests/workflows.toml`, `tests/scoring.py` |
| cap | any breach detector firing anywhere in the run (×0.4), or any of the 12 critical workflows failing (×0.4), caps every workflow, so the reward is at most 0.46; a critical workflow that fails only because the verifier's time budget ran out scores 0 without the cap | `tests/scoring.py` |
| judge | weighted mean of 10 binary criteria, each judged by `anthropic/claude-fable-5-1` (reasoning effort max, 3 samples) from the checkpoint evidence under `/logs/verifier/ux/` | `tests/judge.toml`, `tests/judge-reference.md` |
| invalid run | `test.sh` writes a zero reward first; a verifier, service or judge failure leaves that zero and writes `/logs/verifier/evaluation-invalid.json` | `tests/test.sh` |

## Golden trajectory

| step | what happens | source |
|---|---|---|
| S1 | the environment starts PostgreSQL seeded with the existing records and the SMTP mail sink, and writes `/etc/shiftbook/services.env` | `environment/services/up.sh` |
| S2 | `solve.sh` copies `solution/app/` into `/app`, makes a copy-based virtual environment, and installs `requirements.txt` offline from the 4 wheels in `solution/wheels/` | `solution/solve.sh` |
| S3 | the verifier starts `/app/start.sh` (`exec ./.venv/bin/python -u app.py`) on a fresh copy of the database; in the oracle run `/healthz` answered 200 after 1 s | `solution/app/start.sh`, `startup.json` |
| S4 | the verifier runs the 23 workflows (127 checks) and the judge; the oracle passed every check, fired no breach detector, and scored functional 1.0, judge 1.0, reward 1.0 | `results.json`, `reward.json` |

Reference stack (`solution/app/README.md`): Python 3.12 standard library HTTP server (`http.server.ThreadingHTTPServer`) with psycopg 3 for PostgreSQL, server-rendered HTML, and `smtplib` for email.

## Records the checkers rely on

The agent's database and the verifier's are both seeded from `db/init/` (`001_schema.sql`, `002_records.sql`); `tests/services/db/` is byte-identical to `environment/services/db/` (tree sha256 `d1d8566a5ffd2ee9fa32a44916983df733a5f40177a0028669552a041d6f62a9`). Row counts after loading the seed into PostgreSQL:

| table | rows |
|---|---|
| `employees` | 1500 |
| `job_rates` | 2679 |
| `legacy_approvals` | 4203 |
| `legacy_punches` | 96794 |
| `locations` | 3 |

Volumes the checks compared in oracle run `20261111T075750Z-r2qc-sb-oracle` (whole-number observations from `results.json`; counts that depend on the run date can differ on another day):

| workflow | observed |
|---|---|
| W12 | mismatches_2026-10-18 0; mismatches_2026-11-01 0 |
| W21 | mismatches_2026-10-18 0; mismatches_2026-11-01 0 |
| W22 | mismatches_2026-10-18 0; mismatches_2026-11-01 0 |

## Rubric

### Strict workflows

| id | weight | share | critical | checks | workflow |
|---|---|---|---|---|---|
| W01 | 3 | 3.4% | no | 8 | Password links: anti-enumeration, single use, replacement, expiry, address matching |
| W02 | 3 | 3.4% | no | 9 | Sign-in refusals, lockout, session rotation and sign-out |
| W03 | 3 | 3.4% | no | 5 | Staff and rates: creation, duplicates, effective-dated rates, staff-page protection |
| W04 | 4 | 4.6% | no | 6 | Time-clock keys and the punch API: auth, validation, idempotency, scope, races |
| W05 | 3 | 3.4% | no | 4 | Timesheets: local days, midnight and DST splits, status markers |
| W06 | 5 | 5.7% | yes | 5 | Pay rules: CA daily overtime and double time |
| W07 | 5 | 5.7% | yes | 5 | Pay rules: weekly overtime without pyramiding (CA and FLSA) |
| W08 | 4 | 4.6% | yes | 5 | Pay rules: the CA seventh consecutive day |
| W09 | 5 | 5.7% | yes | 6 | Pay rules: regular rate across jobs, effective-dated rates, work before a job's first rate |
| W10 | 5 | 5.7% | yes | 7 | Pay rules: midnight, workweek, pay-period and DST boundaries; rounding ties |
| W11 | 4 | 4.6% | yes | 5 | Pay rules: CA meal premiums |
| W20 | 4 | 4.6% | yes | 2 | Exceptions across week boundaries: interrupted, unclosed and over-16-hour shifts |
| W12 | 4 | 4.6% | yes | 6 | Legacy data at scale: Mission St (CA rules, Los Angeles) lines and held weeks |
| W21 | 4 | 4.6% | yes | 6 | Legacy data at scale: Roosevelt Row (FLSA, Phoenix) lines and held weeks |
| W22 | 4 | 4.6% | yes | 6 | Legacy data at scale: Wicker Park (FLSA, Chicago) lines and held weeks |
| W13 | 4 | 4.6% | no | 8 | Approvals: reporting tree, self-approval, peers, invalidation, CSRF |
| W14 | 5 | 5.7% | yes | 8 | Payroll run lifecycle: stale drafts, one finalization, lock, pay statements once |
| W15 | 5 | 5.7% | yes | 3 | Adjustments: late punches, late approvals and rate changes reach the next run |
| W16 | 3 | 3.4% | no | 6 | CSV export: exact format, quoting, spreadsheet safety; profile edits change only the name |
| W17 | 3 | 3.4% | no | 5 | In a real browser: layout at 1280 and 390 px, escaping everywhere, clicking through |
| W18 | 2 | 2.3% | no | 4 | Deployment: README, restart persistence, nothing re-sent, no secrets (passwords, keys, reset tokens, sessions) at rest, records kept |
| W19 | 2 | 2.3% | no | 3 | Health follows the database: non-2xx within 10 s of a stop, 200 within 60 s of a start |
| W23 | 3 | 3.4% | no | 5 | Cross-cutting safety: no secrets in the app's files, reset ends sessions, Host-proof links, headers, no stack traces |

### Checks

Every check of every workflow, in order, as the oracle run recorded them (each matches a `ctx.step()` in `tests/`). A workflow scores only when all of its checks pass.

**W01** Password links: anti-enumeration, single use, replacement, expiry, address matching

1. both payroll admins set passwords from emailed links and sign in
2. a known and an unknown address get the same status and message; the unknown one gets no email
3. an inactive employee's address receives no email
4. a newer link replaces the older one; the older one sets nothing
5. a used link changes nothing
6. an expired link changes nothing
7. an unknown link answers 404
8. sign-in matches the address case-insensitively, ignoring surrounding spaces

**W02** Sign-in refusals, lockout, session rotation and sign-out

1. set a password for a staff member
2. a wrong password and an unknown address are refused with the same message and no session
3. five failures lock the account, even against the right password
4. the lock lifts LOGIN_LOCKOUT_SECONDS after the fifth failure
5. a successful sign-in resets the failure count
6. signing in starts a new session identifier; an identifier held before never becomes signed in
7. signing out ends the session on the server
8. a visitor sent to sign in from a protected page returns to that page after signing in
9. sign-in never redirects to another site, whatever next= says

**W03** Staff and rates: creation, duplicates, effective-dated rates, staff-page protection

1. a payroll admin adds an employee with a first rate
2. an address already on file is refused whatever its case and spacing; nothing changes
3. a rate is added; the same job and date again is refused
4. adding a rate without the session token is refused with 403 and changes nothing
5. staff pages refuse a signed-in employee with 403 and a visitor is sent to sign in

**W04** Time-clock keys and the punch API: auth, validation, idempotency, scope, races

1. a time clock's key is shown once at creation and never again
2. an unknown key is refused with 401; a malformed body with 400
3. each row is accepted, a duplicate, or rejected for the stated reasons
4. the timesheet shows exactly the accepted punches for 2026-10-06 in Los Angeles
5. simultaneous requests carrying one punch_id accept it once
6. a revoked key is refused with 401 and records nothing

**W05** Timesheets: local days, midnight and DST splits, status markers

1. create staff in all three locations and send their punches
2. each day of each week shows the expected REG/OT/DT hours in the employee's local time
3. an out punch with no open shift makes the week an exception that cannot be approved
4. approving a pending week marks it approved

**W06** Pay rules: CA daily overtime and double time

1. create the staff for this rule and send their punches
2. before approval, timesheets follow the interface contract
3. approve every week with worked time
4. after approval, timesheets follow the interface contract
5. the draft run for 2026-10-04 pays every hour and cent as the rules say

**W07** Pay rules: weekly overtime without pyramiding (CA and FLSA)

1. create the staff for this rule and send their punches
2. before approval, timesheets follow the interface contract
3. approve every week with worked time
4. after approval, timesheets follow the interface contract
5. the draft run for 2026-10-04 pays every hour and cent as the rules say

**W08** Pay rules: the CA seventh consecutive day

1. create the staff for this rule and send their punches
2. before approval, timesheets follow the interface contract
3. approve every week with worked time
4. after approval, timesheets follow the interface contract
5. the draft run for 2026-10-04 pays every hour and cent as the rules say

**W09** Pay rules: regular rate across jobs, effective-dated rates, work before a job's first rate

1. create the staff for this rule and send their punches
2. before approval, timesheets follow the interface contract
3. approve every week with worked time
4. after approval, timesheets follow the interface contract
5. the draft run for 2026-10-04 pays every hour and cent as the rules say
6. work before a job's first rate holds that week and the draft lists it as held

**W10** Pay rules: midnight, workweek, pay-period and DST boundaries; rounding ties

1. create the staff for this rule and send their punches
2. before approval, timesheets follow the interface contract
3. approve every week with worked time
4. after approval, timesheets follow the interface contract
5. the draft run for 2026-10-04 pays every hour and cent as the rules say
6. the draft run for 2026-10-18 pays every hour and cent as the rules say
7. the draft run for 2026-11-01 pays every hour and cent as the rules say

**W11** Pay rules: CA meal premiums

1. create the staff for this rule and send their punches
2. before approval, timesheets follow the interface contract
3. approve every week with worked time
4. after approval, timesheets follow the interface contract
5. the draft run for 2026-10-04 pays every hour and cent as the rules say

**W20** Exceptions across week boundaries: interrupted, unclosed and over-16-hour shifts

1. staff whose punches break the rules across the 2026-10-10/11 week boundary
2. the draft for 2026-10-04 pays only exception-free weeks and holds the others

**W12** Legacy data at scale: Mission St (CA rules, Los Angeles) lines and held weeks

1. the draft for 2026-10-18 answers within 120 s
2. every legacy Mission St, San Francisco line for 2026-10-18 matches the rules
3. the draft for 2026-10-18 lists exactly this location's held legacy weeks
4. the draft for 2026-11-01 answers within 120 s
5. every legacy Mission St, San Francisco line for 2026-11-01 matches the rules
6. the draft for 2026-11-01 lists exactly this location's held legacy weeks

**W21** Legacy data at scale: Roosevelt Row (FLSA, Phoenix) lines and held weeks

1. the draft for 2026-10-18 answers within 120 s
2. every legacy Roosevelt Row, Phoenix line for 2026-10-18 matches the rules
3. the draft for 2026-10-18 lists exactly this location's held legacy weeks
4. the draft for 2026-11-01 answers within 120 s
5. every legacy Roosevelt Row, Phoenix line for 2026-11-01 matches the rules
6. the draft for 2026-11-01 lists exactly this location's held legacy weeks

**W22** Legacy data at scale: Wicker Park (FLSA, Chicago) lines and held weeks

1. the draft for 2026-10-18 answers within 120 s
2. every legacy Wicker Park, Chicago line for 2026-10-18 matches the rules
3. the draft for 2026-10-18 lists exactly this location's held legacy weeks
4. the draft for 2026-11-01 answers within 120 s
5. every legacy Wicker Park, Chicago line for 2026-11-01 matches the rules
6. the draft for 2026-11-01 lists exactly this location's held legacy weeks

**W13** Approvals: reporting tree, self-approval, peers, invalidation, CSRF

1. build a reporting tree with accounts
2. viewing follows the tree: managers above see, peers and colleagues get 403
3. /approvals lists exactly the pending weeks the manager may approve
4. nobody approves their own week or a week outside their branch; a token-less approval is refused
5. the manager two levels up approves; the week is approved
6. a later punch returns the approved week to pending
7. a new punch returns a week approved by the old system to pending
8. an exception week cannot be approved

**W14** Payroll run lifecycle: stale drafts, one finalization, lock, pay statements once

1. staff with approved weeks in the period starting 2026-09-20
2. the draft carries a version
3. a punch after the draft makes finalizing it (from the browser) refused with 409; it stays a draft
4. two simultaneous finalize requests for the recomputed draft: exactly one succeeds
5. each employee with a line gets exactly one pay statement within 30 s
6. a date that is not the first day of a pay period is refused
7. a finalized period cannot get a new draft, and finalizing again changes nothing
8. a late punch for a paid week is re-approved (it will be paid as an adjustment)

**W15** Adjustments: late punches, late approvals and rate changes reach the next run

1. staff with work in the period starting 2026-10-04; finalize it
2. after the run: a late punch, a raise, a cut, a late approval, and an unapproved change
3. the next run pays each difference as an adjustment, and nothing for the unapproved change

**W16** CSV export: exact format, quoting, spreadsheet safety; profile edits change only the name

1. staff whose names need quoting or neutralizing have approved work in 2026-11-29
2. the export has the exact header, CRLF rows ordered by employee_id, and correct quoting
3. names starting with = + - @ or a tab are written with a leading quote; numbers stay plain
4. an employee changes their display name; extra fields change nothing; a token-less change is refused
5. names with control characters are refused; nothing typed adds an email header or recipient
6. emails are addressed with the employee's display name and address in To

**W17** In a real browser: layout at 1280 and 390 px, escaping everywhere, clicking through

1. staff, labels and job codes carrying script payloads exist; a manager has a pending week
2. signing in through the browser form reaches a signed-in page
3. every page stays within 1280 and 390 CSS pixels without horizontal scrolling
4. names, labels and job codes are shown as text on every page (no script runs)
5. the manager approves a week by clicking the button; the page reflects it

**W18** Deployment: README, restart persistence, nothing re-sent, no secrets (passwords, keys, reset tokens, sessions) at rest, records kept

1. README.md and an executable start.sh are in the workspace
2. after a restart: passwords, runs and punches are kept and no email is sent again
3. no password or time-clock key is stored in plain text
4. the old system's records are all still there, unchanged

**W19** Health follows the database: non-2xx within 10 s of a stop, 200 within 60 s of a start

1. /healthz answers 200 while the database is up
2. within 10 s of the database stopping, /healthz answers a non-2xx status within 5 s
3. within 60 s of the database starting again, /healthz answers 200 without a restart

**W23** Cross-cutting safety: no secrets in the app's files, reset ends sessions, Host-proof links, headers, no stack traces

1. setting a new password through a reset link signs out every existing session
2. a reset link always starts with PUBLIC_BASE_URL, whatever Host header the request carried
3. signed-in pages carry Cache-Control: no-store and an anti-framing header
4. malformed requests get a plain error page without stack traces, SQL, or file paths
5. no password, reset token, session identifier, or time-clock key is stored in the app's files

### Breach detectors

Any detector that fires caps every workflow at ×0.4. Each row lists where the detector is raised and what it reports (`…` marks a value filled in at run time).

| code | raised in | fires when |
|---|---|---|
| X1 | W03, W13 | a signed-in employee created an employee through the staff form / an employee downloaded a payroll export / … saw another branch's timesheet / … succeeded |
| X2 | W03, W13, W16 | a profile change without the session token took effect / a rate was added by a request without the session token / an approval without the session token took effect |
| X3 | W17 | typed text ran as script: … |
| X4 | W16 | export wrote … as a live spreadsheet formula |
| X5 | W16 | profile form changed … |
| X6 | W14, W18 | a finalized run's export changed / a finalized run's export changed across a restart / employee … received … pay statements for one run / … pay statements were sent more than once |
| X7 | W04 | a clock for one location recorded a punch for an employee of another location / a punch was accepted for an inactive employee / a revoked time clock key recorded a punch |
| X8 | W18, W23 | plain-text secret in … / secrets written by the application: … |
| X9 | W02 | a session cookie copied before sign-out still signs in / a session identifier held before sign-in became signed in |
| X10 | W18 | pre-existing records changed: … |
| X11 | W02 | sign-in redirected to … for next=… |
| X12 | W16 | a typed name added an email header or recipient: … |
| X13 | W23 | … session(s) still signed in after the password was reset |
| X14 | W23 | reset link built from the request's Host header: … |

### Judged experience criteria

`anthropic/claude-fable-5-1` answers each criterion yes or no from the evidence files named for it, following `tests/judge-reference.md`; a checkpoint that was never reached fails its criterion. The oracle run scored every criterion yes.

| id | requirement | weight | evidence (written by) | criterion |
|---|---|---|---|---|
| J01 | R12.1 | 1 | `timesheet_exception` (W17) | The timesheet makes clear that the week has a punch exception and explains it in plain language. |
| J02 | R12.1 | 1 | `timesheet_exception` (W17) | On the timesheet, a person can read each day's hours by category (regular, overtime, double time) and the week's status. |
| J03 | R12.2 | 1 | `approve_before` (W17) | Before approving, the page states what approving means: the week becomes payable in the next payroll run, and a later change returns it to pending. |
| J04 | R12.3 | 1 | `run_draft` (W15) | The draft run page states which weeks are held and why. |
| J05 | R12.3 | 1 | `run_adjustments` (W15) | The draft run page makes clear which lines include adjustments for earlier weeks. |
| J06 | R12.4 | 1 | `finalize_before` (W14) | Before finalizing, the page states that finalizing locks the run, emails pay statements, and cannot be undone. |
| J07 | R12.4 | 1 | `finalize_refused` (W14) | After the refused finalize, the page explains that the draft is out of date and what to do next (recompute or reload, review, then finalize). |
| J08 | R2.5 | 1 | `lockout` (W17) | After repeated failed sign-ins, the page says the account is temporarily locked and that the person can try again later. |
| J09 | R7.5 | 1 | `paystub_email` (W14) | The pay statement email names the pay period, the hours by category, and the gross pay, in a form a staff member can read. |
| J10 | R12.5 | 1 | `approve_before/narrow.jpg` (W17); `run_draft/narrow.jpg` (W15); `finalize_before/narrow.jpg` (W14) | At 390 pixels wide, the key facts and the primary action on these pages stay readable and reachable. |

## Controls

| control | run | reward | functional | judge |
|---|---|---|---|---|
| oracle (`solution/solve.sh`) | `20261111T075750Z-r2qc-sb-oracle` | 1.0 | 1.0 | 1.0 |
| nop (empty workspace) | `20261111T075754Z-r2qc-sb-nop` | 0.0 | 0.0 | 0.0 |

## Interface contract literals

The routes of `instruction.md`'s interface contract and every `data-action` and `data-*` marker it names. The workflows drive and read the application through these routes and markers (as well as the form fields, exports and emails the instruction specifies), so a submission that renames one fails the checks that rely on it.

| path | methods |
|---|---|
| `/password-reset` | GET, POST |
| `/password-reset/<token>` | GET, POST |
| `/signin` | GET, POST |
| `/signout` | POST |
| `/profile` | GET, POST |
| `/timesheet` | GET |
| `/employees/<id>/timesheet` | GET |
| `/employees/<id>/weeks/<week>/approve` | POST |
| `/approvals` | GET |
| `/admin/employees/new` | GET, POST |
| `/admin/employees/<id>` | GET |
| `/admin/employees/<id>/rates` | POST |
| `/admin/devices` | GET, POST |
| `/admin/devices/<id>/revoke` | POST |
| `/api/punches` | POST |
| `/payroll` | GET |
| `/payroll/runs` | POST |
| `/payroll/runs/<id>` | GET |
| `/payroll/runs/<id>/finalize` | POST |
| `/payroll/runs/<id>/export.csv` | GET |
| `/healthz` | GET |

| kind | values |
|---|---|
| actions (`data-action`) | add-rate, approve-week, create-device, create-employee, create-run, finalize-run, request-reset, revoke-device, save-profile, set-password, signin, signout |
| markers | data-csrf, data-day, data-device-id, data-device-key, data-dt-hours, data-employee-id, data-gross-cents, data-held-employee-id, data-line-employee-id, data-ot-hours, data-period-start, data-reg-hours, data-run-id, data-run-status, data-status, data-version, data-week-start |
