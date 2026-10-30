## collapse

Over these inputs the absolute-value rule admits 12 eligible pairs. The signed
comparison admits 1. The reason is the sign convention of the statement:
eleven of the twelve rows are debits carried as negative `amount_cents`, and a
receipt `TOTAL` is always unsigned, so comparing an unsigned total against a
negative amount computes roughly twice the transaction value instead of
something near zero. With `amount_tolerance_cents` at 50, every such
comparison fails by a wide margin.

The maximum matching contains 11 pairs under the absolute-value rule and 1 pair
under the signed comparison. The absolute-value rule is therefore the one that
leaves pairs standing; dropping the absolute value does not shift matches
around, it destroys almost all of them, and the reconciliation would report one
matched pair, eleven unreceipted rows and eleven unposted receipts.

Exactly one pair survives the signed comparison: RCP-0011 against TXN-1007.
That pair survives because TXN-1007 is the statement's only credit, posted at
+9900 cents, so its signed amount and its absolute value are the same number
and the two rules compute the same difference for it. It is also the only pair
in the signed graph at all, so it is both the whole eligible set and the whole
maximum matching under that rule. Every pair the absolute-value rule finds on a
debit row — RCP-0001/TXN-1001, RCP-0002/TXN-1002, RCP-0003/TXN-1003,
RCP-0004/TXN-1004, RCP-0005/TXN-1005, RCP-0006/TXN-1006, RCP-0007/TXN-1008,
RCP-0008/TXN-1009, RCP-0011/TXN-1009, RCP-0009/TXN-1010 and
RCP-0010/TXN-1012 — vanishes.

## divergence

Ten rows have a different set of eligible receipts under the two rules, and in
every case the set goes from non-empty to empty: TXN-1001 loses RCP-0001,
TXN-1002 loses RCP-0002, TXN-1003 loses RCP-0003, TXN-1004 loses RCP-0004,
TXN-1005 loses RCP-0005, TXN-1006 loses RCP-0006, TXN-1008 loses RCP-0007,
TXN-1009 loses both RCP-0008 and RCP-0011, TXN-1010 loses RCP-0009, and
TXN-1012 loses RCP-0010. These are precisely the debit rows that had any
eligible receipt to lose.

Two rows have the same set under both rules. TXN-1007 has the set
{RCP-0011} under either rule, and TXN-1011 has the empty set under either rule.
TXN-1007 coincides because it is a credit, so there is no sign to drop.
TXN-1011 coincides for the opposite reason: it is a debit, but no receipt comes
near it on either convention, so there is nothing for the change of rule to
take away. The nearest candidate, RCP-0012 at 61000 cents, is 16300 cents from
its magnitude of 77300 and 138300 cents from its signed amount of -77300; both
figures exceed the 50-cent tolerance, and the dates are six days apart in any
case.

Take TXN-1005 as the worked example from the diverging group. Its
`amount_cents` is -8100 and the receipt in question, RCP-0005, has a total of
8100 cents. The absolute-value rule computes 8100 - |-8100| = 0, and 0 is at or
below the tolerance of 50, so the pair is eligible. The signed comparison
computes 8100 - (-8100) = 16200, whose absolute value is 16200; that is 324
times the tolerance of 50, so the pair is not eligible. The date test is
satisfied either way, the two dates both being 2026-03-09, so the sign
convention alone decides this row.

Take TXN-1007 as the worked example from the coinciding group. Its
`amount_cents` is +9900 and RCP-0011 has a total of 9945 cents. The
absolute-value rule computes 9945 - |9900| = 45; the signed comparison computes
9945 - 9900 = 45. The two differences are identical because the row amount is
positive, and 45 is at or below the tolerance of 50, so the pair is eligible
under both rules. The dates, 2026-03-15 for the receipt and 2026-03-12 for the
row, are 3 days apart and so within the 3-day window, again under both rules.
