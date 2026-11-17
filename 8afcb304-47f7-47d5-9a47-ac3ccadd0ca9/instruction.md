# Harborview Physio: online booking for a two-clinic physiotherapy practice

I run Harborview Physio, a physiotherapy practice with two clinics: Seattle (time zone
`America/Los_Angeles`) and Honolulu (`Pacific/Honolulu`, which has no daylight saving time).
Patients book appointments online and pay a deposit by card. Clinicians keep their own working
hours, each clinic's front desk and manager run the day there, and I administer the rest. Our old
booking system is being retired. You are building its replacement from scratch in this workspace.

The workspace is empty. The environment already runs three services. The first is a PostgreSQL
database holding our clinics, rooms, users, clinicians, appointment types and fees, working
schedules, the old system's appointments, and clinical notes. The second is an SMTP server that
accepts every email the application sends. The third is a card-payment provider with a
Stripe-compatible API (Checkout Sessions with a hosted payment page, Refunds, Webhook Endpoints,
signed webhooks). `GET` on the provider's base URL documents its surface and its test cards. Their
connection details are in `/etc/physio/services.env`, one `NAME=value` per line:

| Variable | Meaning |
|---|---|
| `DATABASE_URL`, `PORT`, `PUBLIC_BASE_URL` | the database; the port to listen on; the address patients use to reach the application |
| `SMTP_HOST`, `SMTP_PORT`, `MAIL_FROM` | outgoing email |
| `PAYMENTS_API_URL` | the payment provider's base URL |
| `PAYMENTS_WEBHOOK_SIGNING_FILE` | a file holding the webhook signing key (`whsec_...`) |
| `RESET_LINK_TTL_SECONDS` | lifetime of a password link (R2) |
| `HOLD_TTL_SECONDS` | lifetime of a booking hold (R6) |
| `OFFER_TTL_SECONDS` | lifetime of a waitlist offer (R9) |
| `MIN_NOTICE_MINUTES` | how soon before its start a slot can still be booked (R5) |
| `BOOKING_HORIZON_DAYS` | how many days ahead slots are offered (R5) |

The application is always started with these variables exported in its environment, so read them
from the environment at run time. Each deployment starts the application with **different values** for
the same variables, a fresh copy of the same database, and a fresh payment provider. Do not
hard-code any host, port, address, duration, or limit.

Money is whole US cents. Pages show it in dollars like `$1,234.56`. "Now" is the application
server's clock. Wherever this page says a time is "local", it means the wall-clock time at the
clinic of the clinician concerned.

## People and roles (R1)

- R1.1 Everyone who signs in is a row in `users`. `role` is `patient`, `clinician`, `front_desk`,
  `manager`, or `admin`. Front desk and managers work for one clinic, given by `users.clinic_id`.
  Only rows with `active = true` may sign in or receive email.
- R1.2 Email addresses are compared case-insensitively and ignoring leading and trailing
  whitespace, everywhere. The old system stored some with odd capitalization and stray spaces, and
  a few addresses appear on more than one row. The **account** for an address is the active row
  with the lowest `id` among the rows whose address matches. Other rows with that address never
  sign in. When the application emails a user, it sends to their address trimmed and lowercased.
- R1.3 What each role may do:
  - a **patient** books, sees, cancels, and reschedules their own appointments, uses the waitlist,
    and edits their own display name;
  - a **clinician** (a `users` row with a `clinicians` row) sees and edits their own working
    schedule, sees their own day sheet, and reads and writes the clinical notes of their own
    appointments;
  - **front desk** works for their clinic: sees its appointments and day sheets, books and cancels
    app-made appointments of its clinicians on a patient's behalf (R6.7, R7.2), sends its reminders
    (R11.2), and exports its appointments (R11.3);
  - a **manager** does everything front desk does for their clinic, and also edits the schedules of
    that clinic's clinicians (R4.5) and adds closures for that clinic (R4.6);
  - an **admin** does everything front desk and managers do, for both clinics, reads every clinical
    note, and creates clinicians, rooms, appointment types, and fees.
  An appointment belongs to the clinic of its clinician. Its page `/appointments/<id>` is visible to
  its patient (the user whose id is its `patient_id`), its clinician, the front desk and managers of
  its clinic, and admins. A signed-in user who requests a page or action they are not allowed gets
  a 403 response and nothing changes. A visitor who is not signed in is sent to
  `/signin?next=<the page's path>`, except for `/api/availability`, which anyone may use.

## Accounts (R2)

- R2.1 `/register` creates a patient: email, full name, and a password of at least 10
  characters. An address that matches any existing `users` row (R1.2) is refused with a message
  and nothing is created. Otherwise the new row is a `users` row with role `patient`, active, and
  the visitor is signed in.
