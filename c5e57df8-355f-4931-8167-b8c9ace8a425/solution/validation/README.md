# Local validation of pickpath-fulfillment (2026-11-10)

> **Database version.** The runs recorded here used PostgreSQL 17, but the
> delivered images pin `postgresql-15=15.19-0+deb12u1` in both
> `environment/Dockerfile` and `tests/Dockerfile`. This record was therefore
> produced on a newer server than the task actually ships, and the results
> below have not been re-measured on 15.

## Setup

- PostgreSQL 17 on 127.0.0.1:55432 (own role and database `pickpath`), seeded from `environment/services/db/init/` (the
  bytes an authoring step assembled from `solution/seed/generate.py`; that assembly script is not part of this
  delivery, and the generated SQL it produced is shipped instead. Both service trees carry identical copies).
- Kit services from `kit/tests/services`: the mail sink (SMTP 56140, HTTP 56141, admin 56142) and the payments mock
  (56143, admin 56144). The oracle is `solution/app` installed as `solution/solve.sh` does (venv with `--copies`), on port 58140.
- `tests/snapshot.py` ran before the app started; `tests/workflows.py` ran with the kit's environment variables plus
  `FK_PAYMENTS_ADMIN_URL` (the local admin port; in the verifier, `ADMIN_SOCKET_DIR/payments.sock`).
- Local stand-ins for the verifier hooks: `FK_RESTART_APP` kills the app by port and starts it again; `FK_DB_STOP` /
  `FK_DB_START` make the app's own role `NOLOGIN` (ending its sessions) and `LOGIN` again, because the shared
  PostgreSQL may not be stopped. W25 therefore passed locally against that stand-in; the real `pg_ctl stop`/`start`
  path is exercised only on EC2.

## Oracle: 26 of 26 workflows pass, no breach (fresh database)

Weights sum to 103; 17 workflows are critical (`critical_cap = 0.4`); 125 strict substeps.

| Id | Title | Weight | Critical | Result | Seconds | Substeps |
|---|---|---|---|---|---|---|
| W01 | Password links, sign-in, sessions and return paths (anti-enumeration, single use, expiry, fixation, open redirect) | 3 |  | PASS | 32.5 | 9 |
| W02 | Setting up: customers, users, SKUs, receipts and prices with validation, lot identity and role checks | 3 |  | PASS | 1.5 | 5 |
| W03 | Order pricing rules: tier prices on the ship date, per-line tax, dimensional-weight freight, cold chain, free freight | 5 | yes | PASS | 10.8 | 4 |
| W04 | Approval limits: the boundary, approvers only (never their own), other companies refused, emails to approvers | 3 | yes | PASS | 7.0 | 5 |
| W05 | Checkout and webhooks: session details, only signed events confirm, forged/stale refused, out of order, duplicates, late payment refunded | 5 | yes | PASS | 15.8 | 7 |
| W06 | FEFO allocation rules: shelf life on the ship date, expiry ties by lot id, whole and split cases, backorder or cancel, fills | 5 | yes | PASS | 60.9 | 4 |
| W07 | Backorders: whole cases, fills by order id with each order's own shelf life, cancelled units refunded exactly | 5 | yes | PASS | 4.0 | 6 |
| W09 | Pick paths: wave task selection and the exact shortest route with the dictionary-order tie-break | 5 | yes | PASS | 27.4 | 3 |
| W10 | Picking: scans must match bin and lot, quantities validated, zones enforced, short picks hold the lot and re-allocate | 4 | yes | PASS | 4.0 | 5 |
| W11 | Cycle counts: version checks, adjustments with picked units, releases newest first, re-allocation, holds cleared, fills | 4 | yes | PASS | 2.6 | 4 |
| W12 | Shipping and invoices: tracking, cumulative line tax across shipments, freight on the first shipment only, emails | 5 | yes | PASS | 2.1 | 4 |
| W13 | Refunds under provider faults: retries keep one Idempotency-Key, one provider refund each, complete only on a signed refund.created | 4 | yes | PASS | 44.6 | 6 |
| W14 | Returns: limits, quarantine, exact credit notes, restock only when not short-dated, write-offs, refills | 4 | yes | PASS | 3.1 | 5 |
| W08 | Concurrency: racing payments for the last units, double cancellations, double shipments and simultaneous counts | 4 | yes | PASS | 7.9 | 4 |
| W16 | The old system's ambient stock: on-hand rows for zone A lots and FEFO over them | 5 | yes | PASS | 12.6 | 3 |
| W26 | The old system's cold stock: on-hand rows for chilled, frozen and quarantined lots and FEFO over them | 5 | yes | PASS | 12.4 | 3 |
| W17 | FIFO valuation and the ledger on the fall-back nights: instants inside the repeated hour, wall-clock order versus real order | 5 | yes | PASS | 2.2 | 7 |
| W27 | FIFO valuation across the skipped spring hour and same-instant receipts, and now: newest layers, half-cent totals | 5 | yes | PASS | 1.9 | 9 |
| W18 | Order history export: the old system's orders in UTC (folds), company scoping, quoting and spreadsheet safety | 3 | yes | PASS | 0.9 | 2 |
| W19 | Low-stock email: below the reorder point, expired and held lots excluded, once per SKU per day to each active supervisor | 3 |  | PASS | 13.0 | 4 |
| W20 | API keys and the order API: shown once, scoped to the company, revocation, inactive users, validation, mass assignment | 3 |  | PASS | 1.6 | 4 |
| W21 | Tenancy and mass assignment: another company's orders, shipments, returns and actions; hidden fields change nothing | 4 |  | PASS | 2.8 | 5 |
| W22 | Every state change needs the session token: orders, approvals, payments, returns, stock, prices, customers, users, keys | 3 |  | PASS | 3.2 | 2 |
| W23 | In a real browser: layout at 1280 and 390 px, typed text never runs as script, paying through the hosted page | 3 |  | PASS | 12.3 | 6 |
| W24 | Deployment: README, conservation, restart persistence, a pending refund survives a restart, nothing re-sent, no secrets at rest, records kept | 3 |  | PASS | 38.6 | 6 |
| W25 | Health follows the database: non-2xx within 10 s of a stop, 200 within 60 s of a start | 2 |  | PASS | 0.1 | 3 |

