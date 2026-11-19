# Lotbook: our household's tax-lot tracker

Priya and Daniel (the Raman-Okafor household) invest through two brokers: Northwind Securities in the US
and Harbor Direct in Europe. We hold US, Canadian and euro securities, reinvest dividends, move positions
between accounts, and sometimes sell at a loss and buy back. Every spring the brokers' tax forms disagree
with each other and with us, mostly about wash sales across accounts, splits and currency conversion. We
are replacing the spreadsheet tool we used until 2025 with a web application we run ourselves. Priya's
sister's family (the Lindqvist household) and Priya's parents (the Castellano household) use the same
installation. Our financial advisor, Morgan Ellis, reads two of the households, and our tax preparer, Hannah
Brooks, reads two and closes their tax years. You are building the application from scratch in this
workspace.

The workspace is empty. The environment already runs two services: a PostgreSQL database holding our
households, people, accounts, securities, daily exchange rates, prices, and three years of transactions the
old tool recorded; and an SMTP server that accepts every email the application sends. Their connection
details are in `/etc/lotbook/services.env`, one `NAME=value` per line: `DATABASE_URL`, `PORT`,
`PUBLIC_BASE_URL`, `SMTP_HOST`, `SMTP_PORT`, `MAIL_FROM`, `RESET_LINK_TTL_SECONDS`, and `IMPORT_MAX_BYTES`.
The application is always started with these variables exported; read them from the environment at run
time. Each deployment starts the application with **different values** for the same variables and a fresh copy
of the same database, so nothing about hosts, ports, addresses, sizes or durations may be hard-coded.

Money is US cents (integers) unless this page says otherwise. Dates are `YYYY-MM-DD`.

## People and roles (R1)

- R1.1 Everyone who signs in is a row in `users`; only rows with `active = true` may sign in. `role` is
  one of five roles. `owner`, `member` and `viewer` belong to the household in `household_id`. `advisor`
  and `preparer` have no household (`household_id` is null); `assignments` lists the households each of
  them is assigned to.
- R1.2 A user *sees* their own household (owner, member, viewer) or their assigned households (advisor,
  preparer), and reads every page and export of a household they see. Exports are byte-for-byte the same
  whoever reads them, and pages show the same records and figures. What each role may change:

  | Change | owner | member | viewer | advisor | preparer |
  |---|---|---|---|---|---|
  | accounts, securities, statement imports, hand-entered transactions, transfers, lot-method changes, corporate actions (R3, R4, R13) | own household | own household | no | no | no |
  | lock a tax year (R8.3) | own household | no | no | no | assigned households |
  | their own display name (R11.4) | yes | yes | yes | no | no |

  Every user signs in and out and sets their own password through R2. A request for a change the table
  does not allow answers 403 and changes nothing, and no page shows a user a form or button for a change
  they may not make.
- R1.3 A signed-in user who requests a page, export or action of a household they do not see gets 403 and
  nothing changes. An account, security, lot or transaction belongs to the household of its account or
  security; whatever id a request names, the household it belongs to decides. A visitor who is not signed
  in is redirected to `/signin` (R2.6).

## Signing in (R2)

- R2.1 Nobody has a password yet. Anyone sets or resets one from `/password-reset` by entering an email
  address. If it belongs to an active user, one email goes to that address with a single-use link that
  starts with `PUBLIC_BASE_URL`, whatever `Host` header the request carried; the link opens a page that sets a new password (at least 10 characters).
  Whether or not the address is on file, the response has the same status code and the same message, and an
  unknown or inactive address receives no email.
- R2.2 A link works once, expires `RESET_LINK_TTL_SECONDS` seconds after it was requested, and stops working
  as soon as a newer link is requested for the same user. A used, expired or replaced link changes nothing
  and says the link is no longer valid; an unknown link answers 404. Setting a new password through a link
  signs out every existing session of that user.
- R2.3 Email addresses are matched case-insensitively and ignoring leading and trailing whitespace,
  everywhere (some are stored with odd capitalization and stray spaces).
- R2.4 Sign-in takes an email address and a password. A wrong password, an unknown address and an inactive
  user are refused with the same message and no session.
- R2.5 Signing in always starts a new session identifier; no identifier the browser held before signing in
  ever becomes signed in. Signing out ends the session on the server: a session cookie copied before
  sign-out no longer signs anyone in. Session cookies are `HttpOnly` and carry `SameSite=Lax` or
  `SameSite=Strict`. Passwords are stored only as salted hashes.
- R2.6 A visitor redirected to `/signin` (R1.3) is sent to `/signin?next=<the path and query they asked
  for>`, and the sign-in form carries that `next` value along. After a successful sign-in the response
  redirects to `next` only when it is a path on this site: it starts with `/`, its second character is
  neither `/` nor `\`, and it contains no control character (such as a tab, CR or LF, which browsers drop
  from addresses). Any other value (for example `//evil.example`, `/\evil.example`,
  `https:evil.example` or a full URL) redirects to `/` instead.

## Accounts, securities and manual entries (R3)

- R3.1 An owner or member adds an account to their household with a name (1 to 100 characters of any text), a broker
  (`northwind` or `harbor`), an account number (1 to 40 characters, surrounding spaces removed) and a lot
  method (`FIFO` or `HIFO`), and an account type (`taxable` or `ira`, R13.9; empty means `taxable`). A broker and
  account number already on file in any household is refused with 409 and nothing changes. The new account is a
  new row in `accounts`.
- R3.2 An owner or member adds a security to their household with a symbol (surrounding spaces removed, upper-cased,
  1 to 12 characters from `A`-`Z`, `0`-`9` and `.`), an ISIN (surrounding spaces removed, upper-cased, two
  letters, nine letters or digits, one digit), a name (1 to 200 characters of any text) and a currency
  (`USD`, `CAD` or `EUR`). A symbol or ISIN already used by a security of the same household is refused with
  409. The new security is a new row in `securities`.