- R2.2 Staff and the old system's patients have no password yet. Anyone sets or resets a password
  from `/password-reset` by entering an address. If the address has an account (R1.2), one email
  goes to it with a single-use link that starts with `PUBLIC_BASE_URL`. The link opens a page that
  sets a new password of at least 10 characters for that account. Whether or not the address has an
  account, the response has the same status and shows the same message, and an address without an
  account gets no email. The link starts with `PUBLIC_BASE_URL` whatever `Host` header the request
  carried. Setting a new password through a link signs out every existing session of that account.
  A link works once. It expires `RESET_LINK_TTL_SECONDS` after it was requested and stops working
  as soon as a newer link is requested for the same account. A used, expired, or replaced link
  changes nothing and says the link is no longer valid. An unknown link answers 404.
- R2.3 Sign-in takes an address and a password. A wrong password, an address without an account, or
  an inactive user is refused with the same message and no session. The sign-in form carries the
  `next` value it was opened with. After signing in, the user is redirected to `next` only when it
  is a path on this site: it starts with exactly one `/`, followed by anything but a second `/` or
  a `\`, and contains no control character. Any other value is ignored, and the user goes to their
  home page instead.
- R2.4 Signing in always starts a new session identifier. No identifier the browser held before
  signing in ever becomes signed in. Signing out ends the session on the server, so a session cookie
  copied before sign-out no longer signs anyone in. Session cookies are `HttpOnly` and
  `SameSite=Lax` or `SameSite=Strict`.
- R2.5 Passwords are stored only as salted hashes.
- R2.6 `/profile` lets any signed-in user change their own display name (`full_name`) and
  nothing else, whatever other fields the request carries.
- R2.7 A name is 1 to 200 characters with no line break. A full name (R2.1, R2.6, R3.5), a room
  name, or a type name containing a carriage return or line feed is refused with 400, and nothing
  changes. Every email to a user is addressed with their display name and address, for example
  `To: Ana Lopez <ana@mail.example>`.

## Clinics, rooms, appointment types, fees (R3)

- R3.1 `clinics` has each clinic's IANA `timezone`. `rooms` belong to a clinic and have a
  `room_type` (for example `treatment`, `gym`, `hydro`). Only active rooms can be used.
- R3.2 `appointment_types` has a unique `code`, a `duration_minutes`, a `buffer_minutes` (time
  after the appointment that stays blocked for the clinician and the room), the `room_type` it
  needs, and `active`. Every clinician can see patients for every active type, provided the
  clinician's clinic has a room of the type's room type.
- R3.3 **Fees are effective-dated.** `type_fees` rows give a type's `fee_cents` and
  `deposit_bp` (deposit rate in basis points: 2000 = 20 %) from `effective_from` on. An
  appointment's fee and rate are those of the row with the latest `effective_from` on or before the
  appointment's local date. A type with no such row cannot be booked on that date.
- R3.4 **Deposit** = fee × `deposit_bp` / 10000, rounded to the nearest cent, with half a cent
  rounding up. It is fixed when the hold is created (R6.1).
- R3.5 An admin creates rooms (`/admin/rooms/new`), appointment types together with their first
  fee row (`/admin/types/new`), and further fee rows for a type (`/admin/types/<code>`). A type
  code that already exists, or a second fee row for the same type and `effective_from`, is refused
  with a message and nothing changes. An admin creates a clinician (`/admin/clinicians/new`) with
  an address, full name, clinic, and `max_per_day`. This adds a `users` row (role `clinician`,
  active) and a `clinicians` row. An address that matches an existing user is refused.

## Working schedules (R4)

- R4.1 A clinician's weekly hours are **schedule versions**. A `schedule_versions` row has an
  `effective_from` local date, and its `schedule_windows` rows give `weekday` (1 = Monday … 7 =
  Sunday), `start_time`, and `end_time`. On local date D the version in effect is the one with the
  latest `effective_from` on or before D. When two versions share that date, the one with the
  higher `id` wins. With no version in effect, the clinician does not work on D. A version with no
  windows means the clinician is off every day.
- R4.2 `schedule_exceptions` rows change one local date (`on_date`). `kind = 'off'` with no
  times makes the whole day off. `kind = 'off'` with `start_time` and `end_time` removes that
  range. `kind = 'extra'` adds that range.
- R4.3 **Working time** on D is the union of the version's windows for D's weekday and the
  `extra` ranges on D, minus the `off` ranges on D. A window or range whose end is not after its
  start (as wall-clock times) is ignored. Every other one covers the instants from its start to its
  end, both turned into instants by R4.4, and the union and removal are done on those instants.
  `end_time` may be `24:00`, meaning the next local midnight. Working time is worked out for each
  local date separately and never joins across midnight. On a date when the clinician's clinic is
  closed (R4.6), the clinician has no working time at all.
- R4.4 **Wall-clock times become instants in the clinic's time zone.** A wall-clock time that
  happens twice (clocks set back) means its first occurrence. A wall-clock time that does not exist
  (clocks set forward) is read with the UTC offset in effect just before the change.
- R4.5 A clinician sees and edits their own schedule at `/clinicians/<id>/schedule`, a manager
  those of their clinic's clinicians, and an admin anyone's. Anyone else gets 403. Adding a version
  takes `effective_from` and `windows`: one window per line, written `DAY HH:MM-HH:MM` with `DAY`
  one of `MON TUE WED THU FRI SAT SUN` (for example `TUE 09:10-12:00`; times run from `00:00` to
  `23:59`, and `24:00` is allowed as an end). An empty `windows` field adds a version with no
  windows. A line that does not parse, or whose end is not after its start, refuses the whole form
  with 400, and nothing changes. Adding an exception takes `on_date`, `kind` (`off` or `extra`),
  `start_time`, and `end_time`. Both times are required for `extra`; for `off`, give both or
  neither; when given, the end must be after the start (`24:00` is allowed as an end, as for windows).
  Anything else is refused with 400. Schedule changes never cancel or move existing appointments.
- R4.6 **Clinic closures.** A `clinic_closures` row (`clinic_id`, `on_date`, `reason`) closes a
  clinic for that whole local date, whatever any schedule or exception says. An admin, or a manager
  of that clinic, sees the clinic's closures and adds one at `/admin/clinics/<id>`; anyone else
  gets 403. A second closure for the same clinic and date is refused with 409. Closures never cancel
  or move existing appointments.

## Availability (R5)

Availability decides which start times a patient may book.

- R5.1 A slot for clinician C, appointment type T, and local date D is an instant *s* that meets
  every condition below. Its local time is on the hour or 15, 30, or 45 minutes past it.
  1. **Fit.** The appointment [*s*, *s* + duration) lies entirely inside one continuous stretch
     of C's working time on D (R4.3).
  2. **Clinician free.** Every appointment has a *busy interval* [start, start + duration +
     buffer) from its type. The candidate's busy interval [*s*, *s* + duration + buffer) overlaps no
     busy interval of any occupying appointment (R5.2) of C. Intervals are half-open.
  3. **Room free.** C's clinic has an active room of T's room type whose occupying appointments'
     busy intervals all miss the candidate's busy interval.
  4. **Daily limit.** C has fewer than `clinicians.max_per_day` occupying appointments whose
     start falls on local date D.
  5. **Notice and horizon.** *s* ≥ now + `MIN_NOTICE_MINUTES`, and D is no later than the
     clinic's local date today plus `BOOKING_HORIZON_DAYS`.
  6. **Fee.** T has a fee in effect on D (R3.3).
  7. C (`clinicians.active`) and T (`appointment_types.active`) are active.
- R5.2 **Occupying appointments** are the old system's appointments with status `booked`,
  `completed`, or `no_show`, plus the application's appointments that are `confirmed`, `held`
  with an unexpired hold (R6.3), or under an unexpired waitlist offer (R9.3). The old system's
  appointments with status `cancelled` occupy nothing. An appointment occupies its clinician and,
  when it has a `room_id`, that room. A few of the old system's rows have no `room_id`; they occupy
  only their clinician.
- R5.3 `GET /api/availability?clinician_id=<id>&type=<code>&date=<YYYY-MM-DD>` answers 200
  with JSON `{"clinician_id": <id>, "type": "<code>", "date": "<YYYY-MM-DD>", "slots":
  [{"start": "<instant>", "end": "<instant>"}, ...]}`. `slots` is in time order, may be empty,
  and `end` is *s* + duration. Every instant in the interface is ISO 8601 with an explicit offset,
  for example `2026-12-08T09:15:00-08:00`. Instants are compared, not offsets. An unknown
  clinician or type answers 404 and a malformed date 400.
- R5.4 `/book?clinician_id=&type=&date=` shows the same slots to a person, in the clinic's local
  time with its time-zone abbreviation. For a signed-in patient each slot has a button that books
  it; everyone else sees the times without those buttons.

Worked examples (slots are local times at the clinician's clinic):

1. **Grid, fit, buffers.** Seattle, Tuesday 2026-12-08, one window `09:10-12:00`, type FU45 (45
   min, buffer 15). The clinician has a confirmed FU30 (30 min, buffer 10) at 10:30, so is busy
   10:30–11:10. FU45 slots: 09:15, 09:30, 11:15.
2. **Versions and exceptions.** Version A from 2026-12-01: `MON 08:00-12:00`, `MON 13:00-17:00`.
   Version B from 2027-01-04: `MON 09:00-15:00`. Exceptions: 2026-12-14 off 10:00–11:00,
   2026-12-21 off all day, 2026-12-28 extra 12:00–13:00. Type INIT60 (60 min, buffer 15), nothing
   booked. Slots on 2026-12-07: 08:00…11:00 and 13:00…16:00 every 15 minutes (26 slots). On
   2026-12-14: 08:00…09:00, 11:00, and 13:00…16:00 (19). On 2026-12-21: none. On 2026-12-28 the
   extra hour joins the two windows: 08:00…16:00 (33). On 2027-01-04: 09:00…14:00 (21).
3. **Rooms and the daily limit.** Rooms 21 and 24 are a clinic's only `gym` rooms. A REHAB45 (45
   min, buffer 15) for clinician A at 10:00 gets room 21. One for clinician B at 10:30 gets room 24,
   because 21 is busy until 11:00. Clinician C, free all day, has no REHAB45 slot from 09:45 through
   10:45 (09:30 still fits in room 24, and 11:00 frees room 21). C's booking at 11:00 gets room 21,
   the lowest-id free room. A clinician with `max_per_day` 2 who already has two appointments
   starting on a date has no slots that date.

## Booking and deposits (R6)

- R6.1 A signed-in patient books with `POST /book` (`clinician_id`, `type`, `start`). Only
  patients book. At that moment the application checks the slot against R5. If it is not a slot,
  it answers 409 with a message and creates nothing. Otherwise it creates the appointment as a new
  row in `appointments` with status `held` and these values:
  - the room is the lowest-id active room of the type's room type in the clinic that is free
    for the whole busy interval;
  - the deposit is set by R3.3–R3.4;
  - `local_start` and `fold` give its start the way the old system stored times (R12.2).
  The application then creates a Checkout Session at the provider with `mode=payment`. It has one
  line item in `usd` whose `unit_amount` is the deposit, quantity 1, and a product name that
  includes the type's name. It sets `customer_email` to the patient's address and
  `metadata[appointment_id]` to the appointment's id. `success_url` is
  `PUBLIC_BASE_URL/appointments/<id>?checkout=success` and `cancel_url` is
  `PUBLIC_BASE_URL/appointments/<id>?checkout=cancelled`. The response is a 303 redirect to the
  session's `url`. If the provider fails to create the session, the hold is removed at once, the
  slot stays free, and the patient sees an error.
- R6.2 When several booking requests compete at the same moment for the last place that only one
  of them can have (the same slot, the last free room, or the clinician's last place that day),
  exactly one gets a hold. The others get 409.
- R6.3 A hold lasts `HOLD_TTL_SECONDS` from the moment it was created. From then on it occupies
  nothing, its status is `expired`, and the application expires its Checkout Session at the
  provider. The patient may give a hold up earlier with `POST /appointments/<id>/release`: its
  status becomes `released`, it stops occupying at once, and the session is expired. Visiting the
  `cancel_url` alone changes nothing.
- R6.4 **Only a signed webhook confirms a booking.** A verified `checkout.session.completed`
  event whose `data.object.metadata.appointment_id` names a `held` appointment whose hold has not
  expired at the moment the application processes the event makes it `confirmed`. The event must
  have `payment_status` `paid`. The application records the session's `payment_intent`, and the
  deposit is now paid. The patient then receives exactly one email whose subject contains
  `Appointment confirmed`, naming the clinic (its `name`), the clinician, and the local date and
  time (`YYYY-MM-DD`, `HH:MM`).
  Returning to `success_url` never confirms anything by itself. Until the event arrives, the page
  says the payment is being confirmed.
- R6.5 **Late payments.** A verified `checkout.session.completed` for an appointment that is
  `expired` or `released`, or `held` with an expired hold, when the application processes the event
  never confirms it. The application refunds the session's full `amount_total` (R7.5), and the
  appointment keeps its status (a `held` appointment whose hold expired is `expired`). A verified
  `checkout.session.completed` for an appointment in any other status changes nothing.
- R6.6 **Webhooks.** When it starts, the application registers
  `PUBLIC_BASE_URL/webhooks/payments` with `POST /v1/webhook_endpoints` (registering the same URL
  again returns the same endpoint). Each delivery carries `Stripe-Signature: t=<unix
  seconds>,v1=<hex>`, where `v1` is the HMAC-SHA256 of `<t>.<raw body>` under the key in
  `PAYMENTS_WEBHOOK_SIGNING_FILE`. A delivery with a missing or malformed header, no matching
  `v1`, or a `t` more than 300 seconds from now is answered 400 and changes nothing. A verified
  delivery is answered 2xx within 5 seconds. Each event `id` takes effect at most once, so a
  repeated delivery changes nothing and sends nothing. Events of other types, and events about
  appointments the application does not know, are answered 2xx and ignored.
- R6.7 **Booking for a patient.** Front desk and managers (for a clinician of their clinic) and
  admins book on a patient's behalf at `/desk/book` (`patient_email`, `clinician_id`, `type`,
  `start`). A clinician of another clinic is refused with 403. The address must have a patient
  account (R1.2); otherwise the request is refused with 400 and nothing is created. The slot is
  checked and held as in R6.1, for that patient, with the same Checkout Session (`customer_email` is
  the patient's address). The response is a page, not a redirect: it shows the session's `url` in an
  element with `data-checkout-url`. The patient receives exactly one email whose subject contains
  `Pay your deposit`, with that URL and the time the hold expires. Confirmation, expiry, and late
  payments then follow R6.3–R6.5.

## Cancellation and refunds (R7)

- R7.1 A patient cancels their own `confirmed` appointment with
  `POST /appointments/<id>/cancel`. The refund depends on the elapsed time from the moment of
  cancelling to the appointment's start:
  - 24 hours (86,400 s) or more: the whole deposit paid;
  - at least 2 hours but less than 24: half of it, with half a cent rounding up;
  - less than 2 hours: nothing.
  An appointment that has already started cannot be cancelled (409).
- R7.2 Front desk and managers of the appointment's clinic, and admins, cancel an app-made
  `confirmed` appointment the same way, and then the patient always gets the whole deposit paid
  back, whatever the time.
- R7.3 The old system's appointments cannot be cancelled or rescheduled through the application:
  such a request is refused with 409 and the row never changes. `held` appointments are released
  (R6.3), not cancelled.
- R7.4 Cancelling makes the appointment `cancelled`. It stops occupying at once, and its refund
  amount is fixed. The patient receives exactly one email whose subject contains
  `Appointment cancelled` and which states the refund amount (or that there is none).
- R7.5 **Refunds go through the provider.** For a refund of more than zero, the application calls
  `POST /v1/refunds` with the appointment's `payment_intent`, the `amount`,
  `metadata[appointment_id]`, and an `Idempotency-Key` header that is the same on every attempt of
  that refund. If the call fails or gets no answer, the cancellation still stands, and the
  application keeps retrying in the background with the same key. The first retry comes within 30
  seconds, and retries continue until the provider accepts. One cancellation or late payment never
  produces more than one refund. The refund is `pending` until a verified `refund.created` event
  whose `data.object.metadata.appointment_id` names the appointment arrives. Then it is `refunded`,
  and the patient receives exactly one email whose subject contains `Refund issued` and which states
  the amount. A refund of zero is never sent to the provider; its status is `none`.

Worked examples:

4. FU45 at fee 12550 and rate 2000 has a deposit of $25.10. Cancelling it 30 hours before its
   start refunds $25.10, 5 hours before refunds $12.55, and 90 minutes before refunds $0.00. Front
   desk cancelling it 90 minutes before refunds $25.10.

## Rescheduling (R8)

- R8.1 A patient reschedules their own app-made `confirmed` appointment with
  `POST /appointments/<id>/reschedule` (`clinician_id`, `start`). The new appointment has the same
  type, with a clinician at the same clinic. This is allowed only while the original starts 24
  hours or more after the request. A request made later, naming a clinician at another clinic, or
  asking for a start that is not a slot by R8.2 is refused with 409, and nothing changes.
- R8.2 The new start must be a slot by R5, except that the original appointment occupies nothing
  during this check (its busy interval, its room, and its place in the daily count).
- R8.3 In one step, the original becomes `rescheduled` and a new appointment is created, already
  `confirmed`, with its room chosen as in R6.1 (also ignoring the original). The new appointment
  carries the original's payment: its deposit is the amount the original paid, with the same
  `payment_intent`, whatever the fee on the new date. No Checkout and no refund happen. The patient
  receives exactly one email whose subject contains `Appointment rescheduled`, naming the new local
  date and time, and no other email about the move. A rescheduled appointment can be rescheduled
  or cancelled later like any other. Its refund is worked out from its own start and the carried
  deposit.
- R8.4 For example, a confirmed FU30 that paid $19.00 (fee 9500, rate 2000) is rescheduled to a
  date when FU30's fee is 10000 at rate 2000 ($20.00). The new appointment's deposit is $19.00, and
  cancelling it 5 hours before its start refunds $9.50.

## Waitlist (R9)

- R9.1 A signed-in patient joins the waitlist for a clinician, appointment type, and local date
  with `POST /waitlist`. The date must be from the clinic's local today up to the horizon (R5.1),
  or the request is refused with 400. A patient who already has a `waiting` entry for the same
  clinician, type, and date is refused with 409. Entries are served in the order they joined.
- R9.2 **Trigger.** An occupying appointment of clinician C that starts on local date D can stop
  occupying: it is cancelled, rescheduled away, released, or its hold or offer expires, or an
  offer is declined. Each time, the application looks at C's `waiting` entries for D in join
  order. The first entry whose type has a slot on D at that moment (R5) gets an offer for the
  earliest such slot. One trigger makes at most one offer. Entries passed over keep waiting.
- R9.3 An **offer** occupies its slot, with a room chosen as in R6.1, for `OFFER_TTL_SECONDS`
  from the moment it is made. The patient receives exactly one email whose subject contains
  `A slot opened up`. It names the local date and time and has a link
  `PUBLIC_BASE_URL/offers/<token>`. The entry's status becomes `offered`.
- R9.4 Accepting (`POST /offers/<token>/accept`, by that patient, before the offer expires)
  turns it into a `held` appointment for the same slot and room. Its hold lasts
  `HOLD_TTL_SECONDS` from the acceptance. The response is a 303 redirect to a new Checkout
  Session (R6.1), and confirmation follows R6.4. The entry becomes `booked`. Accepting an expired
  or declined offer is refused with 409.
- R9.5 Declining (`POST /offers/<token>/decline`) or letting the offer expire ends the entry
  (`declined` or `expired`) and frees the slot, which is a new trigger (R9.2).
- R9.6 For example, Ana joins at 09:00 and Ben at 09:05, both for FU30 with the same clinician and
  date, which is fully booked. A cancellation frees an FU30 slot, and Ana gets the offer. She lets
  it expire, and that expiry gives Ben the offer.

## Clinical notes, day sheets, reminders (R10, R11)

- R10.1 A clinical note (`clinical_notes`) belongs to one appointment. Its notes, at
  `/appointments/<id>/notes`, are visible only to that appointment's clinician and to admins.
  Everyone else, including the appointment's patient, gets 403. Only the appointment's clinician
  adds notes (`body`, 1 to 5000 characters). The author is always the signed-in clinician.
- R11.1 A **day sheet** lists, in time order, the appointments with status `booked`,
  `completed`, `no_show` (old system), or `confirmed` that start on one local date. `/desk/day`
  shows a clinic's to its front desk and managers and to admins, and `/clinician/day` shows the
  signed-in clinician's own. Staff asking for another clinic's day sheet, reminders, or export
  (R11.2, R11.3) get 403.
- R11.2 Front desk and managers (for their clinic) and admins send reminders for a clinic and a
  local date with `POST /desk/reminders`. Every appointment on that day sheet with status `booked`
  or `confirmed` whose patient is active, and which has never had a reminder, gets exactly one email
  to its patient whose subject contains `Appointment reminder`. The email names the clinic, the
  clinician, and the local date and time (`YYYY-MM-DD`, `HH:MM`). The page states how many were
  sent. Sending again for the same day reaches only appointments added since. No appointment is ever
  reminded twice, whatever is retried or restarted.
- R11.3 **Export.** `GET /desk/export.csv?clinic_id=<id>&from=<YYYY-MM-DD>&to=<YYYY-MM-DD>` (the
  clinic's front desk and managers, and admins) returns a CSV of every appointment of that clinic
  whose local start date is from `from` to `to` inclusive. A range of more than 31 days, or `to`
  before `from`, is refused with 400. The export includes the old system's appointments with status
  `booked`, `completed`, `no_show`, or `cancelled`, and the application's with status `confirmed`,
  `cancelled`, or `rescheduled`. It has this exact header row:

  `appointment_id,local_date,local_time,utc_start,clinician_id,clinician_name,patient_name,patient_email,type,status,deposit_cents,refund_cents`

  Then one row per appointment, ordered by start instant and then `appointment_id`, every row
  ending in CRLF. `local_date` (`YYYY-MM-DD`) and `local_time` (`HH:MM`) are at the clinic.
  `utc_start` is `YYYY-MM-DDTHH:MM:SSZ`. `patient_email` is trimmed and lowercased, and `type` is
  the type's code. `deposit_cents` is the appointment's deposit: the stored `deposit_cents` for the
  old system's rows, R3.4 or R8.3 for new ones. `refund_cents` is the refund fixed at cancellation,
  or 0. A field containing a comma, a double quote, CR, or LF is enclosed in double quotes, with
  inner quotes doubled. For spreadsheet safety, a `clinician_name` or `patient_name` that starts
  with `=`, `+`, `-`, `@`, a tab, or CR is written with a single quote `'` in front of it.

