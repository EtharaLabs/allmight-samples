# TRUTH: ethara/pickpath-fulfillment

Task version 1.0.0.

Sources: `task.toml`, `README.md`, `instruction.md`, `environment/`, `solution/`, `tests/` (`workflows.toml`, `reward.toml`, `judge.toml`, `scoring.py`, `test.sh` and the workflow code), the seed loaded into PostgreSQL, and the verifier output of oracle run `20261111T075749Z-r2qc-pp-oracle` (`results.json` sha256 `242aec508c4729c9e2ec1a692c788b01dca574634d853565bcd4f2b191dc37c6`).

This file is for graders and reviewers. The agent never sees it: Harbor does not copy `solution/` into the agent's container, and `environment/Dockerfile` copies only `uv-requirements.lock`, `services`.

## Reward and pass rule

Full reward is `1.0` on the `reward` key of `/logs/verifier/reward.json` (keys `reward`, `functional`, `judge`).

| term | rule | source |
|---|---|---|
| reward | `0.9 × functional + 0.1 × judge` (weighted sum) | `tests/reward.toml` |
| functional | weighted mean of 26 strict workflows (weights sum to 103); a workflow scores 1 only when every one of its checks passes, else 0 | `tests/workflows.toml`, `tests/scoring.py` |
| cap | any breach detector firing anywhere in the run (×0.4), or any of the 17 critical workflows failing (×0.4), caps every workflow, so the reward is at most 0.46; a critical workflow that fails only because the verifier's time budget ran out scores 0 without the cap | `tests/scoring.py` |
| judge | weighted mean of 10 binary criteria, each judged by `anthropic/claude-fable-5-1` (reasoning effort max, 3 samples) from the checkpoint evidence under `/logs/verifier/ux/` | `tests/judge.toml`, `tests/judge-reference.md` |
| invalid run | `test.sh` writes a zero reward first; a verifier, service or judge failure leaves that zero and writes `/logs/verifier/evaluation-invalid.json` | `tests/test.sh` |

## Golden trajectory

| step | what happens | source |
|---|---|---|
| S1 | the environment starts PostgreSQL seeded with the existing records, the SMTP mail sink and the Stripe-compatible payments mock, and writes `/etc/pickpath/services.env` | `environment/services/up.sh` |
| S2 | `solve.sh` copies `solution/app/` into `/app`, makes a copy-based virtual environment, and installs `requirements.txt` offline from the 4 wheels in `solution/wheels/` | `solution/solve.sh` |
| S3 | the verifier starts `/app/start.sh` (`exec ./.venv/bin/python -u app.py`) on a fresh copy of the database; in the oracle run `/healthz` answered 200 after 2 s | `solution/app/start.sh`, `startup.json` |
| S4 | the verifier runs the 26 workflows (125 checks) and the judge; the oracle passed every check, fired no breach detector, and scored functional 1.0, judge 1.0, reward 1.0 | `results.json`, `reward.json` |

Reference stack (`solution/app/README.md`): Python 3.12 standard library HTTP server (`http.server.ThreadingHTTPServer`) with psycopg 3 for PostgreSQL, server-rendered HTML, `smtplib` for email, and `urllib` for the payment provider.

## Records the checkers rely on

The agent's database and the verifier's are both seeded from `db/init/` (`001_schema.sql`, `002_records.sql`); `tests/services/db/` is byte-identical to `environment/services/db/` (tree sha256 `ed118cd84f8868422667cebaacd452877bd7540c9a05f57777224af73c710265`). Row counts after loading the seed into PostgreSQL:

| table | rows |
|---|---|
| `bins` | 1281 |
| `customers` | 160 |
| `legacy_movements` | 79449 |
| `legacy_order_lines` | 17985 |
| `legacy_orders` | 6000 |
| `lots` | 30004 |
| `price_list` | 23105 |
| `sites` | 1 |
| `skus` | 5000 |
| `tax_rates` | 12 |
| `users` | 550 |

Users by role: `approver` 141; `buyer` 385; `finance` 3; `picker` 18; `supervisor` 3.

Volumes the checks compared in oracle run `20261111T075749Z-r2qc-pp-oracle` (whole-number observations from `results.json`; counts that depend on the run date can differ on another day):

