# Shiftbook: timekeeping and payroll for a restaurant group

Our restaurant group runs three kitchens: Mission St in San Francisco, Roosevelt Row in Phoenix,
and Wicker Park in Chicago. Staff clock in and out on time clocks at each site, managers approve
the hours every week, and payroll pays everyone every two weeks. Until now an old system did this,
and it got overtime wrong often enough that we are replacing it. You are building the new web
application from scratch in this workspace.

The workspace is empty. The environment already runs two services: a PostgreSQL database that
holds our locations, staff, pay rates, and the punches and approvals the old system recorded; and
an SMTP server that accepts every email the application sends. Their connection details are in
`/etc/shiftbook/services.env`, one `NAME=value` per line: `DATABASE_URL`, `PORT`,
`PUBLIC_BASE_URL`, `SMTP_HOST`, `SMTP_PORT`, `MAIL_FROM`, `RESET_LINK_TTL_SECONDS`, and
`LOGIN_LOCKOUT_SECONDS`. The application is always started with these variables exported in its
environment; read them from the environment at run time.

Money is whole US cents unless this page says otherwise. Hours are shown with exactly one decimal
place (`8.0`, `12.5`).

## People and roles (R1)

- R1.1 Everyone who uses the application is a row in `employees`. `role` is `employee`,
  `manager`, or `payroll_admin`. Only rows with `active = true` may sign in or be paid for new work.
- R1.2 `manager_id` forms the reporting tree. Employee B is **below** manager A when A is B's
  manager, or the manager of B's manager, and so on up the chain.
- R1.3 What each role may see and do:
  - an employee sees their own timesheets and may change their own display name (R6.4);
  - a `manager` additionally sees the timesheets of everyone below them and approves their weeks;
  - a `payroll_admin` sees every timesheet, approves any week except their own, manages staff,
    rates and time clocks, and runs payroll.
  Nobody ever approves their own week. A signed-in user who requests a page or action they are not
  allowed gets a 403 response and nothing changes; a visitor who is not signed in is sent to the
  sign-in page.

## Signing in (R2)

