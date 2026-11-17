# TRUTH: ethara/harborview-physio

Task version 1.0.0.

Sources: `task.toml`, `README.md`, `instruction.md`, `environment/`, `solution/`, `tests/` (`workflows.toml`, `reward.toml`, `judge.toml`, `scoring.py`, `test.sh` and the workflow code), the seed loaded into PostgreSQL, and the verifier output of oracle run `20261111T111418Z-r2qc-ph-oracle` (`results.json` sha256 `bba2de314b91868183e64f2876730c1091ad49f4b1040b55967a61a0dc2b0599`).

This file is for graders and reviewers. The agent never sees it: Harbor does not copy `solution/` into the agent's container, and `environment/Dockerfile` copies only `uv-requirements.lock`, `services`.

## Reward and pass rule

Full reward is `1.0` on the `reward` key of `/logs/verifier/reward.json` (keys `reward`, `functional`, `judge`).

| term | rule | source |
|---|---|---|
| reward | `0.9 × functional + 0.1 × judge` (weighted sum) | `tests/reward.toml` |
| functional | weighted mean of 27 strict workflows (weights sum to 109); a workflow scores 1 only when every one of its checks passes, else 0 | `tests/workflows.toml`, `tests/scoring.py` |
| cap | any breach detector firing anywhere in the run (×0.4), or any of the 17 critical workflows failing (×0.4), caps every workflow, so the reward is at most 0.46; a critical workflow that fails only because the verifier's time budget ran out scores 0 without the cap | `tests/scoring.py` |
| judge | weighted mean of 10 binary criteria, each judged by `anthropic/claude-fable-5-1` (reasoning effort max, 3 samples) from the checkpoint evidence under `/logs/verifier/ux/` | `tests/judge.toml`, `tests/judge-reference.md` |
| invalid run | `test.sh` writes a zero reward first; a verifier, service or judge failure leaves that zero and writes `/logs/verifier/evaluation-invalid.json` | `tests/test.sh` |

## Golden trajectory

| step | what happens | source |
|---|---|---|
| S1 | the environment starts PostgreSQL seeded with the existing records, the SMTP mail sink and the Stripe-compatible payments mock, and writes `/etc/physio/services.env` | `environment/services/up.sh` |
| S2 | `solve.sh` copies `solution/app/` into `/app`, makes a copy-based virtual environment, and installs `requirements.txt` offline from the 4 wheels in `solution/wheels/` | `solution/solve.sh` |
| S3 | the verifier starts `/app/start.sh` (`exec ./.venv/bin/python -u app.py`) on a fresh copy of the database; in the oracle run `/healthz` answered 200 after 1 s | `solution/app/start.sh`, `startup.json` |
| S4 | the verifier runs the 27 workflows (144 checks) and the judge; the oracle passed every check, fired no breach detector, and scored functional 1.0, judge 1.0, reward 1.0 | `results.json`, `reward.json` |

Reference stack (`solution/app/README.md`): Python 3.12 standard library HTTP server (`http.server.ThreadingHTTPServer`) with psycopg 3 for PostgreSQL, server-rendered HTML, `urllib` for the payment provider, and `smtplib` for email.

## Records the checkers rely on

The agent's database and the verifier's are both seeded from `db/init/` (`001_schema.sql`, `002_records.sql`); `tests/services/db/` is byte-identical to `environment/services/db/` (tree sha256 `eae498e0aef2938d1ab28cf947b92ae606fdf6ac5941071b76c749c91489ad6d`). Row counts after loading the seed into PostgreSQL:

| table | rows |
|---|---|
| `appointment_types` | 6 |
| `appointments` | 31577 |
| `clinic_closures` | 34 |
| `clinical_notes` | 971 |
| `clinicians` | 150 |
| `clinics` | 2 |
| `rooms` | 16 |
| `schedule_exceptions` | 1084 |
| `schedule_versions` | 286 |
| `schedule_windows` | 2158 |
| `type_fees` | 9 |
| `users` | 4390 |

