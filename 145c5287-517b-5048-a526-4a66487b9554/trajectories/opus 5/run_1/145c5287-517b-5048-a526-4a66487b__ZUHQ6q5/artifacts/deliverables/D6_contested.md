## TXN-1009

This row posted on 2026-03-16 for -9950 cents (SALT AND CEDAR), and it is the
one row in the statement that two different receipts were eligible to claim. It
was matched to RCP-0008 (Salt and Cedar, meals, 99.10, dated 2026-03-17), which
gives the pair an amount delta of -40 cents and a date delta of 1 day.

The other eligible receipt was RCP-0011 (Covent Row Catering, meals, 99.45,
dated 2026-03-15). Against this row RCP-0011 is the better fit on both
tie-breaks: its amount delta is -5 cents against RCP-0008's -40, and its date
delta is -1 day against RCP-0008's 1 day. A reviewer looking only at this row
would expect RCP-0011 to win it, and that is exactly why this row is the most
disputable in the file.

It did not win it because the rules maximise the number of matched pairs before
any tie-break is consulted. RCP-0008 is eligible for no row other than
TXN-1009: the only other statement amount within 50 cents of 9910 is TXN-1007
at 9900, and that row posted on 2026-03-12, five days from the receipt's
2026-03-17, outside the three-day window. TXN-1007, for its part, has RCP-0011
as its only eligible receipt. So awarding TXN-1009 to RCP-0011 would strand
both RCP-0008 and TXN-1007 and yield ten matched pairs, whereas awarding it to
RCP-0008 and sending RCP-0011 to TXN-1007 yields eleven. Eleven is the maximum,
the maximum is unique here, and the tie-breaks therefore never come into play:
they only choose among matchings that are already of maximum size.

## TXN-1007

This row posted on 2026-03-12 for +9900 cents and is described as REFUND
NORTHWIND AIR. It is the only credit in the statement, and it was matched to
RCP-0011 (Covent Row Catering, meals, 99.45, dated 2026-03-15), with an amount
delta of 45 cents and a date delta of 3 days.

Nothing else was eligible for it. RCP-0008 (99.10, 2026-03-17) is the only
other receipt whose total comes within the 50-cent tolerance of 9900 cents, and
it sits five days away from this posting date, so the three-day window excludes
it. RCP-0011 itself was eligible for TXN-1009 as well, and that is the
competition this row won, for the reason given in the section above: the
eleven-pair matching requires RCP-0011 here.

A reviewer is likely to dispute the pair on grounds the rules do not model. The
row is an airline refund, a credit, while the receipt is a catering charge with
no plausible relation to it, and the vendor names share nothing. The pair also
sits at both margins of eligibility at once: 45 cents of the 50 allowed, and
exactly 3 days of the 3-day window, so a tolerance of 44 cents or a window of 2
days would have dissolved it. It survives only because eligibility is tested on
the absolute value of the row amount and on date proximity, and never on sign,
description or vendor.

## TXN-1011

This row posted on 2026-03-20 for -77300 cents (UNMATCHED ACH DEBIT) and was
matched to nothing. It is the sole entry in the unreceipted list.

The receipt a reviewer would reach for is RCP-0012 (Thornbury Removals, other,
610.00, dated 2026-03-26), the sole entry in the unposted list. Being the only
two leftovers in the reconciliation, they look like each other's missing half,
and no other receipt is anywhere near this row's magnitude.

The pair is not eligible, and it fails both eligibility tests rather than one.
On amount, the receipt is 61000 cents against a row magnitude of 77300, a
difference of -16300 cents, which is some 326 times the 50-cent tolerance. On
date, 2026-03-26 is six days after 2026-03-20, twice the three-day window.
Because eligibility is a precondition for matching, no tie-break or
maximum-matching consideration can rescue the pair: TXN-1011 stays unreceipted
and RCP-0012 stays unposted, and the resemblance between them is only that each
is the last one standing on its own side.
