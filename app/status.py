"""Check status vocabulary. A leaf module with no imports, on purpose.

Both the scoring engine and the check runner need these four strings, and they
sit on opposite sides of the app.checks package boundary: scoring imports the
check definitions, and the check base imports scoring's statuses. Whichever
module loads first sees the other half-initialized. Constants with no
dependencies break the cycle.
"""

PASS = "pass"
FAIL = "fail"
SKIPPED = "skipped"
ERROR = "error"

VALID_STATUSES = frozenset({PASS, FAIL, SKIPPED, ERROR})

# Skipped and errored both mean "we did not measure this". They are recorded
# separately because one is a policy outcome and the other is a defect, but
# they leave the scoring denominator identically.
NOT_MEASURED = frozenset({SKIPPED, ERROR})