Users by role: `admin` 2; `clinician` 150; `front_desk` 6; `manager` 2; `patient` 4230.

Volumes the checks compared in oracle run `20261111T111418Z-r2qc-ph-oracle` (whole-number observations from `results.json`; counts that depend on the run date can differ on another day):

| workflow | observed |
|---|---|
| W03 | clinicians 120; bookings 147; w03_lists 2160; w03_nonempty 1803 |
| W04 | exceptions 452; w04_lists 3200; w04_nonempty 2551 |
| W05 | bookings 121; w05_groups_lists 192; w05_groups_nonempty 83; w05_misc_lists 31; w05_misc_nonempty 2 |
| W06 | w06_notice_lists 120; w06_notice_nonempty 60; w06_dst_lists 480; w06_dst_nonempty 395 |
| W07 | w07_lists 2418; w07_nonempty 1711 |
| W09 | w09_held_lists 3; w09_held_nonempty 3; w09_still_held_lists 3; w09_still_held_nonempty 3; w09_expired_lists 3; w09_expired_nonempty 3; w09_after_late_lists 3; w09_after_late_nonempty 3 |
| W11 | w11_room_lists 2; w11_room_nonempty 2; w11_limit_lists 1; w11_limit_nonempty 0 |
| W12 | w12_freed_lists 8; w12_freed_nonempty 8 |
| W14 | w14_same_day_lists 1; w14_same_day_nonempty 1; w14_moved_lists 2; w14_moved_nonempty 2; moves 39; w14_moves_lists 20; w14_moves_nonempty 10 |
| W15 | fee_change_offers 12; fee_change_offers_skipped 0 |
| W19 | legacy_closure_lists 360; w19_legacy_lists 360; w19_legacy_nonempty 125; w19_new_lists 111; w19_new_nonempty 36 |
| W20 | seattle-legacy_rows 573; honolulu-legacy_rows 617; seattle-new_rows 567 |
| W21 | w21_after_lists 3; w21_after_nonempty 3 |
| W22 | w22_lists 2100; w22_nonempty 1513 |
| W23 | w23_hours_lists 8; w23_hours_nonempty 7; w23_untouched_lists 2; w23_untouched_nonempty 2 |
| W24 | w24-export_rows 3 |
| W25 | offer_tokens_scanned 16 |

## Rubric

### Strict workflows

