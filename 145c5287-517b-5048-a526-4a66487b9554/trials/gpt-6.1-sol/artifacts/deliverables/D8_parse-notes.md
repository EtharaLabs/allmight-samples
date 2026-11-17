## RCP-0003

This receipt exercises the rule that a VENDOR value can contain a colon and only the first colon separates the field name from its value. A reader splitting at every colon and keeping only the next segment would produce the wrong VENDOR "Heliotype Software". The correct VENDOR is "Heliotype Software: Annual Plan"; the later colon is part of the value.

## RCP-0004

This receipt exercises the rule that additional non-field lines are ignored. A reader treating every line as a field would invent spurious fields named "Thank you for your business." and "Invoice generated automatically; retain for records.". Both lines are ignored. The correct CATEGORY remains "other" and the correct VENDOR remains "Carter & Voss LLP"; neither extra line supplies a field value.

## RCP-0007

This receipt exercises the rule that a VENDOR value can contain a colon and only the first colon separates the field name from its value. A reader splitting at every colon and keeping only the next segment would produce the wrong VENDOR "Meridian Coach Lines". The correct VENDOR is "Meridian Coach Lines: Route 42"; the later colon is part of the value.
