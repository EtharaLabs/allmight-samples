# Pickpath: orders, warehouse and fulfilment for a specialty-food distributor

We are Larder & Line Provisions, a specialty-food distributor. Restaurants, delis and grocers order
olive oil, cheese, cured meat, tinned fish and frozen pastry from us, and we ship from one warehouse
in Secaucus, New Jersey. Our business customers order in a customer portal (or through an API) and pay
by card. The warehouse allocates perishable stock by expiry, picks it along the shortest route, ships it,
and handles shortages, returns and refunds. An old system did this until now. Its records are in the
database, and it got allocation and refunds wrong often enough that we are replacing it. You are
building the new web application from scratch in this workspace.

The workspace is empty. The environment already runs three services:

- a PostgreSQL database with our bins, products, prices, tax rates, customers, users, stock lots and the
  old system's stock movements and orders;
- an SMTP server that accepts every email the application sends;
- a card-payment provider with a Stripe-compatible API (Checkout Sessions with a hosted payment page,
  Refunds, Webhook Endpoints, signed webhooks). `GET` on the provider's base URL documents its surface
  and its test cards.

Their connection details are in `/etc/pickpath/services.env`, one `NAME=value` per line:

| Variable | Meaning |
|---|---|
| `DATABASE_URL` | PostgreSQL connection URL |
| `PORT` | the port the application listens on |
| `PUBLIC_BASE_URL` | the base URL people and the payment provider use to reach the application |
| `SMTP_HOST`, `SMTP_PORT` | the SMTP server (no authentication, no TLS) |
| `MAIL_FROM` | the `From` address of every email |
| `PAYMENTS_API_URL` | the payment provider's base URL |
| `PAYMENTS_WEBHOOK_SIGNING_FILE` | a file holding the webhook signing key (`whsec_...`) |
| `RESET_LINK_TTL_SECONDS` | how long a password link stays valid |

The application is always started with these variables exported in its environment; read them from
the environment at run time. Each deployment starts the application with **different values** for the same
variables and a fresh copy of the same database, so nothing about hosts, ports, addresses, keys, or
durations may be hard-coded.

Money is whole US cents unless this page says otherwise. **Half-up** rounding to the cent means a
fraction of exactly one half rounds up. Amounts shown to people in dollars are
formatted like `$1,234.56`. The warehouse's time zone is `sites.timezone` (`America/New_York`).
**Today** always means the current calendar date in that time zone, and every date in this document
(ship dates, expiry dates, effective dates) is a calendar date in that zone.

## People and roles (R1)

- R1.1 Everyone who uses the application is a row in `users`. `role` is `buyer` or `approver` (both
  belong to the customer company in `customer_id`), `picker` (works one warehouse zone, `zone`),
  `supervisor` (runs the warehouse) or `finance`. Only rows with `active = true` may sign in; when a user
  is deactivated, their sessions and API keys stop working at once.
- R1.2 What each role may see and do:
  - a **buyer** sees their own company's orders, shipments and invoices, returns (RMAs) and credit notes,
    and their company's order export; creates, pays and cancels their company's orders, cancels
    backordered units, requests returns, and manages their own API keys;
  - an **approver** can do everything a buyer can, and approves their company's orders (R4.4);
  - a **picker** sees the waves of their own zone and confirms the picks in them (R7);
  - a **supervisor** manages products (SKUs), receipts, lots and cycle counts, creates waves in every
    zone and confirms any pick, ships orders, receives and inspects returns, sees every order, shipment
    and return, and downloads the on-hand report and the ledger;
  - **finance** manages customers, users and prices, sees every order, shipment, invoice, refund, return
    and credit note, and downloads every report and export.
  Everyone signed in may change their own display name (R2.8) and sign out.
- R1.3 A signed-in user who requests a page or action their role does not allow gets a 403 response and
  nothing changes. A buyer or approver who asks for another company's order, shipment, invoice, return
  or credit note (by any path, including the API) gets a 404 or 403 response that shows none of that
  record's data, and nothing changes. A picker who asks for a wave or a pick of another zone gets 403.
  A visitor who is not signed in is sent to the sign-in page.

## Signing in (R2)

- R2.1 Users in the database have no password yet. Anyone sets or resets a password from
  `/password-reset` by entering their email address. If the address belongs to an active user, one email
  goes to that address with a single-use link that starts with `PUBLIC_BASE_URL`; the link opens a page
  that sets a new password (at least 10 characters). Whether or not the address is on file, the response
  has the same status code and shows the same message, and an address that is not on file (or belongs
  to an inactive user) receives no email.
- R2.2 A link works once, expires `RESET_LINK_TTL_SECONDS` seconds after it was requested, and stops
  working as soon as a newer link is requested for the same user. A used, expired, or replaced link
  changes nothing and says the link is no longer valid; an unknown link answers 404.
- R2.3 Email addresses are matched case-insensitively and ignoring leading and trailing whitespace,
  everywhere.
- R2.4 Sign-in takes an email address and password. A wrong password, an unknown address, or an inactive
  user is refused with the same message and no session.
- R2.5 Signing in always starts a new session identifier; no identifier the browser held before signing
  in ever becomes signed in. Signing out ends the session on the server: a session cookie copied before
  sign-out no longer signs anyone in. Session cookies are `HttpOnly` and `SameSite=Lax` or
  `SameSite=Strict`.
- R2.6 Passwords and API keys (R4.6) are stored only as salted hashes; password-reset tokens and session
  identifiers are stored only as hashes. None of them is ever stored in plain text.