- R3.3 An owner or member changes an account's lot method by recording a method (`FIFO` or `HIFO`) and an effective
  date: a new row in `lot_method_changes`. It decides the method of the account's sales from that date on
  (R6.4). Changes are never edited or removed.
- R3.4 An owner or member records a transaction by hand on an account: `kind` (`buy`, `sell`, `reinvest`, `dividend`
  or `split`), `trade_date`, `security_id` (a security of the same household), and
  - for `buy`, `sell`, `reinvest`: `quantity` (positive, at most 8 decimal places), `price` (at least 0, at
    most 6 decimal places) and `fee` (at least 0, at most 2 decimal places; empty means 0). Its amount in the
    security's currency is quantity × price + fee for a buy or reinvestment and quantity × price − fee for a
    sale, rounded to the cent (R5.2), and must be positive;
  - for `dividend`: `amount` (positive, at most 2 decimal places, in the security's currency);
  - for `split`: `ratio` written `N:M` (R6.3);
  - optionally `memo`, any text up to 500 characters.

  A sale may name specific lots (R6.4): for each chosen lot a field `lot_<lot id>` holding a quantity; empty
  or `0` fields are ignored. A request that breaks any of these rules, names a security or lot of another
  household, has no exchange rate (R5.1), or would leave any transaction short (R6.6) is refused with 422 and
  records nothing.
- R3.5 An owner or member records a transfer between two different accounts of the household: `from_account_id`,
  `to_account_id`, `security_id`, `trade_date`. A transfer moves every share of that security the source
  account holds at that point (R6.5). It is refused with 422 when it would leave any transaction short.

## Broker statements (R4)

Owners and members upload statement files on an account's page. The account's broker decides the format.

- R4.1 **Files.** A file is UTF-8 text, with or without a byte-order mark; lines end in CRLF or LF. Fields
  are separated by `,` (Northwind) or `;` (Harbor Direct); a field may be enclosed in double quotes, inside
  which the separator is literal and `""` is one quote. Everything before the header row is junk to ignore,
  except one line that names the account: `Account Number: <number>` (Northwind) or `Depot: <number>`
  (Harbor Direct). The number, surrounding spaces removed, must equal the account's `account_number`.
  Blank lines are skipped everywhere.
- R4.2 **Northwind Securities.** Header row, exactly:
  `Trade Date,Settlement Date,Activity,Symbol,Description,Quantity,Price,Commission,Net Amount,Currency,Reference`.
  A line whose first field starts with `Total` or that starts with `End of statement` is a footer and is
  skipped. Every other line after the header is a row of 11 fields:
  - Trade Date `MM/DD/YYYY`; Activity `BUY`, `SELL`, `DIVIDEND`, `REINVEST` or `SPLIT` (any letter case);
    Symbol names the security by its symbol (any letter case, surrounding spaces ignored).
  - Numbers use `,` between groups of three digits and `.` before decimals; a number in parentheses is
    negative: `"(1,200)"` is −1200.
  - BUY and REINVEST: Quantity positive, Net Amount negative. SELL: Quantity negative (the number of shares
    sold is its absolute value), Net Amount positive. DIVIDEND: Quantity empty, Net Amount positive. SPLIT:
    Quantity is the ratio `N:M`, Net Amount empty or zero.
  - The row's amount is the absolute value of Net Amount, which already includes the commission. Settlement
    Date, Price and Commission are informational and ignored.
- R4.3 **Harbor Direct.** Header row, exactly:
  `Booking date;Value date;Transaction;ISIN;Name;Shares;Price;Fees;Amount;Currency;Transaction no.`.
  A row whose first field is empty is a footer and is skipped. Every other line after the header is a row
  of 11 fields:
  - Booking date `DD.MM.YYYY` is the trade date; Transaction `Purchase`, `Sale`, `Dividend`,
    `Dividend reinvestment` or `Split` (any letter case); ISIN names the security by its ISIN.
  - Numbers use `.` between groups of three digits and `,` before decimals, and a leading `-` is negative:
    `1.200` is 1200 shares, `0,4375` is 0.4375, `-222.508,90` is −222508.90.
  - Purchase and Dividend reinvestment: Shares positive, Amount negative. Sale: Shares positive, Amount
    positive. Dividend: Shares empty, Amount positive. Split: Shares is the ratio `N:M`, Amount empty or
    zero. The row's amount is the absolute value of Amount; Value date, Price and Fees are ignored.
- R4.4 **Both formats.** Currency must equal the security's currency. The reference (`Reference`,
  `Transaction no.`) is 1 to 64 characters after removing surrounding spaces. Quantities have at most 8
  decimal places and amounts at most 2; neither may be zero. The description (`Description`, `Name`) is kept
  as the transaction's description and shown as text. Activities map to the kinds of R6: BUY/Purchase is
  `buy`, SELL/Sale `sell`, DIVIDEND/Dividend `dividend`, REINVEST/Dividend reinvestment `reinvest`,
  SPLIT/Split `split`.
- R4.5 **Duplicates.** The natural key of a transaction is its account and reference. A row whose key
  matches a transaction already recorded for the account (legacy transactions with that `ref` included)
  with the same content (kind, trade date, security, quantity, amount and ratio) is a *duplicate*: it is
  skipped and counted. The same key with different content is an error. Inside one file, a repeated
  reference with the same content is a duplicate, and with different content an error. Re-importing a file,
  or a statement that overlaps an earlier one, therefore never records anything twice. When two imports
  carrying the same rows arrive at the same moment, each row is still recorded once.
- R4.6 **All or nothing.** A file is checked in two passes. First every row on its own: wrong number of
  fields, unparseable or impossible date, unknown activity, unknown security, wrong currency, malformed or
  wrongly signed number, bad ratio, bad reference, key conflict (R4.5), or no exchange rate (R5.1). If any
  row fails, the file is rejected and every failing row is listed with its line number (the physical line
  in the file, the first line being 1) and a reason. Only when no row failed, the second pass adds the new
  rows to the household's history and checks R6.6; each transaction that would become short is listed, by
  its line number when it comes from the file, otherwise with line number 0. A problem with the whole file
  (no header row, missing or different account number, not UTF-8) is listed with line number 0. A rejected
  file records nothing, and the response answers 422.