| id | weight | share | critical | checks | workflow |
|---|---|---|---|---|---|
| W01 | 3 | 2.8% | no | 6 | Accounts: registration against quirky legacy addresses, reset links, duplicate-address accounts |
| W02 | 3 | 2.8% | no | 7 | Sessions and roles: refusals, fixation, sign-out, role gates, profile mass assignment, CSRF |
| W03 | 5 | 4.6% | yes | 4 | Availability: 15-minute grid, fitting inside working time, buffers, merged and overlapping windows |
| W04 | 4 | 3.7% | yes | 5 | Availability: effective-dated schedule versions, exceptions, schedule edits and who may make them |
| W05 | 5 | 4.6% | yes | 5 | Availability: shared rooms and lowest-id assignment, daily limits with holds, fees in effect, inactive records |
| W06 | 5 | 4.6% | yes | 4 | Time: notice and horizon from now, clock-change days, local dates per clinic, fees by local date |
| W07 | 5 | 4.6% | yes | 4 | Legacy Seattle: the old system's schedules, appointments, folds and gaps, served as they are |
| W08 | 5 | 4.6% | yes | 6 | Booking: deposits on Checkout, mass assignment, confirmation only by signed webhook, one email |
| W09 | 5 | 4.6% | yes | 6 | Holds: expiry measured from creation, release, late and out-of-order payments refunded in full |
| W10 | 4 | 3.7% | no | 4 | Webhook security: unsigned, mis-signed, tampered and stale deliveries change nothing |
| W11 | 4 | 3.7% | yes | 5 | Races: the last slot, the last room and the last daily place each go to exactly one patient |
| W12 | 5 | 4.6% | yes | 6 | Cancellation: refund bands by elapsed time, half-cent rounding, clinic cancellations, emails once |
| W13 | 4 | 3.7% | yes | 6 | Refund retries: provider errors, lost answers and timeouts never refund twice; events before answers |
| W14 | 4 | 3.7% | yes | 7 | Rescheduling: 24-hour window, the original never blocks itself, the deposit carries over |
| W15 | 5 | 4.6% | yes | 7 | Waitlist: join order, offers that occupy, offer and hold lifetimes, decline and expiry chains |
| W16 | 4 | 3.7% | no | 5 | Clinical notes and clinician scope: only the treating clinician and admins, authors from the session |
| W17 | 3 | 2.8% | no | 4 | Reminders: every booked appointment of a clinic's day once, quirky addresses normalized, never twice |
| W18 | 3 | 2.8% | no | 6 | In a real browser: book and pay on the hosted page, layout at 1280 and 390 px, escaping everywhere |
| W19 | 3 | 2.8% | yes | 5 | Clinic closures: the old closures and new ones close every clinician; bookings stay; admins and the clinic's manager only |
| W20 | 4 | 3.7% | yes | 4 | Appointment export: exact columns and order, local and UTC times with folds, quoting, spreadsheet safety |
| W21 | 4 | 3.7% | yes | 5 | Front desk books for patients: holds for the account, payment-link emails, then the patient pays |
| W22 | 5 | 4.6% | yes | 4 | Legacy Honolulu: the old system's schedules, appointments, closures and patients, served as they are |
| W23 | 4 | 3.7% | no | 5 | Clinic staff: managers edit their clinic's schedules, front desk books and cancels only for their clinic |
| W24 | 5 | 4.6% | yes | 7 | A patient's journey: browse, register, book and pay, waitlist, reschedule into an offer, cancel, export |
| W25 | 3 | 2.8% | no | 6 | Cross-cutting safety: secrets nowhere at rest or in logs, reset ends sessions, Host-proof links, headers, no stack traces |
| W26 | 3 | 2.8% | no | 8 | Deployment: README, restart persistence, holds and refunds survive, nothing re-sent, no secrets, records kept |
| W27 | 2 | 1.8% | no | 3 | Health follows the database: non-2xx within 10 s of a stop, 200 within 60 s of a start |

### Checks

Every check of every workflow, in order, as the oracle run recorded them (each matches a `ctx.step()` in `tests/`). A workflow scores only when all of its checks pass.

**W01** Accounts: registration against quirky legacy addresses, reset links, duplicate-address accounts

1. staff set passwords from emailed links and sign in; a new patient registers and is signed in
2. registering an address already on file, in any case or spacing, is refused and adds no row
3. a known and an unknown address get the same reset response; only the known one gets an email
4. a newer link replaces the older; a used link and an unknown link change nothing
5. an expired link changes nothing
6. an address on two rows signs in as its account (lowest active id) and sees only that row's appointments

**W02** Sessions and roles: refusals, fixation, sign-out, role gates, profile mass assignment, CSRF

1. a wrong password and an unknown address are refused with the same message and no session
2. signing in starts a new session identifier; one held before never becomes signed in; cookies are HttpOnly
3. signing out ends the session on the server
4. role gates: 403 for signed-in users outside their role, sign-in redirect for visitors, public availability
5. sign-in follows next only to a path on this site; visitors are sent to /signin?next=<path>
6. names with line breaks are refused; typed text never adds an email recipient or header
7. the profile form changes only the display name; a token-less change is refused

**W03** Availability: 15-minute grid, fitting inside working time, buffers, merged and overlapping windows

