## RCP-0005

This receipt is categorised `meals` and its total is 8100 cents, which is what
the reconciliation carries: the pair sits against TXN-1005 at -8100 cents with
an amount delta of 0. The limit `category_limits_cents` sets for `meals` is
7500 cents.

The pair violates policy. The rule asks whether the receipt total in cents
strictly exceeds the limit for the receipt's own category, and 8100 is greater
than 7500, so the test is met and `violates_policy` is true in
reconciliation.json. The total exceeds the meals limit by 600 cents.

## RCP-0008

This receipt is categorised `meals` and its total is 9910 cents; the
reconciliation pairs it with TXN-1009 at -9950 cents and an amount delta of -40,
which is the same 9910. The limit for `meals` is 7500 cents.

The pair violates policy, because 9910 strictly exceeds 7500. The total runs
over the meals limit by 2410 cents.

## RCP-0011

This receipt is categorised `meals` and its total is 9945 cents; the
reconciliation pairs it with the credit TXN-1007 at 9900 cents with an amount
delta of 45, which again gives 9945. The limit for `meals` is 7500 cents.

The pair violates policy: 9945 is strictly greater than 7500, so the rule fires
and `violates_policy` is true. The total exceeds the meals limit by 2445 cents.
Note that the policy test looks only at the receipt's own total and category, so
the fact that the matched row is a refund credit rather than a charge makes no
difference to the verdict.

## RCP-0001

This receipt is categorised `travel` and its total is 48200 cents; the
reconciliation pairs it with TXN-1001 at -48200 cents with an amount delta of 0.
The limit `category_limits_cents` sets for `travel` is 50000 cents.

The pair does not violate policy. 48200 does not strictly exceed 50000 — it is
below it — so the rule does not fire and `violates_policy` is false. The total
falls short of the travel limit by 1800 cents, which is the room the pair has
left before it would have become a violation.
