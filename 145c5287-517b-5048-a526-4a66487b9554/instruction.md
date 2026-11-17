# Build the Ledgerline reconciliation review application

You are working in an empty `/app`. Build a working web application that
reconciles two independent records of the same expenses and presents the result
for human review, then produce the nine deliverables named at the end of this
document.

The service ingests a bank statement exported as CSV and a set of receipt
documents. The two sources disagree in ways this specification defines, and
resolving those disagreements correctly is the task. The application then has
to show the resolved reconciliation on screen so a reviewer can read it.

## Stack

Use exactly this stack. It is installed in the image already; do not add
runtime dependencies.

- Python 3.12. The standard library is available, and so is `uvicorn` with
  `starlette` for serving the application. No other third-party package is
  present and none may be installed.
- PostgreSQL 16, running in this container and reachable at
  `postgresql://ledgerline:ledgerline@localhost:5432/ledgerline`. The `psql`
  client is on `PATH`, and the server is already started with the `ledgerline`
  role and database created.

Do not reach the network. Everything you need is on disk or in the database.

## Inputs

Three input trees exist under `/app/inputs/` when you start.

### `/app/inputs/statement.csv`

A bank statement with a header row and these columns, in this order:

```
txn_id,posted_date,description,amount_cents,currency
```

- `txn_id` is an opaque string, unique within the file.
- `posted_date` is `YYYY-MM-DD`.
- `amount_cents` is a signed integer. Debits are negative, credits positive.
- `currency` is a three-letter ISO code. All rows are `USD`.

### `/app/inputs/receipts/*.txt`

One plain-text receipt per file. Each receipt carries these fields, one per
line, in any order, with the field name and value separated by a colon and
optional surrounding whitespace:

```
RECEIPT-ID: <opaque string>
DATE: <YYYY-MM-DD>
VENDOR: <free text, may contain colons>
TOTAL: <decimal with exactly two fraction digits, unsigned>
CATEGORY: <one of: travel, meals, software, hardware, other>
```

A receipt may carry additional lines that are not field lines. Ignore them.
A `VENDOR` value may itself contain a colon; only the first colon on a line
separates the field name from its value.

### `/app/inputs/policy.json`

```json
{
  "match_window_days": 3,
  "amount_tolerance_cents": 50,
  "category_limits_cents": { "meals": 7500, "travel": 50000 }
}
```

Read every parameter from this file. Do not hard-code its values.

## Reconciliation rules

Match each receipt to at most one statement row, and each statement row to at
most one receipt.

A receipt and a statement row are **eligible** to match when both hold:

1. The absolute difference between the receipt `TOTAL` converted to cents and
   the absolute value of the row `amount_cents` is at most
   `amount_tolerance_cents`.
2. The difference between `DATE` and `posted_date` is at most
   `match_window_days` days, in either direction.

Among eligible pairs, choose matches to **maximise the number of matched
pairs**. Where several maximum-size matchings exist, break ties by preferring,
in order:

1. the smaller absolute amount difference,
2. then the smaller absolute date difference,
3. then the lexicographically smaller `RECEIPT-ID`.

A statement row with no eligible receipt is **unreceipted**. A receipt with no
eligible statement row is **unposted**.

### Policy violations

For each matched pair, the pair **violates policy** when its receipt
`CATEGORY` appears in `category_limits_cents` and the receipt `TOTAL` in cents
strictly exceeds the limit for that category. A category absent from
`category_limits_cents` has no limit and never violates.

## The application

Serve the application over HTTP on `127.0.0.1:8080`. It has two screens.

### The review screen, at `/`

One page carrying the whole reconciliation:

- A table whose element `id` is `matched-table`, holding one row per matched
  pair. Each row carries a `data-receipt-id` attribute equal to that pair's
  `RECEIPT-ID`, and exactly six cells in this order: receipt id, txn id,
  amount delta in cents, date delta in days, category, policy state. The
  policy-state cell carries the class `state`, and its text reads exactly
  `violation` for a pair that violates policy and exactly `compliant` for one
  that does not. Rows appear in ascending `data-receipt-id` order.
- A list whose element `id` is `unreceipted-list`, holding one entry per
  unreceipted `txn_id`.
- A list whose element `id` is `unposted-list`, holding one entry per unposted
  `RECEIPT-ID`.
- Four elements carrying the four counts, with `id` values `totals-matched`,
  `totals-unreceipted`, `totals-unposted` and `totals-violations`.

### The detail screen, at `/pair/<receipt_id>`

One page per matched pair, reachable from its row on the review screen. It
carries that pair's six values under the element `id` values
`pair-receipt-id`, `pair-txn-id`, `pair-amount-delta`, `pair-date-delta`,
`pair-category` and `pair-policy-state`. `pair-policy-state` reads exactly
`violation` or `compliant`, as on the review screen.

### How the screens must render