- R4.7 An accepted file records its new rows, in file order, and answers with the number of rows recorded
  and the number of duplicates. A file of more than `IMPORT_MAX_BYTES` bytes is refused with 413 and
  changes nothing. The limit applies to the file's content, not to the multipart request around it.
- R4.8 The file's original name is kept only as text: the account page lists earlier imports with their
  original file names, and a file name is never used to build a path on disk.

Worked example 8 (parsing). The Northwind row
`03/02/2026,03/04/2026,SELL,MPLR,MAPLE RAIL,"(1,200)",88.1000,9.99,"105,710.01",CAD,NW26-000230` is a sale of
1200 shares of the security with symbol MPLR on 2026-03-02 for 105710.01 CAD, reference `NW26-000230`. The
Harbor row `14.03.2026;18.03.2026;Purchase;DE000RHC7341;Rhein Chemie AG;1.200;185,4200;4,90;-222.508,90;EUR;HD-2026-00412`
buys 1200 shares of the security with that ISIN on 2026-03-14 for 222508.90 EUR, and
`16.03.2026;16.03.2026;Dividend reinvestment;AT0000ALP551;Alpen Versicherung AG;0,4375;52,1600;0,00;-22,82;EUR;HD-2026-00419`
reinvests 22.82 EUR into 0.4375 shares.

## Money, exchange rates and rounding (R5)

- R5.1 **Rates.** The USD rate of a currency on a date is 1 for USD. For CAD and EUR it comes from the
  `fx_rates` row of that currency with the latest `rate_date` on or before the date (weekends, holidays and
  some other days have no row). A row whose `quote` is `usd_per_unit` gives US dollars per unit; a row whose
  `quote` is `units_per_usd` gives units per US dollar, so the rate is 1 divided by it. A date with no row on
  or before it has no rate.
- R5.2 **Rounding.** To *round* is to take the exact value to the nearest whole cent, a half cent rounding up.
  Nothing is rounded except where this page says so.
- R5.3 **USD values.** A transaction's USD value is its amount (in the security's currency) × the rate on its
  trade date, rounded once.
- R5.4 **Share rule.** When m units are taken out of something that carries C cents over a quantity Q, the
  part taken carries C × m / Q, rounded, and the rest carries C minus that over Q − m. Taking all of Q takes
  all of C. Every split of cents in R6 and R7 uses this rule.
- R5.5 Quantities are shown as exact decimals with no trailing zeros, no exponent and no thousands
  separator (`1200`, `0.4375`, `37.5`).

Worked example 1 (rates). A Harbor purchase dated Sunday 2025-03-09 for 4,321.40 EUR, when the latest EUR row
on or before that date is Friday 2025-03-07 with 1.085000 `usd_per_unit`: 4,321.40 × 1.085 = 4,688.719, so
the lot's basis is 468872 cents. A cash dividend of 18.40 CAD on 2024-05-15 when the CAD row is 1.360000
`units_per_usd`: 18.40 / 1.36 = 13.5294…, so 1353 cents. A buy entered by hand of 3 shares at 10.004 with a
fee of 1.25 has the amount 31.26 in the security's currency (31.262 rounded), and its USD value is that ×
the rate.

## Lots (R6)

Each household is computed on its own, over all its transactions: the legacy ones (R10.2) and those the
application records. Lots, splits, transfers and wash sales never cross households, and cross securities only
through corporate actions (R13).

- R6.1 **Order.** Every transaction has an id: a legacy transaction keeps its `legacy_transactions.id`; give
  every transaction the application records a new id greater than every id before it (rows of one file in
  file order). Transactions are applied in order of trade date, then kind (`split`, then corporate actions
  (R13), then `buy` and `reinvest`, then `transfer`, then `sell`, then `dividend`), then id; corporate actions
  of one date in order of their own id.
- R6.2 **Acquisitions.** A buy or a reinvestment creates a lot: its quantity, a basis equal to its USD value,
  an acquisition date and a holding-period start both equal to its trade date. The lot's id is the
  transaction's id. A cash dividend is income (its USD value) and changes no lot.
- R6.3 **Splits.** A split `N:M` on an account multiplies the quantity of every open lot of that security in
  that account by N/M exactly; basis, dates and lot ids stay. One of N and M is 1 and M has no prime factor
  other than 2 and 5 (`10:1`, `3:1`, `1:8`, `1:20`), so quantities stay exact decimals.
- R6.4 **Sales.** A sale takes its quantity from the account's open lots of that security. The *FIFO order*
  of lots is by acquisition date, then lot id; when a lot is divided (R7.3), the part divided off comes
  immediately before the rest. The account's method on the sale's trade date is that of the
  `lot_method_changes` row of the account with the latest `effective_from` on or before the date (the
  higher id on the same date), or else `accounts.lot_method`. `FIFO` takes lots in FIFO order; `HIFO` takes
  the highest basis per share first (basis ÷ quantity), equal values in FIFO order. A sale that
  names specific lots takes exactly the named quantity from each named lot id (from its parts in FIFO order)
  whatever the method. Each lot a sale takes from is a *portion*, and the portions are ordered as taken (for
  a specific-lot sale: in FIFO order). A portion of m shares from a lot takes its basis by the share rule;
  the sale's USD value (its proceeds) is spread over its portions by the share rule in portion order. A
  portion's gain is its proceeds minus its basis, plus any disallowed loss (R7).
- R6.5 **Transfers.** A transfer moves every open lot of that security from the source account to the
  destination account unchanged (quantity, basis, dates, lot id), so all lots of one acquisition are always
  in one account.