- R2.1 Staff in the database have no password yet. Anyone sets or resets a password from
  `/password-reset` by entering their email address. If the address belongs to an active
  employee, one email goes to that address with a single-use link that starts with
  `PUBLIC_BASE_URL`; the link opens a page that sets a new password (at least 10 characters).
  Whether or not the address is on file, the response has the same status code and shows the same
  message, and an address that is not on file (or an inactive employee's) receives no email.
- R2.2 Setting a new password through a reset link signs out every existing session of that
  employee. A reset link works once, expires `RESET_LINK_TTL_SECONDS` seconds after it was requested,
  and stops working as soon as a newer link is requested for the same employee. A used, expired,
  or replaced link changes nothing and says the link is no longer valid; an unknown link answers 404.
- R2.3 Email addresses are matched case-insensitively and ignoring leading and trailing
  whitespace, everywhere.
- R2.4 Sign-in takes an email address and password. A wrong password, an unknown address, or an
  inactive employee is refused with the same message and no session.
- R2.5 After 5 consecutive failed sign-ins for the same employee, that employee cannot sign in,
  even with the right password, until `LOGIN_LOCKOUT_SECONDS` seconds after the fifth failure; the
  page says the account is temporarily locked. A successful sign-in resets the count.
- R2.6 Signing in always starts a new session identifier; no identifier the browser held before
  signing in ever becomes signed in. Signing out ends the session on the server: a session cookie
  copied before sign-out no longer signs anyone in. Session cookies are `HttpOnly` and
  `SameSite=Lax` or `SameSite=Strict`.
- R2.7 Passwords and time-clock keys (R4.1) are stored only as salted hashes; password-reset tokens
  and session identifiers are stored only as hashes. None of them is ever stored in plain text.
- R2.8 A visitor sent to the sign-in page from a page that needs sign-in returns to that page after
  signing in. `/signin` accepts a `next` query parameter for this; after a successful sign-in the
  browser goes to `next` only when it is a path on this site; otherwise it goes to
  `/timesheet`. The application never redirects anyone to another site.

## Staff, rates, and time clocks (R3, R4)

- R3.1 A payroll admin adds an employee with an email address, full name, location, manager
  (optional), role, and their first job: a job code (for example `COOK`), an hourly rate in cents,
  and the date that rate takes effect. The new employee is a new row in `employees` (active) with
  that rate in `job_rates`. An email address already on file is refused with a message; nothing
  changes.
- R3.2 A payroll admin adds a rate for an employee: job code, hourly rate in cents, and an
  effective date. Rates are never edited or removed; a rate for the same job code and effective date
  as an existing one is refused. Work on a given local date (R5.1) is paid at the rate for that job
  whose effective date is the latest one on or before that date. Work on a date before a job's
  first effective date cannot be paid and is an exception (R5.6).
- R4.1 A payroll admin creates a time clock for one location with a label. The page shown right
  after creation displays its secret key once; afterwards the key is never shown again. A time clock
  can be revoked; a revoked or unknown key is refused with 401 by the punch API.
- R4.2 Time clocks send punches to `POST /api/punches` with the header
  `Authorization: Bearer <key>` and a JSON body `{"punches": [ ... ]}` of at most 5000 rows. Each
  row has `punch_id` (a string of 1 to 64 characters), `employee_id`, `kind` (`in`, `out`,
  `break_start`, or `break_end`), `at` (an RFC 3339 timestamp with `Z` or an explicit offset, to
  the second or finer), and, for `in` only, `job_code`. The response is `200` with
  `{"results": [ ... ]}`: one entry per row, in the same order, each
  `{"punch_id": ..., "status": "accepted" | "duplicate" | "rejected", "reason": ...}`. A body that
  is not valid JSON of that shape is answered 400 and changes nothing.
- R4.3 Rows are processed in order. A row is **rejected**, with a reason, and changes nothing when:
  a field is missing or malformed; the timestamp has no offset; the employee does not exist, is
  inactive, or does not work at the time clock's location; an `in` names a job code the employee
  has no rate for; the employee already has a punch at exactly the same instant; or the `punch_id`
  was already used for a punch with any different field. A row whose `punch_id` was already
  accepted with exactly the same fields is a **duplicate**: it changes nothing. Every other row is
  **accepted**. When two requests carrying the same `punch_id` arrive at the same moment, at most
  one of them accepts it.
- R4.4 The punches the old system recorded are in `legacy_punches`. `local_time` is the wall-clock
  time at the employee's location; for a wall-clock time that happened twice because clocks were
  set back, `fold = 0` means the first occurrence and `fold = 1` the second. Legacy punches count
  exactly like accepted punches. The old system's approvals are in `legacy_approvals`; each row
  means that employee's week starting `week_start` is approved (R6).

## Existing data (R10)

The database already contains the tables `locations`, `employees`, `job_rates`,
`legacy_punches`, and `legacy_approvals`. Serve
that data as it is: never drop, truncate, rename, or empty those tables or their columns, and never
change or delete their existing rows, except that an employee's `full_name` changes when they change
their display name (R6.4). Adding new employees and new rates as R3 describes is expected. You may add tables, columns, indexes, and constraints, and the application must apply
its own schema additions idempotently every time it starts.

## Pay rules (R5)

- R5.1 **Local time.** Each location has an IANA time zone in `locations.timezone`. An employee's
  work is always measured at their own location. A **workday** is a local calendar day, from
  local midnight to the next local midnight. A **workweek** runs from Sunday 00:00 local to the next Sunday 00:00 local. A **pay
  period** is two consecutive workweeks; pay periods start on Sunday 2026-09-20 and every 14 days
  after it (2026-10-04, 2026-10-18, 2026-11-01, ...).
- R5.2 **Shifts.** An employee's punches, in time order, form shifts: `in`, then any number of
  `break_start`/`break_end` pairs, then `out`. Breaks are unpaid meal breaks. The `in` punch's job
  code applies to the whole shift.
- R5.3 **Rounding.** Before anything else, every punch time is rounded to the nearest multiple of
  6 minutes since 1970-01-01T00:00:00Z; a time exactly 3 minutes past a multiple rounds up. Worked
  time of a shift is from its rounded `in` to its rounded `out`, minus each rounded break.
- R5.4 **Classifying worked time.** Walk the workweek's worked time in chronological order. Each
  moment of work is regular (REG), overtime (OT), or double time (DT). Let *h* be the hours the
  employee already worked earlier in the same workday (all shifts, all categories) and *r* the REG
  hours already counted earlier in the same workweek.
  - At a location whose `ruleset` is `CA`:
    - if the employee worked more than zero hours on each of the seven workdays of the
      workweek, the Saturday of that workweek is a *seventh day*: its work is OT while *h* < 8 and
      DT after that;
    - otherwise the work is DT when *h* ≥ 12, else OT when *h* ≥ 8, else OT when *r* ≥ 40, else REG.
  - At a location whose `ruleset` is `FLSA`: the work is REG while *r* < 40, else OT. There is no DT.
- R5.5 **Pay for a workweek.** Each hour is paid at its job's rate (R3.2): this is the
  *straight-time pay*. The workweek's *regular rate* is its straight-time pay divided by all hours
  worked in it. On top of straight-time pay, each OT hour earns half the regular rate and each DT
  hour earns the whole regular rate. **Meal premiums (CA only):** a shift with more than 5 worked
  hours earns one meal premium unless it has a break of at least 30 minutes that starts no later
  than the moment the employee has worked 5 hours in that shift; a meal premium pays one hour at
  the regular rate of the workweek containing the workday the shift started on, and an employee
  earns at most one meal premium per workday (attributed to the workday the shift started on).
  Rounding happens once, at the end, and only to the four amounts named next.
  For each workweek, the four amounts — straight-time pay, OT premium, DT premium, and meal
  premium pay — are each rounded to the nearest cent (half a cent rounds up), and the workweek's
  gross pay is the sum of the four rounded amounts.
- R5.6 **Exceptions.** A workweek has an exception when any of its punches breaks the order in
  R5.2, a shift lasts more than 16 hours from `in` to `out` (unrounded), a shift has no `out` (including a
  shift that a punch breaking the order interrupts), or work falls
  on a date before the job's first effective rate. An exception belongs to the workweek containing
  the local date of the punch that causes it (for a shift that is too long or has no `out`, its
  `in` punch; for work before a job's first rate, the shift's `in` punch). A punch that breaks the
  order is not used to open a shift; pairing continues with the next `in`. A shift that is
  interrupted, has no `out`, or lasts more than 16 hours counts as worked time in no workweek. A week
  with an exception cannot be approved or paid until the punches are fixed.

## Approvals (R6)

- R6.1 Each employee's workweek is `pending`, `approved`, or `exception`. A manager approves weeks
  of people below them and a payroll admin approves anyone's week but their own (R1.3). A week with
  an exception, with no worked time, or already approved cannot be approved; the request is refused
  with a message.
- R6.2 Any accepted punch that changes a workweek's worked time or exceptions, and any new rate
  that changes its pay, returns an approved week to `pending`, including weeks approved in
  `legacy_approvals`.
- R6.3 `/approvals` lists every pending week the signed-in user may approve.
- R6.4 Any employee may change their own display name (`full_name`) from `/profile`. That form
  changes nothing else, whatever other fields the request carries. A display name, here and when a
  payroll admin adds an employee, is 1 to 100 characters with no control characters (no CR, LF, tab,
  or other character below U+0020); anything else is refused with a message and changes nothing.

## Payroll runs (R7)

- R7.1 A payroll admin creates the **draft** run for a pay period by its first date. Creating it
  again while it is a draft recomputes the same run. A period has at most one run, and once it is
  finalized no new draft can be created for it (refused with 409). A date that is not the first day
  of a pay period is refused.
- R7.2 A run has one line per employee who has anything to be paid in it. A workweek of the run's
  period is **payable** when it is approved and has no exception; payable weeks are paid in the line
  (hours by category, the four amounts of R5.5, and gross). A week of the period that has worked
  time but is not payable is **held**: the run lists it with its reason and pays nothing for it.
- R7.3 **Adjustments.** For every workweek that starts before the run's period (from 2026-09-20
  on) and is payable now, compare its gross pay computed now with the gross already paid for it by
  finalized runs. A difference is paid in the line as an adjustment, positive or negative. A week
  that is not payable now gets no adjustment.
