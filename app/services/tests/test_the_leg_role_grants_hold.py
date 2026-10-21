"""The execution pod's database role stays narrow, and stays passwordless.

`agentos_leg` is what the Cloud SQL proxy sidecar authenticates as once
Phase D moves the model leg into the execution pod (plan section 10).
Its grant list IS the security model -- the same sentence
`kernel_roles_grants_seed.sql` writes about itself -- and three things
about it are load-bearing in a way no other test watches:

1. THE GRANT LIST MUST COVER WHAT THE LEG ACTUALLY DOES. It has failed
   this twice, each time for a narrower version of the same reason.
   `kernel_execution_pod_leg_role.sql` derived the list from the leg's
   own bookkeeping and missed `public.resource_publishes`, which
   `publish_artifact` INSERTs from inside the leg
   (`executor_worker_dispatch.py:125` -> `harness/publish.py:127`):
   A LEG IS ALSO EVERY TOOL IT DISPATCHES.
   `kernel_leg_role_publish_grant.sql` is that fix. Then
   `kernel_leg_role_settled_tree.sql` found three more --
   `kernel.env_deltas`, `public.stories`, `public.goal_decisions` --
   because two worker tools have their BODIES outside
   `kernel/harness/`, in `kernel/verifier/` and `kernel/knowledge/`,
   which every sweep so far had scoped itself out of. All of it is
   asserted here so a re-derivation cannot quietly drop any of it.

2. `agentos_leg` MUST NOT BE A MEMBER OF `agentos_worker`. Plan 10a:
   `loopbridge._as_worker` runs `SET ROLE agentos_worker` on every pooled
   connection, so the pod's FIRST failure is "permission denied to set
   role", not "permission denied for table". Granting the membership
   makes that error go away and takes the whole boundary with it -- the
   superset includes the ledger and the credentials tables. The fix is
   D2's pool, not a grant. This assertion is what stops somebody
   unblocking themselves with one line.

3. NO MIGRATION MAY CARRY THE PASSWORD. The role is created LOGIN with
   none, which is what makes it safe for Cloud Build to apply everywhere:
   inert until a human runs one `ALTER ROLE` out of band. A password
   committed here would be in git history forever.

Text-level, deliberately: these files are applied by Cloud Build against
a database this test does not have, and the property being defended is
what the repository SAYS, which is exactly what a reviewer reads.
"""
from __future__ import annotations

import pathlib
import re

from app.services.tests.shipped_tree import IS_DISTRIBUTION, baseline_schema

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_MIGRATIONS = _ROOT / "migrations"

# Strip `--` comments to end of line before scanning for executable SQL.
# Both leg-role files QUOTE the `ALTER ROLE ... PASSWORD` command a human
# owes, in a comment, on purpose -- so a scan that did not strip comments
# would fail on the documentation of the very rule it is checking.
_COMMENT = re.compile(r"--[^\n]*")


def _statements(path: pathlib.Path) -> str:
    return _COMMENT.sub(" ", path.read_text(encoding="utf-8"))


def _all_migrations() -> list:
    return sorted(_MIGRATIONS.glob("*.sql"))


def _leg_grants() -> dict:
    """{object: {privileges}} across every migration that grants the leg."""
    grants: dict = {}
    pattern = re.compile(
        r"GRANT\s+(?P<privs>[A-Za-z ,]+?)\s+ON\s+(?P<obj>[A-Za-z0-9_.]+)"
        r"\s+TO\s+agentos_leg\b",
        re.IGNORECASE)
    for path in _all_migrations():
        for match in pattern.finditer(_statements(path)):
            privs = {p.strip().upper()
                     for p in match.group("privs").split(",") if p.strip()}
            grants.setdefault(match.group("obj").lower(), set()).update(privs)
    return grants


def test_the_leg_may_write_the_row_publish_artifact_returns():
    """`INSERT ... RETURNING id` needs SELECT as well as INSERT.

    Postgres requires SELECT on every column named in RETURNING, so a
    bare INSERT grant fails at runtime on a list that reads as complete.
    `kernel_worker_public_reads.sql` grants `agentos_worker` the same
    pair for the same statement.
    """
    grants = _leg_grants()
    privs = grants.get("public.resource_publishes", set())
    assert {"SELECT", "INSERT"} <= privs, (
        "agentos_leg needs SELECT and INSERT on public.resource_publishes: "
        "publish_artifact is the leg's only road back for a non-git "
        "resource and it runs INSERT ... RETURNING id "
        f"(harness/publish.py:127). Found: {sorted(privs) or 'nothing'}")


