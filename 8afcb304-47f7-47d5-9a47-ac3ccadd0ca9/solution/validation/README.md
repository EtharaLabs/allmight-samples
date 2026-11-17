# Local validation of harborview-physio

How the task was validated on the author's machine (2026-11-10), and the defects planted to show
the workflows catch what they claim to.

> **Database version.** The runs recorded here used PostgreSQL 17, but the
> delivered images pin `postgresql-15=15.19-0+deb12u1` in both
> `environment/Dockerfile` and `tests/Dockerfile`. This record was therefore
> produced on a newer server than the task actually ships, and the results
> below have not been re-measured on 15.

## Setup

- PostgreSQL 17 (local), database and role `physio`, seeded from `solution/seed/generate.py`
(same bytes as `environment/services/db/init/` and `tests/services/db/init/`).
- Kit services from `kit/tests/services`: the SMTP mail sink and the Stripe-compatible payments
mock (admin surface on a loopback port; in the verifier it is the Unix socket
`$ADMIN_SOCKET_DIR/payments.sock`, which `tests/paylib.py` uses automatically).
- The oracle (`solution/app`) with the verifier's short settings: `HOLD_TTL_SECONDS=30`,
`OFFER_TTL_SECONDS=60`, `RESET_LINK_TTL_SECONDS=20`, `MIN_NOTICE_MINUTES=50`,
`BOOKING_HORIZON_DAYS=240` (`tests/verifier-overrides.env`).
- `tests/snapshot.py` baseline, then `tests/workflows.py --out results.json`.

W27 (database stop/start) uses the verifier's `FK_DB_STOP`/`FK_DB_START` hooks, which run
`pg_ctl stop`/`start` inside the verifier container. The local PostgreSQL server is shared, so
locally W27 ran against a stand-in hook that refuses new connections to the `physio` database
(`ALTER DATABASE physio ALLOW_CONNECTIONS false`, plus ending its sessions) and then allows them
again. The real server stop and start is exercised only on EC2. Every other workflow runs the same
way locally as in the verifier.

## Oracle results

Fresh database, all 27 workflows, 2026-11-10 (about 12 minutes of workflow time). The table below is from the
first full pass; the final full pass after the hardening round (27/27 PASS, zero breaches) is recorded at the end of this
file.

| Workflow | Weight | Critical | Result | Seconds |
|---|---|---|---|---|
| W01 Accounts: registration against quirky legacy addresses, reset links, duplicate-address accounts | 3 |  | PASS | 28.7 |
| W02 Sessions and roles: refusals, fixation, sign-out, role gates, profile mass assignment, CSRF | 3 |  | PASS | 5.8 |
| W03 Availability: 15-minute grid, fitting inside working time, buffers, merged and overlapping windows | 5 | yes | PASS | 37.4 |
| W04 Availability: effective-dated schedule versions, exceptions, schedule edits and who may make them | 4 | yes | PASS | 47.7 |
| W05 Availability: shared rooms and lowest-id assignment, daily limits with holds, fees in effect, inactive records | 5 | yes | PASS | 17.3 |
| W06 Time: notice and horizon from now, clock-change days, local dates per clinic, fees by local date | 5 | yes | PASS | 11.1 |
| W07 Legacy Seattle: the old system's schedules, appointments, folds and gaps, served as they are | 5 | yes | PASS | 57.6 |
| W08 Booking: deposits on Checkout, mass assignment, confirmation only by signed webhook, one email | 5 | yes | PASS | 17.4 |
| W09 Holds: expiry measured from creation, release, late and out-of-order payments refunded in full | 5 | yes | PASS | 73.4 |
| W10 Webhook security: unsigned, mis-signed, tampered and stale deliveries change nothing | 4 |  | PASS | 7.9 |
| W11 Races: the last slot, the last room and the last daily place each go to exactly one patient | 4 | yes | PASS | 4.1 |
| W12 Cancellation: refund bands by elapsed time, half-cent rounding, clinic cancellations, emails once | 5 | yes | PASS | 59.4 |
| W13 Refund retries: provider errors, lost answers and timeouts never refund twice; events before answers | 4 | yes | PASS | 25.9 |
| W14 Rescheduling: 24-hour window, the original never blocks itself, the deposit carries over | 4 | yes | PASS | 9.5 |
| W15 Waitlist: join order, offers that occupy, offer and hold lifetimes, decline and expiry chains | 5 | yes | PASS | 74.6 |
| W16 Clinical notes and clinician scope: only the treating clinician and admins, authors from the session | 4 |  | PASS | 2.0 |
| W17 Reminders: every booked appointment of a clinic's day once, quirky addresses normalized, never twice | 3 |  | PASS | 10.0 |
| W18 In a real browser: book and pay on the hosted page, layout at 1280 and 390 px, escaping everywhere | 3 |  | PASS | 43.2 |
| W19 Clinic closures: the old closures and new ones close every clinician; bookings stay; admins and the clinic's manager only | 3 | yes | PASS | 10.2 |
| W20 Appointment export: exact columns and order, local and UTC times with folds, quoting, spreadsheet safety | 4 | yes | PASS | 5.9 |
| W21 Front desk books for patients: holds for the account, payment-link emails, then the patient pays | 4 | yes | PASS | 36.2 |
| W22 Legacy Honolulu: the old system's schedules, appointments, closures and patients, served as they are | 5 | yes | PASS | 47.7 |
| W23 Clinic staff: managers edit their clinic's schedules, front desk books and cancels only for their clinic | 4 |  | PASS | 5.3 |
| W24 A patient's journey: browse, register, book and pay, waitlist, reschedule into an offer, cancel, export | 5 | yes | PASS | 7.1 |
| W25 Cross-cutting safety: secrets nowhere at rest or in logs, reset ends sessions, Host-proof links, headers, no stack traces | 3 |  | PASS | 0.7 |
| W26 Deployment: README, restart persistence, holds and refunds survive, nothing re-sent, no secrets, records kept | 3 |  | PASS | 42.4 |
| W27 Health follows the database: non-2xx within 10 s of a stop, 200 within 60 s of a start | 2 |  | PASS | 0.1 |