These obligations are about what a browser computes, not about what your
markup says. A reviewer reads the rendered page, so the rendered page is what
matters. All of them are measured at a viewport of exactly 1280 by 800
device-independent pixels.

1. **A policy violation is legible as text weight.** On every screen, the
   policy-state cell of a pair that violates policy resolves to a computed
   `font-weight` of `700`, and the policy-state cell of a pair that does not
   resolves to `400`. Colour, an icon, or position may carry the distinction
   as well, but never instead: a reviewer who cannot perceive colour must
   still read which pairs violate.
2. **Every cell named above renders in a font the image ships.** For each element
   named above, `document.fonts.check` must return true at that element's own
   computed `font-weight`, `font-size` and `font-family`. Reference no font by
   URL: nothing is fetched at render time.
3. **The matched table's columns are aligned.** Within `matched-table`, every
   cell of one column shares a left edge with every other cell of that column
   to within one device-independent pixel.

## Deliverables

Write exactly these nine deliverables. Nothing else is read.

### 1. `/app/out/reconciliation.json`

```json
{
  "matched": [
    {
      "receipt_id": "<RECEIPT-ID>",
      "txn_id": "<txn_id>",
      "amount_delta_cents": 0,
      "date_delta_days": 0,
      "category": "meals",
      "violates_policy": false
    }
  ],
  "unreceipted": ["<txn_id>"],
  "unposted": ["<RECEIPT-ID>"],
  "totals": {
    "matched_count": 0,
    "unreceipted_count": 0,
    "unposted_count": 0,
    "violation_count": 0
  }
}
```

- `amount_delta_cents` is the receipt total in cents minus the absolute value
  of the row amount in cents. It is signed.
- `date_delta_days` is the receipt date minus the posted date, in whole days.
  It is signed.
- `matched` is sorted by `receipt_id` ascending.
- `unreceipted` and `unposted` are each sorted ascending.
- Every count in `totals` equals the length of the list it describes, except
  `violation_count`, which counts matched pairs with `violates_policy` true.

### 2. The `reconciliation` table in PostgreSQL

Create and populate a table named `reconciliation` in the `ledgerline`
database with exactly these columns and types:

```sql
receipt_id      text    not null
txn_id          text    not null
amount_delta_cents  integer not null
date_delta_days     integer not null
category        text    not null
violates_policy boolean not null
```

One row per matched pair. The table's contents must agree with the `matched`
array of `reconciliation.json` exactly.

### 3. `/app/out/summary.txt`

Four lines, in this order, with no leading or trailing whitespace on any line
and exactly one space after each colon:

```
matched: <matched_count>
unreceipted: <unreceipted_count>
unposted: <unposted_count>
violations: <violation_count>
```

### 4. `/app/out/snapshot.sql`

A plain-text dump of the `reconciliation` table produced by `pg_dump` or by
`psql` with `--command "copy reconciliation to stdout"`. This file is read
rather than the live database, so a correct table with a missing
or stale `snapshot.sql` counts as if the table were absent. Regenerate it after
the final write to the table.

### 5. `/app/start.sh`

An executable script that starts the application and leaves it serving. It
must:

- bring the application up on `127.0.0.1:8080`, serving `/` and
  `/pair/<receipt_id>` as described above;
- require no argument and no network access;
- invoke the interpreter as `/usr/bin/python3` explicitly, rather than as a
  bare `python3` or `/usr/bin/env python3`. The container that runs the application
  places a virtual environment earlier on `PATH`, and `uvicorn` is installed
  only against the system interpreter, so a bare `python3` resolves to an
  interpreter that cannot import it and the application will not start;
- run from a clean container in which only `/app` and the input tree exist,
  so anything it needs must either be in the image already or be produced by
  it;
- keep running in the foreground until killed.

`/app/start.sh` is run, `/` is polled until it answers, and a browser is then
driven against the running application. No file you wrote describing what your
screens look like is ever read. An application that does not answer
in time counts as if it were absent, whatever your other
deliverables contain.

Every asset a screen needs must be served by the application itself. Loading
any screen must issue no request to an origin other than the application's
own, including requests a script composes at runtime.

### 6. `/app/out/contested.md`

A written account, in English prose, of the three statement rows whose matching
a reviewer is most likely to dispute. Exactly three sections, each opening with
a line of the form:

```
## <txn_id>
```

Under each heading, write prose that states all three of the following about
that row:

1. which receipt it was matched to, or that it was matched to none;
2. which other receipt or rows it was eligible to match instead, naming them;
3. why the matching rules resolved it the way they did rather than the other
   way, in terms of the rules in this document and the values in the inputs.

Choose the three rows yourself. Write what the reconciliation you computed
actually did and why, not what the rules say in general. A section that names
the right identifiers while asserting a relation between them that your own
`reconciliation.json` contradicts is wrong, and so is a section whose stated
reason is not the reason these inputs make true.