def test_the_leg_keeps_the_reads_the_first_derivation_got_wrong():
    """Section 10's list described the LOCK connection, not the leg.

    These are the corrections 10a records. `step_journal` UPDATE is
    `result_ref` written back for an oversized tool result;
    `execution_checkpoints` is how a leg after the first rehydrates
    rather than silently forgetting; `verdicts` SELECT is what stops one
    approval standing for the life of a run (errand 187). Asserted
    because each was absent once and none of them fails loudly.
    """
    grants = _leg_grants()
    required = {
        "kernel.step_journal": {"SELECT", "INSERT", "UPDATE"},
        "kernel.execution_checkpoints": {"SELECT", "INSERT"},
        "kernel.verdicts": {"SELECT"},
        "kernel.executions": {"SELECT", "UPDATE"},
    }
    for table, needed in required.items():
        assert needed <= grants.get(table, set()), (
            f"agentos_leg lost grants on {table}: needs {sorted(needed)}, "
            f"has {sorted(grants.get(table, set())) or 'nothing'}")


def test_the_leg_writes_no_verdict_and_mints_no_grant():
    """The refusals are as load-bearing as the grants.

    Declaring the work good is the Verifier's act, and
    `kernel.errand_schedules` UPDATE would let a pod re-pin the standing
    grant it is currently spending (`park.py::pin_plan_to_grant` is
    conductor-side, reached only from `resolve_request`).
    """
    grants = _leg_grants()
    assert "INSERT" not in grants.get("kernel.verdicts", set()), (
        "agentos_leg must never INSERT a verdict; reading one is the "
        "whole of its interest in that table")
    assert "UPDATE" not in grants.get("kernel.errand_schedules", set()), (
        "agentos_leg must not UPDATE errand_schedules: a pod that can "
        "re-pin its own standing grant can approve its own replan")


def test_the_worker_membership_is_never_granted():
    """Plan 10a: granting it silences the error AND the boundary."""
    pattern = re.compile(
        r"GRANT\s+agentos_worker\b[^;]*?\bTO\b[^;]*?\bagentos_leg\b",
        re.IGNORECASE | re.DOTALL)
    for path in _all_migrations():
        assert not pattern.search(_statements(path)), (
            f"{path.name} makes agentos_leg a member of agentos_worker. "
            "That hands the pod the ledger, the token ledger and the "
            "credentials tables to unblock `SET ROLE` in loopbridge. "
            "Fix the pool in D2 instead (plan 11.5a prerequisite 1)")


def test_no_migration_carries_the_leg_password():
    """The role is inert everywhere until a human runs one ALTER ROLE.

    That is what makes these files safe to apply automatically on every
    environment, and it is why the password lives in Secret Manager and
    reaches the pod only as the `execution-db` Secret key the proxy
    container mounts.
    """
    pattern = re.compile(r"\bagentos_leg\b[^;]{0,200}?\bPASSWORD\b",
                         re.IGNORECASE | re.DOTALL)
    for path in _all_migrations():
        assert not pattern.search(_statements(path)), (
            f"{path.name} sets a password for agentos_leg. A credential "
            "in a migration is a credential in git history forever; the "
            "ALTER ROLE belongs in a human's shell, documented in the "
            "file's trailing comment and nowhere else")