- R2.7 A visitor sent to the sign-in page from a page that needs sign-in returns to that page after
  signing in. `/signin` accepts a `next` query parameter for this; after a successful sign-in the browser
  goes to `next` only when it is a path on this site (it starts with a single `/` that is not followed by
  another `/` or a `\`, and it contains no `\`, no scheme and no host); otherwise it goes to `/`. The
  application never redirects anyone to another site.
- R2.8 Anyone signed in may change their own display name (`full_name`) from `/profile`. That form
  changes nothing else, whatever other fields the request carries. A display name, here and wherever
  finance types one, is 1 to 100 characters with no control characters (no CR, LF, tab, or other
  character below U+0020); anything else is refused with a message and changes nothing.

## Warehouse, products, prices and customers (R3)

- R3.1 **Bins.** `bins` holds every storage location. Zone `A` (ambient) is aisles 1–10, zone `C`
  (chilled) aisles 11–14, zone `F` (frozen) aisles 15–16; each aisle has bays 1–20 and levels 1–4. A bin
  code is the zone letter, the two-digit aisle, the two-digit bay and the level, as in `A03-07-2`. The
  bin `QA-01` (zone `Q`, no aisle, bay or level) is the **quarantine** bin.
- R3.2 **SKUs.** A supervisor adds a product with: `code` (after trimming and upper-casing, 1–24
  characters of `A`–`Z`, `0`–`9` and `-`, starting with a letter or digit; unique, compared that way),
  `name` (1–120 characters, no control characters), `case_pack` (units per case, 1–500),
  `unit_weight_g` (1–100000), `length_cm`, `width_cm`, `height_cm` (the case's outside dimensions,
  1–300 each), `storage` (`ambient`, `chilled` or `frozen`), `taxable` (`true` or `false`), and
  `reorder_point` (0–1000000). It becomes a new active row in `skus` with the code stored upper-cased. A
  storage type belongs to one zone: ambient to `A`, chilled to `C`, frozen to `F`.
- R3.3 **Receipts.** A supervisor receives stock with: the SKU `code`, a `lot_code` (1–32 characters,
  no control characters), an `expiry_date`, a `bin` code, `units` (1–1000000) and `unit_cost` (US
  dollars per unit, greater than zero, at most 4 decimal places, such as `2.1250`). The bin must exist
  and be in the zone of the SKU's storage type. A lot is identified by its SKU and its lot code, compared
  case-insensitively and ignoring surrounding whitespace. If the SKU has no such lot, a new row in `lots`
  is created with that code, expiry date, bin and unit cost (`qa_hold = false`). If it has one, the
  receipt must name the same expiry date, bin and unit cost; otherwise it is refused with 409 and
  nothing changes. Every accepted receipt records a `receipt` movement of `+units` for the lot (R9.1)
  and then fills backorders (R6.4). Anything invalid is refused with a message and changes nothing.
- R3.4 **Prices.** Finance adds a price with a `tier` (`standard`, `gold` or `platinum`), the SKU
  `code`, `unit_price_cents` (1–10000000) and `effective_from`. It becomes a new row in `price_list`.
  Prices are never edited or removed; a price for the same tier, SKU and effective date as an existing
  one is refused.
- R3.5 **Customers.** Finance adds a customer company with: `name` (1–120 characters, no control
  characters), `tier`, `ship_to_state` (a state in `tax_rates`), `approval_limit_cents` (empty for no
  limit, else 0–100000000), `min_shelf_life_days` (0–365), `allow_split_cases` and `backorders_allowed`
  (`true` or `false`). It becomes a new row in `customers`. Finance may change any of these settings
  later on the customer's page; a change applies to what happens afterwards (an order's prices and
  whether it needs approval are fixed when it is created; allocation uses the settings at the moment it
  runs).
- R3.6 **Users.** Finance adds a user with `email` (unique, compared as in R2.3), `full_name`, `role`,
  `customer_id` (required for buyers and approvers, empty otherwise) and `zone` (`A`, `C` or `F`,
  required for pickers, empty otherwise). It becomes a new active row in `users`. Finance can deactivate
  an active user; a user who is already inactive cannot be deactivated again. An email already on file, or any invalid field, is refused with a message and changes nothing.

## Orders, prices, tax and freight (R4, R5)

- R4.1 A buyer or approver creates an order for their own company with: a requested `ship_date` (from
  today to today + 365 days, inclusive), a `po_number` (0–40 characters, no control characters),
  `notes` (0–500 characters, no control characters except line breaks) and 1 to 12 lines, each a SKU
  `code` (matched as in R3.2) and a `qty` in units (a whole number from 1 to 100000), each SKU at most
  once. The order is refused with 422 and nothing is created when any field is invalid, a SKU is
  unknown or inactive, the company's tier has no price for a SKU on the ship date, or the company does
  not allow split cases and a quantity is not a whole number of cases (a multiple of `case_pack`).
- R4.2 An accepted order gets the number `PP-` followed by its id zero-padded to 6 digits (`PP-000042`),
  its prices, taxes and freight are fixed by R5, and it never changes afterwards except as this page
  describes.
- R4.3 The order **needs approval** when its company's `approval_limit_cents` is not empty and the
  order's total is greater than it. Such an order starts as `awaiting_approval`, and each active
  approver of the company other than the order's creator receives one email whose subject contains
  `Approval needed` and the order number. Any other order starts as `awaiting_payment`.
- R4.4 An approver of the same company who is not the order's creator approves an `awaiting_approval`
  order; it becomes `awaiting_payment`. Approving an order in any other state is refused with 409.
  Nobody approves an order they created.
- R4.5 A buyer or approver of the company cancels an order that is `awaiting_approval` or
  `awaiting_payment`; it becomes `cancelled`, and if it has an open Checkout Session the application
  expires it at the provider. A paid order cannot be cancelled (only its backordered units, R6.5).
- R4.6 **API keys.** A buyer or approver creates API keys at `/account/api-keys` with a label. The page
  shown right after creation displays the key once; afterwards the key is never shown again. A key that
  is not revoked can be revoked; a revoked key cannot be revoked again. A request with `Authorization: Bearer <key>` acts as the key's user. An unknown or revoked
  key, or one whose user is inactive, is answered 401.
- R4.7 `POST /api/v1/orders` with a JSON body
  `{"ship_date": "YYYY-MM-DD", "po_number": "...", "notes": "...", "lines": [{"sku": "...", "qty": 12}]}`
  creates an order exactly as R4.1–R4.3 describe (`po_number` and `notes` may be omitted). It answers
  201 with the order JSON (R4.9). A refused order answers 422 with `{"errors": ["...", ...]}` (at least one
  message) and creates nothing; a body that is not a JSON object answers 400.
- R4.8 `GET /api/v1/orders/<id>` answers 200 with the order JSON for an order of the key's company and
  404 for any other id.
- R4.9 The **order JSON** has exactly these keys: `id`, `number`, `status`, `customer_id`,
  `ship_date`, `po_number`, `subtotal_cents`, `tax_cents`, `freight_cents`, `freight_tax_cents`,
  `total_cents`, `lines`, `refunds`, `shipments`, `credit_notes`. `status` is `awaiting_approval`,
  `awaiting_payment`, `cancelled`, `in_fulfilment` (paid, with units still to ship) or `complete`
  (every unit shipped or cancelled, at least one shipped). A paid order whose units are all cancelled is
  `cancelled`. Each line, in `line_no` order, has `line_no`, `sku`, `qty`, `unit_price_cents`,
  `amount_cents`, `tax_cents`, `reserved` (allocated units not yet shipped, picked or not), `shipped`,
  `backordered`, `cancelled`, `returned` (units on its received returns, R8.6) and `allocations`: a list
  of `{"lot_id", "units"}` giving the reserved units per lot, by `lot_id` ascending, without zero entries.
  For a paid order `qty = reserved + shipped + backordered + cancelled` on every line. `refunds` lists
  `{"id", "amount_cents", "status"}` (`id` is the application's id for the refund, the value it sends as
  `metadata[refund_id]`; `status` is `pending` or `refunded`), `shipments` lists `{"id",
  "tracking_number", "invoice_number", "invoice_total_cents"}` and `credit_notes` lists `{"id",
  "rma_id", "amount_cents"}`, each oldest first.

Prices, tax and freight (follow them exactly; every cent matters):

- R5.1 **Unit price.** A line's unit price is the `price_list` row for the company's tier and that SKU
  whose `effective_from` is the latest one on or before the order's ship date.
- R5.2 **Tax.** A line's amount is `qty × unit price`. Its tax is the amount times the ship-to state's
  `tax_rates.rate`, rounded half-up, when the SKU is taxable, else 0. The order's subtotal and tax are
  the sums over its lines.
- R5.3 **Freight.** For each line: `cases = ceil(qty / case_pack)`, the actual weight is
  `qty × unit_weight_g` grams, and the dimensional weight is `cases × ceil(length × width × height / 5)`
  grams; the line's billable weight is the larger of the two. The order is billed for
  `ceil(total billable grams / 500)` half-kilograms. Freight is `1200 + 90 × half-kilograms`, or 0 when
  the subtotal is 250000 or more; a cold-chain surcharge of 650 is added when any line's SKU is chilled
  or frozen (also when freight is otherwise free). Freight tax is freight times the rate, rounded
  half-up, when the state's `freight_taxable` is true, else 0.
- R5.4 The order's total is subtotal + tax + freight + freight tax.

Worked examples (NY: rate 0.08875, freight taxable; PA: rate 0.06, freight not taxable):

1. NY. `OIL-750` (not taxable, case pack 6, 1050 g, case 30×20×32 cm) 12 units at 1450, and `TIN-SAR`
   (taxable, case pack 24, 130 g, case 30×20×12 cm) 30 units at 389. Amounts 17400 and 11670; tax
   0 + 1036 (1035.7125); subtotal 29070. Billable weights: oil max(12600, 2 × 3840) = 12600 g, tins
   max(3900, 2 × 1440) = 3900 g; 16500 g is 33 half-kilograms, so freight is 1200 + 2970 = 4170 and
   freight tax 370. Total 29070 + 1036 + 4170 + 370 = **34646**.
2. PA. `BUR-125` (chilled, not taxable, case pack 12, 60 g, case 25×19×9 cm) 600 units at 417: subtotal
   250200, so freight is free but the cold-chain surcharge applies: total 250200 + 650 = **250850**.
3. PA. The same with 588 units: subtotal 245196; 49 cases of 855 g dimensional weight (41895 g) beat
   35280 g actual; 84 half-kilograms; freight 1200 + 7560 + 650 = 9410; total **254606**.
4. Gold prices for `OIL-750` are 1500 from 2026-01-01 and 1450 from 2026-12-01: a gold customer's order
   shipping 2026-11-20 pays 1500 a unit, one shipping 2026-12-15 pays 1450.

## Payment and refunds (R5, continued)

- R5.5 **Checkout.** A buyer or approver of the company pays an `awaiting_payment` order with
  `POST /orders/<id>/pay`. The application creates a Checkout Session at the provider with
  `mode=payment`, one line item whose name contains the order number, `unit_amount` equal to the
  order's total, `quantity` 1 and currency `usd`, `customer_email` set to the paying user's address
  (trimmed, lower-case), `metadata[order_id]` set to the order's id, `success_url`
  `PUBLIC_BASE_URL/orders/<id>?checkout=success` and `cancel_url`
  `PUBLIC_BASE_URL/orders/<id>?checkout=cancelled`, and answers with a 303 redirect to the session's
  `url`. Paying again while that session is still open redirects to the same session and creates no new
  one. Paying an order in any other state is refused with 409 and creates no session.
- R5.6 **Webhooks.** When it starts, the application registers `PUBLIC_BASE_URL/webhooks/payments`
  with `POST /v1/webhook_endpoints` (registering the same URL again is harmless). Every delivery carries
  a `Stripe-Signature` header `t=<unix seconds>,v1=<hex>`, where `v1` is the HMAC-SHA256, under the key
  in `PAYMENTS_WEBHOOK_SIGNING_FILE`, of `<t>.<raw request body>`. A delivery with a missing or malformed
  header, no matching `v1`, or a `t` more than 300 seconds away from the current time is answered 400
  and changes nothing. A verified event of a type the application does not use is answered 2xx.
- R5.7 **Only a signed webhook confirms payment.** A verified `checkout.session.completed` event whose
  session is the order's open Checkout Session, with `payment_status` `paid` and `amount_total` equal
  to the order's total, makes the order paid: it records the event's `payment_intent`, the order
  becomes `in_fulfilment`, allocation runs (R6.3), and the order's creator receives one email whose
  subject contains `Order confirmed` and the order number and whose body states the total. Returning
  to `success_url` changes nothing by itself. Every event acts at most once: a repeated delivery of an
  event that already acted, a `payment_intent.succeeded` event, or any retry changes
  nothing more, and allocation runs once per order.
- R5.8 **Late payments.** A verified `checkout.session.completed` for an order that is cancelled (or
  already paid through another session) does not change the order: the application refunds that
  session's `amount_total` in full (R5.9).
- R5.9 **Refunds go through the provider.** A refund calls `POST /v1/refunds` with the order's
  `payment_intent`, the `amount`, `metadata[order_id]`, `metadata[refund_id]` (the application's own id
  for that refund) and an `Idempotency-Key` header that is the same on every attempt of that refund and
  different for every other refund. If the call fails or gets no answer, the application keeps retrying
  it with the same key until the provider answers (the first retry within 30 seconds), across restarts.
  One refund of the application never becomes more than one refund at the provider. A refund is
  `pending` until a verified `refund.created` event for it arrives (matched by the provider's refund id
  or by `metadata[refund_id]`); it is then `refunded`, and the order's creator receives one email whose
  subject contains `Refund issued` and the order number and whose body states the amount. A
  `refund.created` that does not verify changes nothing.
- R5.10 **Refund amounts.** An order's **due** amount is, over its lines, the sum of `kept × unit price`
  plus the tax on that (rounded half-up, R5.2), where `kept = qty − cancelled`, plus the order's freight
  and freight tax; when every unit of the order is cancelled, the due amount is 0. Each event that
  cancels units (R6.3, R6.5, R7.6, R7.7) refunds once the due amount before it minus the due amount
  after it; an event that cancels units on several lines makes a single refund.

Worked example: in example 1, cancelling 18 of the 30 tins leaves 12 (4668, tax 414): the refund is
7002 + 1036 − 414 = **7624**, and the due amount becomes 27022.

## Allocation, backorders and cancellations (R6)

- R6.1 A lot's **on hand** is the sum of its movements (R9.1). Its **reserved** units are those allocated
  to orders and not yet shipped; **available** = on hand − reserved, which never goes below zero. A lot
  is **allocatable** when its bin is not `QA-01`, `qa_hold` is false, and it is not on count hold (R7.6).
- R6.2 **FEFO.** To allocate `n` units of an order line, take the allocatable lots of the line's SKU
  whose expiry date is at least `min_shelf_life_days` days after the order's ship date
  (`expiry_date − ship_date ≥ min_shelf_life_days`), in order of expiry date, then lot id. From each lot
  in turn take as many of the remaining units as it has available, except that for a company that does
  not allow split cases, a lot gives only whole cases (`floor(available / case_pack) × case_pack` at
  most). Stop when `n` units are allocated or the lots run out. Units of a line allocated from a lot
  are reserved from it at once.
- R6.3 When an order is paid, its lines are allocated in `line_no` order. Units that cannot be allocated
  become **backordered** when the company's `backorders_allowed` is true; otherwise they are
  **cancelled** and refunded (R5.10, one refund for the order).
- R6.4 **Filling backorders.** After every receipt (R3.3), restock (R8.8) and cycle count (R7.7) of a
  lot of a SKU, the backordered units of that SKU are allocated by R6.2, each with its own order's ship
  date and its company's current settings, taking lines in order of order id, then `line_no`, until no
  more can be allocated.
- R6.5 A buyer or approver of the company cancels some or all backordered units of a line with
  `POST /orders/<id>/lines/<line_no>/cancel-backorder` and a field `units` (1 to the units backordered);
  they become cancelled and are refunded (R5.10). Anything else is refused and changes nothing.
- R6.6 Reserving never exceeds what a lot has on hand. When two payments compete for the last units,
  exactly one order gets them; when two identical requests (such as the same cancellation submitted
  twice at once) race, one acts and the other changes nothing.

Worked examples (ship date 2026-12-01, `min_shelf_life_days` 14, case pack 12, 60 units ordered; lot
501 expires 2026-12-08 with 40 available, lot 502 2026-12-20 with 30, lot 503 2027-01-10 with 100):

1. Lot 501 has 7 days of shelf life left on the ship date and is not eligible. Whole cases only: 502
   gives 24 and 503 gives 36.
2. The same company with split cases allowed: 502 gives 30 and 503 gives 30.
3. With only lots 501 and 502 and backorders allowed, whole cases give 24 and 36 units are backordered;
   when a receipt later adds an eligible lot, those 36 units are allocated from it.

## Waves, pick paths, picking and cycle counts (R7)

- R7.1 A **pick task** is the units of one order line reserved from one lot that have not been picked.
  When more units of a line are allocated from a lot for which that line already has a task that is not
  picked and not in a wave, they are added to that task; otherwise they form a new task.
- R7.2 **Waves.** A supervisor creates a wave with a `zone` (`A`, `C` or `F`), a `ship_date` and
  `max_tasks` (1–12). The wave takes the tasks that are not picked and not in any wave, whose lot's bin is
  in that zone and whose order's ship date is that date, ordered by order id, then `line_no`, then lot
  expiry date, then lot id, and keeps the first `max_tasks` of them. When there is none, the wave is
  refused with 409.
- R7.3 **The pick path.** The stops of a wave are its distinct bins. A picker starts at the depot (aisle
  0, bay 0), visits every stop once, and returns to the depot. The distance between bays `b1` in aisle
  `a1` and `b2` in aisle `a2` is `|b1 − b2|` in the same aisle; between different aisles it is
  `4 × |a1 − a2| + min(b1 + b2, 42 − b1 − b2)` (aisles are crossed only at the front, bay 0, or the
  back, bay 21; levels do not matter). The depot counts as aisle 0, bay 0. The wave's route is the
  visiting order with the smallest total distance; among equally short routes, the one whose list of bin
  codes in visiting order is smallest in dictionary order (compare the first codes, then the second,
  and so on). The wave page lists the stops in route order and states the route's total distance.
- R7.4 **Confirming a pick.** A picker of the bin's zone or a supervisor confirms a task of a wave with
  `bin_code`, `lot_code` and `units`. A bin code or lot code that does not match the task's (compared
  case-insensitively, ignoring surrounding whitespace) is refused with 409, a task already confirmed is
  refused with 409, and `units` must be a whole number from 0 to the task's units (and a whole number of
  cases when the company does not allow split cases), otherwise 422; a refused confirmation changes
  nothing. Confirming all of the task's units marks it picked (the units stay reserved until shipped).
- R7.5 **Short picks.** Confirming fewer units than the task's marks the task picked with that many
  units. The lot goes on **count hold** (not allocatable until its next cycle count), and the missing
  units of the line are allocated anew by R6.2; units that cannot be allocated are backordered or
  cancelled and refunded as R6.3 says. The lot's on-hand does not change until it is counted.
- R7.6 A lot is on count hold from a short pick on it until its next cycle count.
- R7.7 **Cycle counts.** A lot's **version** is the number of movements recorded for it (old system and
  new). A supervisor counts a lot with `counted_units` (the units physically in the bin, a whole number
  from 0) and the `version` they saw. When the version is not the lot's current version, the count is
  refused with 409 and nothing changes. Otherwise: units picked from the lot but not yet shipped are not
  in the bin, so the new on-hand is `counted_units` plus those units; a `count` movement of (new on-hand
  − old on-hand) units is recorded, even when that is 0; the count hold ends. When the lot's reserved
  units that are not yet picked exceed `counted_units`, the excess is released from the lot's unpicked
  tasks, the task of the highest order id (then the highest `line_no`) first; released units leave their
  task (a task left with 0 units disappears from its wave) and are allocated anew by R6.2, and units that
  cannot be allocated are backordered or cancelled and refunded (R6.3). Then backorders fill (R6.4).
  When two counts with the same version arrive at the same moment, exactly one succeeds.

Pick path worked examples:

1. Stops `A03-07-2`, `A05-15-1`, `A03-18-4`, `A05-04-3`: the route is `A03-07-2`, `A03-18-4`,
   `A05-15-1`, `A05-04-3`, of 19 + 11 + 17 + 11 + 24 = **82** (from bay 18 of aisle 3 to bay 15 of
   aisle 5 is 8 + min(33, 9) = 17).
2. Stops `A01-05-1`, `A01-12-3`, `A02-08-2`: the route is `A01-05-1`, `A01-12-3`, `A02-08-2`, of
   9 + 7 + 24 + 16 = **56**.

## Shipping, invoices, returns and credits (R8)

- R8.1 A supervisor ships an order with `POST /orders/<id>/ship` when it has picked units not yet shipped
  and no task of it is still unpicked (otherwise 409 and nothing changes). The shipment contains every
  picked unit of the order not yet shipped; each lot records a `ship` movement of minus its units, and
  those units stop being reserved. The shipment gets a tracking number made of `PP` and 10 digits,
  unique to it. Two simultaneous ship requests for one order make one shipment.
- R8.2 Each shipment has an **invoice** with a unique invoice number and one line per order line in the
  shipment: its units, the unit price, the amount (units × price), and the tax. A line's tax is the tax
  (R5.2) on everything of that order line shipped so far, including this shipment, minus the tax on what
  of that line was shipped before this shipment. The order's freight and freight tax are invoiced on its
  first shipment only. The invoice total is the amounts plus taxes plus any freight and freight tax.
- R8.3 When an order ships, its creator receives one email whose subject contains `Shipped` and the order
  number and whose body states the tracking number, the invoice number and the invoice total.
- R8.4 **Returns.** A buyer or approver of the company requests a return (an RMA) for a line of an
  order with `line_no`, `units` (1 to the line's shipped units minus the units of its earlier RMAs) and a
  `reason` (1–500 characters). It starts as `requested`.
- R8.5 A supervisor **receives** a requested RMA: it becomes `quarantined`; its units sit in `QA-01`
  and are part of no lot's on-hand. Receiving an RMA that is not `requested`, or inspecting one that is not
  `quarantined` (R8.7), is refused with 409 and changes nothing.
- R8.6 Receiving issues a **credit note** for the RMA, and the RMA's requester receives one email whose
  subject contains `Credit note` and the order number and whose body states the credit amount. The
  units count as `returned` on the line. The credit is the value (units × price plus the tax on it,
  R5.2) of the line's shipped-and-not-returned units before the return, minus the same value after it.
- R8.7 A supervisor **inspects** a quarantined RMA with the outcome `restock` or `damaged`; it becomes
  `closed`. The RMA's lot is the lot with the earliest expiry date (then the lowest lot id) among the lots
  that order line shipped from. With `restock`, when that lot's expiry date is at least 7 days after
  today, a `restock` movement of `+units` returns the units to the lot (in its bin) and backorders fill
  (R6.4); the disposition is `restocked`. With `damaged`, or with `restock` for a lot that expires sooner,
  the units are written off and nothing is added to any lot; the disposition is `written_off`. For
  example, inspected on 2026-11-10, a lot expiring 2027-02-01 is restocked and one expiring 2026-11-12 is
  written off.

Worked examples (NY, example 1's tins at 389, taxable): shipping 12 tins and later 18 tins invoices tax
414 and then 1036 − 414 = **622**. Returning 5 of the 30 shipped tins credits 1945 + 1036 − 863 =
**2118** (25 tins carry tax 863).

## Inventory ledger, low stock, reports and exports (R9)

- R9.1 Every change of a lot's stock is a **movement** with a kind, a signed number of units and an
  instant: `receipt` (+), `ship` (−), `count` (±), `restock` (+), and, in the old system's records,
  `adjust` (±) and `writeoff` (−). A lot's on-hand is the sum of its movements, so on-hand = receipts −
  shipments ± adjustments, and it is never negative.
- R9.2 **Low stock.** A SKU's available stock is the sum, over its allocatable lots whose expiry date is
  today or later, of on hand − reserved. Whenever a receipt, allocation, pick, count, shipment or restock
  changes a SKU's stock or reservations and its available stock is then below its `reorder_point`, every
  active supervisor receives an email whose subject contains `Low stock` and the SKU code and whose body
  states the available units and the reorder point, unless that SKU already triggered one today. So each
  supervisor receives at most one per SKU per day.
- R9.3 `GET /reports/on-hand.csv` (supervisors and finance) has the header
  `lot_id,sku,lot_code,bin,expiry_date,on_hand,reserved,available,status` and one row per lot with
  on-hand above 0, by `lot_id` ascending. `sku` is the SKU code, `lot_code` and `bin` are as stored,
  `expiry_date` is `YYYY-MM-DD`. `status` is `quarantine` when the bin is `QA-01`, else `hold` when
  `qa_hold` is true or the lot is on count hold, else `expired` when the expiry date is before today,
  else `available`; `available` is on hand − reserved for an `available` lot and 0 otherwise.
- R9.4 `GET /reports/ledger.csv?sku=<code>` (supervisors and finance) has the header
  `lot_id,kind,units,at` and one row per movement of the SKU's lots, the old system's and the new ones,
  ordered by `at`; `at` is the movement's instant in UTC as `YYYY-MM-DDTHH:MM:SSZ`.
- R9.5 `GET /reports/valuation.csv?as_of=<instant>` (finance) takes an RFC 3339 instant with `Z` or an
  explicit offset (anything else is answered 400) and has the header `sku,on_hand,fifo_value_cents` and
  one row per SKU whose on-hand at that instant is above 0, by SKU code ascending. The on-hand at an
  instant counts every movement of the SKU's lots at or before it. The value is **FIFO**: the units on
  hand are the most recently received ones. Walk the SKU's positive movements at or before the instant
  (receipts, restocks, and positive counts and adjustments) from the newest to the oldest (later
  instants first; between movements at the same instant, the one recorded later first), taking each
  movement's units, the last one partially, until the on-hand units are covered. Each unit is worth its
  lot's `unit_cost`. `fifo_value_cents` is the total in cents, rounded half-up.
- R9.6 `GET /orders/export.csv` has the header
  `number,customer_id,customer_name,placed_at,status,po_number,ship_date,total_cents` and one row per
  order, the old system's included, ordered by `placed_at` and then `number`. Buyers and approvers get
  their own company's orders; finance gets every order. `placed_at` is when the order was created (for
  the old system's orders, `placed_local` read as R10.2 says) in UTC as `YYYY-MM-DDTHH:MM:SSZ`.
- R9.7 Every CSV row ends in CRLF. A field that contains a comma, a double quote, CR or LF is enclosed in
  double quotes with inner quotes doubled. **Spreadsheet safety:** a text value (codes, names, lot codes,
  PO numbers) that starts with `=`, `+`, `-`, `@`, a tab or CR is written with a single quote `'` in
  front of it. Numbers are written as plain integers.

FIFO worked example: a SKU's lot X (cost 2.1250) receives 40 units on 2026-05-04 and lot Y (cost 2.3000)
30 units on 2026-05-06; 45 units ship on 2026-05-08. Valued after that, 25 units are on hand and all
come from Y, the newer receipt: 25 × 2.3000 = **5750** cents. Valued on 2026-05-05, only X's 40 units
exist: **8500**.

## Existing data (R10)

- R10.1 The database already contains the tables `sites`, `bins`, `skus`, `tax_rates`, `price_list`,
  `customers`, `users`, `lots`, `legacy_movements`, `legacy_orders` and `legacy_order_lines`. Inspect
  them with `psql` using the database URL. Serve that data as it is: never drop, truncate, rename, or
  empty those tables or their columns, and never change or delete their existing rows, except a user's
  `full_name` (R2.8) and `active` (R3.6) and a customer's settings (R3.5). Adding rows as R3 describes is
  expected. You may add tables, columns, indexes, and constraints, and the application must apply its
  own schema additions idempotently every time it starts.
- R10.2 `legacy_movements.local_time` and `legacy_orders.placed_local` are wall-clock times in the
  warehouse's time zone with no offset. For a wall-clock time that happened twice because clocks were
  set back, `fold = 0` means the first occurrence and `fold = 1` the second. For a wall-clock time that
  never happened because clocks were set forward, `fold = 0` means it is read with the UTC offset in
  effect before the change and `fold = 1` with the offset after it. At any other time `fold` has no effect.
- R10.3 A legacy movement's units are signed as R9.1 says. The old system had no open allocations: every
  lot starts with nothing reserved and no count hold.
- R10.4 Creating the on-hand report or the valuation report over the data the database starts with
  answers within 60 seconds.

## Safety (R11)

- R11.1 Every request that changes anything from a signed-in browser page must carry a token tied to that
  session that a page on another site cannot obtain; a request without the right token is refused with
  403 and changes nothing. The order API uses its bearer key instead, and the webhook its signature.
- R11.2 Text that people typed or that came from the old system (names, SKU names, lot codes, PO
  numbers, notes, reasons, labels) and anything taken from a request's path or query is always shown as
  text, on every page (error and "not found" pages included) and in every attribute; it never becomes
  markup or script. Nothing typed ever adds a header or a recipient to an email.
- R11.3 Prices, amounts, quantities allocated, roles, companies, zones, limits and whose data a request
  touches always come from the application's own records and rules; extra or altered form fields or
  JSON keys never change them.
- R11.4 Every email is addressed with the user's display name and address in its `To` header (for
  example `To: Maria Lopez <maria.lopez@cheeseworks.example>`).