| workflow | observed |
|---|---|
| W03 | accepted 117; refused 13; hard_cases.line tax tie 49; hard_cases.freight tax tie 12; hard_cases.weight boundary 72; hard_cases.split case 14 |
| W06 | backordered_lines 91; cancelled_lines 72; dst_threshold_orders 10 |
| W16 | lots 17154; orders 30 |
| W26 | lots 12850; orders 30 |

## Rubric

### Strict workflows

| id | weight | share | critical | checks | workflow |
|---|---|---|---|---|---|
| W01 | 3 | 2.9% | no | 9 | Password links, sign-in, sessions and return paths (anti-enumeration, single use, expiry, fixation, open redirect) |
| W02 | 3 | 2.9% | no | 5 | Setting up: customers, users, SKUs, receipts and prices with validation, lot identity and role checks |
| W03 | 5 | 4.9% | yes | 4 | Order pricing rules: tier prices on the ship date, per-line tax, dimensional-weight freight, cold chain, free freight |
| W04 | 3 | 2.9% | yes | 5 | Approval limits: the boundary, approvers only (never their own), other companies refused, emails to approvers |
| W05 | 5 | 4.9% | yes | 7 | Checkout and webhooks: session details, only signed events confirm, forged/stale refused, out of order, duplicates, late payment refunded |
| W06 | 5 | 4.9% | yes | 4 | FEFO allocation rules: shelf life on the ship date, expiry ties by lot id, whole and split cases, backorder or cancel, fills |
| W07 | 5 | 4.9% | yes | 6 | Backorders: whole cases, fills by order id with each order's own shelf life, cancelled units refunded exactly |
| W09 | 5 | 4.9% | yes | 3 | Pick paths: wave task selection and the exact shortest route with the dictionary-order tie-break |
| W10 | 4 | 3.9% | yes | 5 | Picking: scans must match bin and lot, quantities validated, zones enforced, short picks hold the lot and re-allocate |
| W11 | 4 | 3.9% | yes | 4 | Cycle counts: version checks, adjustments with picked units, releases newest first, re-allocation, holds cleared, fills |
| W12 | 5 | 4.9% | yes | 4 | Shipping and invoices: tracking, cumulative line tax across shipments, freight on the first shipment only, emails |
| W13 | 4 | 3.9% | yes | 6 | Refunds under provider faults: retries keep one Idempotency-Key, one provider refund each, complete only on a signed refund.created |
| W14 | 4 | 3.9% | yes | 5 | Returns: limits, quarantine, exact credit notes, restock only when not short-dated, write-offs, refills |
| W08 | 4 | 3.9% | yes | 4 | Concurrency: racing payments for the last units, double cancellations, double shipments and simultaneous counts |
| W16 | 5 | 4.9% | yes | 3 | The old system's ambient stock: on-hand rows for zone A lots and FEFO over them |
| W26 | 5 | 4.9% | yes | 3 | The old system's cold stock: on-hand rows for chilled, frozen and quarantined lots and FEFO over them |
| W17 | 5 | 4.9% | yes | 7 | FIFO valuation and the ledger on the fall-back nights: instants inside the repeated hour, wall-clock order versus real order |
| W27 | 5 | 4.9% | yes | 9 | FIFO valuation across the skipped spring hour and same-instant receipts, and now: newest layers, half-cent totals |
| W18 | 3 | 2.9% | yes | 2 | Order history export: the old system's orders in UTC (folds), company scoping, quoting and spreadsheet safety |
| W19 | 3 | 2.9% | no | 4 | Low-stock email: below the reorder point, expired and held lots excluded, once per SKU per day to each active supervisor |
| W20 | 3 | 2.9% | no | 4 | API keys and the order API: shown once, scoped to the company, revocation, inactive users, validation, mass assignment |
| W21 | 4 | 3.9% | no | 5 | Tenancy and mass assignment: another company's orders, shipments, returns and actions; hidden fields change nothing |
| W22 | 3 | 2.9% | no | 2 | Every state change needs the session token: orders, approvals, payments, returns, stock, prices, customers, users, keys |
| W23 | 3 | 2.9% | no | 6 | In a real browser: layout at 1280 and 390 px, typed text never runs as script, paying through the hosted page |
| W24 | 3 | 2.9% | no | 6 | Deployment: README, conservation, restart persistence, a pending refund survives a restart, nothing re-sent, no secrets at rest, records kept |
| W25 | 2 | 1.9% | no | 3 | Health follows the database: non-2xx within 10 s of a stop, 200 within 60 s of a start |