def test_all_three_leg_role_migrations_are_in_sequence():
    """A migration absent from SEQUENCE is never applied.

    Three files now, and the order is not cosmetic: the third REVOKEs the
    table-level `kernel.questions` grant the first issues, so running it
    out of order would revoke nothing and leave the broad grant standing.

    The distribution ships the three as one login migration; what must
    survive the squash is each one's result, in the order they compose.
    """
    if IS_DISTRIBUTION:
        listed = (_MIGRATIONS / "SEQUENCE").read_text(encoding="utf-8").split()
        assert "0004_execution_pod_login" in listed
        assert (_MIGRATIONS / "0004_execution_pod_login.sql").is_file()
        grants = _leg_grants()
        # the publish grant
        assert "INSERT" in grants.get("public.resource_publishes", set())
        # the settled tree: a tool body outside kernel/harness/
        assert "INSERT" in grants.get("kernel.env_deltas", set())
        # and its narrowing ran after the broad grant, not before it
        held = _effective_table_privileges("kernel.questions")
        assert "SELECT" in held and not {"UPDATE", "INSERT"} & held, held
        return
    ordered = ("kernel_execution_pod_leg_role",
               "kernel_leg_role_publish_grant",
               "kernel_leg_role_settled_tree")
    listed = (_MIGRATIONS / "SEQUENCE").read_text(encoding="utf-8").split()
    for name in ordered:
        assert name in listed, f"{name} is missing from migrations/SEQUENCE"
        assert (_MIGRATIONS / f"{name}.sql").is_file()
    assert [listed.index(n) for n in ordered] == sorted(
        listed.index(n) for n in ordered), (
        f"the leg-role migrations must run in the order {ordered}: each "
        "one amends the grants the one before it issued")


# ── The third derivation, against the settled tree ───────────────────────
#
# `kernel_leg_role_settled_tree.sql` re-walked the leg after `park.py`,
# `executor.py`, `loop.py` and `worker/service.py` were split into
# facades. The TABLE set was confirmed unchanged; a VERB was not, and the
# `questions` gap §10a recorded turned out not to be closable by a grant
# at all. These assertions pin the three results so a fourth derivation
# cannot quietly undo them.

def _column_grants() -> dict:
    """{(object, privilege): {columns}} for every column-level leg grant.

    `_leg_grants()` above cannot see these: its `privs` group is
    `[A-Za-z ,]+?` and stops dead at the `(`. That is deliberate --
    keeping the two parsers separate is what lets a test say "table-level
    UPDATE is GONE and column-level UPDATE is present", which is the
    whole property.
    """
    out: dict = {}
    pattern = re.compile(
        r"GRANT\s+(?P<priv>SELECT|INSERT|UPDATE)\s*\((?P<cols>[^)]*)\)\s*"
        r"ON\s+(?P<obj>[A-Za-z0-9_.]+)\s+TO\s+agentos_leg\b",
        re.IGNORECASE)
    for path in _all_migrations():
        for match in pattern.finditer(_statements(path)):
            cols = {c.strip().lower()
                    for c in match.group("cols").split(",") if c.strip()}
            key = (match.group("obj").lower(), match.group("priv").upper())
            out.setdefault(key, set()).update(cols)
    return out


def _effective_table_privileges(obj: str) -> set:
    """Table-level privileges the leg still holds on `obj`, in SEQUENCE order.

    Replays GRANT and REVOKE in the order the migrations actually run,
    because a grant list is a running total and not a set of files. A
    later migration re-granting table-level UPDATE on `kernel.questions`
    would silently restore the hole the narrowing closed, and this is
    what notices.
    """
    order = (_MIGRATIONS / "SEQUENCE").read_text(encoding="utf-8").split()
    grant = re.compile(
        r"GRANT\s+(?P<privs>[A-Za-z ,]+?)\s+ON\s+" + re.escape(obj) +
        r"\s+TO\s+agentos_leg\b", re.IGNORECASE)
    revoke = re.compile(
        r"REVOKE\s+(?P<privs>[A-Za-z ,]+?)\s+ON\s+" + re.escape(obj) +
        r"\s+FROM\s+agentos_leg\b", re.IGNORECASE)
    held: set = set()
    for name in order:
        path = _MIGRATIONS / f"{name}.sql"
        if not path.is_file():
            continue
        # Statement order within a file matters as much as file order:
        # the narrowing REVOKEs then GRANTs column-level in one file.
        for match in re.finditer(r"(GRANT|REVOKE)[^;]*;",
                                 _statements(path), re.IGNORECASE):
            stmt = match.group(0)
            found = grant.search(stmt)
            if found:
                held |= {p.strip().upper()
                         for p in found.group("privs").split(",") if p.strip()}
            gone = revoke.search(stmt)
            if gone:
                held -= {p.strip().upper()
                         for p in gone.group("privs").split(",") if p.strip()}
    return held