- R7.4 **Finalizing.** The run page carries a version that changes every time the draft is
  recomputed; finalizing sends that version. Finalizing succeeds only when the version is current
  and recomputing the run at that moment would give exactly the lines the draft shows; otherwise it
  is refused with 409, the draft stays a draft, and the page says the draft is out of date. A
  finalized run never changes. When two finalize requests for the same run arrive at the same
  moment, exactly one succeeds.
- R7.5 Every email the application sends is addressed with the employee's display name and address
  in its `To` header (for example `To: Maria Lopez <maria.lopez.7@shiftbook.example>`).
  When a run is finalized, each employee with a line receives exactly one email whose subject
  contains `Pay statement` and whose plain-text body names the period's first and last dates
  (`YYYY-MM-DD`), the REG, OT, and DT hours, and the gross pay in dollars, formatted like
  `$1,234.56` (negative amounts like `-$12.00`). It is sent within 30 seconds of finalizing and never
  again, whatever is retried or restarted.
- R7.6 Creating or recomputing a draft for any period, with the data the database starts with,
  answers within 120 seconds.

## Export (R8)

- R8.1 `GET /payroll/runs/<id>/export.csv` (payroll admins only) returns the run as CSV: this
  exact header row, then one row per line ordered by `employee_id` ascending, every row ending in
  CRLF:

  `employee_id,full_name,location,reg_hours,ot_hours,dt_hours,meal_premiums,straight_cents,ot_premium_cents,dt_premium_cents,meal_cents,adjustment_cents,gross_cents`

  `location` is the location's name. Hours, premiums, and amounts are for the run's own payable
  weeks; `adjustment_cents` is the sum of the line's adjustments; `gross_cents` is the four amounts
  plus `adjustment_cents`. A field that contains a comma, a double quote, CR, or LF is enclosed in
  double quotes with inner quotes doubled.
