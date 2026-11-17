# TRUTH: ethara/seatwise-tickets

Task version 1.0.0.

Sources: `task.toml`, `README.md`, `instruction.md`, `environment/`, `solution/`, `tests/` (`workflows.toml`, `reward.toml`, `judge.toml`, `scoring.py`, `test.sh` and the workflow code), the seed loaded into PostgreSQL, and the verifier output of oracle run `20261111T114744Z-r2qc-sw-oracle` (`results.json` sha256 `27e72479bcbeb88fc5beff7097f8e7d1fa6bd50ac4b0f9db2b9364e1b6341f7b`).

This file is for graders and reviewers. The agent never sees it: Harbor does not copy `solution/` into the agent's container, and `environment/Dockerfile` copies only `uv-requirements.lock`, `services`.

## Reward and pass rule

Full reward is `1.0` on the `reward` key of `/logs/verifier/reward.json` (keys `reward`, `functional`, `judge`).

| term | rule | source |
|---|---|---|
| reward | `0.9 × functional + 0.1 × judge` (weighted sum) | `tests/reward.toml` |
| functional | weighted mean of 27 strict workflows (weights sum to 103); a workflow scores 1 only when every one of its checks passes, else 0 | `tests/workflows.toml`, `tests/scoring.py` |
| cap | any breach detector firing anywhere in the run (×0.4), or any of the 15 critical workflows failing (×0.4), caps every workflow, so the reward is at most 0.46; a critical workflow that fails only because the verifier's time budget ran out scores 0 without the cap | `tests/scoring.py` |
| judge | weighted mean of 10 binary criteria, each judged by `anthropic/claude-fable-5-1` (reasoning effort max, 3 samples) from the checkpoint evidence under `/logs/verifier/ux/` | `tests/judge.toml`, `tests/judge-reference.md` |
| invalid run | `test.sh` writes a zero reward first; a verifier, service or judge failure leaves that zero and writes `/logs/verifier/evaluation-invalid.json` | `tests/test.sh` |

## Golden trajectory

| step | what happens | source |
|---|---|---|
| S1 | the environment starts PostgreSQL seeded with the existing records, the SMTP mail sink and the Stripe-compatible payments mock, and writes `/etc/seatwise/services.env` | `environment/services/up.sh` |
| S2 | `solve.sh` copies `solution/app/` into `/app`, makes a copy-based virtual environment, and installs `requirements.txt` offline from the 4 wheels in `solution/wheels/` | `solution/solve.sh` |
| S3 | the verifier starts `/app/start.sh` (`exec ./.venv/bin/python -u app.py`) on a fresh copy of the database; in the oracle run `/healthz` answered 200 after 1 s | `solution/app/start.sh`, `startup.json` |
| S4 | the verifier runs the 27 workflows (148 checks) and the judge; the oracle passed every check, fired no breach detector, and scored functional 1.0, judge 1.0, reward 1.0 | `results.json`, `reward.json` |

## Records the checkers rely on

The agent's database and the verifier's are both seeded from `db/init/` (`001_schema.sql`, `002_records.sql`); `tests/services/db/` is byte-identical to `environment/services/db/` (tree sha256 `3918668174d7a4eb83c8ff066123156f180c3937a0d0fee40e17678ab853b179`). Row counts after loading the seed into PostgreSQL:

| table | rows |
|---|---|
| `event_prices` | 140 |
| `events` | 58 |
| `legacy_orders` | 6268 |
| `legacy_tickets` | 20099 |
| `legacy_transfers` | 773 |
| `memberships` | 23 |
| `organizations` | 3 |
| `seats` | 1662 |
| `sections` | 9 |
| `users` | 22 |
| `venues` | 4 |

Volumes the checks compared in oracle run `20261111T114744Z-r2qc-sw-oracle` (whole-number observations from `results.json`; counts that depend on the run date can differ on another day):

| workflow | observed |
|---|---|
| W06 | orders_checked 116; seat_order_orders 25 |

## Rubric