## Experience (R12)

Judged from what a person sees in a browser, except R12.6, which is also measured: at 1280 and 390 CSS pixels
the document is never wider than the viewport, even when names, codes and other typed values are long words
without spaces; colours, fonts, layout and wording are yours.

- R12.1 An order page makes clear, per line, how many units are reserved, shipped, backordered and
  cancelled, and states each refund with its amount and whether it is pending or refunded.
- R12.2 Before approving, the page states the order's total and the company's approval limit and that
  approving lets the buyer pay; before cancelling backordered units, it states that the units will be
  refunded.
- R12.3 A wave page lists the stops in walking order with each stop's bin, SKU, lot code and units, and
  states the total distance.
- R12.4 A refused pick (wrong bin, wrong lot) and a refused stale count each explain what was wrong and
  what to do next.
- R12.5 An invoice states its lines with units, unit price, amount and tax, any freight, and the total;
  the shipment email states the tracking number and the invoice total.
- R12.6 Every page stays usable 1280 and 390 CSS pixels wide, with no horizontal scrolling.

## Deployment (R13)

- R13.1 The workspace root contains an executable `start.sh` that starts the application listening on
  `0.0.0.0` at the port in `PORT`, in the foreground, until it is stopped.
- R13.2 `GET /healthz` answers 200 only while the application can reach the database, and a non-2xx
  status within 10 seconds of the database stopping, each answer arriving within 5 seconds. Once the
  database is back, `/healthz` answers 200 again within 60 seconds without a restart. The application
  answers `/healthz` with 200 within 60 seconds of `start.sh` starting.