def test_the_leg_may_delete_its_own_snapshot_probe():
    """The preflight writes a probe row and DELETES it, before step one.

    `harness/snapshots.py::snapshot_preflight` is the first database
    write of every leg (`loop_leg.py:124` -> `loop_result.py:51`), and it
    proves durability by writing and removing a row rather than reading
    one. Its try/except catches everything, so a missing DELETE does not
    surface as a permission error: it becomes
    `stop_reason: "preflight_failed"` and NO LEG EVER RUNS A STEP. Worse,
    the refused DELETE strands the probe at (execution_id, 0, -1), and
    `checkpoints_unique` then makes the NEXT leg fail with a unique
    violation instead -- same stop_reason, different cause.

    The first derivation's file says "append-only ... no UPDATE, no
    DELETE", which is true of checkpoints and not of the probe. Do not
    restore that reading by dropping this grant.
    """
    grants = _leg_grants()
    privs = grants.get("kernel.execution_checkpoints", set())
    assert {"SELECT", "INSERT", "DELETE"} <= privs, (
        "agentos_leg needs DELETE on kernel.execution_checkpoints for "
        "snapshot_preflight's probe row; without it the harness refuses "
        f"to start and says the table is unreachable. Found: {sorted(privs)}")
    assert "UPDATE" not in privs, (
        "a written checkpoint is immutable -- DELETE is for the preflight "
        "probe only, and UPDATE has no caller at all")


def test_the_questions_grant_is_column_level_and_table_level_is_gone():
    """§10a's decision, and the REVOKE that makes it mean anything.

    Postgres keeps table-level privileges in `pg_class.relacl` and
    column-level ones in `pg_attribute.attacl`, independently:
    `GRANT UPDATE (col)` does NOT displace a table-level UPDATE. Without
    the REVOKE the column list is decoration and the pod still holds
    everything. This asserts the running total, in SEQUENCE order, so a
    later migration cannot re-grant it back.

    THE COLUMN SETS ARE ASSERTED BY EQUALITY, not by membership, and that
    is what makes the WITHHELD columns load-bearing rather than
    incidental. `close_open_questions` runs in the pod, so `answer_text`
    and `answered_by` could not be withheld -- the trigger covers those.
    What equality pins is the sharper set, the one that survives a real
    person being in the loop: `payload` carries the signature
    `permission.py::approved_calls` hands back as approved,
    `content_hash` is what `pin_plan_to_grant` pins a standing grant to,
    `card_posted_at` decides whether a person is ever TOLD, `generation`
    is how `answered_since` tells a fresh answer from a spent one, and
    `step_notes` is written only by `resolve_request`, conductor-side.
    Adding a name to either set is a security change; make it here.
    """
    held = _effective_table_privileges("kernel.questions")
    assert "UPDATE" not in held, (
        "agentos_leg holds TABLE-LEVEL UPDATE on kernel.questions again. "
        "That restores 10a's gap: a pod can rewrite an approved "
        "question's `payload` and launder a human's approval onto a "
        "different call (permission.py::approved_calls reads "
        "payload->>'signature'). Narrow it, do not re-grant it")
    assert "INSERT" not in held, (
        "agentos_leg holds TABLE-LEVEL INSERT on kernel.questions again. "
        "A pod can then INSERT a row already ANSWERED and undeclined, "
        "which plan_is_approved reads as an approval -- one statement "
        "past the close-out trigger")
    assert "SELECT" in held, (
        "the leg reads questions at every leg start (park_read, "
        "permission, batching); SELECT stays table-level")

    cols = _column_grants()
    update = cols.get(("kernel.questions", "UPDATE"), set())
    assert update == {"answer_text", "answered_at", "answered_by", "batch_id",
                      "checks", "declined", "status", "verdict"}, (
        "the UPDATE column list must be exactly what the POD writes: "
        "`checks` (park_open.py:72), `batch_id` (batching.py:75 via "
        "park_open.py:93), and the six of `close_open_questions` "
        "(park_open.py:210), which runs in the pod via "
        "loop_intake.py:83 -> intake.py:235. "
        f"Found: {sorted(update) or 'nothing'}")
    insert = cols.get(("kernel.questions", "INSERT"), set())
    assert insert == {"blocking_on", "content_hash", "execution_id",
                      "generation", "kind", "payload", "proposed_default",
                      "question_text", "reversible"}, (
        "the INSERT column list must be exactly mailbox/api.py:125, the "
        "only INSERT into this table, so that status/declined/verdict/"
        f"answer_text fall to their defaults. Found: {sorted(insert)}")