1. 120 clinicians at both clinics with weekly hours dense in edge cases; six types; plenty of rooms
2. confirmed bookings, each pair touching at a busy-interval boundary
3. every slot list (three days of the week per clinician, six types) matches the rules exactly
4. unknown clinician or type answers 404; a malformed date 400

**W04** Availability: effective-dated schedule versions, exceptions, schedule edits and who may make them

1. 100 clinicians with several weekly-hours versions each, ties on the same date, versions with no hours
2. one-day exceptions aligned with each clinician's windows: whole and partial days off, joining extra hours
3. a malformed schedule form is refused and changes nothing
4. nobody edits another clinician's schedule; a token-less edit is refused
5. slot lists across version changes, ties, versions without hours, and every exception match

**W05** Availability: shared rooms and lowest-id assignment, daily limits with holds, fees in effect, inactive records

1. worked example 3: two shared rooms, three clinicians, lowest-id room for each booking
2. four groups of eight clinicians share two or three rooms each; 120 bookings each get the lowest-id free room
3. every group's slot lists match: rooms, daily limits, buffers
4. a clinician at the daily limit has no slots; an unexpired hold counts toward it
5. no fee in effect yet, no room of the type, inactive types and clinicians: no slots

**W06** Time: notice and horizon from now, clock-change days, local dates per clinic, fees by local date

1. round-the-clock clinicians at both clinics: notice from now and the last bookable date
2. forty clinicians whose hours and exceptions cross the next Seattle clock change get exactly the rules' slots
3. deposits around a fee change use each appointment's local date at its own clinic
4. each new appointment is a row in appointments whose local_start and fold give its start at its clinic

**W07** Legacy Seattle: the old system's schedules, appointments, folds and gaps, served as they are

1. slot lists for every old clinician of the clinic on busy, exceptional, version-change and closure dates match
2. the clinic's front desk sees its day sheets with the old appointments in time order
3. old patients with quirky or shared addresses see exactly their account's appointments, with no change actions
4. an old night-clinic clinician sees the clock-change night on their own day sheet, both 01:30s in order

**W08** Booking: deposits on Checkout, mass assignment, confirmation only by signed webhook, one email

1. thirty appointment types with fees that land on half cents; clinicians in both clinics
2. each hold's Checkout Session carries the exact deposit, metadata, URLs and address, whatever extra fields say
3. returning from payment does not confirm; the verified event does, and one confirmation email follows
4. repeated and simultaneous deliveries of the same event change nothing and send nothing
5. a declined card leaves the hold in place; a good card then confirms it
6. refused bookings create nothing: staff, no token, not a slot, unknown type, provider failure

**W09** Holds: expiry measured from creation, release, late and out-of-order payments refunded in full

1. a dozen holds occupy their slots until HOLD_TTL_SECONDS after creation, then free them
2. the application expires each expired hold's Checkout Session at the provider
3. payments whose events arrive after the hold expired are refunded in full and never confirm
4. a hold released by the patient frees its slot at once; a payment forced through afterwards is refunded
5. each late payment is refunded once, in full, with an Idempotency-Key; one refund email each; no confirmation
6. visiting the cancel link alone changes nothing; paying within the hold then confirms

**W10** Webhook security: unsigned, mis-signed, tampered and stale deliveries change nothing

1. a held appointment and forged completion events for it
2. missing, malformed, wrong-key, tampered, stale and future signatures are answered 400 and change nothing
3. verified events of other types or for unknown appointments are answered 2xx within 5 s and ignored
4. the genuine payment then confirms; a second verified completion with a new event id sends nothing more

**W11** Races: the last slot, the last room and the last daily place each go to exactly one patient

1. fixtures: a one-slot day, a one-room pair of clinicians, a clinician with one place left
2. eight patients ask for the only slot of the day at the same moment: exactly one hold
3. two clinicians, one room: simultaneous requests for the same hour get one booking between them
4. a clinician with one place left that day: simultaneous requests for different times get one booking
5. the same patient submitting the same booking twice at once gets one hold