- R6.6 **Short.** A sale is short when, at its place in the order, the account holds fewer shares of the
  security than it sells, or a named lot does not hold the named quantity in that account; a transfer is
  short when the source account holds no shares of the security. The legacy data has no short transaction,
  and the application never records anything that makes any transaction short.
- R6.7 **Holding period.** A portion is long-term when the sale's trade date is later than the anniversary
  of the portion's holding-period start (the same month and day one year later; the anniversary of
  February 29 is February 28), otherwise short-term.

Worked example 2 (FIFO, share rule, holding period). A FIFO account buys 10 shares on 2024-03-04 for 100003
cents and 4 shares on 2024-06-03 for 50002 cents, then sells 12 shares on 2025-04-01 for 150002 cents. The
first portion is all 10 shares of the first lot: proceeds 150002 × 10/12 = 125001.67, rounded 125002; basis
100003; gain 24999; long-term (2025-04-01 is after the anniversary 2025-03-04). The second portion is 2 of
the 4 shares: proceeds 150002 − 125002 = 25000; basis 50002 × 2/4 = 25001; gain −1; short-term. The lot
keeps 2 shares and 50002 − 25001 = 25001 cents.

Worked example 3 (method change). An account bought 10 shares on 2025-01-10 (lot A, 1200000 cents), 10 on
2025-02-10 (lot B, 1500000) and 10 on 2025-03-10 (lot C, 1350000), and is FIFO with a change to HIFO
effective 2025-07-01. Selling 5 on 2025-06-30 for 700000 takes 5 of lot A (basis 600000, gain 100000).
Selling 12 on 2025-07-08 for 1680000 takes all of lot B (150000 cents per share) and then 2 of lot C
(135000 per share), with proceeds 1400000 and 280000: a loss of 100000 on lot B (no purchase within 30 days,
so nothing is disallowed) and a gain of 10000 on lot C.

Worked example 4 (splits and transfers). Account 1 buys 30 shares on 2024-03-04 for 300003 cents; a `10:1`
split on 2024-06-10 makes the lot 300 shares with the same basis and dates; a transfer on 2024-09-02 moves
it to account 2; a `1:8` split there on 2025-01-06 makes it 37.5 shares. Selling all 37.5 shares on
2025-03-05 for 412550 cents gives a long-term gain of 112547 (the holding period started on 2024-03-04).

## Wash sales (R7)

The result is defined over the household's whole history: a purchase made after a sale can change that
sale.

- R7.1 A *loss portion* is a portion whose proceeds are less than its basis; its loss is basis minus
  proceeds. When a sale is applied, all its portions are taken first (R6.4); then its loss portions are
  matched, in portion order.
- R7.2 **Replacement shares** for a loss portion of a sale on date D are shares of the same security
  acquired by a buy or a reinvestment, by any account of the household, with a trade date from D − 30 days to
  D + 30 days inclusive, except: shares acquired by the same transaction as the portion's own shares; shares
  that have already served as replacement shares once; and, for an acquisition dated on or before D, shares
  no longer held at that point (sold, including by this sale). Shares acquired after D count in full (less
  what earlier matches already claimed).
- R7.3 **Matching.** Walk the acquisitions in order of trade date, then id (earliest first); for one already
  applied, its open lots that have not served as replacement shares, in FIFO order. Each time, take
  m = the smaller of the portion's unmatched quantity and the shares available. The disallowed part of the
  loss for those m shares is the portion's unmatched loss × m / its unmatched quantity, rounded (the share
  rule over the loss). The m shares become their own lot, divided off by the share rule if they are only
  part of a lot (or of a later acquisition when it is applied); that lot's basis is increased by the
  disallowed part, its holding-period start moves back by the number of days from the sold shares'
  holding-period start to D, and it has served as replacement shares. Stop when the portion is fully
  matched or no replacement shares remain. The portion's disallowed loss is the sum of its disallowed parts,
  and its reported gain is proceeds − basis + disallowed loss.
- R7.4 Portions with a gain or no loss are never matched. No data the application is given, here or in any
  deployment, has a split of a security within 30 days before or after a loss sale of it, so you need not
  handle that case.

Worked example 5 (purchases after the sale). Account 1 buys 100 shares on 2025-01-06 for 500000 cents and
sells them all on 2025-03-03 for 400000: a loss of 100000, held 56 days. Account 2 buys 30 shares on
2025-03-20 for 123000, and account 1 buys 80 shares on 2025-03-24 for 336001 and 40 shares on 2025-04-17 for
170000. Matching: account 2's 30 shares first, disallowed 100000 × 30/100 = 30000; then 70 of the 80 shares,
disallowed 70000 × 70/70 = 70000. The whole loss is disallowed: the sale's row shows proceeds 400000, basis
500000, disallowed 100000, gain 0. Account 2's lot becomes 30 shares, basis 153000, holding-period start
2025-01-23 (2025-03-20 moved back 56 days). The 80-share lot is divided: 70 shares with basis 336001 × 70/80
= 294000.875, rounded 294001, plus 70000 = 364001, starting 2025-01-27; then 10 shares with 42000 starting
2025-03-24. The 40 shares bought on 2025-04-17, 45 days after the sale, are not replacement shares.

Worked example 6 (purchases before the sale, reinvestments). A HIFO account buys 20 shares on 2025-05-05
(lot A, 200000 cents) and 20 on 2025-05-20 (lot B, 260000). On 2025-06-02 it sells 10 for 110000: HIFO takes
10 of lot B (basis 130000), a loss of 20000. The other 10 shares of lot B came from the same transaction and
are not replacement shares, but lot A was bought within 30 days before: 10 of its shares absorb all 20000.
Lot A is divided into 10 shares with basis 100000 + 20000 = 120000 starting 2025-04-22 (2025-05-05 moved back
13 days), then 10 shares with 100000 starting 2025-05-05. On 2025-06-09 it sells 10 for 115000: HIFO takes
lot B's other 10 shares (13000 cents per share beats 12000), a loss of 15000, held 20 days. Lot A was bought 35
days earlier, so only a reinvestment of 50.31 USD into 0.4375 shares on 2025-06-16 in another account of
the household qualifies: disallowed 15000 × 0.4375/10 = 656.25, rounded 656; reported gain −14344. That
lot becomes 0.4375 shares with basis 5031 + 656 = 5687, starting 2025-05-27.

