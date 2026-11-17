## TXN-1007

This row is matched to RCP-0011. Its receipt total is 9945 cents, compared with the row's absolute amount of 9900 cents; the absolute amount difference is 45 cents and the absolute date difference is 3 day(s). RCP-0011 is its only eligible receipt, and that receipt is also eligible for TXN-1009. RCP-0008 is five days from TXN-1007, beyond the three-day window, so cannot replace it here. Assigning RCP-0011 to TXN-1009 would leave TXN-1007 unmatched. Maximising cardinality therefore puts RCP-0011 here and RCP-0008 at TXN-1009, even though RCP-0011 has a smaller amount difference at TXN-1009 (5 rather than 45 cents).

## TXN-1009

This row is matched to RCP-0008. Its receipt total is 9910 cents, compared with the row's absolute amount of 9950 cents; the absolute amount difference is 40 cents and the absolute date difference is 1 day(s). This row could instead match RCP-0011: its 9945-cent total differs by 5 cents and its March 15 date is one day before the March 16 posting. RCP-0008 totals 9910 cents and is dated March 17. Both candidates pass the 50-cent and three-day limits. The smaller amount difference would favour RCP-0011 if cardinality were equal, but RCP-0011 is the only candidate for TXN-1007. RCP-0008 cannot match TXN-1007 because its date is five days away. The chosen assignment matches both rows; the alternative matches only one of them.

## TXN-1012

This row is matched to RCP-0010. Its receipt total is 1840 cents, compared with the row's absolute amount of 1875 cents; the absolute amount difference is 35 cents and the absolute date difference is 1 day(s). There is no other eligible receipt for this row, and RCP-0010 has no other eligible statement row. The 1840-cent receipt dated March 22 is 35 cents below the 1875-cent magnitude posted March 21, within the 50-cent tolerance and three-day window. Leaving this pair unmatched would reduce cardinality by one; no tie-break is needed.