**W12** Cancellation: refund bands by elapsed time, half-cent rounding, clinic cancellations, emails once

1. round-the-clock clinicians; patients book and pay appointments near the 2-hour and 24-hour boundaries
2. another patient cannot cancel; a cancellation without the session token is refused
3. each cancellation fixes the refund its band gives at the moment it happens
4. refunds go through the provider once each, keyed, and complete on the refund webhook
5. one cancellation email per appointment; one refund email per refund above zero
6. a cancelled appointment cannot be cancelled again; its slot is free; the old system's appointments refuse

**W13** Refund retries: provider errors, lost answers and timeouts never refund twice; events before answers

1. paid appointments more than a day ahead
2. a provider error: the cancellation stands, the refund is pending, then retried with the same key
3. a lost answer: the provider refunds but answers 503; the retry with the same key replays it
4. a timeout: the provider does nothing and answers late; the retry refunds once
5. refund events held back: the refund stays pending until its event arrives, in any order
6. every refund completes exactly once with one Idempotency-Key per refund; one refund email each

**W14** Rescheduling: 24-hour window, the original never blocks itself, the deposit carries over

1. clinicians at both clinics; a type whose fee changes mid-way; paid appointments
2. moving 15 minutes later the same day is allowed: the original does not block itself
3. moving to another clinician after the fee change carries the deposit paid, not the new fee's
4. refused: another clinic, a taken time, another patient, the old system's appointments, under 24 hours
5. a move to a time a few hours away, then a cancellation, refunds the carried deposit by its band
6. one reschedule email per move, no new confirmation
7. thirty moves at the daily limit and in single rooms: rooms, deposits and slot lists follow the rules

**W15** Waitlist: join order, offers that occupy, offer and hold lifetimes, decline and expiry chains

1. a full day: two appointments fill a short window; four patients join the waitlist in order
2. a cancellation frees 40 minutes: the FU30 waiter first in line gets the offer, the INIT60 waiter keeps waiting
3. someone else cannot accept the offer; when it expires the next FU30 waiter gets one
4. Cy accepts the offer, pays, and the booking is confirmed
5. another cancellation offers to Ana first; her decline passes it to Dee, the next in line
6. Dee accepts late in her offer; the hold then lasts HOLD_TTL_SECONDS from acceptance and the payment confirms
7. offers accepted after a fee change pay the deposit in effect when the hold is created

**W16** Clinical notes and clinician scope: only the treating clinician and admins, authors from the session

1. two clinicians, a patient with one appointment each, accounts for everyone
2. the treating clinician adds a note; its author is the signed-in clinician whatever the form says
3. other clinicians, front desk, and the patient get 403 and never see the note; an admin sees it
4. an old system's clinician reads exactly the old notes of their appointment
5. day sheets and appointment pages are scoped: each clinician sees only their own

**W17** Reminders: every booked appointment of a clinic's day once, quirky addresses normalized, never twice

1. pick a Honolulu day ahead with old bookings, including quirky and inactive patients; add new bookings
2. a patient cannot send reminders; front desk sends one per booked appointment with an active patient
3. sending again reaches nobody; a booking made since gets exactly one
4. the clinic's day sheet lists exactly the day's appointments in time order

**W18** In a real browser: book and pay on the hosted page, layout at 1280 and 390 px, escaping everywhere

1. a clean booking setup: a clinician, a type and two rooms, a patient, and an expiring hold
2. a patient signs in through the form, picks a slot, and pays on the provider's hosted page
3. an expired hold, a clinician's day sheet, and a waitlist offer as people see them
4. every page stays within 1280 and 390 CSS pixels without horizontal scrolling
5. a clinician, a type, a room, a patient and a note carrying script payloads, and a booking with them
6. names, type and room names, notes, old records, and echoed request text never run as script

**W19** Clinic closures: the old closures and new ones close every clinician; bookings stay; admins and the clinic's manager only