### Checks

Every check of every workflow, in order, as the oracle run recorded them (each matches a `ctx.step()` in `tests/`). A workflow scores only when all of its checks pass.

**W01** Password links, sign-in, sessions and return paths (anti-enumeration, single use, expiry, fixation, open redirect)

1. finance and a supervisor set passwords from emailed links and sign in
2. a known and an unknown address get the same status and message; the unknown one gets no email
3. an inactive user's address receives no email
4. a newer link replaces the older one; the older one sets nothing; a used link changes nothing
5. an expired link changes nothing; an unknown link answers 404
6. sign-in matches the address case-insensitively and padded; wrong password and unknown address are refused alike
7. signing in starts a new session identifier; an identifier held before never becomes signed in
8. signing out ends the session on the server
9. a visitor sent to sign in returns to the page afterwards; sign-in never redirects to another site

**W02** Setting up: customers, users, SKUs, receipts and prices with validation, lot identity and role checks

1. finance adds a customer and a buyer; an address on file (any case and spacing) and a control-character name are refused
2. a supervisor adds a SKU (code stored upper-cased); a duplicate code in other case and an invalid case pack are refused
3. receipts: a new lot, the same lot again (code in other case and spacing), conflicting lot data and wrong-zone bins are refused
4. finance adds prices; the same tier, SKU and date again is refused
5. each role is refused other roles' pages and actions with 403, and nothing changes

**W03** Order pricing rules: tier prices on the ship date, per-line tax, dimensional-weight freight, cold chain, free freight

1. SKUs with tier prices that change on a ship date, plus a SKU priced only later
2. customers in nine states and three tiers, with keys
3. 130 orders through the API: every price, tax, freight and total matches the rules; refused ones answer 422
4. hard cases: half-cent line and freight taxes, exact half-kilogram boundaries, per-line billable weight, split cases

**W04** Approval limits: the boundary, approvers only (never their own), other companies refused, emails to approvers

1. a company with an approval limit, a buyer, two approvers, and another company's approver
2. a total equal to the limit needs no approval; one cent... one unit more does, and each other approver is emailed
3. an approver's own order emails only the other approver
4. the buyer cannot pay or approve; another company's approver and the creating approver cannot approve
5. the other approver approves; approving again or approving a cancelled order is refused with 409

**W05** Checkout and webhooks: session details, only signed events confirm, forged/stale refused, out of order, duplicates, late payment refunded

1. a company, a SKU with stock, and three orders
2. paying redirects to a Checkout Session with the stated amount, metadata, email and return URLs; paying again reuses it
3. with deliveries held, the card is paid and the buyer returns to success_url: the order stays unpaid
4. unsigned, mis-signed, stale and wrong-key deliveries are answered 400 and change nothing
5. payment_intent.succeeded first changes nothing; the signed checkout.session.completed then confirms once
6. a declined card leaves the order unpaid
7. a payment that completes after the order was cancelled is refunded in full through the provider, once

**W06** FEFO allocation rules: shelf life on the ship date, expiry ties by lot id, whole and split cases, backorder or cancel, fills

1. six companies with different contracts; 24 SKUs with lots expiring around each shelf-life threshold
2. lots received: 2 to 5 per SKU, with ties on expiry
3. 130 orders paid one after another (a second batch of receipts arrives half way): every allocation matches FEFO
4. across the next two clock changes, a lot exactly at the shelf-life floor is eligible and one a day short is not

**W07** Backorders: whole cases, fills by order id with each order's own shelf life, cancelled units refunded exactly