### Strict workflows

| id | weight | share | critical | checks | workflow |
|---|---|---|---|---|---|
| W01 | 4 | 3.9% | no | 8 | Accounts and sessions: verification, reset links, refusals, session rotation, sign-out, next= |
| W02 | 4 | 3.9% | no | 10 | Organizations: invitations, the five-role permission matrix, cross-organization isolation |
| W03 | 3 | 2.9% | no | 6 | Venues and seat maps; events in venue time zones (folds, gaps); drafts, publishing, rescheduling |
| W04 | 3 | 2.9% | no | 5 | Sales windows: presale with a code, on-sale, off-sale, at the venue's local times |
| W05 | 5 | 4.9% | yes | 6 | Purchase: seat map, hold, price breakdown, Checkout Session, hosted payment, webhook, tickets, one email |
| W06 | 5 | 4.9% | yes | 4 | Prices at scale: fees, tax, stacked codes, largest-remainder allocation and rounding ties on every line |
| W07 | 4 | 3.9% | yes | 5 | Promo codes: case-insensitive entry, one percent plus one fixed, refusals, locking at Pay, caps under concurrency |
| W08 | 5 | 4.9% | yes | 8 | Holds: expiry from creation, release, provider sessions expired, late payments confirmed or refunded |
| W09 | 4 | 3.9% | yes | 5 | Webhooks: forged, unsigned and stale deliveries refused; duplicates and out-of-order events harmless; declined cards |
| W10 | 4 | 3.9% | yes | 4 | Seat races and the per-buyer limit: one winner per seat, six tickets per buyer across orders and old orders |
| W11 | 5 | 4.9% | yes | 5 | Buyer refunds: 168/48-hour tiers to the second, deadlines across DST, reschedules, who may ask |
| W12 | 4 | 3.9% | yes | 6 | Refund mechanics: Idempotency-Key retries through provider faults, pending until the webhook, events out of order |
| W13 | 5 | 4.9% | yes | 7 | Organizer refunds and cancelling an event: top-up refunds, one email per address, late payment after cancellation |
| W14 | 4 | 3.9% | no | 4 | Transfers: the recipient holds a new QR token, every earlier token stops admitting, emails, who may transfer |
| W15 | 4 | 3.9% | yes | 5 | Scanner devices and online check-in: keys shown once, scope, every refusal reason, idempotent rescans, door list |
| W16 | 4 | 3.9% | yes | 8 | Offline sync: earliest admission wins across gates and batches, ties, scan_id reuse, windows to the second across DST |
| W17 | 4 | 3.9% | no | 7 | Waiting lists: join rules, offers in join order with skip-over, offer expiry and decline, reserved seats |
| W18 | 4 | 3.9% | yes | 4 | Dashboard figures at request time: sold, held, available, admitted, gross, refunded, net, per section |
| W19 | 3 | 2.9% | no | 3 | Attendee export: header, CRLF, seat order (numeric), resold seats, quoting, spreadsheet safety, who may download |
| W20 | 3 | 2.9% | no | 3 | Reminders and email safety: one email per holder address, one-time key, header injection, names in To |
| W21 | 4 | 3.9% | yes | 6 | Old platform, Lantern Room Presents (Chicago): dashboards, exports, sale pages, holders |
| W22 | 4 | 3.9% | yes | 5 | Old platform, Copperline Live (Denver): dashboards, exports, sale pages, holders |
| W23 | 4 | 3.9% | yes | 6 | Old platform, Saguaro Sounds (Phoenix): dashboards, exports, sale pages, holders, old barcodes at the door |
| W24 | 3 | 2.9% | no | 7 | In a real browser: layout at 1280 and 390 px, typed text never runs as script, a click-through purchase |
| W25 | 2 | 1.9% | no | 3 | Deployment: README, restart persistence (accounts, orders, QR tokens), nothing re-sent, old records kept |
| W26 | 3 | 2.9% | no | 5 | Cross-cutting safety: secrets only hashed at rest, reset ends sessions, Host-proof links, headers, no stack traces |
| W27 | 2 | 1.9% | no | 3 | Health follows the database: non-2xx within 10 s of a stop, 200 within 60 s of a start |