1. the old system's closures within the horizon leave no slots for any clinician of that clinic
2. an admin closes each clinic on a future day; booked appointments that day stay confirmed and listed
3. on the closed days no clinician has a slot; the days around them are unaffected
4. a second closure for the same day is refused; only admins and the clinic's manager add closures; a token-less closure is refused
5. the clinic page offers add-closure to admins and that clinic's manager only

**W20** Appointment export: exact columns and order, local and UTC times with folds, quoting, spreadsheet safety

1. the old system's past weeks export exactly, including the clock-change night
2. patients and clinicians with commas, quotes and formula-like names book, cancel and reschedule
3. the export of those days lists exactly the right rows, quoted and spreadsheet-safe
4. ranges over 31 days or backwards are refused; patients cannot export

**W21** Front desk books for patients: holds for the account, payment-link emails, then the patient pays

1. front desk holds slots for patients by address, including the old system's quirky addresses; the patients pay
2. each session is for the account's address and the deposit; each patient gets one payment-link email
3. the paid bookings are confirmed and belong to the patients' accounts
4. the last unpaid hold expires like any other
5. refused: unknown or inactive addresses, non-patients, patients using the desk, no token

**W22** Legacy Honolulu: the old system's schedules, appointments, closures and patients, served as they are

1. slot lists for every old clinician of the clinic on busy, exceptional, version-change and closure dates match
2. the clinic's front desk sees its day sheets with the old appointments in time order
3. old patients with quirky or shared addresses see exactly their account's appointments, with no change actions
4. Honolulu clinicians' own day sheets list their old appointments

**W23** Clinic staff: managers edit their clinic's schedules, front desk books and cancels only for their clinic

1. a Seattle and a Honolulu clinician; the Seattle manager sets the Seattle clinician's hours
2. a manager cannot see or edit another clinic's schedules; front desk cannot edit schedules
3. Seattle front desk books for a patient with a Seattle clinician; a Honolulu clinician is refused
4. the appointment is visible to its clinic's staff only; other-clinic desk pages are refused
5. another clinic's manager cannot cancel; the clinic's manager cancels with a full refund and one email each

**W24** A patient's journey: browse, register, book and pay, waitlist, reschedule into an offer, cancel, export

1. a visitor sees a clinician's times without booking buttons, registers, and then sees the buttons
2. the patient books 10:00 and pays; one confirmation; the list shows it
3. another patient takes 11:00, the day is full, and the patient joins its waitlist
4. moving the 10:00 appointment to a time soon after now frees the slot, and the waitlist offers it back
5. the fee changes while the offer is open; accepting pays the deposit in effect on that date
6. cancelling the moved appointment refunds the carried deposit by its band, once
7. the patient's list and the clinic's export show the whole history

**W25** Cross-cutting safety: secrets nowhere at rest or in logs, reset ends sessions, Host-proof links, headers, no stack traces

1. setting a new password through a reset link signs out every existing session of that account
2. a reset link always starts with PUBLIC_BASE_URL, whatever Host header the request carried
3. signed-in responses carry Cache-Control: no-store and every page an anti-framing header
4. malformed requests get a plain error page without stack traces, SQL, or file paths
5. a waitlist offer is made; its link reaches the patient
6. no password, reset token, offer token, session identifier, or signing key is stored in plain text (database or workspace files)

**W26** Deployment: README, restart persistence, holds and refunds survive, nothing re-sent, no secrets, records kept

1. README.md and an executable start.sh are in the workspace
2. before a restart: an unpaid hold, and a refund the provider has not answered yet
3. after a restart: passwords, appointments and the hold's expiry are kept
4. the refund still pending at the restart completes once
5. no email sent before the restart is sent again
6. no password is stored in plain text
7. the old system's records are all still there, unchanged
8. every appointment the verifier made is a row in appointments with local_start and fold of its start