def test_the_pod_may_decline_its_own_question_and_never_approve_one():
    """The trigger, because a grant cannot say "only like this".

    The leg must hold UPDATE on `answered_by`/`answer_text` --
    `close_open_questions` writes both -- and the moment it does it can
    set `status='ANSWERED', declined=FALSE` on its own plan approval.
    `plan_is_approved` (park_approval.py:158) requires exactly ANSWERED
    and not-declined and verdict approve, and NEVER READS `answered_by`,
    so narrowing to columns does not reach it.

    Pinned to the close-out's own shape, which is unconditional in
    park_open.py:210. A decline from an agent is safe and the repository
    already says so -- park_resolve.py:112, "A decline still relays:
    saying no is safe from anybody."
    """
    if IS_DISTRIBUTION:
        # pg_dump spells it CREATE FUNCTION; the facts are the same.
        sql = _COMMENT.sub(" ", baseline_schema())
        start = sql.index(
            "CREATE FUNCTION kernel.leg_answers_only_its_own_close_out()")
        body = sql[start:sql.index("$$;", start)]
        for needle in ("current_user = 'agentos_leg'",
                       "NEW.declined IS NOT TRUE",
                       "NEW.verdict IS DISTINCT FROM 'declined'",
                       "NEW.answered_by IS DISTINCT FROM 'kernel'",
                       "RAISE EXCEPTION"):
            assert needle in body, (
                f"the close-out trigger no longer checks {needle!r}")
        assert re.search(
            r"BEFORE\s+UPDATE\s+ON\s+kernel\.questions\s+FOR\s+EACH\s+ROW\s+"
            r"EXECUTE\s+FUNCTION\s+kernel\.leg_answers_only_its_own_close_out",
            sql, re.IGNORECASE), (
            "the trigger must be attached BEFORE UPDATE on kernel.questions")
        return
    sql = _statements(_MIGRATIONS / "kernel_leg_role_settled_tree.sql")
    assert "kernel.leg_answers_only_its_own_close_out" in sql, (
        "the close-out trigger function is gone; without it the column "
        "narrowing does not close 10a's forging gap at all")
    body = sql[sql.index("CREATE OR REPLACE FUNCTION"):]
    for needle in ("current_user = 'agentos_leg'",
                   "NEW.declined IS NOT TRUE",
                   "NEW.verdict IS DISTINCT FROM 'declined'",
                   "NEW.answered_by IS DISTINCT FROM 'kernel'",
                   "RAISE EXCEPTION"):
        assert needle in body, (
            f"the close-out trigger no longer checks {needle!r}. All "
            "three values are required together: `plan_is_approved` "
            "reads `declined` and `verdict` and ignores `answered_by`, "
            "so dropping any one of them reopens a different gate")
    assert re.search(r"BEFORE\s+UPDATE\s+ON\s+kernel\.questions", sql,
                     re.IGNORECASE), (
        "the trigger must be attached BEFORE UPDATE on kernel.questions; "
        "a function nothing fires is a comment")
    assert "DROP TRIGGER IF EXISTS" in sql, (
        "re-applying the migration must be a no-op -- "
        "CREATE OR REPLACE TRIGGER is PG14+ and this runs everywhere")