### Checks

Every check of every workflow, in order, as the oracle run recorded them (each matches a `ctx.step()` in `tests/`). A workflow scores only when all of its checks pass.

**W01** Accounts and sessions: verification, reset links, refusals, session rotation, sign-out, next=

1. sign-up sends a verification link; sign-in is refused until it is followed; addresses match ignoring case and spaces
2. signing up again before verifying replaces the link and password; an old or expired link verifies nothing
3. sign-up is refused for a verified address, an organization member's address, and a name with a control character
4. password reset: same answer for known and unknown addresses; single use; replaced; expired; unknown link 404
5. a wrong password and an unknown address are refused with the same message and no session
6. signing in starts a new session identifier; an identifier held before never becomes signed in; cookie flags
7. signing out ends the session on the server
8. a visitor sent to sign in returns to the page; next= never leads to another site

**W02** Organizations: invitations, the five-role permission matrix, cross-organization isolation

1. an account creates an organization and becomes its owner
2. the owner invites a manager, box office and two scanners; invitations are emailed and accepted
3. an invitation never makes an owner; an existing member cannot be invited again; a token-less invite is refused
4. an invitation link works only for the invited address; a visitor is sent to sign in
5. each role reaches exactly the pages R1.3 allows (403 otherwise); a non-member gets 403; a visitor is sent to sign in
6. forbidden actions answer 403 and change nothing; allowed ones work
7. the owner changes roles and removes members; permissions follow at once; the owner's own role cannot change
8. another organization's events, tickets and devices stay out of reach, even through this organization's URLs
9. extra form fields never move an invitation or an event into another organization
10. a scanner of two organizations has each organization's scanner rights only

**W03** Venues and seat maps; events in venue time zones (folds, gaps); drafts, publishing, rescheduling

1. a manager builds a venue with sections, row ranges and accessible seats; bad sections are refused
2. event times are wall-clock times at the venue: a repeated time means its first occurrence, a missing one is refused
3. invalid event settings are refused and create nothing
4. drafts are invisible outside the organization; publishing shows them; the publish button only on drafts
5. the seat map is fixed once an event exists
6. rescheduling moves the start; a past or missing wall-clock time is refused

**W04** Sales windows: presale with a code, on-sale, off-sale, at the venue's local times

1. an event with a presale, on-sale and off-sale a minute apart, typed in the venue's local time
2. before the presale nobody may hold seats, code or not
3. during the presale a hold needs the code (any case, spaces ignored)
4. from on-sale anyone may hold seats without a code
5. from off-sale nobody may hold seats

**W05** Purchase: seat map, hold, price breakdown, Checkout Session, hosted payment, webhook, tickets, one email

1. a published event shows its seat map with prices, availability and accessible seats
2. an accessible seat needs the accessible-seating box; a hold creates a pending order with an expiry
3. Pay redirects to one Checkout Session for the order total; pressing it again goes to the same session
4. paying on the hosted page returns to the order page; only the webhook marks it paid and issues tickets
5. exactly one confirmation email lists every seat, ticket id and QR token, addressed with the buyer's name
6. My tickets and the ticket pages show the tickets; the event page shows the seats sold

**W06** Prices at scale: fees, tax, stacked codes, largest-remainder allocation and rounding ties on every line

1. pricing events with the fixture's section prices, facility fees and promo codes
2. each of 116 orders shows every line's face, discount share, fees, tax share and total exactly
3. seat order decides ties: sections created out of order (ZED, MID, ALP), seat 9 before seat 10
4. paid orders charge exactly the quoted total and keep their breakdown

**W07** Promo codes: case-insensitive entry, one percent plus one fixed, refusals, locking at Pay, caps under concurrency