Prose, not a table and not JSON. No length is prescribed: say what is true
about the row and stop. This deliverable is read as an account a colleague
would act on, so a reader who knows the rules but not your run must be able to
check your reasoning against the inputs from your words alone.

### 7. `/app/out/policy-notes.md`

A written account, in English prose, of the policy decision on every matched
pair that violates policy, and of one matched pair that does not. One section
per pair, each opening with a line of the form:

```
## <receipt_id>
```

Under each heading, write prose that states all three of the following about
that pair:

1. the receipt's `CATEGORY` and its `TOTAL` in cents, as your own
   `reconciliation.json` carries them;
2. the limit `category_limits_cents` sets for that category, or that the
   category appears in no limit at all;
3. whether the pair violates policy and why that follows from the two numbers
   above under the rule in this document;
4. by how many cents the total exceeds that limit, or falls short of it, stated
   as a number of cents.

Order the sections by `receipt_id`, ascending, and put the non-violating pair
last. Name the category's own limit, never another category's: a section that
compares a `meals` total against the `travel` limit is wrong even where its
verdict happens to agree with `reconciliation.json`. A section whose stated
relation between the total and the limit contradicts its own verdict is wrong,
and so is a section asserting a limit that `policy.json` does not set for that
category.

The figure you state for the fourth item must be the arithmetic difference
between the two numbers that section names. A section that states the right
relation, reaches the right verdict, and then reports a difference those two
numbers do not have is wrong. Phrase it however reads naturally; no particular
wording is required.

Prose, not a table and not JSON. No length is prescribed.

### 8. `/app/out/parse-notes.md`

A written account, in English prose, of every receipt in `/app/inputs/receipts/`
whose bytes a careless reader of the format rules above would get wrong. One
section per such receipt, each opening with a line of the form:

```
## <receipt_id>
```

Order the sections by `receipt_id`, ascending. Under each heading, write prose
that states all three of the following about that receipt:

1. which of this document's two format rules its bytes exercise — that a
   `VENDOR` value may itself contain a colon and only the first colon on a line
   separates name from value, or that a receipt may carry additional lines that
   are not field lines and they are ignored;
2. what a reader who ignored that rule would have produced for this receipt,
   quoting the wrong value or naming the spurious field;
3. what the correct reading is, quoting the value the rule yields.

Attribute a rule to a receipt. It is not enough to list which receipts are
awkward: each section must say which of the two rules *that* receipt exercises,
and a section that names the right receipt while attributing the wrong rule to
it is wrong even where it names both rules somewhere in the document. A section
whose stated wrong reading is not what the careless parse would actually
produce is also wrong, and so is one whose stated correct reading is not the
value the rule yields.

Do not discuss a receipt whose bytes exercise neither rule.

Prose, not a table and not JSON. No length is prescribed.

### 9. `/app/out/convention-notes.md`

The first eligibility rule above compares the receipt `TOTAL` against the
**absolute value** of the row `amount_cents`. A reader who dropped that
absolute value and compared against the signed amount instead would build a
different eligibility graph from the same inputs.

Write an account, in English prose, of what that mistake would cost. The
account has exactly two sections, in this order:

```
## collapse
## divergence
```

Under `## collapse`, state all three of the following:

1. how many eligible pairs the absolute-value rule admits over these inputs,
   and how many the signed comparison would admit;
2. how many pairs the maximum matching contains under each rule;
3. which pairs survive the signed comparison, naming each surviving
   `receipt_id` and `txn_id`.

Under `## divergence`, state all three of the following:

1. every `txn_id` whose set of eligible receipts differs between the two
   rules, naming each one;
2. every `txn_id` whose set is the same under both rules, naming each one;
3. for one row of your choice from each group, the arithmetic that puts it
   there — the receipt total, the row `amount_cents`, the difference each rule
   computes, and how each compares against `amount_tolerance_cents`.

Name the rows. It is not enough to say that the conventions differ on debits
and agree on credits: the `divergence` section must name each diverging
`txn_id` and each coinciding one, because an account that states only the
general shape cannot be checked against these inputs. A section that names the
right rows while asserting a collapse those rows do not produce is wrong, and
so is one that states the collapse correctly while naming the wrong rows as
diverging.

Report what these inputs make true, not what sounds plausible. Which rule leaves
more pairs standing, and which rows survive, follow from the signs in
`statement.csv` and the tolerance in `policy.json`, and an account that has the
direction backwards is wrong however fluently it reasons.

Prose, not a table and not JSON. No length is prescribed.

## Done

You are done when all nine deliverables exist and agree with each other: the
JSON, the table, the summary, the dump, the served screens, the written
account, the policy notes, the parse notes and the convention notes all
describe one reconciliation of one set of inputs. The application's internal
structure does not matter, only
these outputs and how its screens render when a browser drives them.