1. two companies: backorders with whole cases and 10 days' shelf life; no backorders with split cases
2. payments allocate whole cases and backorder the rest, or cancel and refund it
3. receipts fill backorders by order id, each with its own ship date's shelf life
4. cancelling backordered units refunds exactly; out-of-range cancellations are refused
5. every refund carries order and refund metadata and its own Idempotency-Key
6. an order whose every unit is cancelled at payment refunds its whole total, freight included, and is cancelled

**W09** Pick paths: wave task selection and the exact shortest route with the dictionary-order tie-break

1. 90 SKUs each with one lot in a chosen bin (shared bins, stacked levels, mirrored bays)
2. 40 ship dates, one or two paid orders each; each wave takes the first tasks and walks the shortest route
3. a wave with nothing to pick is refused with 409

**W10** Picking: scans must match bin and lot, quantities validated, zones enforced, short picks hold the lot and re-allocate

1. pickers for zones A and C; a whole-case company; SKU lots in two bins
2. a zone C picker gets 403 for the zone A wave and its picks, and nothing changes
3. a wrong bin or lot is refused with 409, bad quantities with 422, a token-less confirm with 403; nothing changes
4. a short pick (codes typed in other case and spacing) holds the lot and re-allocates the rest from the next lot
5. a short pick with nothing left to allocate cancels and refunds for a company without backorders

**W11** Cycle counts: version checks, adjustments with picked units, releases newest first, re-allocation, holds cleared, fills

1. lots with known versions; a stale count is refused with 409 and changes nothing
2. picked units waiting to ship are added to the count
3. a count below the open reservations releases the newest order's units first and re-allocates them
4. a count clears a short pick's hold and fills backorders from the counted lot

**W12** Shipping and invoices: tracking, cumulative line tax across shipments, freight on the first shipment only, emails

1. a NY company orders taxable tins, oil and cheese; part of the tins are backordered
2. while picks are open there is no ship form and shipping is refused with 409
3. the first shipment invoices its lines with tax and the order's freight; the buyer is emailed the tracking number
4. the backorder fills, ships second: cumulative tax and no freight; the order completes and invoices plus refunds equal the charge

**W13** Refunds under provider faults: retries keep one Idempotency-Key, one provider refund each, complete only on a signed refund.created

1. a company with five paid orders whose units are all backordered
2. provider fault 'error' on the refund call: exactly one provider refund, one key, refunded, one email
3. provider fault 'timeout' on the refund call: exactly one provider refund, one key, refunded, one email
4. provider fault 'lost_response' on the refund call: exactly one provider refund, one key, refunded, one email
5. provider fault 'slow' on the refund call: exactly one provider refund, one key, refunded, one email
6. a refund stays pending until its refund.created verifies; a forged one changes nothing

**W14** Returns: limits, quarantine, exact credit notes, restock only when not short-dated, write-offs, refills

1. a NY company buys 30 tins from three lots expiring in 6, 7 and 30 days; they ship
2. return requests beyond the shipped units, without a reason, or on another company's order are refused
3. receiving puts the units in quarantine and issues the exact credit note, emailed once; stock is unchanged
4. restocking a lot that expires within 7 days writes it off; damaged goods are written off; nothing is added
5. a return from a lot with 7 days left is restocked into that lot and fills a backorder

**W08** Concurrency: racing payments for the last units, double cancellations, double shipments and simultaneous counts

1. six orders for the last 12 units are paid while confirmations are held
2. the same cancellation submitted twice at once cancels and refunds once
3. two simultaneous ship requests make one shipment and one stock movement
4. two counts with the same version at the same moment: exactly one succeeds

**W16** The old system's ambient stock: on-hand rows for zone A lots and FEFO over them

1. the on-hand report lists every ambient lot of the old system exactly, within 60 seconds
2. orders on 30 ambient SKUs of the old system allocate by FEFO over its lots (expired, held, quarantined, odd codes)
3. the on-hand report shows those reservations on the ambient lots

**W26** The old system's cold stock: on-hand rows for chilled, frozen and quarantined lots and FEFO over them

1. the on-hand report lists every cold lot of the old system exactly, within 60 seconds
2. orders on 30 cold SKUs of the old system allocate by FEFO over its lots (expired, held, quarantined, odd codes)
3. the on-hand report shows those reservations on the cold lots