1. an owner creates codes; malformed or duplicate codes are refused
2. codes apply in any case; one percent and one fixed stack; repeats, a second percent code and other events' codes are refused
3. extra form fields never change amounts; codes lock once Pay is pressed
4. applied at the same moment to eight orders, a code with three redemptions lands on exactly three
5. paid orders keep their redemption; released and expired holds give theirs back

**W08** Holds: expiry from creation, release, provider sessions expired, late payments confirmed or refunded

1. a hold expires HOLD_TTL_SECONDS after it was created, even when Pay was pressed later
2. at expiry the order is expired and its seats are available at once to the next buyer
3. the expired order's Checkout Session is expired at the provider within 30 s
4. a payment completing after expiry, with the seats still free, is confirmed as normal
5. a payment completing after expiry, when a seat was sold meanwhile, is refunded in full once and emailed once
6. a late payment whose seat is now held by another buyer is refunded; the other hold is untouched
7. a late payment whose seat is now under an open waitlist offer is refunded; the offer stays open
8. releasing a hold frees its seats at once and expires its session

**W09** Webhooks: forged, unsigned and stale deliveries refused; duplicates and out-of-order events harmless; declined cards

1. forged, unsigned, stale or mis-signed completion events are answered 400 and change nothing
2. a declined card leaves the order pending; paying again with a good card confirms it
3. the completion event delivered again changes nothing and sends nothing
4. events delivered out of order (payment_intent.succeeded before checkout.session.completed) confirm once
5. returning from the hosted page's cancel link leaves the order pending; the webhook path refuses junk

**W10** Seat races and the per-buyer limit: one winner per seat, six tickets per buyer across orders and old orders

1. eight buyers holding the same last seat at the same moment: exactly one gets it
2. two simultaneous holds of four seats by one buyer cannot together pass six
3. paid tickets and unexpired holds count toward six; a refund gives room; a transfer does not
4. the old platform's orders count, matched by address whatever their case and spacing

**W11** Buyer refunds: 168/48-hour tiers to the second, deadlines across DST, reschedules, who may ask

1. tickets for events 10 days, 100 hours and 20 hours away and across DST changes show their deadlines
2. refunds at 100 % and 50 % return each ticket's share exactly; within 48 hours none is possible
3. at the 168-hour and 48-hour deadlines (to the second): D-5 s and D+0.05 s get the higher tier, D+1.05 s and D+2 s the lower
4. after a reschedule, orders paid before it get 100 % until the new start; later orders follow the tiers
5. a buyer who transferred a ticket away, and its new holder, cannot get a buyer refund

**W12** Refund mechanics: Idempotency-Key retries through provider faults, pending until the webhook, events out of order

1. a buyer with six tickets for an event ten days away
2. a refund whose first provider call ends in 'lost_response' is retried with the same key and made exactly once
3. a refund whose first provider call ends in 'timeout' is retried with the same key and made exactly once
4. a refund whose first provider call ends in 'error' is retried with the same key and made exactly once
5. a refund stays refund_pending (no email, not counted) until its refund.created event, delivered out of order
6. refund.created delivered again changes nothing and sends nothing; every refund used its own key

**W13** Organizer refunds and cancelling an event: top-up refunds, one email per address, late payment after cancellation