## Tax years, locks and the year-end email (R8)

- R8.1 **Figures.** For a tax year Y and a security, *short* is the sum of the reported gains of short-term
  portions whose sale is dated in Y, *long* the same for long-term portions, *disallowed* the sum of their
  disallowed losses, and *dividends* the sum of the USD values of the security's cash dividends dated in Y.
- R8.2 **Locked years.** A household's locked years are all the years up to its lock-through year L, if it
  has one. The old tool locked 2023 for the Raman-Okafor and Lindqvist households (`legacy_year_locks`): for
  them L starts as 2023, and the frozen 2023 figures of each security are those in `legacy_locked_figures`
  (a security without a row there has zero figures). The old tool never applied the wash-sale rule, so they
  differ from what R6 and R7 give for 2023.
- R8.3 **Locking.** An owner of the household, or a preparer assigned to it, locks year Y from the year's
  page (R1.2). It is allowed only when Y has ended (its
  31 December is before the current UTC date) and Y is L + 1, or the household has no lock-through year yet;
  otherwise it is refused with 409 and nothing changes. When two lock requests for one household arrive at
  the same moment, exactly one succeeds and the other is refused with 409. Locking Y sets L to Y; it
  freezes Y's reports and figures (and, when the household had no lock-through year, those of every earlier
  year as well): from then on the year page, `summary.csv` and `realized.csv` of a locked year never change,
  byte for byte, whatever is recorded later.
- R8.4 **Adjustments.** Later changes to a locked year appear in the first unlocked year, L + 1, and nowhere
  else. For each locked year Y, each security and each category (short, long, dividends), the adjustment is:
  the figure the rules give now − the frozen figure − the adjustments for the same year, security and
  category already frozen into locked years after Y. Adjustments are per security and category, summed over
  the locked years.