- R8.2 Spreadsheet safety: in `full_name` and `location`, a value that starts with `=`, `+`, `-`,
  `@`, a tab, or CR is written with a single quote `'` in front of it. Numeric columns are written
  as plain numbers.

## Deployment (R9)

- R9.1 The workspace root contains an executable `start.sh` that starts the application listening
  on `0.0.0.0` at the port in `PORT`, in the foreground, until it is stopped.
- R9.2 `GET /healthz` answers 200 only while the application can reach the database, and a non-2xx
  status within 10 seconds of the database stopping, each answer arriving within 5 seconds. Once the
  database is back, `/healthz` answers 200 again within 60 seconds without a restart. The
  application answers `/healthz` with 200 within 60 seconds of `start.sh` starting.
- R9.3 Deployment copies the workspace to a clean machine with no network access, as regular files
  (symbolic links that point outside the workspace are not preserved). Every runtime dependency must
  already be inside the workspace (for example a virtual environment made with
  `python -m venv --copies`, packages installed into a folder in the workspace, or `node_modules` in
  the workspace), and `start.sh` must not download, install, or build anything.
- R9.4 Use only the two services in the services file. The application must not contact any other
  host.
- R9.5 Add a `README.md` at the workspace root naming the stack, how `start.sh` starts the app, and
  the environment variables it reads.
- R9.6 Everything the application stores must survive the application being stopped and started
  again.

## Safety (R11)

- R11.1 Every request that changes anything from a signed-in browser page must carry a token tied
  to that session that a page on another site cannot obtain; a request without the right token is
  refused with 403 and changes nothing. The punch API (R4.2) uses its bearer key instead.
- R11.2 Text that people typed and anything taken from a request's
  path or query is always shown as text, on every page; it never becomes markup or script.
- R11.3 Amounts, hours, rates, roles, locations, approvers, and whose data a request touches always
  come from the application's own records and rules; extra or altered form fields never change them.
- R11.4 Every response to a signed-in user carries `Cache-Control: no-store`, and no page may be shown
  inside a frame on another site (`X-Frame-Options: DENY`, or a `Content-Security-Policy` with
  `frame-ancestors 'none'`).
- R11.5 An unexpected error or a malformed request is answered with a plain error page; it never shows
  a stack trace, database error text, or file paths.

## Experience (R12)

- R12.1 A timesheet makes clear, for each day, the hours by category, and for the week whether it
  is pending, approved, or has an exception, with the exception explained in plain language.
