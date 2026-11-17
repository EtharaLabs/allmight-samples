## collapse

The absolute-value rule admits 12 eligible pairs; comparison with the signed amount admits 1. The maximum matching contains 11 pairs under the absolute-value rule and 1 under the signed rule. The surviving eligible pairs under signed comparison are: RCP-0011 with TXN-1007. These are also all pairs in its maximum matching.

## divergence

The rows whose eligible receipt sets differ are TXN-1001, TXN-1002, TXN-1003, TXN-1004, TXN-1005, TXN-1006, TXN-1008, TXN-1009, TXN-1010, TXN-1012. The rows whose sets are the same are TXN-1007, TXN-1011. TXN-1011 has an empty eligible set under both conventions; TXN-1007 has RCP-0011 under both. For a differing row, TXN-1001 has amount_cents -48200 and RCP-0001 totals 48200 cents, on the same date. The absolute-value difference is |48200 - |-48200|| = 0 cents, within the 50-cent tolerance. The signed-comparison difference is |48200 - (-48200)| = 96400 cents, above 50 cents. For a coinciding row, TXN-1007 has amount_cents +9900 and RCP-0011 totals 9945 cents, three days later. The absolute-value difference is |9945 - |9900|| = 45 cents, and the signed-comparison difference is |9945 - 9900| = 45 cents; both are within 50 cents.