- R8.5 **Email.** When a year is locked, each active owner, member and viewer of the household receives
  exactly one email, within 30 seconds, and never again for that year whatever is retried or restarted. Its subject contains
  `Year-end summary <year>` and its plain-text body names the household and contains these five lines, the
  totals of the year's frozen `summary.csv` rows, written like `$1,234.56` and `-$12.00`:
  `Short-term gains: <amount>`, `Long-term gains: <amount>`, `Wash-sale loss disallowed: <amount>`,
  `Dividends: <amount>`, `Adjustments for earlier years: <amount>` (the sum of the three adjustment columns).
  The email's `To` header names the recipient by display name and address (`Elena Castellano
  <elena.castellano@lotbook.example>`), and its subject ends with `locked by <display name of the user who
  locked it>`. Locking earlier years together with Y (R8.3) sends nothing for them. Advisors and preparers
  receive no such email.

Worked example 7 (locks). A household with no lock buys 10 shares on 2024-02-01 for 100000 cents and sells
them on 2024-08-01 for 150000: 2024's short figure is 50000. It locks 2024. Then a missed buy of 10 shares on
2024-01-15 for 80000 is imported: FIFO now sells that lot instead, so the rules give 70000 for 2024. 2024's
reports still say 50000; 2025's summary carries `adj_short_term_cents` 20000 for the security. After 2025
is locked, a missed cash dividend of 12.34 USD dated 2024-11-15 is recorded: 2026's summary carries
`adj_dividends_cents` 1234, and no further short adjustment (70000 − 50000 − 20000 = 0).

## Corporate actions and retirement accounts (R13)

- R13.1 **Corporate actions.** A corporate action is a row of `corporate_actions` (the old tool's) or one recorded
  under R13.11 (stored there or in the app's own tables). It belongs to the household of its security and applies,
  on its `action_date`, to every account of the household holding that security at that point (R6.1), in order of
  account id, and in each account to its open lots in FIFO order; the rows it produces (R9.1) follow that order.
  Its `kind` is `spinoff`, `merger` or `return_of_capital`. Its prices and amounts are per share and in the
  currency of its security; a spun-off security and an acquirer have that same currency. To *convert* an amount
  below is to round it to the cent in that currency and then take its USD value (R5.3).
- R13.2 **Spin-off.** The action names the parent (`security_id`), the spun-off security (`new_security_id`),
  `ratio` (spun-off shares received per parent share), `parent_fmv` and `new_fmv` (the value of one share of each
  after the distribution) and `cash_in_lieu_price`. Its allocation fraction is a = ratio × new_fmv / (ratio ×
  new_fmv + parent_fmv), exact. For each lot of the parent (each part divided off under R7.3 is its own lot), the
  allocated basis is the lot's basis × a, rounded; the parent lot keeps its quantity, its dates and the rest of its
  basis.
- R13.3 The lot receives n = its quantity × ratio spun-off shares. The whole number w of them becomes a new lot;
  the fraction f = n − w is sold for cash in lieu on the action date. The fraction takes f/n of the allocated
  basis by the share rule (R5.4) and the new lot keeps the rest. The cash is f × `cash_in_lieu_price`,
  converted; the sale is a row of the spun-off security with that cash as proceeds, the fraction's basis, and
  the parent lot's holding-period start. When w is 0 there is no new lot; when f is 0 there is no sale.
- R13.4 A lot created by a spin-off or a merger keeps the original lot's lot id, acquisition date and
  holding-period start, whether its shares have served as replacement shares (R7.2), and whether its basis is
  marked as including a disallowed loss (R12.2). Its place in FIFO order follows from those (R6.4).
- R13.5 **Merger.** The action names the target (`security_id`), the acquirer (`acquirer_security_id`, empty
  for a cash-only merger), `shares_per_share`, `cash_per_share` and `acquirer_fmv` (the value of one acquirer
  share). For each lot of the target, with q shares and basis B: the cash C is q × `cash_per_share`, converted;
  the value V of the stock received is q × `shares_per_share` × `acquirer_fmv`, converted; the realized gain is
  G = C + V − B. The recognized gain R is the smaller of G and C when G is positive, and 0 otherwise: a loss is
  not recognized when stock is received. When C is positive the lot produces a row of the target with the lot's
  quantity q, proceeds C, basis C − R and gain R. The lot becomes a lot of the acquirer with q ×
  `shares_per_share` shares and basis B − C + R (R13.4). The target has no lots afterwards.
- R13.6 A cash-only merger (`shares_per_share` 0) sells each lot of the target: a row with proceeds C and basis B,
  so a loss is recognized.
- R13.7 **Return of capital.** The action names the security and `amount_per_share`. For each lot, the
  distribution is its quantity × `amount_per_share`, converted, and it reduces the lot's basis. When it is more
  than the basis, the basis becomes 0 and the excess is a row with the lot's quantity, proceeds equal to the
  excess and basis 0, short- or long-term from the lot's holding-period start. A return of capital is not a
  dividend (R8.1), and a basis never goes below 0.
- R13.8 **Order and wash sales.** Corporate actions of a date come after that date's splits and before its buys
  (R6.1), so a split and a spin-off on one date give spun-off shares for the post-split quantity. Rows produced by
  corporate actions count in R8 and R9 like the portions of a sale, their term running from the lot's
  holding-period start to the action date, but they are never matched by the wash-sale rule; lots created by a
  spin-off or a merger are not acquisitions, so they are never replacement shares (R7.2) in their own right (they
  keep the original lot's state, R13.4). No data the application is given, here or in any deployment, has a
  corporate action involving a security (as parent, spun-off security, target, acquirer or distributing
  security) within 30 days before or after a loss sale of that security.
- R13.9 **Retirement accounts.** `accounts.account_type` is `taxable` or `ira`. Lots in an IRA follow R6 and R13
  and appear on lots pages and in `unrealized.csv` like any lots, but nothing that happens in an IRA is reported:
  its sales, cash in lieu, corporate-action rows and dividends appear in no `realized.csv`, `summary.csv`, year
  figure or adjustment (R8, R9). A sale in an IRA is never matched by the wash-sale rule.
- R13.10 When replacement shares (R7.2) of a loss sale in a taxable account are in an IRA, their part of the loss
  is disallowed for good: the sale's row shows it as disallowed (and its gain includes it), and those IRA shares
  are divided off and have served as replacement shares (R7.3), but nothing is added to their basis and their
  holding-period start does not move. A transfer between an IRA and a taxable account is refused with 422.
- R13.11 **Recording corporate actions.** An owner or member records an action from
  `/households/<hid>/corporate-actions`. It gets a new id greater than every action id before it. It is refused
  with 422 and records nothing when a field is missing or malformed (`ratio` and `shares_per_share` have at most 8
  decimal places, the other numbers at most 6; `ratio`, `parent_fmv`, `new_fmv`, `cash_in_lieu_price` and
  `amount_per_share` are positive; a merger has a positive `cash_per_share` or a positive `shares_per_share`, and
  names an acquirer and a positive `acquirer_fmv` when `shares_per_share` is positive, and neither when it is 0),
  when a named security is not of the household, is the same security twice, or has another currency, when there
  is no exchange rate on its date (R5.1), or when it would leave any transaction short (R6.6).

Worked example 9 (spin-off). A FIFO account holds 10 shares of P bought on 2024-03-04 (basis 100003) and 7
shares bought on 2024-05-06 (basis 77000). A spin-off on 2024-08-05 gives 0.5 shares of N per share of P with
parent_fmv 90, new_fmv 20 and cash_in_lieu_price 21.50, all USD: a = 0.5 × 20 / (0.5 × 20 + 90) = 0.1. The first
lot allocates 10000.3, rounded 10000, and keeps 90003; it receives 5 shares of N, a new lot with basis 10000
acquired and held from 2024-03-04. The second allocates 7700 and keeps 69300; it receives 3.5 shares: the 0.5
share takes 7700 × 0.5/3.5 = 1100 and is sold for 0.5 × 21.50 = 10.75, a short-term row of N (proceeds 1075,
basis 1100, gain −25, acquired 2024-05-06), and the new lot is 3 shares with basis 6600.

Worked example 10 (merger). A lot of 10 shares of T bought on 2023-01-10 has basis 50000; another of 10 shares
bought on 2023-02-14 has basis 60000. On 2025-06-16 T merges into A for 0.5 shares of A plus 12.00 cash per
share, with acquirer_fmv 80, all USD. First lot: C = 12000, V = 40000, G = 2000, R = 2000: a long-term row of 10
shares with proceeds 12000, basis 10000 and gain 2000, and a lot of 5 shares of A with basis 50000 − 12000 + 2000 =
40000, acquired and held from 2023-01-10. Second lot: G = −8000, so R = 0: a long-term row of 10 shares with
proceeds 12000, basis 12000 and gain 0, and a lot of 5 shares of A with basis 48000.

Worked example 11 (return of capital). A lot of 100 shares bought on 2025-02-03 has basis 4000. A return of
capital of 0.25 per share on 2025-04-01 distributes 25.00: the basis becomes 1500. Another of 0.25 on
2025-07-01 distributes 25.00 against a basis of 1500: the basis becomes 0 and a short-term row of 100 shares has
proceeds 1000, basis 0 and gain 1000.

Worked example 12 (IRA replacement). A taxable account buys 10 shares on 2025-01-06 for 40000 and sells them on
2025-03-03 for 35000, a loss of 5000; an IRA of the household buys 10 shares on 2025-03-10 for 30000. The sale's
row shows disallowed 5000 and gain 0; the IRA lot keeps basis 30000 and its holding period starts on 2025-03-10.

## Reports and exports (R9)

- R9.1 `GET /households/<id>/reports/<year>/realized.csv`: one row per portion whose sale is dated in the
  year, and per row a corporate action produces in it (R13), ordered by date, then as R6.1 orders their sales and
  actions (a date's corporate actions before its sales), then by the sale's or action's id, then portion order
  (R13.1 for an action), with the header
  `sale_date,account,symbol,quantity,date_acquired,term,proceeds_cents,basis_cents,wash_disallowed_cents,gain_cents`.
  `account` is the account's name; `date_acquired` is the portion's holding-period start; `term` is `short`
  or `long`; `gain_cents` is the reported gain.
- R9.2 `GET /households/<id>/reports/<year>/summary.csv`: one row per security that has a portion or a cash
  dividend in the year, or a non-zero adjustment in it (a sale, corporate-action row or dividend in an IRA does not
  count, R13.9), ordered by symbol, with the header
  `symbol,name,short_term_gain_cents,long_term_gain_cents,wash_disallowed_cents,dividends_cents,adj_short_term_cents,adj_long_term_cents,adj_dividends_cents`.
  The first four amounts are the year's figures (R8.1), the last three its adjustments (R8.4). For 2023 of
  the households the old tool locked, the summary shows the legacy figures with zero adjustments and
  `realized.csv` shows what the rules give; those two files are informational only.
- R9.3 `GET /households/<id>/unrealized.csv`: one row per open lot (after every transaction) of a security
  that has a row in `security_prices`, ordered by symbol, then account id, then FIFO order, with the header
  `account,symbol,quantity,date_acquired,term,basis_cents,market_value_cents,unrealized_cents`. The price is
  the security's row with the latest `price_date`; market value is quantity × close × the rate on that
  `price_date`, rounded; unrealized is market value − basis; `term` is as R6.7 with the price date as the
  sale date; `date_acquired` is the holding-period start.
- R9.4 Every export is UTF-8 CSV: the exact header row, then the rows, each line ending in CRLF. A field that
  contains a comma, a double quote, CR or LF is enclosed in double quotes with inner quotes doubled. Amounts
  are plain integers, quantities as R5.5, dates `YYYY-MM-DD`.
- R9.5 Spreadsheet safety: in the text columns (`account`, `symbol`, `name`), a value that starts with `=`,
  `+`, `-`, `@`, a tab or CR is written with a single quote `'` in front of it.