1. an event 100 hours away with buyers, a 50 % buyer refund, a transfer, and a box office refund
2. only the owner may cancel; the owner cancels while a pending order and a pending 50 % buyer refund exist
3. every ticket is refunded exactly what it had left (counting the pending refund), once, at the provider
4. each buyer and holder gets exactly one cancellation email; the only refund email is the buyer's own 50 % refund
5. a payment completing after the cancellation is refunded in full and the buyer told
6. an event with tickets from the old platform cannot be cancelled
7. cancelling an event ends an open waitlist offer: shown expired or declined (R14.3's endings), no controls, no order

**W14** Transfers: the recipient holds a new QR token, every earlier token stops admitting, emails, who may transfer

1. a buyer transfers a ticket; the recipient and the buyer each get one email; the new token admits, the old one does not
2. nobody else may transfer it, nor transfer to themselves, nor transfer an admitted ticket
3. a chain of transfers leaves only the newest token admitting; altered tokens are invalid
4. an old-platform ticket is transferred here: its barcode stops admitting, the new token admits

**W15** Scanner devices and online check-in: keys shown once, scope, every refusal reason, idempotent rescans, door list

1. an owner and manager create devices (key shown once); box office and scanners may not
2. a valid token is admitted once; scanning it again reports the first admission
3. refusals: invalid, wrong event (same and other organization), refunded, cancelled, outside the window
4. unknown and revoked keys get 401; malformed bodies get 400
5. the door list offers check-in only for valid tickets not yet admitted; a door check-in admits with gate Door

**W16** Offline sync: earliest admission wins across gates and batches, ties, scan_id reuse, windows to the second across DST

1. events on the next spring-forward (Chicago) and fall-back (Denver) mornings with tickets and devices
2. batch 1 (North): window edges to the second, a DST-shifted early scan, two scans of one ticket
3. batch 2 (South): an earlier scan takes the admission; equal times keep the first; repeated and reused scan_ids
4. batch 3 (East): later scans report the earliest admission; an online scan now is outside the window
5. batch 4: refunded, transferred-away and other-event tokens are refused for that reason, inside or outside the window
6. the fall-back event's window opens at the first 01:00 (07:00Z), not at local midnight
7. a later request with an equal scan time keeps the first admission; a scan_id belongs to one event only
8. the export and dashboard show each ticket's earliest admission and its gate

**W17** Waiting lists: join rules, offers in join order with skip-over, offer expiry and decline, reserved seats

1. a sold-out section offers a waiting list; a section with seats does not
2. entries join in order; a second entry and a section with seats are refused
3. a freed seat goes to the first entry it fits (skipping a larger one), is held, and the offer is emailed
4. only the entrant may accept; accepting creates a pending order for exactly the offered seats
5. an expired offer passes on; a seat no entry fits stays reserved (status waitlist) and cannot be held
6. two reserved seats go to the entry that needed two; declining empties the list and the seats return to sale
7. a two-seat offer lists its seats in seat order (seat 9 before seat 10)

**W18** Dashboard figures at request time: sold, held, available, admitted, gross, refunded, net, per section

1. an event with paid orders, 50 % and organizer refunds, a late payment refunded, and admissions
2. with a refund pending and a hold active, every figure matches for owner and box office; a scanner gets 403
3. once the refund completes, refunded and net move; nothing else does
4. the moment a hold expires its seats count as available, before any background work

**W19** Attendee export: header, CRLF, seat order (numeric), resold seats, quoting, spreadsheet safety, who may download

1. tickets whose names, addresses and gates need quoting or neutralizing; a refunded and resold seat
2. the export has the exact header, CRLF line ends, and every row as the rules say
3. box office, scanners and other organizations cannot download it

**W20** Reminders and email safety: one email per holder address, one-time key, header injection, names in To

1. holders: one with two tickets, one who also received a transfer under another name, one transferred away, one refunded
2. a reminder submitted twice at once and again later reaches each holder address exactly once, by name
3. typed text never adds headers or recipients; other roles and token-less posts send nothing

**W21** Old platform, Lantern Room Presents (Chicago): dashboards, exports, sale pages, holders

1. staff set passwords through reset links (addresses stored with odd case and spaces) and sign in
2. every one of the organization's 24 old events has the dashboard the records imply
3. every old event's attendee export lists every ticket as recorded (DST folds, transfers, refunds, resold seats)
4. upcoming old events are on sale at their recorded start, with their sold seats shown sold
5. an old ticket holder signs up and sees exactly their tickets, with barcodes and no refund
6. the event the old platform cancelled shows as cancelled

**W22** Old platform, Copperline Live (Denver): dashboards, exports, sale pages, holders

1. staff set passwords through reset links (addresses stored with odd case and spaces) and sign in
2. every one of the organization's 18 old events has the dashboard the records imply
3. every old event's attendee export lists every ticket as recorded (DST folds, transfers, refunds, resold seats)
4. upcoming old events are on sale at their recorded start, with their sold seats shown sold
5. an old ticket holder signs up and sees exactly their tickets, with barcodes and no refund

**W23** Old platform, Saguaro Sounds (Phoenix): dashboards, exports, sale pages, holders, old barcodes at the door

1. staff set passwords through reset links (addresses stored with odd case and spaces) and sign in
2. every one of the organization's 16 old events has the dashboard the records imply
3. every old event's attendee export lists every ticket as recorded (DST folds, transfers, refunds, resold seats)
4. upcoming old events are on sale at their recorded start, with their sold seats shown sold
5. an old ticket holder signs up and sees exactly their tickets, with barcodes and no refund
6. old barcodes admit at an upcoming event's door; a refunded one is refused

**W24** In a real browser: layout at 1280 and 390 px, typed text never runs as script, a click-through purchase

1. a plainly named event at Juniper Hall with two seats sold; its dashboard and manage page (experience evidence)
2. names, venues, sections, events, descriptions, holders and gates carry script payloads
3. signing in through the browser form reaches a signed-in page
4. a buyer clicks through: seats, hold, pay on the hosted page, back to a paid order
5. a refused hold (6 more seats over the buyer's 4) for the experience review
6. typed text and URL parts are shown as text on every page (no script runs)
7. every page stays within 1280 and 390 CSS pixels without horizontal scrolling

**W25** Deployment: README, restart persistence (accounts, orders, QR tokens), nothing re-sent, old records kept

1. README.md and an executable start.sh are in the workspace
2. after a restart: accounts, orders and QR tokens still work, and no email is sent again
3. the old platform's records are all still there, unchanged

**W26** Cross-cutting safety: secrets only hashed at rest, reset ends sessions, Host-proof links, headers, no stack traces

1. setting a new password through a reset link signs out every existing session
2. emailed links start with PUBLIC_BASE_URL whatever Host header the request carried
3. signed-in pages carry Cache-Control: no-store and every page an anti-framing header
4. malformed requests get a plain error page without stack traces, SQL, or file paths
5. no password, token, session id, device key or signing key is stored in plain text (database, workspace files)

**W27** Health follows the database: non-2xx within 10 s of a stop, 200 within 60 s of a start

1. /healthz answers 200 while the database is up
2. within 10 s of the database stopping, /healthz answers a non-2xx status within 5 s
3. within 60 s of the database starting again, /healthz answers 200 without a restart

### Breach detectors

Any detector that fires caps every workflow at ×0.4. Each row lists where the detector is raised and what it reports (`…` marks a value filled in at run time).

| code | raised in | fires when |
|---|---|---|
| X1 | W02, W05, W14, W17, W19, W20 | a manager changed a member's role / a manager of another organization read … / a manager refunded another organization's ticket through their own organization's URL / a manager revoked another organization's scanner device / a manager sent an invitation / a non-member reached … / a scanner refunded a ticket / an account read another buyer's order page / an account saw another holder's QR token / another account accepted an invitation meant for a different address / another account accepted someone else's waitlist offer / another account read a buyer's order page / another organization's owner downloaded … / box office created a promo code / box office sent a reminder / the former holder transferred a ticket they no longer hold / … cancelled an event / … created a scanner device / … downloaded the attendee export / … reached … |
| X2 | W02, W20 | a reminder was sent by a request without the session token / an invitation was sent by a request without the session token |
| X3 | W24 | typed text ran as script: … |
| X4 | W19 | export wrote … as a live spreadsheet formula |
| X5 | W02, W07 | an event appeared under another organization / an invitation made a second owner / extra fields changed the order total to … / extra fields moved an invitation into another organization or role |
| X6 | W07, W08, W09, W10, W11, W12, W13, W18, W25 | a code with 3 redemptions was applied to … orders / a fully refunded ticket was refunded again at cancellation / a late payment issued a ticket for a seat already sold to another buyer (seat sold twice) / a late payment took a seat that was … / a late payment was refunded … times / an offer of a cancelled event was accepted into an order / an order was marked paid by a webhook that did not verify / duplicate deliveries issued … tickets for 2 seats / one buyer held 8 seats of an event at once / ticket … was refunded … at the provider, more than its total … / ticket … was refunded … times after a '…' fault / ticket … was refunded … times at the provider / … buyers held the same seat / … emails were sent again after a restart: … |
| X7 | W14, W15 | a revoked device key admitted a ticket / a … token was admitted: … / an altered QR token was admitted: … |
| X8 | W26 | plain-text secret in the database: … / secrets stored in the application's files: … |
| X9 | W01 | a session cookie copied before sign-out still signs in / a session identifier held before sign-in became signed in |
| X10 | W13, W25 | an event with old-platform tickets was cancelled / pre-existing records changed: … |
| X11 | W01 | sign-in redirected to … for next=… |
| X12 | W20 | typed text added a recipient: … |
| X13 | W26 | … session(s) still signed in after the password was reset |
| X14 | W26 | an emailed link was built from the request's Host header: … |

### Judged experience criteria

`anthropic/claude-fable-5-1` answers each criterion yes or no from the evidence files named for it, following `tests/judge-reference.md`; a checkpoint that was never reached fails its criterion. The oracle run scored every criterion yes.

| id | requirement | weight | evidence (written by) | criterion |
|---|---|---|---|---|
| J01 | R22.1 | 1 | `event_page` (W24) | The event page makes clear which seats are available, held, sold and accessible, shows each section's price, and states the event's local start time with its time zone. |
| J02 | R22.2 | 1 | `order_pending` (W24) | The order page explains the price breakdown: face prices, discount, service fees, facility fees, tax and the total, each labelled. |
| J03 | R22.2 | 1 | `order_pending` (W24) | The order page tells the buyer how long the seats are held (an expiry time or a countdown) and what happens when the hold ends. |
| J04 | R22.3 | 1 | `ticket_page` (W24) | The ticket page states what a refund request would return now and shows the 100 % and 50 % refund deadlines in the venue's local time. |
| J05 | R22.3 | 1 | `ticket_page` (W24) | The ticket page explains that transferring the ticket gives the recipient a new QR code and that the current QR code stops working. |
| J06 | R22.4 | 1 | `cancel_before` (W24) | Before cancelling, the event's manage page states that every ticket will be refunded, every buyer and holder emailed, and that cancelling cannot be undone. |
| J07 | R22.4 | 1 | `hold_refused` (W24) | After a refused hold (the buyer's 6-ticket limit), the page explains why it was refused and what the buyer can do next. |
| J08 | R22.5 | 1 | `dashboard` (W24) | The dashboard is readable at a glance: capacity, sold, held, available, admitted, gross, refunded and net are labelled, for each section and in total. |
| J09 | R17.4 | 1 | `confirmation_email` (W05) | The order confirmation email names the event and, for each ticket, its seat, ticket id and QR token, in a form a fan can read. |
| J10 | R22.6 | 1 | `event_page/narrow.jpg` (W24); `order_pending/narrow.jpg` (W24); `ticket_page/narrow.jpg` (W24) | At 390 pixels wide, the key facts and the primary action on these pages stay readable and reachable. |

## Controls

| control | run | reward | functional | judge |
|---|---|---|---|---|
| oracle (`solution/solve.sh`) | `20261111T114744Z-r2qc-sw-oracle` | 1.0 | 1.0 | 1.0 |
| nop (empty workspace) | `20261111T114745Z-r2qc-sw-nop` | 0.0 | 0.0 | 0.0 |

## Interface contract literals

The routes of `instruction.md`'s interface contract and every `data-action` and `data-*` marker it names. The workflows drive and read the application through these routes and markers (as well as the form fields, exports and emails the instruction specifies), so a submission that renames one fails the checks that rely on it.

| path | methods |
|---|---|
| `/signup` | GET, POST |
| `/verify/<token>` | GET |
| `/password-reset` | GET, POST |
| `/password-reset/<token>` | GET, POST |
| `/signin` | GET, POST |
| `/signout` | POST |
| `/` | GET |
| `/events/<event>` | GET |
| `/events/<event>/hold` | POST |
| `/events/<event>/sections/<code>/waitlist` | POST |
| `/orders/<order>` | GET |
| `/orders/<order>/codes` | POST |
| `/orders/<order>/codes/<code>/remove` | POST |
| `/orders/<order>/pay` | POST |
| `/orders/<order>/release` | POST |
| `/tickets` | GET |
| `/tickets/<ticket>` | GET |
| `/tickets/<ticket>/refund` | POST |
| `/tickets/<ticket>/transfer` | POST |
| `/waitlist/offers/<token>` | GET |
| `/orgs/new` | GET, POST |
| `/orgs/<org>` | GET |
| `/orgs/<org>/members` | GET, POST |
| `/invites/<token>` | GET, POST |
| `/orgs/<org>/members/<user>/role` | POST |
| `/orgs/<org>/members/<user>/remove` | POST |
| `/orgs/<org>/venues/new` | GET, POST |
| `/orgs/<org>/venues/<venue>` | GET |
| `/orgs/<org>/venues/<venue>/sections` | POST |
| `/orgs/<org>/events/new` | GET, POST |
| `/orgs/<org>/events/<event>` | GET |
| `/orgs/<org>/events/<event>/publish` | POST |
| `/orgs/<org>/events/<event>/reschedule` | POST |
| `/orgs/<org>/events/<event>/cancel` | POST |
| `/orgs/<org>/events/<event>/codes` | GET, POST |
| `/orgs/<org>/events/<event>/orders` | GET |
| `/orgs/<org>/tickets/<ticket>/refund` | POST |
| `/orgs/<org>/events/<event>/dashboard` | GET |
| `/orgs/<org>/events/<event>/attendees.csv` | GET |
| `/orgs/<org>/events/<event>/door` | GET |
| `/orgs/<org>/tickets/<ticket>/checkin` | POST |
| `/orgs/<org>/events/<event>/devices` | GET, POST |
| `/orgs/<org>/events/<event>/devices/<device>/revoke` | POST |
| `/orgs/<org>/events/<event>/reminders` | GET, POST |
| `/webhooks/payments` | POST |
| `/api/scan` | POST |
| `/api/scan/batch` | POST |
| `/healthz` | GET |

| kind | values |
|---|---|
| actions (`data-action`) | accept-invite, accept-offer, add-section, apply-code, cancel-event, change-role, create-code, create-device, create-event, create-org, create-venue, decline-offer, door-checkin, hold-seats, invite-member, join-waitlist, organizer-refund, pay, publish-event, release-hold, remove-code, remove-member, request-refund, request-reset, reschedule-event, revoke-device, send-reminder, set-password, signin, signout, signup, transfer-ticket |
| markers | data-accessible, data-admitted, data-admitted-at, data-applied-code, data-available, data-capacity, data-csrf, data-dashboard-event-id, data-device-id, data-device-key, data-discount-cents, data-event-id, data-event-status, data-face-cents, data-facility-fee-cents, data-full-refund-until, data-gross-cents, data-half-refund-until, data-held, data-hold-expires-at, data-holder-email, data-member-email, data-member-user-id, data-my-role, data-my-waitlist, data-net-cents, data-offer-seats, data-offer-status, data-order-id, data-order-status, data-org-id, data-price-cents, data-qr-token, data-refund-percent, data-refunded-cents, data-reminder-key, data-revoked, data-role, data-sale-state, data-seat, data-seat-count, data-seat-status, data-section, data-service-fee-cents, data-sold, data-starts-at, data-subtotal-cents, data-tax-cents, data-ticket-id, data-ticket-status, data-total-cents, data-venue-id |
