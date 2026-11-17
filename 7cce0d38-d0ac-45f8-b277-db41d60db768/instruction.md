# Seatwise: reserved-seat ticketing for small venues

I run Seatwise, a ticketing service for independent venues. Promoters (we call them organizations)
set up their venues and seat maps, put shows on sale, and run the door with phone scanners; fans buy
reserved seats, pass tickets to friends, and ask for refunds. Three promoters already sell through
our old platform: Lantern Room Presents (Chicago), Copperline Live (Denver) and Saguaro Sounds
(Phoenix). Their venues, events, orders and tickets are in the database. You are building the new web
application from scratch in this workspace; it replaces the old platform.

The workspace is empty. The environment already runs three services: a PostgreSQL database with the
old platform's data, an SMTP server that accepts every email the application sends, and a card-payment
provider with a Stripe-compatible API (its home page, `GET PAYMENTS_API_URL/`, documents its surface
and test cards). Their details are in `/etc/seatwise/services.env`, one `NAME=value` per line:
`DATABASE_URL`, `PORT`, `PUBLIC_BASE_URL`, `SMTP_HOST`, `SMTP_PORT`, `MAIL_FROM`, `PAYMENTS_API_URL`,
`PAYMENTS_WEBHOOK_SIGNING_FILE` (a file holding the webhook signing secret), `HOLD_TTL_SECONDS`,
`WAITLIST_OFFER_TTL_SECONDS`, `ACCOUNT_LINK_TTL_SECONDS`, and `TICKET_SIGNING_KEY`. The application is
always started with these variables exported in its environment; read them at run time. Each deployment
starts the application with **different values** for the same variables and a fresh copy of the same
database, so nothing about hosts, ports, addresses, secrets or durations may be hard-coded.

Money is whole US cents everywhere (`4500` is $45.00). An **instant** is shown as RFC 3339 UTC to the
second, like `2027-06-13T01:00:00Z`. "Half up" means a value exactly halfway between two whole cents
goes to the larger one.

## People, organizations and roles (R1)

- R1.1 An **account** is a person who signs in with an email address and password. Anyone may create
  an account (R2.1). Email addresses are matched ignoring case and leading or trailing whitespace,
  everywhere: accounts, buyers, ticket holders, invitations, the old platform's records (some were
  stored with odd capitals and stray spaces).
- R1.2 An **organization** has members, each with exactly one role: `owner`, `manager`, `box_office`,
  or `scanner`. Each organization has exactly one owner: whoever created it (or, for the three existing
  ones, the member the database names as owner). Any signed-in account may create an organization and
  becomes its owner. Every signed-in account is also an **attendee**: it may buy, hold and transfer
  tickets for itself.
- R1.3 What members may do inside their own organization (✓ = allowed):

  | Action | owner | manager | box_office | scanner |
  |---|---|---|---|---|
  | invite members, change a member's role, remove a member (R3) | ✓ | | | |
  | venues and seat maps; create, publish and reschedule events; promo codes (R4, R5, R7) | ✓ | ✓ | | |
  | cancel an event (R10.6) | ✓ | | | |
  | create and revoke scanner devices (R13.1) | ✓ | ✓ | | |
  | event dashboard (R15) | ✓ | ✓ | ✓ | |
  | orders list and organizer refunds (R10.5) | ✓ | ✓ | ✓ | |
  | attendee export (R16) | ✓ | ✓ | | |
  | send reminders (R17.3) | ✓ | ✓ | | |
  | door list and door check-in (R13.5) | ✓ | ✓ | ✓ | ✓ |

  Membership in one organization gives nothing in another. A signed-in account that requests an
  organization page or action it is not allowed answers **403** and nothing changes; a visitor who is
  not signed in is sent to the sign-in page. An event, venue, order, ticket or device that belongs to
  another organization is treated the same way: 403 (or 404) and nothing changes.

## Accounts and sessions (R2)

- R2.1 `/signup` takes an email address, a full name and a password (at least 10 characters). It
  creates an unverified account and emails a verification link that starts with `PUBLIC_BASE_URL`.
  Following the link verifies the account; until then signing in is refused with a message saying to
  verify the address first. An address that already belongs to a verified account is refused with a
  message, and so is the address of a member of an existing organization (they set their password with
  R2.3 instead). Signing up again with an address whose account is not yet verified replaces the name
  and password and sends a new link; older verification links for it stop working.
- R2.2 Full names, here and everywhere a person types a name, are 1 to 100 characters with no control
  characters (nothing below U+0020); anything else is refused with a message and changes nothing.
- R2.3 `/password-reset` takes an email address. If it belongs to a verified account or to a member of
  an organization, one email goes to that address with a single-use link that starts with
  `PUBLIC_BASE_URL`; the link opens a page that sets a new password (at least 10 characters) and marks
  the account verified. Setting a new password through a reset link signs out every existing session of
  that account. Whether or not the address is known, the response has the same status code and the same
  message, and an unknown address gets no email.
- R2.4 Verification and reset links work once, expire `ACCOUNT_LINK_TTL_SECONDS` seconds after they
  were sent, and a reset link stops working as soon as a newer reset link is sent for the same
  account. A used, expired or replaced link changes nothing and says the link is no longer valid; an
  unknown link answers 404.
- R2.5 Sign-in takes an email address and password. A wrong password and an unknown address are refused
  with the same message and no session.
- R2.6 Signing in always starts a new session identifier; no identifier the browser held before
  signing in ever becomes signed in. Signing out ends the session on the server: a session cookie
  copied before sign-out no longer signs anyone in. Session cookies are `HttpOnly` and `SameSite=Lax`
  or `SameSite=Strict`.
