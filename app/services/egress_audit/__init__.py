"""Measuring the egress allowlist against what installs actually reach.

WHY IT IS MEASURED AND NOT REVIEWED. Every entry on the list was added
because something failed: node-gyp's headers, the credential broker on
`.run.app`, the ecosystems a language-blind registry could declare and
then not fetch. Each was found by a story failing in a way that read as
something else -- "an authentication failure rather than a blocked
host". A list grown that way has two defects nobody can see by reading
it: hosts an install reaches that are NOT on it (the next such failure,
already in flight) and entries nothing has reached in months (reach
granted for no reason).

WHY IT RECURS. A single measurement drifts the moment the image or a
registry row moves -- and this phase moves both. So the runner exits
non-zero on an unknown host and is meant to run after every change to
either, not once.

WHAT IT CANNOT SEE is stated in the report itself, because "the
allowlist is measured" would otherwise overclaim. `report.BLIND_SPOTS`
is that list and the report prints it every time.
"""