- R12.2 Before approving, the page states what approval means (the week becomes payable in the
  next run, and later changes return it to pending).
- R12.3 A draft run page states which weeks are held and why, and which lines include adjustments
  for earlier weeks.
- R12.4 Before finalizing, the page states that finalizing locks the run, emails pay statements,
  and cannot be undone; after any refused action the page explains what to do next.
- R12.5 Every page stays usable 1280 and 390 CSS pixels wide, with no horizontal scrolling.

## Interface contract

The application is driven through a real browser and through these paths, form fields,
and markers. Pages may carry any other content and styling. `<id>` values are ids from the
database; `<week>` is a workweek's Sunday as `YYYY-MM-DD`.

| Path | Method | Purpose |
|---|---|---|
| `/password-reset` | GET, POST | field `email`; button `data-action="request-reset"` (R2.1) |
| `/password-reset/<token>` | GET, POST | field `password`; button `data-action="set-password"` |
| `/signin` | GET, POST | fields `email`, `password`; button `data-action="signin"` |
| `/signout` | POST | ends the session; button `data-action="signout"` |
| `/profile` | GET, POST | field `full_name`; button `data-action="save-profile"` (R6.4) |
| `/timesheet` | GET | the signed-in user's own timesheet; query `week=<week>` (default: the current week) |
| `/employees/<id>/timesheet` | GET | that employee's timesheet; query `week=<week>` |
| `/employees/<id>/weeks/<week>/approve` | POST | approve (R6) |
| `/approvals` | GET | weeks awaiting the signed-in user's approval (R6.3) |
| `/admin/employees/new` | GET, POST | fields `email`, `full_name`, `location_id`, `manager_id` (may be empty), `role`, `job_code`, `rate_cents`, `effective_from` (`YYYY-MM-DD`); button `data-action="create-employee"`; success shows the new employee's page |
| `/admin/employees/<id>` | GET | the employee, their rates |
| `/admin/employees/<id>/rates` | POST | fields `job_code`, `rate_cents`, `effective_from`; button `data-action="add-rate"` |
| `/admin/devices` | GET, POST | field `location_id`, `label`; button `data-action="create-device"` |
| `/admin/devices/<id>/revoke` | POST | button `data-action="revoke-device"` |
| `/api/punches` | POST | the punch API (R4.2) |
| `/payroll` | GET | payroll home: the form that creates a draft run |
| `/payroll/runs` | POST | field `period_start` (`YYYY-MM-DD`); button `data-action="create-run"`; responds by redirecting to the run's page |
| `/payroll/runs/<id>` | GET | the run |
| `/payroll/runs/<id>/finalize` | POST | field `version`; button `data-action="finalize-run"` |
| `/payroll/runs/<id>/export.csv` | GET | R8 |
| `/healthz` | GET | R9.2 |

Every form that changes something includes, inside the form, its session token as a hidden
`<input>` with the attribute `data-csrf`; whatever its `name`, it is submitted unchanged with
the form.

Markers, as HTML attributes:

- The new employee's page after R3.1 carries `data-employee-id="<id>"`.
- After creating a time clock, the page carries `data-device-id="<id>"` and an element with
  `data-device-key="<key>"`.
- A timesheet page carries one element with `data-employee-id`, `data-week-start`, and
  `data-status="<pending|approved|exception>"`, containing seven elements with `data-day="<YYYY-MM-DD>"`,
  `data-reg-hours`, `data-ot-hours`, and `data-dt-hours`; a button `data-action="approve-week"` inside
  a form posting to the approve path, shown only when the signed-in user can approve that week
  (R1.3, R6.1).
- On `/approvals`, each week is inside an element with `data-employee-id` and `data-week-start`.
- A run page carries `data-run-id`, `data-run-status="<draft|finalized>"`, `data-period-start`,
  and `data-version`; each line is inside an element with `data-line-employee-id` and
  `data-gross-cents`; each held week is inside an element with `data-held-employee-id` and
  `data-week-start`; while a draft, a button `data-action="finalize-run"` inside a form posting to
  the finalize path with a field `version`.
- When signed in, the sign-out button has `data-action="signout"`.