Breaches observed: none.

## Planted defects

Each defect is one realistic bug planted in a copy of the oracle (`mutants.py` holds the exact
text replaced), run on a fresh database against the workflow it targets. Every one fails that
workflow; the security defects also fire their breach detector (X1 authorization, X8 secrets at
rest, X11 open redirect, X13 session survives a password reset, X15 CSV formula injection). The
breach catalogue also has X2 (CSRF), X3 (XSS), X4 (mass assignment), X5 (webhook forgery), X6
(double effects), X7 (session fixation or sign-out survival), X9 (legacy tampering), X10
(confirmed without a verified webhook), X12 (email header injection), and X14 (Host-header reset
link).

| Defect                      | Lever                                            | Planted in              | Target | Result                                                                                                               | Breach |
| --------------------------- | ------------------------------------------------ | ----------------------- | ------ | -------------------------------------------------------------------------------------------------------------------- | ------ |
| grid-from-window-start      | rule engine                                      | `harborview/avail.py`   | W03    | FAIL: booking clinician 159 VC995F924 at 2026-11-15T12:15:00-08:00 answered 409: Not possible · Harborview Physio Ha | -      |
| room-highest-id             | rule engine (rooms)                              | `harborview/avail.py`   | W05    | FAIL: rooms of the 10:00 and 10:30 bookings expected ['17', '18'], got ['18', '17']                                  | -      |
| legacy-fold-ignored         | temporal (fold)                                  | `harborview/avail.py`   | W07    | FAIL: 3 of 2418 slot lists differ: clinician 4 FU30 2027-03-14: 16 slots, expected 16; missing ['2027-03-14T01:00:00 | -      |
| offer-keeps-offer-expiry    | temporal (TTL)                                   | `harborview/service.py` | W15    | FAIL: status after the offer's own expiry time passed expected 'held', got 'expired'                                 | -      |
| late-payment-confirms       | payments (late webhook)                          | `harborview/service.py` | W09    | FAIL: statuses once the late completions arrived expected ['expired', 'expired', 'expired'], got ['confirmed', 'conf | -      |
| refund-new-key-per-attempt  | payments (idempotency)                           | `harborview/service.py` | W13    | FAIL: 2 of 5 refunds differ: appointment 31578: got ('cancelled', '2469', 'refunded', [2469], True, True, False), wa | -      |
| deposit-rounds-down         | money                                            | `harborview/service.py` | W08    | FAIL: 32 of 40 holds differ: V4960DA10 appointment 31578: got (3436, 'usd', 'payment', '31578', 'http://127.0.0.1:58 | -      |
| notes-visible-to-desk       | security (IDOR)                                  | `harborview/web.py`     | W16    | FAIL: front desk reading the notes expected 403, got 200                                                             | X1     |
| open-redirect               | security (redirect)                              | `harborview/web.py`     | W02    | FAIL: open redirects: ["'//evil.example/x' -> '//evil.example/x'", "'/\\\\evil.example/x' -> '/\\\\evil.example/x'", | X11    |
| closures-ignored            | rule engine (closures)                           | `harborview/avail.py`   | W19    | FAIL: 130 of 396 slot lists differ: clinician 2 FU30 2026-11-26: 24 slots, expected 0; missing [] extra ['2026-11-26 | -      |
| reschedule-blocks-itself    | lifecycle (reschedule)                           | `harborview/service.py` | W14    | FAIL: rescheduling 31578 answered 409 without redirecting to the new appointment                                     | -      |
| export-formula-live         | security (CSV injection)                         | `harborview/service.py` | W20    | FAIL: seattle-legacy: 573 rows, expected 573; row 157 is ['1028', '2026-10-23', '15:45', '2026-10-23T22:45:00Z', '55 | X15    |
| deposit-half-even           | money (ties)                                     | `harborview/service.py` | W08    | FAIL: 32 of 40 holds differ: VF2C61710 appointment 31578: got (3436, 'usd', 'payment', '31578', 'http://127.0.0.1:58 | -      |
| desk-sees-every-clinic      | security (tenant authorization)                  | `harborview/web.py`     | W23    | FAIL: Honolulu front desk opening a Seattle appointment expected 403, got 200                                        | X1     |
| book-slot-for-everyone      | UI contract                                      | `harborview/web.py`     | W05    | FAIL: book-slot buttons for a patient, a visitor and front desk expected (25, 0, 0), got (25, 25, 25)                | -      |
| reset-link-in-outbox        | security (secrets at rest)                       | `harborview/web.py`     | W25    | FAIL: secrets stored in files [] or database ['hv_outbox.body']                                                      | X8     |
| reset-keeps-sessions        | security (session survival)                      | `harborview/web.py`     | W25    | FAIL: sessions survived the password reset: ['first', 'second']                                                      | X13    |
| offer-deposit-at-offer-time | money/lifecycle                                  | `harborview/service.py` | W15    | FAIL: 6 of 12 accepted offers differ: offer for clinician 153 on 2026-11-17 (new fee row added before acceptance, ef | -      |
| offer-email-in-outbox       | security (offer token at rest)                   | `harborview/service.py` | W25    | FAIL: secrets stored in files [] or database ['hv_outbox.body']                                                      | X8     |

## Hardening round (2026-11-10)

Known limitation forced by the hashing rule: reset and offer emails carry tokens that are stored only as hashes,
so the oracle sends them right after the transaction commits and never stores them. A crash between that commit
and the SMTP send loses that one email (the patient can request a new reset link; an offer whose email was lost
still expires and passes to the next patient).

Final full pass after the hardening round (fresh database, 2026-11-10): 27 of 27 workflows PASS, breaches
none, 625 s of workflow time; W15 accepted all 12 fee-change offers in time (0 skipped), W25
scanned 16 offer tokens.

## QC fix pass (2026-11-11)

Settings: `OFFER_TTL_SECONDS` is now 60 (was 30). W15 times every offer from the moment the verifier made it
(the cancellation or decline request), not from when the email was seen, so an offer email that arrives up to
about 20 s late still leaves time to accept inside the offer.

Calendar horizon: `valid_until = "2030-01-31"` (task.toml `[metadata]`).

- The seed's dense legacy data covers 2026-10-01 to 2027-06-30. Sparse later rows extend it to 2030-06-30:
  bookings on about 7% of clinician-days (none before 04:00 local, away from clock-change hours), one to five
  schedule exceptions per clinician, and 24 holiday closures. They come from separate random streams and are
  appended, so every earlier row and id is unchanged.
- The tightest consumer is W17, which needs old Honolulu bookings within 120 days. On 2030-01-31 it still finds
  about 340 of them (about 40 with quirky addresses). W07 and W22 still find at least 400 future old bookings per
  clinic (at least 50 with quirky addresses), and W19 still has old closures inside the 240-day horizon. After
  2030-06 W17 and W07 run out of fixtures.
- Fixed offsets from today (today+3 to today+10) were used as booking days. Grading on a date that put one on an
  old closure would have failed the oracle; for example, grading on 2026-11-21 puts today+5 on Thanksgiving.
  `H.open_day` and `H.quiet_block` now skip closures at both clinics.
- W06 also had a seasonal fixture: "instants differ" assumed Seattle is two hours ahead of Honolulu, which holds
  only in winter. It now checks that 00:30 in Seattle is still the previous date in Honolulu, which is true all
  year.

Judge evidence no longer depends on strict checks passing:

- W08, W12 and W15 capture their email as soon as it arrives, before the checks on it.
- W18 captures each page before the checks on it, and runs its second evidence step even when the first fails.
  The layout and escaping steps run only after both evidence steps.
- If W08, W12 or W15 stops before its capture, `evidence_backstop` builds that email from a fresh minimal
  scenario. This is best effort, is never a check, and runs inside the workflow's time limit.
- A forced first-step failure of W08, W12 and W15 against the oracle produced all three emails with their
  truth files.

Full pass after this QC round (fresh database with the extended seed, 2026-11-11): 27 of 27 workflows PASS,
breaches none, 714 s of workflow time. All ten judge checkpoints have page text and truth files. W19 compared
396 slot lists on old closure dates; W15 skipped 0 fee-change offers; W25 scanned 16 offer tokens.

## QC round 2 (2026-11-11)

- No hold waits behind a batch for its payment. `adjacent_bookings` (W03, W05) pays each hold's Checkout
  Session right after the hold is made and polls only the confirmations in batches; `Harness.book_many`
  already did. W21 pays each desk hold except the last as soon as it is made, and its third step waits for the
  confirmations. Against a copy of the oracle that answers every page and form 0.8 s late (the availability
  API excluded to keep the run short), W03 (981 s), W05 (590 s), W14 (302 s) and W21 (115 s) all PASS with no
  breaches and no hold expiring before its payment.
- W18 creates the script-payload room (with its own room type), type, clinician, patient, booking and note in a
  step just before the escaping step, so the evidence steps never touch them. The return-page URL and the expired
  hold's status are asserted after the later captures.
- Spec (R4.5, exception times): "(`24:00` is allowed as an end, as for windows)".
- Oracle on a fresh database: 27 of 27 PASS, breaches none, 708 s; all ten judge checkpoints captured. The 19
  planted defects above were re-run against these tests and the extended seed: each fails its target workflow,
  with the breach shown.

## QC round 3 (2026-11-11): run-date fixtures

- `slot_near` now falls back to other windows when a clinic closure covers the whole window asked for.
  - W14's under-24-hour refusal takes any slot from 0.9 to 23.75 h ahead. It is skipped only when no slot under
    24 h exists; the skip is recorded with `ctx.observe`, and the expected confirmation count drops from 2 to 1.
  - The "few hours away" moves in W14 and W24 fall back from 3–20 h to 1–23 h, then 25–70 h.
  - W24's button check now uses the start time actually chosen.
- W05's four room groups now share one Monday quiet block. The groups' hours are a fixed seeded draw, so they
  always take 121 bookings, whatever the run date. W23 uses the coordinator's `first_open_slot`.
- Offline simulation with `tests/reference/engine.py` and the seed's 34 closures:
  - Every 15 minutes from 2026-11-11 to 2027-01-05, and every 30 minutes within three days of each later
    Seattle closure up to 2030, no fixture failed.
  - The under-24-hour refusal was skipped in 18 of 5,280 quarter-hours. Each was a time when no slot under 24 h
    existed.
  - W05, at five run times a day from 2026-11-11 to 2030-01-31, took 121 bookings every time.
- Local oracle on a fresh database: 27 of 27 PASS, breaches none, 706 s (W05: 121 bookings).
- The 19 planted defects, re-run against these tests: each still fails its target workflow, with the same
  breaches as the table above.