def test_the_bridge_reaches_no_database_and_needs_no_grant():
    """Item 3: D2 moves the runner into the worker container.

    The previous derivation skipped `kernel/bridge/` on the reading that
    it runs conductor-side. It is database-free for a better reason than
    that -- it is an HTTP seam, and `harness_jobs.py` executes INSIDE the
    sandbox on the shim's `sys.path`. THAT is why nothing is granted for
    it, and this test is what keeps the reason true: a bridge module that
    grows a `conn` is a `permission denied` in the middle of a leg,
    against grants nobody wrote.
    """
    bridge = _ROOT / "kernel" / "bridge"
    forbidden = re.compile(
        r"\b(SELECT\s|INSERT\s+INTO|UPDATE\s+[a-z_]+\s+SET|DELETE\s+FROM"
        r"|asyncpg|make_pool|loopbridge|fetchval|fetchrow|conn)\b",
        re.IGNORECASE)
    offenders = []
    for path in sorted(bridge.glob("*.py")):
        for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1):
            if forbidden.search(line):
                offenders.append(f"{path.name}:{number}: {line.strip()[:90]}")
    assert not offenders, (
        "kernel/bridge/ has grown database access:\n  "
        + "\n  ".join(offenders)
        + "\nAfter D2 this runs in the execution pod as `agentos_leg`, "
          "which holds no grant for it. Either keep the seam HTTP-only "
          "or add a new sequential migration naming the statement")


def test_the_third_derivation_is_in_sequence_and_runs_last():
    """It REVOKEs what the first file granted; order is the whole point."""
    if IS_DISTRIBUTION:
        # The order's result: column-level grants stand, table-level gone.
        held = _effective_table_privileges("kernel.questions")
        assert not {"UPDATE", "INSERT"} & held, (
            f"the narrowing did not take; table-level grants remain: {held}")
        cols = _column_grants()
        assert cols.get(("kernel.questions", "UPDATE"))
        assert cols.get(("kernel.questions", "INSERT"))
        return
    listed = (_MIGRATIONS / "SEQUENCE").read_text(encoding="utf-8").split()
    name = "kernel_leg_role_settled_tree"
    assert name in listed, f"{name} is missing from migrations/SEQUENCE"
    assert (_MIGRATIONS / f"{name}.sql").is_file()
    assert listed.index(name) > listed.index("kernel_leg_role_publish_grant")
    assert listed.index(name) > listed.index("kernel_execution_pod_leg_role"), (
        "the narrowing REVOKEs the table-level grant the role migration "
        "issues; running it first would revoke nothing and leave the "
        "broad grant standing")


# ── The three tables every derivation so far walked past ─────────────────
#
# The first two files swept `kernel/harness/`, `kernel/workspace/`,
# `kernel/mailbox/` and `kernel/worker/`. Three of the leg's SQL
# statements live outside all four, reached by two WORKER TOOLS and one
# call inside `run_leg`:
#
#   env_install    -> kernel/verifier/environment.py::record
#   goal_knowledge -> kernel/knowledge/read.py
#   settle_claim   -> kernel/knowledge/write.py::record_cycle
#
# The publish-grant file's lesson was "a leg is also every tool it
# dispatches". These are the half of that lesson it did not reach: a tool
# whose BODY lives in somebody else's directory. The two tests below pin
# the grants; the third pins the reason, so a sweep scoped by directory
# cannot quietly come back.

def test_the_leg_can_record_what_it_installed():
    """`env_install` writes `kernel.env_deltas`, and the Verifier reads it.

    Withheld by the first migration in a list that names it beside
    `contracts` and `criteria` -- true of those two, false of this one.
    `env_deltas` is not the contract; it is what THIS execution installed
    into its own sandbox, written by a tool the model calls
    (`executor_env_install.py:199` -> `verifier/environment.py:140`).

    The `await record(...)` is bare, so the tool fails loudly -- but only
    after the install has succeeded, and the Verifier then rebuilds the
    environment from `deltas_for()` WITHOUT the package. That reads as
    "the tests are broken", which is the one thing it is not.
    """
    grants = _leg_grants()
    privs = grants.get("kernel.env_deltas", set())
    assert {"SELECT", "INSERT"} <= privs, (
        "agentos_leg needs SELECT and INSERT on kernel.env_deltas: "
        "`record()` reads MAX(seq) and then inserts. "
        f"Found: {sorted(privs) or 'nothing'}")
    assert not ({"UPDATE", "DELETE"} & privs), (
        "env_deltas is append-only: the ORDER of installs is the "
        "environment, and a rewritable history replays as something else")
    # A SEPARATE PARSE, because `_leg_grants()` cannot see this: its
    # object group stops at the bare name and `ON SEQUENCE x` is two
    # tokens. Worth its own regex rather than its own assumption.
    sequence = re.compile(
        r"GRANT\s+[A-Za-z, ]*USAGE[A-Za-z, ]*\s+ON\s+SEQUENCE\s+"
        r"kernel\.env_deltas_id_seq\s+TO\s+agentos_leg\b", re.IGNORECASE)
    assert any(sequence.search(_statements(p)) for p in _all_migrations()), (
        "env_deltas.id is DEFAULT nextval(), not GENERATED AS IDENTITY, "
        "so the sequence IS ACL-checked and the INSERT fails with "
        "'permission denied for sequence' on a table grant that looks "
        "complete -- unlike resource_publishes, where the same line is "
        "belt-and-braces")