**W27** Health follows the database: non-2xx within 10 s of a stop, 200 within 60 s of a start

1. /healthz answers 200 while the database is up
2. within 10 s of the database stopping, /healthz answers a non-2xx status within 5 s
3. within 60 s of the database starting again, /healthz answers 200 without a restart

### Breach detectors

Any detector that fires caps every workflow at ×0.4. Each row lists where the detector is raised and what it reports (`…` marks a value filled in at run time).

| code | raised in | fires when |
|---|---|---|
| X1 | W01, W02, W04, W12, W14, W15, W16, W20, W21, W23 | Honolulu front desk exported Seattle appointments / Honolulu front desk opened a Seattle appointment / Honolulu front desk read the Seattle day sheet / Seattle front desk held a slot with a Honolulu clinician / a clinician opened another clinician's appointment / a clinician or front desk changed another clinician's schedule / a manager changed another clinic's clinician's schedule / a manager of another clinic cancelled an appointment / a patient accepted another patient's waitlist offer / a patient created a room through the admin form / a patient downloaded the appointment export / a patient opened another patient's appointment / a refused desk booking created a Checkout Session / address … showed appointments of user rows that are not its account: … / another patient cancelled appointment … / another patient rescheduled appointment … / … read a clinical note |
| X2 | W02, W04, W08, W12 | a booking without the session token created a hold / a cancellation without the session token took effect for … / a profile change without the session token took effect / a schedule exception without the session token took effect |
| X3 | W18 | stored or reflected text ran as script: … |
| X4 | W02, W08, W16, W21 | a desk booking's extra fields set the deposit of … / a note's author or appointment came from the form: … / extra booking fields changed appointment …: amount …, marker … / the profile form changed … |
| X5 | W10 | a webhook with a … signature confirmed appointment … |
| X6 | W08, W09, W10, W11, W12, W13, W26 | a refund pending across the restart was paid … times / appointment … produced … confirmation emails / late payment for appointment … was refunded … times / … emails were sent again after the restart: … / … refund emails for … refunds / …: appointment … got … refunds totalling … / …: … cancellation and … refund emails for … cancellations / …: … holds were created for one place |
| X7 | W02 | a session cookie copied before sign-out still signs in / a session identifier held before sign-in became signed in |
| X8 | W25, W26 | plain-text password in … / secrets stored in the application's files: … / secrets stored in the database: … |
| X9 | W12, W26 | cancelling an old-system appointment changed its row (status …) / pre-existing records changed or removed: … |
| X10 | W08 | appointment … was confirmed without a verified webhook |
| X11 | W02 | sign-in redirected off the site for next=…: Location … |
| X12 | W02 | typed text added the recipient … to an email |
| X13 | W25 | … session(s) still signed in after the password was reset |
| X14 | W25 | reset link built from the request's Host header: … |
| X15 | W20, W24 | …: the export wrote a live spreadsheet formula … |

### Judged experience criteria

`anthropic/claude-fable-5-1` answers each criterion yes or no from the evidence files named for it, following `tests/judge-reference.md`; a checkpoint that was never reached fails its criterion. The oracle run scored every criterion yes.