- R9.6 Every page, export and import answers within 60 seconds with the data the database starts with.

## Existing data and deployment (R10)

- R10.1 The database already contains `households`, `users`, `assignments`, `accounts`,
  `lot_method_changes`, `securities`, `fx_rates`, `security_prices`, `legacy_transactions`,
  `corporate_actions`, `legacy_year_locks` and `legacy_locked_figures`. Inspect them with `psql`. Serve that data as it is: never
  drop, truncate, rename or empty those tables or columns, and never change or delete their existing rows,
  except that a user's `full_name` changes through `/profile`. Adding rows to `accounts`, `securities`,
  `lot_method_changes` and `corporate_actions` as R3 and R13 describe is expected (a recorded corporate action may
  instead be kept in your own tables, R13.1). You may add your own tables, columns and indexes, and the
  application must apply its own schema additions idempotently every time it starts.
- R10.2 Each `legacy_transactions` row is one transaction of `account_id`. `kind` is `buy`, `sell`,
  `dividend`, `reinvest`, `split` or `transfer` in any letter case with stray spaces. `quantity` and
  `amount` are in the security's currency and some rows store them negative: use their absolute values.
  `currency` repeats the security's currency in any letter case. A split has `split_new:split_old` as its
  ratio. A transfer moves from `account_id` to `to_account_id`, and its `quantity` is informational. `ref`
  is the broker reference (R4.5); it is null for transfers and some old manual entries.
- R10.3 The workspace root contains an executable `start.sh` that starts the application listening on
  `0.0.0.0` at `PORT`, in the foreground, until stopped, and a `README.md` naming the stack, how `start.sh`
  starts the app, and the environment variables it reads.
- R10.4 `GET /healthz` answers 200 only while the application can reach the database, and a non-2xx status
  within 10 seconds of the database stopping, each answer arriving within 5 seconds; once the database is
  back it answers 200 again within 60 seconds without a restart. It answers 200 within 60 seconds of
  `start.sh` starting.
- R10.5 Deployment copies the workspace to a clean machine with no network access, as regular files
  (symbolic links pointing outside the workspace are not kept). Every runtime dependency must already be in
  the workspace (for example a virtual environment made with `python -m venv --copies`, or `node_modules`),
  and `start.sh` must not download, install or build anything. Use only the two services in the services
  file; contact no other host. Everything the application stores survives it being stopped and started.

## Safety (R11)

- R11.1 Every request that changes anything from a signed-in browser page carries a token tied to the
  session that a page on another site cannot obtain; a request without the right token is refused with 403
  and changes nothing.
- R11.2 Text that people typed or uploaded (account and security names, descriptions, memos, file names) is
  always shown as text, on every page and in every attribute; it never becomes markup or script. The same
  holds for any part of a request (its path, query or form fields) that a page repeats, including not-found
  and error pages.
- R11.3 Households, accounts, amounts, roles and owners always come from the application's records and these
  rules; extra or altered form fields never change them (for example a `household_id` field on any form, or
  `role` on the profile form, is ignored).
- R11.4 An owner, member or viewer may change their own display name (`full_name`, any text of 1 to 120 characters) from
  `/profile`; that form changes nothing else.
- R11.5 Typed text never adds or changes an email header or recipient: wherever a display name or other
  typed text goes into a header (R8.5), every CR and LF in it is replaced by a space.
- R11.6 Passwords, reset tokens and session identifiers never appear in anything the application writes
  (its database, its files, its own output and logs), except the reset link inside the email it sends.
- R11.7 Every response to a signed-in user carries `Cache-Control: no-store`, and no page may be shown inside
  a frame on another site (`X-Frame-Options: DENY`, or a `Content-Security-Policy` with
  `frame-ancestors 'none'`).