def test_the_leg_can_file_what_the_work_taught_and_cannot_rewrite_a_story():
    """`record_cycle` runs inside `run_leg`, and its failure is swallowed.

    `loop_claim.py::settle_claim` calls `knowledge/write.py::record_cycle`
    at the moment the executor claims completion, inside
    `except Exception: logger.exception("... the claim stands")`. That is
    the KER15 shape `stage_l_worker_grants.sql` was written about: the
    story lands, the surface is empty, and the only trace is a swallowed
    exception in a log.

    SELECT stays TABLE-LEVEL deliberately. The five columns are
    enumerable, but `stage_l_worker_grants.sql` measured `SELECT ... FOR
    UPDATE` against a live database with a table-level SELECT and a
    column-level UPDATE, and `_apply` needs that row lock. Narrowing both
    halves at once, on the one path whose failure is silent, would re-run
    that experiment with nobody watching.
    """
    held = _effective_table_privileges("public.stories")
    assert "SELECT" in held, (
        "agentos_leg needs SELECT on public.stories: `goal_knowledge` "
        "reads across the goal and `_apply` locks the row it updates")
    assert "UPDATE" not in held, (
        "table-level UPDATE on public.stories would let a pod set "
        "`state` and mark its own story done without a verdict. The "
        "kernel owns two columns here and no more")
    cols = _column_grants().get(("public.stories", "UPDATE"), set())
    assert cols == {"knowledge", "updated_at"}, (
        "the leg writes exactly `knowledge` and `updated_at` "
        "(knowledge/write.py:92). `agentos_worker` also holds "
        "`result_summary` for `assistant/write.py`, which is not on the "
        f"leg's graph. Found: {sorted(cols) or 'nothing'}")

    decisions = _effective_table_privileges("public.goal_decisions")
    assert decisions == {"SELECT"}, (
        "goal_knowledge reads goal_decisions (knowledge/read.py:193) and "
        "writes none. INSERT and UPDATE(retired_at) are GO's, in the "
        "conductor: a pod that could retire a decision could remove the "
        "constraint it is about to violate and read the goal back as if "
        f"it had never been decided. Found: {sorted(decisions)}")


def test_the_two_tools_that_reach_outside_the_swept_directories():
    """The grants above are not speculative; this is what makes them real.

    If either tool leaves the worker surface, or either import goes, the
    grant should be revisited rather than left standing -- and if they
    stay, no future derivation may drop the grant on the grounds that
    `kernel/knowledge/` and `kernel/verifier/` are somebody else's code.
    """
    from kernel.tools.catalog import tools_by_side
    worker = set(tools_by_side("worker"))
    assert {"env_install", "goal_knowledge"} <= worker, (
        "env_install and goal_knowledge are the two worker tools whose "
        "bodies reach outside kernel/harness/. If they are no longer "
        f"worker tools, revisit their grants. Worker side: {sorted(worker)}")

    reaches = {
        "kernel/harness/executor_env_install.py": "kernel.verifier.environment",
        "kernel/harness/loop_claim.py": "kernel.knowledge.write",
        "kernel/harness/executor_tools.py": "kernel.knowledge.read",
    }
    for rel, module in reaches.items():
        text = (_ROOT / rel).read_text(encoding="utf-8")
        assert module in text, (
            f"{rel} no longer imports {module}. That import is why "
            "agentos_leg holds grants on tables in that module; if the "
            "reach is gone the grant should go with it, in a new "
            "sequential migration")