**W17** FIFO valuation and the ledger on the fall-back nights: instants inside the repeated hour, wall-clock order versus real order

1. valuation as of 2026-11-01T01:30:00-04:00 matches FIFO over the old system's movements for every SKU
2. valuation as of 2026-11-01T01:30:00-05:00 matches FIFO over the old system's movements for every SKU
3. valuation as of 2026-11-01T01:05:00-05:00 matches FIFO over the old system's movements for every SKU
4. valuation as of 2025-11-02T06:20:00Z matches FIFO over the old system's movements for every SKU
5. valuation as of 2025-11-02T01:59:59-04:00 matches FIFO over the old system's movements for every SKU
6. valuation as of 2026-11-09T23:59:59+00:00 matches FIFO over the old system's movements for every SKU
7. the ledger of SKUs with movements in a repeated hour lists them at the right UTC instants, in order

**W27** FIFO valuation across the skipped spring hour and same-instant receipts, and now: newest layers, half-cent totals

1. valuation as of 2026-03-08T06:59:59Z matches FIFO over the old system's movements for every SKU
2. valuation as of 2026-03-08T07:00:00Z matches FIFO over the old system's movements for every SKU
3. valuation as of 2026-03-08T03:30:00-04:00 matches FIFO over the old system's movements for every SKU
4. valuation as of 2026-03-08T01:59:59-05:00 matches FIFO over the old system's movements for every SKU
5. valuation as of 2026-06-15T14:30:00Z matches FIFO over the old system's movements for every SKU
6. valuation as of 2026-06-15T14:29:59Z matches FIFO over the old system's movements for every SKU
7. the ledger of SKUs with movements stored in the skipped hour lists them at the stated instants
8. new receipts at half-cent costs, negative and positive counts: valued now, newest layers first, half-up
9. an as_of without an offset is answered 400; a supervisor gets 403

**W18** Order history export: the old system's orders in UTC (folds), company scoping, quoting and spreadsheet safety

1. a buyer of a company with an odd name (from the old system) exports their history: exact rows in UTC
2. finance exports every order, the old system's exactly; a supervisor gets 403

**W19** Low-stock email: below the reorder point, expired and held lots excluded, once per SKU per day to each active supervisor

1. a deactivated supervisor, a SKU with a reorder point of 20 and 30 units: no email yet
2. an allocation takes it to 18: every active supervisor gets one email naming the SKU, 18 and 20
3. further drops and a receipt the same day send nothing more; the deactivated supervisor got nothing
4. expired lots do not count: stock that is all expired triggers the email at once

**W20** API keys and the order API: shown once, scoped to the company, revocation, inactive users, validation, mass assignment

1. a key is shown once and works; it is not shown again
2. malformed bodies answer 400; invalid orders answer 422 with errors and create nothing
3. extra JSON keys never change the company, prices or status
4. a revoked key, an unknown key and the key of a deactivated user answer 401

**W21** Tenancy and mass assignment: another company's orders, shipments, returns and actions; hidden fields change nothing

1. company A has a shipped order with a received return; company B has a buyer and an approver
2. company B's buyer gets 404/403 and none of the data for A's order, shipment, invoice and return
3. company B's buyer and approver cannot act on A's order; nothing changes
4. hidden order fields change nothing: the order belongs to the buyer's company at list prices
5. the profile form changes only the display name; an approver cannot change the company's limit

**W22** Every state change needs the session token: orders, approvals, payments, returns, stock, prices, customers, users, keys

1. fixtures: orders awaiting approval, awaiting payment, backordered, shipped; returns; a wave; keys
2. each form submitted without its token is refused with 403 and changes nothing

**W23** In a real browser: layout at 1280 and 390 px, typed text never runs as script, paying through the hosted page

1. records carrying script payloads in every typed field
2. signing in through the browser form reaches a signed-in page
3. evidence pages for the experience review
4. every page stays within 1280 and 390 CSS pixels without horizontal scrolling
5. names, codes, PO numbers, notes, reasons and labels are shown as text everywhere, error pages included
6. the buyer pays by clicking through the hosted page and returns to an order awaiting confirmation, then paid

**W24** Deployment: README, conservation, restart persistence, a pending refund survives a restart, nothing re-sent, no secrets at rest, records kept