- R13.3 Deployment copies the workspace to a clean machine with no network access, as regular files
  (symbolic links that point outside the workspace are not preserved). Every runtime dependency must
  already be inside the workspace (for example a virtual environment made with `python -m venv --copies`,
  packages installed into a folder in the workspace, or `node_modules` in the workspace), and `start.sh`
  must not download, install, or build anything.
- R13.4 Use only the three services in the services file. The application must not contact any other
  host.
- R13.5 Add a `README.md` at the workspace root naming the stack, how `start.sh` starts the app, and the
  environment variables it reads.
- R13.6 Everything the application stores must survive the application being stopped and started
  again, and nothing it already did (an email, a refund) is done again because of a restart.

## Interface contract

The application is driven through a real browser, through these paths, form fields and
markers, through the order API, and through the payment provider. Pages may carry any other content and
styling. `<id>` values are ids from the database.

| Path | Method | Purpose |
|---|---|---|
| `/` | GET | home page of the signed-in user |
| `/password-reset` | GET, POST | field `email`; button `data-action="request-reset"` (R2.1) |
| `/password-reset/<token>` | GET, POST | field `password`; button `data-action="set-password"` |
| `/signin` | GET, POST | fields `email`, `password`; button `data-action="signin"` (R2.7: query `next`) |
| `/signout` | POST | button `data-action="signout"` |
| `/profile` | GET, POST | field `full_name`; button `data-action="save-profile"` |
| `/skus/new` | GET, POST | fields of R3.2 (`taxable` is `true`/`false`); button `data-action="create-sku"`; success redirects to `/skus/<id>` |
| `/skus/<id>` | GET | the SKU and its lots |
| `/receipts/new` | GET, POST | fields `sku`, `lot_code`, `expiry_date`, `bin`, `units`, `unit_cost`; button `data-action="receive"`; success redirects to `/lots/<id>` |
| `/lots/<id>` | GET | the lot; count form |
| `/lots/<id>/count` | POST | fields `counted_units`, `version`; button `data-action="count-lot"` |
| `/prices` | GET, POST | fields `tier`, `sku`, `unit_price_cents`, `effective_from`; button `data-action="create-price"` |
| `/customers/new` | GET, POST | fields of R3.5 (`true`/`false` for the two flags); button `data-action="create-customer"`; success redirects to `/customers/<id>` |
| `/customers/<id>` | GET, POST | the customer; the same fields; button `data-action="save-customer"` |
| `/users/new` | GET, POST | fields `email`, `full_name`, `role`, `customer_id`, `zone`; button `data-action="create-user"`; success redirects to `/users/<id>` |
| `/users/<id>` | GET | the user |
| `/users/<id>/deactivate` | POST | button `data-action="deactivate-user"` |
| `/orders` | GET | orders the user may see (the old system's included) |
| `/orders/new` | GET, POST | fields `ship_date`, `po_number`, `notes`, and one `sku` and one `qty` field per line, repeated in line order (rows with an empty `sku` are ignored); button `data-action="create-order"`; success redirects to `/orders/<id>` |
| `/orders/<id>` | GET | the order (R12.1) |
| `/orders/<id>/approve` | POST | button `data-action="approve-order"` (R4.4) |
| `/orders/<id>/cancel` | POST | button `data-action="cancel-order"` (R4.5) |
| `/orders/<id>/pay` | POST | button `data-action="pay-order"` (R5.5) |
| `/orders/<id>/lines/<line_no>/cancel-backorder` | POST | field `units`; button `data-action="cancel-backorder"` (R6.5) |
| `/orders/<id>/ship` | POST | button `data-action="ship-order"` (R8.1); success redirects to `/shipments/<id>` |
| `/orders/<id>/rmas` | POST | fields `line_no`, `units`, `reason`; button `data-action="request-rma"` (R8.4); success redirects to `/rmas/<id>` |
| `/orders/export.csv` | GET | R9.6 |
| `/shipments/<id>` | GET | the shipment and its invoice (R8.2) |
| `/rmas/<id>` | GET | the return and its credit note |
| `/rmas/<id>/receive` | POST | button `data-action="receive-rma"` (R8.5) |
| `/rmas/<id>/inspect` | POST | field `outcome` (`restock` or `damaged`); button `data-action="inspect-rma"` (R8.7) |
| `/waves` | GET, POST | fields `zone`, `ship_date`, `max_tasks`; button `data-action="create-wave"`; success redirects to `/waves/<id>` |
| `/waves/<id>` | GET | the wave, its route and its tasks |
| `/pick-tasks/<id>/confirm` | POST | fields `bin_code`, `lot_code`, `units`; button `data-action="confirm-pick"` (R7.4) |
| `/account/api-keys` | GET, POST | field `label`; button `data-action="create-api-key"` |
| `/account/api-keys/<id>/revoke` | POST | button `data-action="revoke-api-key"` |
| `/api/v1/orders` | POST | R4.7 |
| `/api/v1/orders/<id>` | GET | R4.8 |
| `/webhooks/payments` | POST | R5.6 |
| `/reports/on-hand.csv` | GET | R9.3 |
| `/reports/ledger.csv` | GET | R9.4 |
| `/reports/valuation.csv` | GET | R9.5 |
| `/healthz` | GET | R13.2 |

Every form that changes something includes, inside the form, its session token as a hidden `<input>`
with the attribute `data-csrf`; whatever its `name`, it is submitted unchanged with the form. Each
form named above is a `<form method="post">` whose `action` is the path in the table, with its button
inside it. A refused form submission answers a 4xx status.

Every action form is on a page **exactly when** the signed-in user may take that action at that moment,
as R1–R8 define who may do what and when, and never otherwise. For example, an order page shows
`pay-order` to a buyer or approver of the order's company while the order is `awaiting_payment`, and to
nobody else.

Markers, as HTML attributes:

- `/skus/<id>` carries `data-sku-id="<id>"`; `/customers/<id>` carries `data-customer-id`;
  `/users/<id>` carries `data-user-id`.
- `/lots/<id>` carries one element with `data-lot-id`, `data-on-hand`, `data-reserved`,
  `data-lot-version` and `data-lot-status` (the R9.3 status).
- An order page carries one element with `data-order-id`, `data-order-status` and `data-total-cents`;
  each line is inside an element with `data-line-no`, `data-reserved`, `data-shipped`,
  `data-backordered` and `data-cancelled`, which contains that line's `cancel-backorder` form and its
  `request-rma` form (with a hidden `line_no`).
- A wave page carries one element with `data-wave-id`, `data-zone`, `data-ship-date` and
  `data-route-distance`; each stop is an element with `data-stop-seq` (1, 2, … in route order) and
  `data-bin`, containing one element per task with `data-task-id`, `data-order-id`, `data-line-no`,
  `data-lot-id`, `data-units` and `data-task-status` (`open` or `picked`); a task's element contains its
  `confirm-pick` form.
- A shipment page carries `data-shipment-id`, `data-order-id`, `data-tracking-number`,
  `data-invoice-number`, `data-invoice-total-cents`, `data-freight-cents` and `data-freight-tax-cents`;
  each invoice line is an element with `data-invoice-line-no` (the order's `line_no`), `data-units`,
  `data-amount-cents` and `data-tax-cents`.
- An RMA page carries `data-rma-id`, `data-rma-status` (`requested`, `quarantined` or `closed`),
  `data-disposition` (empty, `restocked` or `written_off`) and `data-credit-cents` (empty until
  received).
- Right after creating an API key, the page carries an element with `data-api-key="<the key>"` and
  `data-api-key-id="<id>"`; each listed key is an element with `data-api-key-id` that contains its revoke
  form.
- When signed in, every page has the sign-out button `data-action="signout"`.