## Existing data (R12)

- R12.1 The database already contains `clinics`, `clinic_closures`, `rooms`, `users`, `clinicians`,
  `appointment_types`, `type_fees`, `schedule_versions`, `schedule_windows`,
  `schedule_exceptions`, `appointments`, and `clinical_notes`. Inspect them with `psql`. Serve
  that data as it is. Never drop, truncate, rename, or empty those tables or their columns, and
  never change or delete their existing rows, except that a user's `full_name` changes through
  `/profile`. Adding rows as this page describes is expected: users, clinicians, rooms, types,
  fees, schedule rows, closures, appointments, and notes. You may add tables, columns, indexes, and
  constraints. The application must apply its own schema additions idempotently every time it
  starts.
- R12.2 `appointments.local_start` is the start's local wall-clock time at the clinician's
  clinic. For a time that happened twice, `fold = 0` means the first occurrence and `fold = 1` the
  second. Times in a gap are read as in R4.4. Every appointment the application creates is a new
  row in `appointments`, numbered from its id sequence, with `local_start` and `fold` filled in
  this way. So the old system's appointments and new ones share one id space and one page,
  `/appointments/<id>`.

## Deployment (R13)

- R13.1 The workspace root contains an executable `start.sh` that starts the application
  listening on `0.0.0.0` at `PORT`, in the foreground, until it is stopped.