```
[wf] W01 PASS 32.5s 
[wf] W02 PASS 1.5s 
[wf] W03 PASS 10.8s 
[wf] W04 PASS 7.0s 
[wf] W05 PASS 15.8s 
[wf] W06 PASS 60.9s 
[wf] W07 PASS 4.0s 
[wf] W09 PASS 27.4s 
[wf] W10 PASS 4.0s 
[wf] W11 PASS 2.6s 
[wf] W12 PASS 2.1s 
[wf] W13 PASS 44.6s 
[wf] W14 PASS 3.1s 
[wf] W08 PASS 7.9s 
[wf] W16 PASS 12.6s 
[wf] W26 PASS 12.4s 
[wf] W17 PASS 2.2s 
[wf] W27 PASS 1.9s 
[wf] W18 PASS 0.9s 
[wf] W19 PASS 13.0s 
[wf] W20 PASS 1.6s 
[wf] W21 PASS 2.8s 
[wf] W22 PASS 3.2s 
[wf] W23 PASS 12.3s 
[wf] W24 PASS 38.6s 
[wf] W25 PASS 0.1s 
```

Observed fixture density in that run: W03 planted 49 half-cent line-tax ties, 12 half-cent freight-tax ties, 72
half-kilogram/dimensional boundary orders and 14 split-case orders (plus 117 random orders and 13 refusals); W06
paid 130 random orders (91 backordered and 72 cancelled lines) plus 10 orders whose lots sit at, one day under and one day
over the shelf-life floor across the next two clock changes; W16/W26 compared 17,154 ambient and 12,850 chilled,
frozen or quarantined legacy lots and FEFO over 30 legacy SKUs each; W17/W27 compared the valuation report for all
5,000 SKUs at 12 instants (fold hours, the skipped hour, same-instant receipts) and the ledgers of the legacy SKUs with movements in those hours (up to 120 and 60); the seed
values 114 SKUs at exactly half a cent.

## Calendar horizon

`valid_until = "2027-11-01"` (task.toml `[metadata]`). No check needs a fixed date to lie in the future: every ship
date, shelf-life threshold and clock change is derived from today and zoneinfo, and the fixed instants of W17/W27
are past legacy history, so nothing fails after this date. What fades is the discriminating power of FEFO over the
old system's lots (W16, W26): the seed dates their expiries around 2026-11-10 (they run to 2030-02-14). For a ship
date 12 days out and a 5-day shelf-life floor, the share of legacy SKUs (3+ lots) that still have two or more
eligible lots is 80% on 2026-11-10, 59% on 2027-05-01, 39% on 2027-11-01, 25% on 2028-05-01 and 11% on
2028-11-01; a lot within 10 days of the floor exists for 14%, 6%, 5%, 4% and 2% of them. After `valid_until`,
regenerate the seed with its dates moved forward.

## No-op agent

With the services up and no application, all 26 workflows fail at their first substep (0.6 s in total) and no breach is
recorded.

## Planted defects (each a copy of the oracle with one realistic bug)

| Mutant | Lever | Defect | Target | Result |
|---|---|---|---|---|
| m1_fefo_strict | allocation / FEFO | "at least N days of shelf life" read as "more than N days" (`expiry > ship + N`) | W06, W16, W26 | W06 FAIL (98 order fields), W16 FAIL, W26 pass (no threshold lot in that segment) |
| m10_dst_hours | allocation / temporal | shelf life counted as elapsed seconds between local midnights // 86400 instead of calendar days | W06 | W06 FAIL only at the clock-change threshold step (the 130 ordinary orders pass) |
| m2_greedy_route | pick path | nearest-neighbour route instead of the exact shortest tour | W09 | W09 FAIL (wave 1 route 174 vs the optimum) |
| m3_float_tax | money | line tax as `round(amount * float(rate))` (floats, banker's rounding) | W03, W12, W14 | all three FAIL (one cent off on half-cent ties) |
| m4_refund_portion | money / refunds | refund tax on the cancelled units alone instead of the difference of the line's tax | W07 | W07 FAIL (refund 3920 vs 3919) |
| m5_race | concurrency | payment allocation without the inventory lock (read, then write) | W08 | W08 FAIL, breach X13 (72 units reserved from a lot of 12) |
| m6_idor_shipment | tenancy / security | the shipment page does not check the viewer's company | W21 | W21 FAIL, breach X1 |
| m7_no_low_stock | missing secondary feature | no low-stock email | W19 | W19 FAIL |
| m8_fold_ignored | legacy time | the `fold` column is ignored | W17, W18, W27 | all three FAIL (271, 66 SKU rows; export placed_at) |
| m9_ship_button | UI contract | ship form shown while picks are still open | W12 | W12 FAIL |

Each mutant ran on a fresh database and fresh services with `--only` its target workflows.