- R11.8 An unexpected error or a malformed request is answered with a plain error page; it never shows a
  stack trace, database error text or file paths.

## Experience (R12)

Judged from what a person sees in a browser; colours, fonts, layout and wording are yours.

- R12.1 After an import, the page says plainly how many rows were recorded and how many were duplicates, or,
  when rejected, that nothing was imported, listing each failing line with its reason.
- R12.2 A lots page shows, per open lot, the quantity, basis, acquisition date, holding-period start and
  whether it is short- or long-term today, and marks lots whose basis includes a disallowed wash-sale loss.
- R12.3 The sale form explains that without chosen lots the account's method (FIFO or HIFO) applies.
- R12.4 A year page shows the short-term, long-term, disallowed, dividend and adjustment totals with labels,
  and says whether the year is locked. Before locking, it states that locking freezes the year's reports,
  that later changes appear as adjustments in the next year, that each person in the household gets a
  summary email, and that it cannot be undone.
- R12.5 Viewers, advisors and preparers can tell what their access allows (read-only, or reading and
  locking tax years).
- R12.6 Every page stays usable 1280 and 390 CSS pixels wide, with no horizontal scrolling of the page.

## Interface contract

The application is driven through a real browser and these paths, fields and markers. Pages may
carry any other content. `<hid>`, `<aid>` and `<sid>` are ids from the database.

| Path | Method | Purpose |
|---|---|---|
| `/password-reset` | GET, POST | field `email`; button `data-action="request-reset"` (R2.1) |
| `/password-reset/<token>` | GET, POST | field `password`; button `data-action="set-password"` |
| `/signin` | GET, POST | fields `email`, `password`; button `data-action="signin"` |
| `/signout` | POST | button `data-action="signout"` |
| `/` | GET | signed in: a link per household the user may see, each with `data-household-id` |
| `/profile` | GET, POST | field `full_name`; button `data-action="save-profile"` |
| `/households/<hid>` | GET | the household: each account inside an element with `data-account-id` and `data-account-type` (`taxable` or `ira`) |
| `/households/<hid>/accounts` | POST | fields `name`, `broker`, `account_number`, `lot_method`, `account_type`; button `data-action="create-account"` (form on the household page); success redirects (303) to `/accounts/<aid>` |
| `/households/<hid>/securities` | GET, POST | fields `symbol`, `isin`, `name`, `currency`; button `data-action="create-security"`; each security inside an element with `data-security-id` and `data-symbol` |
| `/households/<hid>/corporate-actions` | GET, POST | the household's corporate actions, each inside an element with `data-action-id`, `data-kind`, `data-action-date` and `data-symbol` (the parent, target or distributing security); the form (R13.11) has fields `kind`, `security_id`, `action_date`, and for `spinoff` `new_security_id`, `ratio`, `parent_fmv`, `new_fmv`, `cash_in_lieu_price`; for `merger` `acquirer_security_id` (empty for cash only), `shares_per_share` (0 for cash only), `cash_per_share`, `acquirer_fmv`; for `return_of_capital` `amount_per_share`; fields that do not belong to the kind are ignored; button `data-action="record-corporate-action"`; success redirects (303) to the same page |
| `/households/<hid>/transfers` | GET, POST | fields `from_account_id`, `to_account_id`, `security_id`, `trade_date`; button `data-action="record-transfer"` |
| `/accounts/<aid>` | GET | the account: `data-account-id`; each transaction inside an element with `data-txn-id`, `data-kind`, `data-trade-date`, `data-symbol`, `data-ref` (empty when none); each earlier import inside an element with `data-import-filename`; the upload form |
| `/accounts/<aid>/imports` | POST | multipart field `file`; button `data-action="import-statement"` (form on the account page) |
| `/accounts/<aid>/transactions/new` | GET | the entry form (button `data-action="record-transaction"`); with `?kind=sell&security_id=<sid>` the form starts as a sale of that security and lists that security's open lots in the account, each with an input `name="lot_<lot id>"` inside the form, and fields `kind`, `trade_date`, `security_id`, `quantity`, `price`, `fee`, `amount`, `ratio`, `memo` (R3.4) |
| `/accounts/<aid>/transactions` | POST | R3.4 fields (the entry page's form posts here); success redirects (303) to `/accounts/<aid>` |
| `/accounts/<aid>/lots` | GET | open lots, each inside an element with `data-lot-id`, `data-symbol`, `data-quantity`, `data-acquired`, `data-holding-start`, `data-basis-cents` and `data-wash-adjusted="true\|false"` |
| `/accounts/<aid>/method` | POST | fields `lot_method`, `effective_from`; button `data-action="change-method"` (form on the account page) |
| `/households/<hid>/reports/<year>` | GET | the year page (R12.4) |
| `/households/<hid>/reports/<year>/lock` | POST | button `data-action="lock-year"` (form on the year page) |
| `/households/<hid>/reports/<year>/summary.csv` | GET | R9.2 |
| `/households/<hid>/reports/<year>/realized.csv` | GET | R9.1 |
| `/households/<hid>/unrealized.csv` | GET | R9.3 |
| `/healthz` | GET | R10.4 |

Every form that changes something includes, inside the form, its session token as a hidden `<input>` with
the attribute `data-csrf`; whatever its `name`, it is submitted unchanged with the form.

Markers, as HTML attributes:

- An import's result page carries one element with `data-import-status="imported"` and
  `data-imported-count` and `data-duplicate-count`, or `data-import-status="rejected"` containing one
  element per problem with `data-error-line="<line number>"`.
- A year page carries one element with `data-year`, `data-locked="true|false"`, `data-total-short-cents`,
  `data-total-long-cents`, `data-total-disallowed-cents`, `data-total-dividends-cents` and
  `data-total-adjustment-cents` (the totals of its `summary.csv`); while lockable by the signed-in user, a
  button `data-action="lock-year"` inside a form posting to the lock path.
- When signed in, the sign-out button has `data-action="signout"`.