- R13.2 `GET /healthz` answers 200 only while the application can reach the database. Within 10
  seconds of the database stopping it answers a non-2xx status, and each answer arrives within 5
  seconds. Once the database is back, it answers 200 again within 60 seconds without a restart. It
  answers 200 within 60 seconds of `start.sh` starting.
- R13.3 Deployment copies the workspace to a clean machine with no network access, as regular
  files (symbolic links that point outside the workspace are not kept). Every runtime dependency
  must already be in the workspace (for example a virtual environment made with
  `python -m venv --copies`, or `node_modules`). `start.sh` must not download, install, or build
  anything.
- R13.4 Use only the services in the services file. The application must not contact any other
  host.
- R13.5 Add a `README.md` at the workspace root naming the stack, how `start.sh` starts the app,
  and the environment variables it reads.
- R13.6 Everything the application stores survives a restart. Holds and offers keep their expiry
  times, refunds still pending are retried, and nothing already sent is sent again.

## Safety (R14)

- R14.1 Every request that changes anything from a signed-in browser page must carry a token
  tied to that session that a page on another site cannot obtain. A request without the right
  token is refused with 403 and changes nothing. The webhook endpoint uses its signature instead.
- R14.2 Text that people typed or that the old system stored (names, notes, type names, room
  names) is always shown as text, on every page and in every attribute. The same holds for any part
  of a request that a page shows back, including not-found and error pages. It never becomes markup
  or script.
