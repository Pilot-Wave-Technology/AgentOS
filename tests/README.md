# `tests/` — the sweeps that make raw SQL safe

Most of AgentOS's tests live beside the code they test, under
`app/services/tests/` and `kernel/*/tests/`. **This directory is different.** It
holds whole-codebase sweeps: tests that read every file and would be meaningless
scoped to one module.

```bash
pytest                                  # everything
pytest tests/                           # the sweeps only
AGENTOS_TEST_WORKERS=4 pytest           # parallel, per-worker databases
```

A PostgreSQL is required. `conftest.py` starts an embedded one and stamps the
schema from `migrations/SEQUENCE`; point `DATABASE_URL` at your own to use that
instead. Tests that need a database and cannot find one **skip** rather than
fail, so a clone with no Postgres still gives a useful run.

---

## Why these exist

There is no ORM. Around **5,700** table references are written as raw SQL, and
that is a deliberate choice — but it gives up the one thing an ORM reliably
buys: the compiler knowing whether a column exists. These sweeps buy it back.

| Sweep | Asks |
|---|---|
| `test_every_query_prepares.py` | does **every SQL literal in the codebase** parse and resolve against the real schema? |
| `test_row_keys_exist.py` | does every `row["key"]` name a column the query beside it actually selects? |
| `test_sql_values_are_parameterised.py` | is any value interpolated into SQL instead of passed as a parameter? |
| `test_table_lists_do_not_drift.py` | do the hand-maintained table lists still match the schema? |

### The PREPARE sweep is the important one

PostgreSQL resolves table, column and function names at PREPARE time. So
PREPAREing a statement — without executing it — asks the database whether every
name in it is real. Roughly **3,300** literals are collected and checked on
every run.

It found three live bugs on the day it was written, each of the same shape: a
column that had been renamed, in a query on a path nobody had exercised since.
That is a defect class which previously reached production and surfaced as a
500 from one endpoint.

Control-plane SQL is prepared on a plain connection; kernel SQL on one with
`search_path=kernel,public`, because that is how each is really executed.

**Collection is itself asserted.** The sweep fails if it collects fewer than
2,000 literals — because the failure mode of a collector is finding nothing and
reporting success.

### The analysers are tested in both directions

`sql_literals.py` and `sql_row_keys.py` are the AST collectors; `test_row_keys_guard.py`
feeds them code that *should* be flagged and code that should not. A guard that
currently reports nothing is indistinguishable from a guard that cannot report
anything, and these report nothing today.

---

## Running in parallel

`AGENTOS_TEST_WORKERS=N` enables `pytest-xdist` with a database per worker,
stamped from a shared template rather than migrated N times. It is **opt-in**:
unset, the suite runs exactly as it always did, which is what CI relies on.

Two tests are marked `no_parallel` because they assert on global state.

---

## Writing a sweep

The pattern is: a collector module that walks the AST, plus a test that runs it
across the tree and fails on findings. Two rules worth copying:

1. **Assert the collection size.** A sweep that silently collects nothing passes
   forever.
2. **Unit-test the collector on known-bad input.** Otherwise you are testing
   that the codebase is clean, not that the sweep works.

Suppress a genuine false positive with a `sql: safe` marker on the line or
within the few lines above it. There is no `#` in the marker, so it works inside
a SQL comment as well as a Python one.
