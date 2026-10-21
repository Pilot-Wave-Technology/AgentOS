#!/usr/bin/env bash
# migrations/check_sequence.sh
#
# Validates that migrations/SEQUENCE is consistent with the .sql files on disk.
#
# Requires nothing beyond bash and standard Unix tools (grep, sort, comm) —
# all of which are available via Git Bash on Windows, macOS, or any Linux.
# No Python, no pip install, no project setup needed.
#
# Run locally before pushing:
#   bash migrations/check_sequence.sh
#
# Also runs as the first Cloud Build step in all pipelines (feature, dev, main)
# before apply.py touches the database. Fails fast on any inconsistency.
#
# Checks:
#   1. migrations/SEQUENCE exists
#   2. No duplicate entries in SEQUENCE
#   3. Every name in SEQUENCE has a corresponding .sql or .py file
#   4. Every .sql and .py file in migrations/ appears in SEQUENCE (no orphans)
#
# Exit codes:
#   0 — all checks pass
#   1 — validation failed (details printed to stderr)

set -euo pipefail

MIGRATIONS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SEQUENCE_FILE="$MIGRATIONS_DIR/SEQUENCE"
ERRORS=0

# ── Helper ────────────────────────────────────────────────────────────────────
fail() {
    echo "  ✗ $1" >&2
    ERRORS=$((ERRORS + 1))
}

# ── 1. SEQUENCE must exist ────────────────────────────────────────────────────
if [ ! -f "$SEQUENCE_FILE" ]; then
    echo "FATAL: migrations/SEQUENCE not found." >&2
    exit 1
fi

# ── 2. Parse SEQUENCE — strip blank lines and comments ───────────────────────
# Produces a clean list of migration names, one per line.
SEQUENCE=$(grep -v '^\s*#' "$SEQUENCE_FILE" | grep -v '^\s*$' || true)

if [ -z "$SEQUENCE" ]; then
    echo "FATAL: migrations/SEQUENCE is empty or contains only comments." >&2
    exit 1
fi

SEQUENCE_COUNT=$(echo "$SEQUENCE" | wc -l | tr -d ' ')

# ── 3. No duplicates in SEQUENCE ─────────────────────────────────────────────
DUPLICATES=$(echo "$SEQUENCE" | sort | uniq -d || true)
if [ -n "$DUPLICATES" ]; then
    while IFS= read -r name; do
        fail "Duplicate entry in SEQUENCE: '$name'"
    done <<< "$DUPLICATES"
fi

# ── 4. Every SEQUENCE entry has a corresponding .sql or .py file ─────────────
while IFS= read -r name; do
    if [ ! -f "$MIGRATIONS_DIR/$name.sql" ] && [ ! -f "$MIGRATIONS_DIR/$name.py" ]; then
        fail "SEQUENCE references '$name' but neither $name.sql nor $name.py exists."
    fi
done <<< "$SEQUENCE"

# ── 5. Every .sql and .py file appears in SEQUENCE (no orphans) ──────────────
# Collect all migration filenames (without extension), sorted.
# Excludes apply.py and check_sequence.sh which live in the same directory.
MIGRATION_FILES=$(
    for f in "$MIGRATIONS_DIR"/*.sql "$MIGRATIONS_DIR"/*.py; do
        [ -f "$f" ] || continue
        base=$(basename "$f")
        # Skip the runner and utility scripts themselves — not migrations.
        [ "$base" = "apply.py" ] && continue
        [ "$base" = "reset_db.py" ] && continue
        # `_name.py` is a helper apply.py imports, never a migration.
        case "$base" in _*) continue ;; esac
        [ "$base" = "check_sequence.sh" ] && continue
        basename "$f" | sed 's/\.[^.]*$//'
    done | sort
)

SEQUENCE_SORTED=$(echo "$SEQUENCE" | sort)

# comm -23 = lines only in MIGRATION_FILES (not in SEQUENCE) — these are orphans.
ORPHANS=$(comm -23 <(echo "$MIGRATION_FILES") <(echo "$SEQUENCE_SORTED") || true)
if [ -n "$ORPHANS" ]; then
    while IFS= read -r name; do
        fail "'$name' exists but is not listed in SEQUENCE. Add it at the correct position."
    done <<< "$ORPHANS"
fi

# ── Report ────────────────────────────────────────────────────────────────────
if [ "$ERRORS" -gt 0 ]; then
    echo "" >&2
    echo "migrations/SEQUENCE validation FAILED ($ERRORS error(s))." >&2
    echo "Fix the issues above, then re-run: bash migrations/check_sequence.sh" >&2
    exit 1
fi

echo "migrations/SEQUENCE OK — $SEQUENCE_COUNT migrations in declared order."