- R14.3 Amounts, deposits, rooms, roles, clinicians, patients, and authors always come from the
  application's own records and rules. Extra or altered form fields never change them.
- R14.4 Typed text never adds, removes, or changes an email header or recipient.
- R14.5 Passwords, password-reset tokens, waitlist offer tokens, and session identifiers are stored
  only as hashes, never in plain text. The application never stores the webhook signing key.
- R14.6 Every response to a signed-in user carries `Cache-Control: no-store`, and no page may be
  shown inside a frame on another site (`X-Frame-Options: DENY`, or a `Content-Security-Policy`
  with `frame-ancestors 'none'`).
- R14.7 An unexpected error or a malformed request is answered with a plain error page. It never
  shows a stack trace, database error text, or file paths.

## Experience (R15)

This is judged from what a person sees in a browser. Colours, fonts, layout, and wording are
yours.

- R15.1 Slot lists and appointment pages show local times with the clinic's name and time-zone
  abbreviation. The slot list states the deposit due.
- R15.2 After returning from the payment page, a not-yet-confirmed appointment says the payment
  is being confirmed and how long the slot stays held. A confirmed appointment shows the deposit
  paid.
- R15.3 Before cancelling, the page states the refund cancelling now would give and why (which
  notice band applies). After cancelling, the page shows the refund amount and whether it is
  pending or refunded.