| id | requirement | weight | evidence (written by) | criterion |
|---|---|---|---|---|
| J01 | R15.1 | 1 | `slot_list` (W18) | The slot list shows each start time in the clinic's local time with the clinic's name and its time-zone abbreviation, and states the deposit due. |
| J02 | R15.2 | 1 | `checkout_return` (W18) | After returning from the payment page, the appointment page says the payment is being confirmed and how long the slot stays held. |
| J03 | R15.2 | 1 | `appointment_confirmed` (W18) | The confirmed appointment page shows that the booking is confirmed, its local date and time with the clinic, and the deposit paid. |
| J04 | R15.3 | 1 | `appointment_confirmed` (W18) | Before cancelling, the confirmed appointment page states the refund that cancelling now would give and which notice band applies. |
| J05 | R15.3 | 1 | `cancel_after` (W18) | After cancelling, the page shows the refund amount and whether the refund is pending or already refunded. |
| J06 | R15.4 | 1 | `hold_expired` (W18) | The expired hold's page explains that the slot was given up and how to book again. |
| J07 | R15.5 | 1 | `offer_page` (W18) | The waitlist offer page states the slot, when the offer expires, and that accepting holds the slot and asks for the deposit. |
| J08 | R15.6 | 1 | `clinician_day` (W18) | The clinician's day sheet makes each appointment's local time, patient, and appointment type clear. |
| J09 | R6.4, R7.4, R9.3 | 1 | `confirmation_email` (W08); `cancellation_email` (W12); `offer_email` (W15) | The confirmation email names the clinic, the clinician, and the local date and time; the cancellation email states the refund amount; the offer email names the slot and links to the offer. |
| J10 | R15.7 | 1 | `slot_list/narrow.jpg` (W18); `appointment_confirmed/narrow.jpg` (W18); `offer_page/narrow.jpg` (W18) | At 390 pixels wide, the key facts and the primary action on these pages stay readable and reachable. |

## Controls

| control | run | reward | functional | judge |
|---|---|---|---|---|
| oracle (`solution/solve.sh`) | `20261111T111418Z-r2qc-ph-oracle` | 1.0 | 1.0 | 1.0 |
| nop (empty workspace) | `20261111T111419Z-r2qc-ph-nop` | 0.0 | 0.0 | 0.0 |

## Interface contract literals

The routes of `instruction.md`'s interface contract and every `data-action` and `data-*` marker it names. The workflows drive and read the application through these routes and markers (as well as the form fields, exports and emails the instruction specifies), so a submission that renames one fails the checks that rely on it.

| path | methods |
|---|---|
| `/register` | GET, POST |
| `/password-reset` | GET, POST |
| `/password-reset/<token>` | GET, POST |
| `/signin` | GET, POST |
| `/signout` | POST |
| `/profile` | GET, POST |
| `/api/availability` | GET |
| `/book` | GET |
| `/book` | POST |
| `/appointments` | GET |
| `/appointments/<id>` | GET |
| `/appointments/<id>/release` | POST |
| `/appointments/<id>/cancel` | POST |
| `/appointments/<id>/reschedule` | POST |
| `/appointments/<id>/notes` | GET, POST |
| `/waitlist` | GET, POST |
| `/offers/<token>` | GET |
| `/offers/<token>/accept` | POST |
| `/offers/<token>/decline` | POST |
| `/clinician/day` | GET |
| `/clinicians/<id>/schedule` | GET |
| `/clinicians/<id>/schedule/versions` | POST |
| `/clinicians/<id>/schedule/exceptions` | POST |
| `/desk/day` | GET |
| `/desk/reminders` | GET, POST |
| `/desk/book` | GET, POST |
| `/desk/export.csv` | GET |
| `/admin/clinics/<id>` | GET, POST |
| `/admin/clinicians/new` | GET, POST |
| `/admin/rooms/new` | GET, POST |
| `/admin/types/new` | GET, POST |
| `/admin/types/<code>` | GET, POST |
| `/webhooks/payments` | POST |
| `/healthz` | GET |

| kind | values |
|---|---|
| actions (`data-action`) | accept-offer, add-closure, add-exception, add-fee, add-note, add-version, book-slot, cancel-appointment, create-clinician, create-room, create-type, decline-offer, desk-book, join-waitlist, register, release-hold, request-reset, reschedule, save-profile, send-reminders, set-password, signin, signout |
| markers | data-appointment-id, data-checkout-url, data-clinician-id, data-closure-date, data-csrf, data-date, data-deposit-cents, data-entry-status, data-note-id, data-offer-status, data-refund-cents, data-refund-status, data-reminders-sent, data-room-id, data-start, data-status, data-type, data-type-code, data-user-id |