1. README.md and an executable start.sh are in the workspace
2. for every SKU the verifier touched, each lot's ledger adds up to its on-hand and nothing is negative
3. a refund whose provider call hangs when the application restarts is completed afterwards, exactly once
4. after the restart: passwords, keys, orders and stock are kept and no email is sent again
5. no password, API key, reset token or session identifier is stored in plain text
6. the old system's records are all still there, unchanged

**W25** Health follows the database: non-2xx within 10 s of a stop, 200 within 60 s of a start

1. /healthz answers 200 while the database is up
2. within 10 s of the database stopping, /healthz answers a non-2xx status within 5 s
3. within 60 s of the database starting again, /healthz answers 200 without a restart

### Breach detectors

Any detector that fires caps every workflow at ×0.4. Each row lists where the detector is raised and what it reports (`…` marks a value filled in at run time).

| code | raised in | fires when |
|---|---|---|
| X1 | W02, W04, W10, W14, W18, W20, W21 | a buyer or supervisor added a price / a buyer requested a return on another company's order / a buyer's export listed other companies' orders: … / a key of another company read an order / a picker confirmed a pick in another zone / a picker saw another zone's wave / another company's buyer saw … / another company's order is in a buyer's export / another company's order is in a buyer's order list / another company's requests changed an order / finance received stock / … approved order … |
| X2 | W04, W10, W22 | a refused or token-less pick confirmation took effect / an approval without the session token took effect / confirm-pick without the session token took effect / … without the session token took effect |
| X3 | W23 | typed text ran as script: … |
| X5 | W20, W21 | an approver changed the company's settings / extra JSON keys changed the order: … … … / hidden form fields changed an order: … / the profile form changed … |
| X6 | W05, W08, W13, W19, W24 | a refund was made twice across a restart / a repeated payment event allocated the order twice / a repeated payment event sent the confirmation twice / one cancellation submitted three times refunded or cancelled more than once / one refund became … provider refunds under fault … / two counts with one version both applied (statuses …, version …) / two ship requests made … shipments (lot on-hand …) / … emails were sent more than once: … / … got … low-stock emails for one SKU on one day |
| X7 | W05, W13, W20 | a body that does not match its signature marked the order paid / a deactivated user's API key still worked / a forged refund.created completed a refund / a revoked API key still worked / a webhook with … signature marked the order paid |
| X8 | W24 | plain-text secret in … |
| X9 | W01, W20 | a deactivated user's session still worked / a session cookie copied before sign-out still signs in / a session identifier held before sign-in became signed in |
| X10 | W24 | pre-existing records changed: … |
| X11 | W01 | sign-in redirected to … for next=… |
| X13 | W08, W24 | lot … has negative stock (…) / … units reserved from a lot holding 12 |

### Judged experience criteria

`anthropic/claude-fable-5-1` answers each criterion yes or no from the evidence files named for it, following `tests/judge-reference.md`; a checkpoint that was never reached fails its criterion. The oracle run scored every criterion yes.

| id | requirement | weight | evidence (written by) | criterion |
|---|---|---|---|---|
| J01 | R12.1 | 1 | `order_backorder` (W23) | The order page makes clear, for each line, how many units are reserved, shipped, backordered and cancelled. |
| J02 | R12.1 | 1 | `order_refund` (W23) | After backordered units are cancelled, the order page states the refund with its amount and whether it is pending or refunded. |
| J03 | R12.2 | 1 | `order_backorder` (W23) | Before cancelling backordered units, the page states that the cancelled units will be refunded. |
| J04 | R12.2 | 1 | `approve_before` (W23) | Before approving, the page states the order's total, the company's approval limit, and that approving lets the buyer pay. |
| J05 | R12.3 | 1 | `wave_page` (W23) | The wave page lists the stops in walking order with each stop's bin, SKU, lot code and units, and states the total distance. |
| J06 | R12.4 | 1 | `pick_refused` (W23) | After a pick with the wrong bin is refused, the page explains what was wrong and what to do next. |
| J07 | R12.4 | 1 | `count_refused` (W23) | After a stale cycle count is refused, the page explains that the lot changed since it was opened and what to do next. |
| J08 | R12.5 | 1 | `invoice` (W23) | The invoice states its lines with units, unit price, amount and tax, any freight, and the invoice total. |
| J09 | R12.5 | 1 | `shipment_email` (W12) | The shipment email states the tracking number and the invoice total in dollars. |
| J10 | R12.6 | 1 | `order_backorder/narrow.jpg` (W23); `wave_page/narrow.jpg` (W23); `invoice/narrow.jpg` (W23) | At 390 pixels wide, the key facts and the primary action on these pages stay readable and reachable. |