- R15.4 An expired or released hold explains that the slot was given up and how to book again.
- R15.5 A waitlist offer page states the slot, when the offer expires, and what accepting does
  (a hold and a deposit payment).
- R15.6 A clinician's day sheet makes each appointment's local time, patient, and type clear.
- R15.7 Every page stays usable 1280 and 390 CSS pixels wide, with no horizontal scrolling.

## Interface contract

The application is driven through a real browser and through these paths, form fields,
and markers. Pages may carry any other content and styling. `<id>` values are database ids.

| Path | Method | Purpose |
|---|---|---|
| `/register` | GET, POST | fields `email`, `full_name`, `password`; button `data-action="register"` |
| `/password-reset` | GET, POST | field `email`; button `data-action="request-reset"` |
| `/password-reset/<token>` | GET, POST | field `password`; button `data-action="set-password"` |
| `/signin` | GET, POST | fields `email`, `password`, and `next` (hidden, R2.3); button `data-action="signin"` |
| `/signout` | POST | button `data-action="signout"` |
| `/profile` | GET, POST | field `full_name`; button `data-action="save-profile"` |
| `/api/availability` | GET | R5.3 |
| `/book` | GET | query `clinician_id`, `type`, `date`; one form per slot posting to `/book` with button `data-action="book-slot"` |
| `/book` | POST | fields `clinician_id`, `type`, `start` (R6.1) |
| `/appointments` | GET | the signed-in patient's appointments (old and new) |
| `/appointments/<id>` | GET | the appointment, with the forms below that the signed-in user may use now |
| `/appointments/<id>/release` | POST | button `data-action="release-hold"` |
| `/appointments/<id>/cancel` | POST | button `data-action="cancel-appointment"` |
| `/appointments/<id>/reschedule` | POST | fields `clinician_id`, `start`; button `data-action="reschedule"`; success redirects to the new appointment's page |
| `/appointments/<id>/notes` | GET, POST | field `body`; button `data-action="add-note"` |
| `/waitlist` | GET, POST | the patient's entries and a form to join, with fields `clinician_id`, `type`, `date`; button `data-action="join-waitlist"` |
| `/offers/<token>` | GET | the offer, with buttons `data-action="accept-offer"` and `data-action="decline-offer"` |
| `/offers/<token>/accept`, `/offers/<token>/decline` | POST | R9.4, R9.5 |
| `/clinician/day` | GET | query `date` |
| `/clinicians/<id>/schedule` | GET | the clinician's versions and exceptions, with the two forms below |
| `/clinicians/<id>/schedule/versions` | POST | fields `effective_from`, `windows`; button `data-action="add-version"` |
| `/clinicians/<id>/schedule/exceptions` | POST | fields `on_date`, `kind`, `start_time`, `end_time`; button `data-action="add-exception"` |
| `/desk/day` | GET | query `clinic_id`, `date` |
| `/desk/reminders` | GET, POST | fields `clinic_id`, `date`; button `data-action="send-reminders"` |
| `/desk/book` | GET, POST | fields `patient_email`, `clinician_id`, `type`, `start`; button `data-action="desk-book"` (R6.7) |
| `/desk/export.csv` | GET | query `clinic_id`, `from`, `to` (R11.3) |
| `/admin/clinics/<id>` | GET, POST | the clinic and its closures; fields `on_date`, `reason`; button `data-action="add-closure"` |
| `/admin/clinicians/new` | GET, POST | fields `email`, `full_name`, `clinic_id`, `max_per_day`; button `data-action="create-clinician"` |
| `/admin/rooms/new` | GET, POST | fields `clinic_id`, `name`, `room_type`; button `data-action="create-room"` |
| `/admin/types/new` | GET, POST | fields `code`, `name`, `duration_minutes`, `buffer_minutes`, `room_type`, `fee_cents`, `deposit_bp`, `effective_from`; button `data-action="create-type"` |
| `/admin/types/<code>` | GET, POST | the type and its fees; fields `effective_from`, `fee_cents`, `deposit_bp`; button `data-action="add-fee"` |
| `/webhooks/payments` | POST | R6.6 |
| `/healthz` | GET | R13.2 |

