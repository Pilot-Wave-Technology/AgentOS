#!/usr/bin/env bash
# The test suite, against a database built from SEQUENCE.
#
# WHY THIS EXISTS. Cloud Build ran three `check_*` guards and no pytest at
# all -- a suite of ~1,800 tests that gated nothing. Two of the defects it
# would have caught were already in the tree: a test whose fixture read a
# migration deleted eight commits earlier (pytest reports a missing
# fixture file as an ERROR, not a failure, so it stopped RUNNING rather
# than stopped passing), and a guard comparing the kernel's tool surface
# against the gateway's that resolved a path only a laptop has.
#
# RUNS INSIDE THE `test` IMAGE with the checkout bind-mounted. The image
# is `docker build --target test`, so the packages the tests run against
# are the packages the app runs with. That parity is the point: the same
# week this was written, python-multipart was correctly pinned in
# requirements.txt and absent from a developer's interpreter, and it
# presented as a test-collection error that reads like a test problem.
# The tests themselves come from the mount, because .dockerignore keeps
# them out of the image on purpose and the production image should stay
# that way.
#
# Usage (from the repo root, inside the test image):
#   INTERLOCK_TEST_DSN=postgresql://... scripts/ci/run_suite.sh
set -euo pipefail

DSN="${INTERLOCK_TEST_DSN:?INTERLOCK_TEST_DSN must be set}"

# Test roots. NOT infra/e0/ci -- that is the chaos suite, which needs a
# live E0 stack with a docker socket and is a different gate. NOT
# infra/e0/corpus either: those are fixture repositories that happen to
# contain files named test_*.
# `packages` carries agentos-ledger-client's own suite. Without it the
# package shipped ~440 lines of tests that were never collected, which
# is the failure the mass-skip guard below exists to catch.
ROOTS="${AGENTOS_TEST_ROOTS:-app kernel tests packages}"

# A per-test deadline. A hung test and a slow one look identical while a
# build is running -- the step emits nothing and the BUILD's timeout kills
# it, with the log ending mid-suite naming nothing. `thread` because the
# default SIGALRM method does not exist on Windows, and a deadline that
# behaves differently where developers reproduce failures is worse than
# none.
TIMEOUT_S="${AGENTOS_TEST_TIMEOUT_S:-300}"

# THE MASS-SKIP GUARD, and the reason this script is not just a pytest
# invocation. Every database-backed test in this repo calls a
# `_connect_or_skip()` helper: with no reachable database it SKIPS. So a
# CI job whose database failed to start does not fail -- it goes green
# having run a fraction of the suite, and reports a number nobody reads
# as suspicious. The measured floor is 34 skips with a healthy database
# and several hundred without one, so anything past this ceiling means
# the database, not the tests.
MAX_SKIPS="${AGENTOS_MAX_SKIPS:-120}"

# Relative to the working directory, which is the mounted checkout --
# so the report lands in /workspace on the build host and later steps
# can read it.
REPORT="${AGENTOS_TEST_REPORT:-pytest-report.xml}"

# PARALLELISM, OFF BY DEFAULT AND DELIBERATELY SO.
#
# Empty means no `-n` reaches pytest and this script runs exactly the
# command it ran before pytest-xdist was installed. Installing the
# plugin changes nothing on its own; only this variable does.
#
# It is off in CI because CI hands the suite a DSN it did not create
# (`INTERLOCK_TEST_DSN`, the `test-db` step's container). Down that
# path conftest creates no database, so every worker would share one
# and corrupt each other's fixtures -- conftest raises rather than let
# that happen, which would turn this into a hard failure, not a
# speed-up. Making CI parallel means giving each worker its own
# database on that server first; that is a separate change.
#
# Locally, with no DSN set, the embedded server stamps one database
# per worker from the shared template and `AGENTOS_TEST_WORKERS=auto`
# is the whole opt-in.
# Two tests carry `no_parallel` and are deselected here -- a fixed TCP
# port and a shared asyncpg connection, neither worth redesigning a
# working test over. They DO run in CI, because CI is serial.
# AN ARRAY, because the marker expression contains a space: an
# unquoted "$PARALLEL" would split `-m "not no_parallel"` into three
# words and pytest would read `no_parallel` as a test path.
WORKERS="${AGENTOS_TEST_WORKERS:-}"
PARALLEL=()
if [ -n "$WORKERS" ]; then
    PARALLEL=(-n "$WORKERS" -m "not no_parallel")
    echo "--- parallel: -n $WORKERS (deselecting no_parallel)"
fi

echo "--- waiting for ${DSN##*@}"
python - "$DSN" <<'PROBE'
import asyncio
import sys
import time

import asyncpg


async def probe(dsn):
    conn = await asyncpg.connect(dsn)
    await conn.close()


dsn, deadline, last = sys.argv[1], time.time() + 120, ""
while time.time() < deadline:
    try:
        asyncio.run(probe(dsn))
        print("database is up")
        break
    except Exception as exc:                                    # noqa: BLE001
        last = f"{type(exc).__name__}: {exc}"
        time.sleep(2)
else:
    sys.exit(f"database never accepted a connection -- {last}")
PROBE

echo "--- applying migrations"
python migrations/apply.py "$DSN"

echo "--- pytest ($ROOTS)"
set +e
python -m pytest $ROOTS \
    -q -p no:cacheprovider \
    ${PARALLEL[@]+"${PARALLEL[@]}"} \
    --timeout="$TIMEOUT_S" --timeout-method=thread \
    --junitxml="$REPORT" \
    -rf
rc=$?
set -e

# Read the skip count off the report rather than the summary line, which
# omits "skipped" entirely when there are none.
skips=$(python - "$REPORT" <<'COUNT'
import sys
import xml.etree.ElementTree as ET

try:
    root = ET.parse(sys.argv[1]).getroot()
except Exception:                                               # noqa: BLE001
    print(-1)
    raise SystemExit
suite = root if root.tag == "testsuite" else root.find("testsuite")
print(int(suite.get("skipped", 0)) if suite is not None else -1)
COUNT
)

echo "--- skipped: $skips (ceiling $MAX_SKIPS)"
if [ "$skips" -lt 0 ]; then
    echo "could not read the junit report; treating as a failure" >&2
    exit 1
fi
if [ "$skips" -gt "$MAX_SKIPS" ]; then
    echo "TOO MANY SKIPS: $skips > $MAX_SKIPS." >&2
    echo "The database-backed tests skip when they cannot reach a" >&2
    echo "database, so this is almost always an unreachable DSN and not" >&2
    echo "a change in the tests. A green build here would mean a" >&2
    echo "fraction of the suite ran." >&2
    exit 1
fi

exit "$rc"