## Controls

| control | run | reward | functional | judge |
|---|---|---|---|---|
| oracle (`solution/solve.sh`) | `20261111T075749Z-r2qc-pp-oracle` | 1.0 | 1.0 | 1.0 |
| nop (empty workspace) | `20261111T075753Z-r2qc-pp-nop` | 0.0 | 0.0 | 0.0 |

## Interface contract literals

The routes of `instruction.md`'s interface contract and every `data-action` and `data-*` marker it names. The workflows drive and read the application through these routes and markers (as well as the form fields, exports and emails the instruction specifies), so a submission that renames one fails the checks that rely on it.

| path | methods |
|---|---|
| `/` | GET |
| `/password-reset` | GET, POST |
| `/password-reset/<token>` | GET, POST |
| `/signin` | GET, POST |
| `/signout` | POST |
| `/profile` | GET, POST |
| `/skus/new` | GET, POST |
| `/skus/<id>` | GET |
| `/receipts/new` | GET, POST |
| `/lots/<id>` | GET |
| `/lots/<id>/count` | POST |
| `/prices` | GET, POST |
| `/customers/new` | GET, POST |
| `/customers/<id>` | GET, POST |
| `/users/new` | GET, POST |
| `/users/<id>` | GET |
| `/users/<id>/deactivate` | POST |
| `/orders` | GET |
| `/orders/new` | GET, POST |
| `/orders/<id>` | GET |
| `/orders/<id>/approve` | POST |
| `/orders/<id>/cancel` | POST |
| `/orders/<id>/pay` | POST |
| `/orders/<id>/lines/<line_no>/cancel-backorder` | POST |
| `/orders/<id>/ship` | POST |
| `/orders/<id>/rmas` | POST |
| `/orders/export.csv` | GET |
| `/shipments/<id>` | GET |
| `/rmas/<id>` | GET |
| `/rmas/<id>/receive` | POST |
| `/rmas/<id>/inspect` | POST |
| `/waves` | GET, POST |
| `/waves/<id>` | GET |
| `/pick-tasks/<id>/confirm` | POST |
| `/account/api-keys` | GET, POST |
| `/account/api-keys/<id>/revoke` | POST |
| `/api/v1/orders` | POST |
| `/api/v1/orders/<id>` | GET |
| `/webhooks/payments` | POST |
| `/reports/on-hand.csv` | GET |
| `/reports/ledger.csv` | GET |
| `/reports/valuation.csv` | GET |
| `/healthz` | GET |

| kind | values |
|---|---|
| actions (`data-action`) | approve-order, cancel-backorder, cancel-order, confirm-pick, count-lot, create-api-key, create-customer, create-order, create-price, create-sku, create-user, create-wave, deactivate-user, inspect-rma, pay-order, receive, receive-rma, request-reset, request-rma, revoke-api-key, save-customer, save-profile, set-password, ship-order, signin, signout |
| markers | data-amount-cents, data-api-key, data-api-key-id, data-backordered, data-bin, data-cancelled, data-credit-cents, data-csrf, data-customer-id, data-disposition, data-freight-cents, data-freight-tax-cents, data-invoice-line-no, data-invoice-number, data-invoice-total-cents, data-line-no, data-lot-id, data-lot-status, data-lot-version, data-on-hand, data-order-id, data-order-status, data-reserved, data-rma-id, data-rma-status, data-route-distance, data-ship-date, data-shipment-id, data-shipped, data-sku-id, data-stop-seq, data-task-id, data-task-status, data-tax-cents, data-total-cents, data-tracking-number, data-units, data-user-id, data-wave-id, data-zone |