Dates are `YYYY-MM-DD`, form times `HH:MM`. Every form that changes something includes, inside
the form, its session token as a hidden `<input>` with the attribute `data-csrf`. Whatever its
`name`, it is submitted unchanged with the form. Refusals use the status codes stated
above: 400 for invalid input, 403 for not allowed, 409 for a conflict with the current state.

Markers, as HTML attributes:

- After creating a clinician, the page carries `data-clinician-id` and `data-user-id`. After
  creating a room, it carries `data-room-id`. After creating a type, it carries `data-type-code`.
- An appointment page has one element with `data-appointment-id`, `data-status` (`held`,
  `confirmed`, `cancelled`, `expired`, `released`, `rescheduled`, or the old system's status),
  `data-start` (instant), `data-clinician-id`, `data-type` (code), `data-room-id` (empty when
  none), `data-deposit-cents`, `data-refund-cents` (0 when none), and `data-refund-status`
  (`none`, `pending`, `refunded`).
- On `/appointments`, `/desk/day`, and `/clinician/day`, each appointment is inside an element
  with `data-appointment-id`, `data-start`, and `data-status`. Day sheets add `data-clinician-id`.
- On `/waitlist`, each entry is inside an element with `data-clinician-id`, `data-type`,
  `data-date`, and `data-entry-status` (`waiting`, `offered`, `booked`, `declined`, `expired`).
- An offer page has one element with `data-offer-status` (`offered`, `accepted`, `declined`,
  `expired`), `data-start`, `data-clinician-id`, and `data-type`.
- Each note on a notes page is inside an element with `data-note-id`.
- After sending reminders, the page carries `data-reminders-sent="<n>"`.
- After booking for a patient, the page carries `data-appointment-id` and an element with
  `data-checkout-url="<the session url>"`.
- On `/admin/clinics/<id>`, each closure is inside an element with `data-closure-date`.
- When signed in, the sign-out button has `data-action="signout"`.

Which actions a page offers: a page shows a button, with its form, exactly when the signed-in user
may take that action at that moment.

- An appointment page shows `release-hold` to its patient while it is `held`;
  `cancel-appointment` while the signed-in user may cancel it (R7.1–R7.3); and `reschedule` to its
  patient while R8.1 allows it (24 hours or more before its start).
- A notes page shows `add-note` to the appointment's clinician only.
- An offer page shows `accept-offer` and `decline-offer` while the offer is `offered`.
- `/book` shows `book-slot` buttons to a signed-in patient only (R5.4).
- `/admin/clinics/<id>` shows `add-closure` to those who may add a closure there (R4.6).
