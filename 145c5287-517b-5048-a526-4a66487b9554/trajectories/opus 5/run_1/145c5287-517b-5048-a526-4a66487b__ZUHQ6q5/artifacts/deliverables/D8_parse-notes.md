## RCP-0003

This receipt exercises the rule that a `VENDOR` value may itself contain a
colon, and that only the first colon on a line separates the field name from
its value. Its vendor line reads `VENDOR: Heliotype Software: Annual Plan`, so
the line carries two colons where every other field line in the file carries
one.

A reader who ignored the rule and split the line on every colon would keep only
the fragment between the first and the second, producing the vendor value
`Heliotype Software` and silently dropping `Annual Plan`; the same careless
split taken from the other end yields `Annual Plan` and loses the company name
instead. A stricter reader who insisted that a field line contain exactly one
colon would reject the line outright and report the receipt as having no vendor
at all.

The correct reading splits at the first colon only, which gives the vendor
`Heliotype Software: Annual Plan`, colon and all. Nothing else on this receipt
is affected: `RECEIPT-ID` is `RCP-0003`, `TOTAL` is `120.00` and `CATEGORY` is
`software` under either reading, which is what makes the error easy to miss —
it corrupts a value that the matching rules never look at, and so it shows up
only when a human reads the vendor back.

## RCP-0004

This receipt exercises the other rule: a receipt may carry additional lines
that are not field lines, and those lines are ignored. After its five field
lines it carries two more, `Thank you for your business.` and `Invoice
generated automatically; retain for records.`, neither of which is a field
line.

A reader who ignored the rule and treated every line in the file as a field
line would turn those two lines into spurious fields. Because neither line
contains a colon, there is no name to separate from a value, so the careless
parse produces a field named by the whole line with an empty value — a field
`Thank you for your business.` and a field `Invoice generated automatically;
retain for records.` — or, in a parser that assumes a colon is present and
indexes blindly past it, fails on the line and loses the receipt altogether. A
reader who guessed that the second line's semicolon was the separator would
invent a field named `Invoice generated automatically` with the value `retain
for records.`.

The correct reading ignores both trailing lines, leaving exactly the five
fields: `RECEIPT-ID` is `RCP-0004`, `DATE` is `2026-03-07`, `VENDOR` is
`Carter & Voss LLP`, `TOTAL` is `315.00` and `CATEGORY` is `other`. The
ampersand in the vendor is ordinary free text and raises no question of its
own.

## RCP-0007

This receipt exercises the same rule as RCP-0003: a `VENDOR` value may contain
a colon, and only the first colon on the line separates name from value. Its
vendor line reads `VENDOR: Meridian Coach Lines: Route 42`.

A reader who split on every colon and kept the second field would produce the
vendor `Meridian Coach Lines`, dropping the route that distinguishes this
service; keeping the trailing fragment instead would produce the bare `Route
42`, which names no vendor at all. Here the truncated reading is particularly
plausible-looking, because `Meridian Coach Lines` is a perfectly ordinary
vendor name and matches the statement description MERIDIAN COACH LINES, so
nothing about the wrong value looks wrong.

The correct reading is the whole remainder of the line after the first colon:
`Meridian Coach Lines: Route 42`. The receipt's other fields are unambiguous —
`TOTAL` is `54.00`, `DATE` is `2026-03-15`, `CATEGORY` is `travel` — and the
pair it forms with TXN-1008 is unaffected either way.