- R2.7 A visitor sent to the sign-in page from a page that needs sign-in returns to that page after
  signing in. `/signin` accepts a `next` query parameter for this; after a successful sign-in the
  browser goes to `next` only when it is a path on this site (it starts with a single `/` that is not
  followed by another `/` or a `\`, and it has no scheme or host); otherwise it goes to `/tickets`. The
  application never redirects anyone to another site.
- R2.8 Passwords and scanner device keys are stored only as salted hashes; verification, reset and
  invitation tokens and session identifiers are stored only as hashes. None of them is ever stored in
  plain text.

## Organizations and members (R3)

- R3.1 `/orgs/new` creates an organization from a name (1 to 100 characters, no control characters).
- R3.2 The owner invites someone by email address with a role of `manager`, `box_office` or `scanner`
  (an invitation can never make anyone an owner; any other role value is refused). One email goes to
  that address with a single-use invitation link starting with `PUBLIC_BASE_URL`. The link works only
  for a signed-in account with the invited address (anyone else gets 403 and nothing changes); a
  visitor is sent to sign in and returns to the link. Accepting makes that account a member with the
  invited role. Inviting an address that is already a member is refused with a message.
- R3.3 The owner may change a member's role among `manager`, `box_office` and `scanner`, and may
  remove a member. The owner's own role and membership cannot be changed or removed.

## Venues and seat maps (R4)

- R4.1 A venue belongs to one organization and has a name, an address, and an IANA time zone such as
  `America/Chicago`; a time zone name the system does not know is refused.
- R4.2 A venue's seat map is a list of sections. A section has a code (1 to 8 characters of `A`–`Z` and
  `0`–`9`, unique within the venue), a name, a range of row letters (for example `A-F`: rows A, B, C,
  D, E, F; rows are single letters `A`–`Z`), a number of seats per row (1 to 60, numbered from 1), and a
  list of accessible seats written like `A1 A2 F14` (each must be a seat of the section). A seat's
  **label** is `<section code>-<row>-<seat number>`, for example `ORCH-C-12`.
- R4.3 **Seat order**: seats are ordered by section code (alphabetical), then row letter, then seat
  number (numerically, so `ORCH-C-9` comes before `ORCH-C-10`). Every list of seats or tickets in this
  page uses seat order unless it says otherwise.
- R4.4 A venue's seat map is fixed once any event has been created at it: adding a section is then
  refused, and the add-section form is no longer offered.

## Events and sales windows (R5)

- R5.1 An event belongs to one organization and one of its venues and has a name, a description, a
  start, a facility fee in cents (0 or more, charged per ticket), and a face price in cents for every
  section of the venue (100 or more each). It also has an **on-sale** instant, an optional **off-sale**
  instant (when empty, sales end at the start), and an optional **presale** instant with a presale code.
- R5.2 Every time an organizer types (start, on-sale, off-sale, presale) is a wall-clock time
  `YYYY-MM-DDTHH:MM` in the venue's time zone. A wall-clock time that occurs twice because clocks were
  set back means its first occurrence; a wall-clock time that does not exist because clocks were set
  forward is refused with a message. Pages show event times in the venue's local time; the markers
  carry the instant (R19).
- R5.3 Creating an event requires: the start in the future; presale (when given) before on-sale;
  on-sale before off-sale (or, when there is no off-sale, before the start); off-sale no later than the
  start; a presale code (4 to 20
  letters `A`–`Z` and digits, typed in either case and stored in capitals) exactly when a presale instant is
  given. Anything else is refused
  with a message and nothing is created. A new event is a **draft**: only members of its organization
  can see it, and to everyone else its page answers 404. **Publishing** makes it visible to everyone.
- R5.4 Sales windows, decided at the moment of each hold request (R6.1): before the presale instant
  (or before on-sale when there is no presale) nobody may hold seats; from the presale instant until
  on-sale a hold needs the presale code (matched ignoring case and surrounding whitespace); from
  on-sale until off-sale anyone may hold seats; from off-sale on nobody may. A refused hold says why.
- R5.5 **Rescheduling** changes a published event's start to a new future wall-clock time. When the
  off-sale instant (or the start, if there is none) would then be after the new start, sales end at
  the new start. Orders paid before a reschedule get the refund right in R10.3.

## Holds and orders (R6)

- R6.1 A signed-in attendee holds seats of a published event by choosing 1 to 6 seat labels on the
  event page. The seats must all be **available**: not in a valid or refund-pending ticket, not in an
  unexpired hold, not under an open waitlist offer, and not reserved for a waiting list (R14.2). An
  accessible seat can be held only when
  the attendee ticks that they need accessible seating. Otherwise the hold is refused with a message
  and nothing is held. When two hold requests for the same seat arrive at the same moment, at most one
  of them gets it.
- R6.2 A hold creates an **order** with status `pending` and holds its seats for `HOLD_TTL_SECONDS`
  seconds from the moment the hold was created. The order page shows the seats, the price breakdown
  (R9), and an expiry instant. When the hold expires unpaid, the order becomes `expired` and its seats
  are available again at that same moment, whether or not any background work has run. The attendee
  may also release the hold (order `released`, seats available at once).
- R6.3 **Per-buyer limit**: a buyer may have at most 6 tickets for one event, counting every
  non-refunded ticket of orders they bought (the old platform's orders included, matched by address)
  plus every seat in their pending orders with unexpired holds. Tickets transferred to or from them do
  not change this count. A hold that would exceed 6 is refused with a message; two hold requests from
  the same buyer arriving at the same moment cannot together exceed it.
- R6.4 Order statuses: `pending`, `expired`, `released`, `paid`, and `unavailable` (R8.4).

## Promo codes (R7)

- R7.1 Owners and managers create promo codes for one event: a code (4 to 20 letters `A`–`Z` and digits,
  which may be typed in either case; it is stored and shown in capitals and is unique within the event), a kind `percent` (value 1 to
  100) or `fixed` (value in cents, 1 or more), a minimum face subtotal in cents (0 or more), and an
  optional maximum number of redemptions (1 or more; empty means unlimited).
- R7.2 On a pending order whose hold has not expired and whose Pay button has not been pressed, the
  buyer may apply codes (typed in any case, surrounding whitespace ignored) and remove applied codes.
  An order may carry at most one percent code and at most one fixed code. A code is refused, with a
  message and no change, when it is not a code of this event, is already applied, is of a kind the
  order already carries, when the order's face subtotal is below its minimum, or when it has no
  redemption left.
- R7.3 A code's redemptions in use are the paid orders carrying it plus the pending orders carrying it
  whose hold has not expired. When codes are applied to different orders at the same moment, no more
  orders end up carrying a code than its maximum allows.

## Payment (R8)

- R8.1 At start-up the application registers `PUBLIC_BASE_URL/webhooks/payments` as a webhook endpoint
  with the provider. Pay on a pending order with an unexpired hold creates one Checkout Session at the
  provider (`mode=payment`, currency `usd`) whose `amount_total` is the order total (R9), with
  `metadata[order_id]` set to the order id and `success_url` and `cancel_url` pointing back to the order
  page, and answers with a redirect (303) to the session's `url`. Pressing Pay again for the same order
  redirects to the same session. After Pay, the order's codes can no longer change.
- R8.2 Only a webhook delivery whose `Stripe-Signature` verifies under the signing secret, with a
  timestamp no more than 300 seconds from the current time, may change anything. Any other request to
  the webhook path is answered 400 and changes nothing. A browser returning to the order page, or
  anything else, never marks an order paid.
- R8.3 A verified `checkout.session.completed` event for the order's session marks a pending order
  `paid` and issues one ticket per seat (status `valid`, held by the buyer). The same event delivered
  again, or other event types arriving before or after it in any order, change nothing more and send
  nothing more. A declined card changes nothing: the order stays pending until its hold ends, and the
  buyer may pay again with another card.
- R8.4 **Late payments.** When an order's hold expires (or it is released), the application expires its
  Checkout Session at the provider within 30 seconds. If a completed payment for an `expired` or
  `released` order arrives anyway, the order is paid as normal when every one of its seats is still
  available and the event is not cancelled. Otherwise the whole payment is refunded through the provider
  (one refund of the full amount), the order becomes `unavailable`, and the buyer gets one email saying
  the seats were no longer available and the payment was refunded.
- R8.5 When a payment is confirmed the buyer receives exactly one confirmation email listing every
  ticket: its seat label, its ticket id, and its QR token (R12.1).

## Prices (R9)

- R9.1 Each ticket of an order has a **face price**: the event's price for the seat's section. The
  order's **face subtotal** is the sum of its face prices.
- R9.2 **Discounts.** A percent code takes `round_half_up(face subtotal × value / 100)`. A fixed code
  then takes its value, but never more than what is left of the face subtotal after the percent
  discount. The order's discount is the sum of the two.
- R9.3 **Allocation** (largest remainder). An order-level amount *A* is spread over the order's tickets
  in proportion to weights *w*: each ticket first gets the whole-cent part of *A × w / Σw*; the cents
  still missing are then given one each to the tickets with the largest remainders of that division,
  and between equal remainders to the ticket that comes first in seat order (R4.3).
- R9.4 Per ticket: the **discount share** is the order discount allocated by face price; the **service
  fee** is `round_half_up(7.5 % × face price)`; the **facility fee** is the event's facility fee.
- R9.5 **Tax** is 8.875 % of the order's taxable amount (face subtotal − discount + service fees),
  rounded half up, once per order. It is allocated to tickets (R9.3) in proportion to each ticket's
  taxable amount (face − discount share + service fee).
- R9.6 A ticket's **total** is face − discount share + service fee + facility fee + tax share. The
  order total is the sum of its ticket totals.

Worked examples (facility fee as stated, codes as stated):

1. `ORCH-C-11` at 4000 and `ORCH-C-12` at 2600, facility fee 200, no codes. Service fees 300 and 195;
   taxable 7095; tax 629.68 → 630, allocated 381.82… and 248.18… → 381 + 248 with one cent left, which
   goes to the larger remainder: 382 and 248. Ticket totals 4882 and 3243; order total 8125.
2. `BALC-A-3` and `BALC-A-4` at 3000 and `ORCH-B-7` at 5000, facility fee 150, percent code SPRING20
   (20 %). Subtotal 11000, discount 2200 allocated 600, 600, 1000. Service fees 225, 225, 375. Taxable
   9625, tax 854.21… → 854, allocated 232.9…, 232.9…, 388.2… → floors 232, 232, 388 and the two
   missing cents go to the first two: 233, 233, 388. Totals 3008, 3008, 4913; order total 10929.
3. The same seats with SPRING20 and a fixed code TAKE4 (400). Percent discount 2200, fixed 400,
   discount 2600 allocated 709.09…, 709.09…, 1181.81… → 709, 709, 1182. Taxable 9225, tax 818.71… →
   819, allocated 223, 223, 372 plus one cent to `BALC-A-3` (equal remainders: first in seat order):
   224, 223, 372. Totals 2890, 2889, 4715; order total 10494.
4. One ticket at 2000, facility fee 100, fixed code of 2500: the fixed discount is 2000. Service fee
   150, taxable 150, tax 13.31… → 13; total 263.

## Refunds and cancellation (R10)

- R10.1 A ticket's **refunded amount** is the sum of its completed refunds. Every refund other than the
  late-payment refund of R8.4 is one provider refund for one ticket, sent with `payment_intent` set to
  the order's payment, `amount`, `metadata[ticket_id]`, and an `Idempotency-Key` header unique to that
  refund. When the provider does not answer a refund request with success (an error, a timeout, or no
  answer), the application sends the same request again with the same `Idempotency-Key`, within 10
  seconds of each failed attempt, until it succeeds; the provider then performs each refund once.
- R10.2 **Buyer refunds.** The buyer of a ticket may request a refund while they still hold it (it was
  never transferred away), it is `valid`, not yet admitted (R13), and the event has not started. With
  *S* the event's start and *t* the moment of the request, to the second: if *t* is at or before
  *S* − 168 hours the refund is 100 % of the ticket total; otherwise if *t* is at or before *S* − 48
  hours it is 50 % of the ticket total, rounded half up; otherwise no refund is possible. Hours are
  elapsed time. The ticket page shows the two deadlines and what a request now would return.
- R10.3 **Rescheduled events.** When an event's start changes after an order was paid, that order's
  tickets get 100 % at any moment before the new start (the 168/48-hour tiers do not apply to them).
- R10.4 Requesting a refund makes the ticket `refund_pending` at once: it no longer admits anyone and
  cannot be transferred. The refund completes only when the provider's verified `refund.created` event
  for that refund arrives: the ticket becomes `refunded`, its seat becomes available, and the buyer gets
  exactly one email stating the amount refunded in dollars (like `$12.34`) and the seat. Duplicate or
  out-of-order provider events change nothing more and send nothing more.
- R10.5 **Organizer refunds.** Owners, managers and box office refund any `valid` ticket of their
  organization's events (admitted or not, transferred or not) for its full total, at any time, to the
  original payment. It completes like R10.4.
- R10.6 **Cancelling an event** (owner only; published events whose tickets were all sold through this
  application) marks it `cancelled`: holds end at once (pending orders become `expired`), waitlist
  entries close, nothing more can be sold, and every ticket of the event is refunded the difference
  between its total and its completed and pending refunds (tickets with nothing left get no refund).
  Each distinct address that bought or holds a ticket of the event receives exactly one cancellation
  email, and no separate refund emails are sent for these refunds. An event that has tickets from the
  old platform cannot be cancelled here (refused with a message).
- R10.7 Tickets sold by the old platform cannot be refunded in this application (there is no payment
  to refund): the refund actions are not offered for them and are refused.

Worked examples (with example 3's order; `S` is the event start):

5. Requested at `S − 200 h`: `ORCH-B-7` refund 4715 (100 %). Requested at `S − 100 h`: `ORCH-B-7`
   refund 2358 (50 % of 4715 is 2357.5, half up), `BALC-A-3` refund 1445.
6. An event at a Chicago venue starting `2027-06-12T20:00` local starts at `2027-06-13T01:00:00Z`. Its
   100 % deadline is `2027-06-06T01:00:00Z` and its 50 % deadline `2027-06-11T01:00:00Z`.

## Transfers and QR tokens (R11, R12)

- R11.1 The current holder of a `valid`, not yet admitted ticket may transfer it, before the event
  starts and unless the event is cancelled, to another address with a name. The recipient becomes the
  holder; the ticket gets a new QR token and every earlier token of the ticket stops admitting (it is
  answered `transferred`, R13.3). The recipient receives one email with the ticket (seat label, ticket
  id, new token, and a link to the ticket page); the previous holder receives one email saying the
  ticket was transferred. Transferring to oneself is refused. Old-platform tickets may be transferred
  the same way.
- R11.2 A signed-in account sees, at `/tickets`, every ticket whose current holder is its address
  (the old platform's tickets included, R18), and each ticket's page with its QR token. The buyer of a
  ticket that was transferred away may still see its page, without a token.
- R12.1 A ticket's QR token is a string the application signs with `TICKET_SIGNING_KEY` (for example an
  HMAC) so that the application can check it was issued for this ticket and is still the ticket's
  current token. Changing any character makes it invalid. An old-platform ticket's token is its
  `barcode` until it is first transferred here.

## Check-in (R13)

- R13.1 Owners and managers create **scanner devices** for one event, each with a label. The page shown
  right after creation displays the device's secret key once; afterwards it is never shown again. A
  device can be revoked. The scan API takes the key as `Authorization: Bearer <key>`; a revoked or
  unknown key is answered 401 and changes nothing.
- R13.2 **Online scan**: `POST /api/scan` with JSON `{"token": ..., "gate": ...}` (gate: 1 to 40
  characters, no control characters). The scan time is the moment the request arrives, to the second.
  **Offline sync**: `POST /api/scan/batch` with JSON `{"scans": [ ... ]}` of at most 1000 rows, each
  `{"scan_id": ..., "token": ..., "gate": ..., "scanned_at": ...}` (`scan_id` a string of 1 to 64
  characters, `scanned_at` RFC 3339 with `Z` or an explicit offset). A body that is not valid JSON of
  that shape is answered 400 and changes nothing.
- R13.3 A scan is **refused**, with the first reason that applies in this order: `invalid` (not a
  token this application issued, or altered), `wrong_event` (a ticket of another event than the
  device's), `cancelled` (the event is cancelled), `refunded` (the ticket is `refund_pending` or
  `refunded`), `transferred` (an earlier token of a ticket that was transferred since),
  `outside_window` (the scan time is before *S* − 3 hours or at or after *S* + 6 hours, *S* the event's
  start). Every other scan admits the ticket.
- R13.4 **One admission per ticket, earliest wins.** A ticket's admission is its accepted scan with the
  earliest scan time, from any device or gate of the event, whatever order the scans arrive in; when
  scan times are equal, an admission already recorded stays, and within one batch the earlier row
  wins. Before anything in R13.3, a batch row whose `scan_id` the event already received (in an earlier
  request or an earlier row, whatever its result was) is a `duplicate` and changes nothing when all its
  fields are the same as then; with any different field it is refused with reason `scan_id_reused`. Responses are `200` with one result per row in the same order (for `/api/scan`, a
  single result), each `{"scan_id": ..., "result": "admitted" | "already_admitted" | "refused" |
  "duplicate", "reason": ..., "ticket_id": ..., "admitted_at": ..., "gate": ...}`: `admitted` for the
  row that is the ticket's admission once the whole request is processed, `already_admitted` for any
  other accepted scan of that ticket, with `admitted_at` (an instant) and `gate` describing the
  ticket's admission at the end of the request. Refused rows carry `reason`; `ticket_id` is `null` for
  `invalid`.
- R13.5 The **door list** of an event lists every ticket with its seat, holder name, status and
  admission. A door check-in button appears for each `valid` ticket that is not yet admitted; pressing
  it scans the ticket at that moment with gate `Door`, following R13.3 and R13.4.

Worked example (event starting `2027-05-01T02:00:00Z`): gate North scans ticket 7 at
`2027-05-01T01:30:00Z` online; later a batch from gate South carries ticket 7 scanned at
`2027-05-01T01:10:00Z`. The batch row is `admitted` and the ticket's admission becomes South at 01:10.
A further batch from gate East with ticket 7 at `2027-05-01T01:20:00Z` is `already_admitted`, with
`admitted_at` `2027-05-01T01:10:00Z` and gate `South`.

## Waiting lists (R14)

- R14.1 When a section of a published event on sale has no available seat, a signed-in attendee may
  join its waiting list for 1 to 4 seats (no more than their per-buyer limit allows at that moment). The
  join form is offered only for sections with no available seat; joining a section with an available
  seat, or a second time while one's entry is waiting or has an open offer, is refused.
- R14.2 While a section has waiting entries, each of its non-accessible seats that is not sold, held
  or under an offer is **reserved** for the waiting list: the event page shows it as `waitlist` and
  holds for it are refused. Whenever a section has reserved seats, the application makes offers in join
  order: the earliest waiting entry whose quantity is at most the number of reserved seats gets an offer
  for that many seats, the first ones in seat order; entries asking for more seats keep their place;
  this repeats while an entry fits. Offers are made within 30 seconds of the seats becoming free. When a
  section has no waiting entries left, its reserved seats are on public sale again. Accessible seats are
  never reserved or offered.
- R14.3 An offer holds its seats for `WAITLIST_OFFER_TTL_SECONDS` seconds and is emailed (one email) to
  the entrant with a link to the offer page. The entrant, signed in, may accept it while it is open:
  this creates a pending order for exactly those seats with a new hold (R6.2; the per-buyer limit
  applies). When an offer expires or is declined, its entry leaves the list and its seats go to the next
  fitting entry.

## Dashboard (R15)

- R15.1 An event's dashboard shows, for the whole event and for each section, figures computed at the
  moment of the request: **capacity** (seats); **sold** (tickets that are `valid` or `refund_pending`,
  old-platform ones included); **held** (seats in unexpired holds and open offers); **available**
  (capacity − sold − held); **admitted** (tickets with an admission); **gross** (every payment received
  for the event's orders, whatever happened later: each paid order's total and each `unavailable`
  order's payment); **refunded** (completed refunds, including late-payment refunds of R8.4); and
  **net** (gross − refunded). Per section, an order's payment and a late-payment refund count as the
  totals of the order's seats in that section (R9.6).
- R15.2 Old-platform tickets count with the amounts the old platform recorded (R18.3): their totals in
  gross, their `refunded_cents` in refunded.

## Attendee export (R16)

- R16.1 `GET /orgs/<org>/events/<event>/attendees.csv` returns every ticket of the event (all statuses,
  old-platform ones included) as CSV: this exact header row, then one row per ticket in seat order (R4.3)
  and, for the same seat, by ticket id; every row ends in CRLF:

  `ticket_id,order_id,section,row,seat,holder_name,holder_email,buyer_name,buyer_email,status,total_cents,refunded_cents,purchased_at,checked_in_at,gate`

  `status` is `valid`, `refund_pending` or `refunded`; addresses are written trimmed and in lower case;
  `purchased_at` is the instant the order was paid (for the old platform, when it was placed);
  `checked_in_at` and `gate` describe the admission, empty when there is none. A field that contains a
  comma, a double quote, CR or LF is enclosed in double quotes with inner quotes doubled.
- R16.2 Spreadsheet safety: in `holder_name`, `holder_email`, `buyer_name`, `buyer_email` and `gate`, a
  value that starts with `=`, `+`, `-`, `@`, a tab or CR is written with a single quote `'` in front of
  it. Numeric columns are plain integers.

## Email (R17)

- R17.1 Every email is sent from `MAIL_FROM` and addressed with the recipient's name and address in
  its `To` header (for example `To: Maria Lopez <maria.lopez@example.com>`). Nothing a person typed ever
  adds a header or a recipient.
- R17.2 Each email of R2, R3, R8, R10, R11, R14 and R17.3 is sent within 30 seconds of its cause and
  never again, whatever is retried or restarted.
- R17.3 Owners and managers send a **reminder** for an event: a subject (1 to 150 characters, no control
  characters) and a body. One email goes to each distinct address that currently holds a `valid` ticket
  of the event, with exactly that subject and the body, addressed with the holder name of that
  address's ticket that comes first in seat order. The reminder form carries a one-time key (R19);
  submitting the same form again, at the same moment or later, sends nothing more.

- R17.4 Each kind of email has a subject containing these words (any capitalization), and a plain-text
  body:

  | Email | Subject contains | Body contains |
  |---|---|---|
  | verification (R2.1) | `Verify` | the link |
  | password reset (R2.3) | `Reset` | the link |
  | invitation (R3.2) | `Invitation` | the link and the organization's name |
  | order confirmation (R8.5) | `Your tickets` | the event name; for each ticket its seat label, ticket id and QR token |
  | late payment refunded (R8.4) | `Seats no longer available` | the amount refunded in dollars |
  | refund completed (R10.4, R10.5) | `Refund` | the amount in dollars and the seat label |
  | event cancelled (R10.6) | `Cancelled` | the event name |
  | ticket received (R11.1) | `You have a ticket` | seat label, ticket id, the new QR token, and the ticket page link |
  | ticket transferred away (R11.1) | `Ticket transferred` | the seat label |
  | waitlist offer (R14.3) | `Seats available` | the offer link |
  | reminder (R17.3) | the typed subject (exactly) | the typed body |

  Dollar amounts are written like `$1,234.56`.

## The old platform's data (R18)

- R18.1 The database already contains `organizations`, `users`, `memberships`, `venues`, `sections`,
  `seats`, `events`, `event_prices`, `legacy_orders`, `legacy_tickets` and `legacy_transfers`. Inspect
  them with `psql` using the database URL. Serve that data as it is: never drop, truncate, rename or
  empty those tables or their columns, and never change or delete their existing rows (except that a
  membership changes when an owner changes a member's role or removes a member, R3.3). You may add rows,
  tables, columns, indexes and constraints; the application applies its own schema additions
  idempotently every time it starts. Ticket ids and order ids are unique across old and new records.
- R18.2 `users` and `memberships` are the existing organizations' members; they have no password yet
  (R2.3). Existing `events` rows are published (or `cancelled`), on sale from `on_sale_local` until
  their start, at the prices in `event_prices` and with their `facility_fee_cents`.
- R18.3 Old-platform timestamps (`starts_local`, `on_sale_local`, `placed_local`, `refunded_local`,
  `checked_in_local`, `transferred_local`) are wall-clock times at the event's venue; when a wall-clock
  time happened twice because clocks were set back, the matching `*_fold` column is 0 for the first
  occurrence and 1 for the second. `legacy_tickets` records each ticket's amounts as the old platform
  charged them (its fee and tax rules differed from R9; never recompute them), its current holder, its
  status (`valid`; `comp`, a complimentary ticket that behaves exactly like `valid`; or `refunded`, with
  `refunded_cents` paid back), its `barcode`, and its admission (`checked_in_local` and
  `checked_in_gate`, or none). `legacy_orders` records the buyer and when the order was placed;
  `legacy_transfers` is the history of tickets passed between addresses.

## Deployment (R20)

- R20.1 The workspace root contains an executable `start.sh` that starts the application listening on
  `0.0.0.0` at the port in `PORT`, in the foreground, until it is stopped.
- R20.2 `GET /healthz` answers 200 only while the application can reach the database, and a non-2xx
  status within 10 seconds of the database stopping, each answer arriving within 5 seconds. Once the
  database is back, `/healthz` answers 200 again within 60 seconds without a restart. The application
  answers `/healthz` with 200 within 60 seconds of `start.sh` starting.
- R20.3 Deployment copies the workspace to a clean machine with no network access, as regular files
  (symbolic links that point outside the workspace are not preserved). Every runtime dependency must
  already be inside the workspace (for example a virtual environment made with
  `python -m venv --copies`, packages installed into a folder in the workspace, or `node_modules` in the
  workspace), and `start.sh` must not download, install or build anything.
- R20.4 Use only the three services in the services file; the application must not contact any other
  host.
- R20.5 Add a `README.md` at the workspace root naming the stack, how `start.sh` starts the app, and the
  environment variables it reads.
- R20.6 Everything the application stores must survive the application being stopped and started
  again; QR tokens issued before a restart keep working after it.

## Safety (R21)

- R21.1 Every request that changes anything from a signed-in browser page must carry a token tied to
  that session that a page on another site cannot obtain; a request without the right token is refused
  with 403 and changes nothing. The scan API uses its bearer key and the webhook its signature instead.
- R21.2 Text that people typed (names, event and venue names, section names, descriptions, gate names,
  reminder text) and anything taken from a request's path or query is always shown as text, on every
  page (error and "not found" pages included) and in every attribute; it never becomes markup or script.
- R21.3 Prices, fees, amounts, roles, organizations, holders and whose data a request touches always
  come from the application's own records and rules; extra or altered form fields never change them.
- R21.4 Passwords, verification, reset and invitation tokens, session identifiers and scanner device keys
  are stored only as hashes, and `TICKET_SIGNING_KEY` is never stored; none of them is ever stored in plain
  text (R2.8).
- R21.5 Every link the application emails starts with `PUBLIC_BASE_URL`.
- R21.6 Every response to a signed-in user carries `Cache-Control: no-store`, and no page may be shown
  inside a frame on another site (`X-Frame-Options: DENY`, or a `Content-Security-Policy` with
  `frame-ancestors 'none'`).
- R21.7 An unexpected error or a malformed request is answered with a plain error page; it never shows a
  stack trace, database error text, or file paths.

## Experience (R22)

Judged from what a person sees in a browser; colours, fonts, layout and wording are yours.

- R22.1 The event page makes clear which seats are available, held, sold and accessible, with each
  section's price, and the event's local start time with its time zone.
- R22.2 The order page explains the price breakdown (face prices, discount, service fees, facility fees,
  tax, total) and shows how long the hold lasts.
- R22.3 A ticket page states what a refund request would return now and the two refund deadlines in the
  venue's local time, and what transferring does to the old QR code.
- R22.4 Before cancelling an event, the page states that every ticket will be refunded, every buyer and
  holder emailed, and that this cannot be undone; a refused action explains what to do next.
- R22.5 The dashboard is readable at a glance (figures labelled, per section and in total).
- R22.6 Every page stays usable 1280 and 390 CSS pixels wide, with no horizontal scrolling.

## Interface contract (R19)

The application is driven through a real browser and through these paths, form fields and
markers. Pages may carry any other content and styling. `<org>`, `<event>`, `<order>`, `<ticket>`,
`<venue>`, `<device>`, `<user>` are ids; `<code>` is a section code or promo code.

| Path | Method | Purpose |
|---|---|---|
| `/signup` | GET, POST | fields `email`, `full_name`, `password`; button `data-action="signup"` (R2.1) |
| `/verify/<token>` | GET | the verification link (R2.1) |
| `/password-reset` | GET, POST | field `email`; button `data-action="request-reset"` |
| `/password-reset/<token>` | GET, POST | field `password`; button `data-action="set-password"` |
| `/signin` | GET, POST | fields `email`, `password`; button `data-action="signin"` |
| `/signout` | POST | button `data-action="signout"` |
| `/` | GET | published events |
| `/events/<event>` | GET | the event page (seat map); a hold form posting to `/events/<event>/hold` |
| `/events/<event>/hold` | POST | fields `seats` (one value per seat label), `presale_code`, `accessible` (`1` when ticked); button `data-action="hold-seats"`; success redirects to the order page |
| `/events/<event>/sections/<code>/waitlist` | POST | field `quantity`; button `data-action="join-waitlist"` |
| `/orders/<order>` | GET | the order page (buyer only) |
| `/orders/<order>/codes` | POST | field `code`; button `data-action="apply-code"` |
| `/orders/<order>/codes/<code>/remove` | POST | button `data-action="remove-code"` |
| `/orders/<order>/pay` | POST | button `data-action="pay"`; redirects (303) to the provider's session `url` |
| `/orders/<order>/release` | POST | button `data-action="release-hold"` |
| `/tickets` | GET | the signed-in account's tickets |
| `/tickets/<ticket>` | GET | a ticket page |
| `/tickets/<ticket>/refund` | POST | button `data-action="request-refund"` (R10.2) |
| `/tickets/<ticket>/transfer` | POST | fields `to_email`, `to_name`; button `data-action="transfer-ticket"` |
| `/waitlist/offers/<token>` | GET | an offer page; forms posting to `/waitlist/offers/<token>/accept` (button `data-action="accept-offer"`, redirects to the new order page) and `/waitlist/offers/<token>/decline` (button `data-action="decline-offer"`) |
| `/orgs/new` | GET, POST | field `name`; button `data-action="create-org"`; redirects to `/orgs/<org>` |
| `/orgs/<org>` | GET | organization home |
| `/orgs/<org>/members` | GET, POST | fields `email`, `role`; button `data-action="invite-member"` |
| `/invites/<token>` | GET, POST | button `data-action="accept-invite"` |
| `/orgs/<org>/members/<user>/role` | POST | field `role`; button `data-action="change-role"` |
| `/orgs/<org>/members/<user>/remove` | POST | button `data-action="remove-member"` |
| `/orgs/<org>/venues/new` | GET, POST | fields `name`, `address`, `timezone`; button `data-action="create-venue"`; success shows the venue page |
| `/orgs/<org>/venues/<venue>` | GET | the venue and its seat map |
| `/orgs/<org>/venues/<venue>/sections` | POST | fields `code`, `name`, `rows` (like `A-F`), `seats_per_row`, `accessible`; button `data-action="add-section"` |
| `/orgs/<org>/events/new` | GET, POST | fields `venue_id`, `name`, `description`, `starts_at`, `on_sale_at`, `off_sale_at`, `presale_at`, `presale_code`, `facility_fee_cents`, and `price_<code>` per section; button `data-action="create-event"`; success shows the event's manage page |
| `/orgs/<org>/events/<event>` | GET | the manage page |
| `/orgs/<org>/events/<event>/publish` | POST | button `data-action="publish-event"` |
| `/orgs/<org>/events/<event>/reschedule` | POST | field `starts_at`; button `data-action="reschedule-event"` |
| `/orgs/<org>/events/<event>/cancel` | POST | button `data-action="cancel-event"` |
| `/orgs/<org>/events/<event>/codes` | GET, POST | fields `code`, `kind`, `value`, `min_subtotal_cents`, `max_redemptions`; button `data-action="create-code"` |
| `/orgs/<org>/events/<event>/orders` | GET | orders and tickets, with organizer refund forms |
| `/orgs/<org>/tickets/<ticket>/refund` | POST | button `data-action="organizer-refund"` (R10.5) |
| `/orgs/<org>/events/<event>/dashboard` | GET | R15 |
| `/orgs/<org>/events/<event>/attendees.csv` | GET | R16 |
| `/orgs/<org>/events/<event>/door` | GET | door list |
| `/orgs/<org>/tickets/<ticket>/checkin` | POST | button `data-action="door-checkin"` |
| `/orgs/<org>/events/<event>/devices` | GET, POST | field `label`; button `data-action="create-device"` |
| `/orgs/<org>/events/<event>/devices/<device>/revoke` | POST | button `data-action="revoke-device"` |
| `/orgs/<org>/events/<event>/reminders` | GET, POST | fields `subject`, `body`; button `data-action="send-reminder"` |
| `/webhooks/payments` | POST | provider webhooks (R8.2) |
| `/api/scan`, `/api/scan/batch` | POST | R13.2 |
| `/healthz` | GET | R20.2 |

Every form that changes something includes, inside the form, its session token as a hidden `<input>`
with the attribute `data-csrf`; whatever its `name`, it is submitted unchanged with the form. Every
other hidden input of the form is also submitted unchanged.

Markers, as HTML attributes:

- When signed in, the sign-out button has `data-action="signout"`.
- Organization home: `data-org-id` and `data-my-role`. Members page: each member inside an element with
  `data-member-user-id`, `data-member-email` (the address, trimmed and in lower case) and `data-role`; for the owner, the change-role and remove forms of each member
  other than the owner.
- Venue page: `data-venue-id`; each section inside an element with `data-section="<code>"` and
  `data-seat-count`; the add-section form only while the seat map can change (R4.4).
- Event manage page: `data-event-id`, `data-event-status="<draft|published|cancelled>"`, `data-starts-at`
  (instant); the publish button only for drafts; the reschedule form only for published events that have
  not started; the cancel button only for the owner, on a published event that can be cancelled (R10.6).
- Event page: `data-event-id`, `data-starts-at`, `data-sale-state="<not_on_sale|presale|on_sale|ended|cancelled>"`;
  each section inside an element with `data-section`, `data-price-cents` and `data-available` (the
  number of its seats whose status is `available`);
  each seat an element with `data-seat="<label>"`, `data-seat-status="<available|held|sold|waitlist>"` and
  `data-accessible="<true|false>"`; the join-waitlist form only inside sections with `data-available="0"`.
  For a signed-in account that has a waiting entry or an open offer in a section, that section's element
  also carries `data-my-waitlist="<quantity>"`.
  The hold form contains, for each seat whose status is `available`, a checkbox
  `<input type="checkbox" name="seats" value="<label>">`, plus a checkbox `accessible` (value `1`) and a
  text input `presale_code`.
- Order page: `data-order-id`, `data-order-status`, `data-hold-expires-at` (instant), `data-subtotal-cents`,
  `data-discount-cents`, `data-service-fee-cents`, `data-facility-fee-cents`, `data-tax-cents`,
  `data-total-cents`; each ticket line inside an element with `data-seat`, `data-face-cents`,
  `data-discount-cents`, `data-service-fee-cents`, `data-facility-fee-cents`, `data-tax-cents`,
  `data-total-cents`, and (once paid) `data-ticket-id`; each applied code an element with
  `data-applied-code="<CODE>"`. The pay and release buttons only while the order is pending with an
  unexpired hold; the apply-code form only while codes can change (R7.2).
- `/tickets`: each ticket inside an element with `data-ticket-id`, `data-event-id`, `data-seat` and
  `data-ticket-status`.
- Ticket page: `data-ticket-id`, `data-seat`, `data-ticket-status`, `data-holder-email` (trimmed, lower
  case), `data-admitted-at` (instant, or empty); for the holder an element with `data-qr-token`; for
  tickets sold here, `data-full-refund-until` and `data-half-refund-until` (instants; for R10.3 both are
  the new start) and `data-refund-percent="<100|50|0>"` (what a request now would return); the
  request-refund button only when R10.2 allows a refund now; the transfer form only when R11.1 allows a
  transfer now.
- Offer page: `data-offer-status="<open|accepted|expired|declined>"` and `data-offer-seats` (labels,
  space-separated, seat order); the accept and decline buttons only while open.
- Orders list: each ticket inside an element with `data-ticket-id` and `data-ticket-status`; the
  organizer-refund button only for `valid` tickets sold here.
- Dashboard: an element with `data-dashboard-event-id` and `data-capacity`, `data-sold`, `data-held`,
  `data-available`, `data-admitted`, `data-gross-cents`, `data-refunded-cents`, `data-net-cents`; each
  section inside an element with `data-section` carrying the same figure attributes.
- Door list: each ticket inside an element with `data-ticket-id`, `data-ticket-status` and
  `data-admitted-at`; the door-checkin button only as R13.5 says.
- Devices page: after creation `data-device-id="<id>"` and an element with `data-device-key="<key>"`;
  each device inside an element with `data-device-id` and, for revoked ones, `data-revoked="true"`.
- Reminder form: a hidden input with `data-reminder-key` (the one-time key of R17.3).
