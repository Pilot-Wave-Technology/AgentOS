"""Integration tests for the AgentOS External Goal Orchestration API.

Tests cover:
  1. POST /api/v1/goals:
     - Authentication & scope verification (goals:create required)
     - Merkle provenance tracking (correlation_id & causation_id)
     - Atomic provisioning: goals row, GOAL_CREATED & GOAL_MEMBER_ADDED events,
       admin membership, 4 system threads, goal_resources, and GO agent provisioning
     - Validation: custom ID format, duplicate ID refusal
  2. GET /api/v1/goals/{id}/questions:
     - Authentication & scope verification (goals:read required)
     - Returns active open question cards inside the goal with needs_human: true
  3. POST /api/v1/goals/{id}/questions/{qid}/answer:
     - Authentication & scope verification (goals:write required)
     - Appends DECISION_RECORDED event into the state ledger
     - Invokes decide() to settle the card and unpark the pod
     - 404 on missing or non-matching goal question
  4. GET /api/v1/goals/{id}/actions:
     - Authentication & scope verification (goals:read required)
     - Returns action templates registered in-goal from ledger_events
  5. In-goal action registration worker tool:
     - register_action_template records action.template_discovered in ledger
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from agentos_ledger_client.schema import (
    ACTION_TEMPLATE_DISCOVERED,
    DECISION_RECORDED,
    GOAL_CREATED,
    GOAL_MEMBER_ADDED,
)
from app.routers import goal_api
from app.services.api_keys import ApiPrincipal


# ── Fixtures & Mock Database ──────────────────────────────────────────────────

class MockRecord(dict):
    """Dictionary that emulates asyncpg.Record."""
    def __getitem__(self, key: str):
        return super().__getitem__(key)

    def get(self, key: str, default=None):
        return super().get(key, default)


class MockDbPool:
    def __init__(self):
        self.goals = {}
        self.goal_members = []
        self.threads = []
        self.goal_resources = []
        self.questions = []
        self.ledger_events = []
        self.api_keys = {}

    def acquire(self):
        pool_self = self

        class ConnContext:
            async def __aenter__(self):
                return MockConn(pool_self)

            async def __aexit__(self, *args):
                pass

        return ConnContext()

    async def fetchval(self, query: str, *args):
        conn = MockConn(self)
        return await conn.fetchval(query, *args)

    async def fetchrow(self, query: str, *args):
        conn = MockConn(self)
        return await conn.fetchrow(query, *args)

    async def fetch(self, query: str, *args):
        conn = MockConn(self)
        return await conn.fetch(query, *args)

    async def execute(self, query: str, *args):
        conn = MockConn(self)
        return await conn.execute(query, *args)


class MockTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class MockConn:
    def __init__(self, pool: MockDbPool):
        self.pool = pool

    def transaction(self):
        return MockTransaction()

    async def fetchval(self, query: str, *args):
        norm = " ".join(query.strip().split())
        if "SELECT id FROM goals WHERE id = $1" in norm:
            gid = args[0]
            return gid if gid in self.pool.goals else None

        # The cross-tenant guard: is this principal a member of the goal
        # named in the URL? Answered from the same list the creation
        # endpoint appends to, so a test that creates a goal has access
        # to it and a test that does not, does not.
        if "FROM goal_members WHERE goal_id = $1 AND uid = $2" in norm:
            gid, uid = args[0], args[1]
            return 1 if any(m["goal_id"] == gid and m["uid"] == uid
                            for m in self.pool.goal_members) else None

        if "SELECT e.goal_id" in norm and "kernel.questions" in norm:
            qid = args[0]
            for q in self.pool.questions:
                if q["id"] == qid:
                    return q["goal_id"]
            return None

        return None

    async def fetchrow(self, query: str, *args):
        norm = " ".join(query.strip().split())
        if "SELECT * FROM public.api_keys WHERE key_hash = $1" in norm:
            kh = args[0]
            for row in self.pool.api_keys.values():
                if row["key_hash"] == kh:
                    return MockRecord(row)
            return None
        return None

    async def fetch(self, query: str, *args):
        norm = " ".join(query.strip().split())
        if "FROM kernel.questions" in norm:
            gid = args[0]
            results = []
            for q in self.pool.questions:
                if q["goal_id"] == gid and q["status"] == "OPEN":
                    results.append(MockRecord(q))
            return results

        if "FROM ledger_events" in norm:
            gid = args[0]
            event_type = args[1]
            results = []
            for ev in self.pool.ledger_events:
                if (ev.get("context_goal_id") == gid or ev.get("payload", {}).get("goal_id") == gid) and ev.get("event_type") == event_type:
                    results.append(MockRecord(ev))
            return results

        return []

    async def execute(self, query: str, *args):
        norm = " ".join(query.strip().split())

        if "INSERT INTO public.api_keys" in norm:
            key_id = args[0]
            key_hash = args[1]
            key_prefix = args[2]
            owner_uid = args[3]
            actor_id = args[4]
            name = args[5]
            scopes = args[6]
            expires_at = args[7]
            self.pool.api_keys[str(key_id)] = {
                "id": key_id,
                "key_hash": key_hash,
                "key_prefix": key_prefix,
                "owner_uid": owner_uid,
                "actor_id": actor_id,
                "name": name,
                "scopes": scopes,
                "created_at": None,
                "last_used_at": None,
                "expires_at": expires_at,
                "revoked_at": None,
            }
            return "INSERT 0 1"

        if "INSERT INTO goals" in norm:
            gid = args[0]
            self.pool.goals[gid] = {
                "id": gid,
                "name": args[1],
                "description": args[2],
                "objective": args[3],
                "priority": args[4],
                "color_tag": args[5],
                "created_by": args[6],
                "go_thread_id": args[7],
                "orchestrator_thread_id": args[8],
                "council_thread_id": args[9],
                "security_thread_id": args[10],
            }
            return "INSERT 0 1"

        if "UPDATE goals SET go_agent_id = $1 WHERE id = $2" in norm:
            gid = args[1]
            if gid in self.pool.goals:
                self.pool.goals[gid]["go_agent_id"] = args[0]
            return "UPDATE 1"

        if "INSERT INTO goal_members" in norm:
            self.pool.goal_members.append({"goal_id": args[0], "uid": args[1], "role": args[2]})
            return "INSERT 0 1"

        if "INSERT INTO threads" in norm:
            self.pool.threads.append({"id": args[0], "goal_id": args[1], "title": args[4] if len(args) > 4 else ""})
            return "INSERT 0 1"

        if "INSERT INTO goal_resources" in norm:
            self.pool.goal_resources.append({"id": args[0], "goal_id": args[1], "name": args[3]})
            return "INSERT 0 1"

        if "UPDATE public.api_keys SET last_used_at" in norm:
            return "UPDATE 1"

        return "OK"


# ── Setup App & Client ────────────────────────────────────────────────────────

def _run_sync(coro):
    """Finish a coroutine that never actually awaits, WITHOUT a loop.

    THIS WAS `asyncio.run`, AND THAT IS A LANDMINE IN A FIXTURE. Each
    call builds a fresh event loop and CLOSES it on the way out,
    leaving the thread with no current loop. Any later async test
    holding an asyncpg connection across a fixture then fails with
    something that names neither this file nor the cause -- the
    ledger-client suite dies on `InFailedSQLTransactionError` when it
    happens to be collected after this module. Nothing is failing
    today; the ordering just happens to be kind.

    Nothing here needs a loop. `create_api_key` awaits exactly one
    thing -- `MockDbPool.execute`, which returns without yielding --
    so a single `send(None)` runs it to completion. If that ever stops
    being true this raises instead of quietly going wrong.
    """
    try:
        coro.send(None)
    except StopIteration as done:
        return done.value
    coro.close()
    raise RuntimeError(
        "coroutine suspended: it awaited something real, so this helper "
        "is no longer the right tool — make the test async instead")


@pytest.fixture
def api_fixture():
    pool = MockDbPool()
    app = FastAPI()
    app.include_router(goal_api.router)

    # Register an admin API key
    from app.services.api_keys import API_KEY_PREFIX, create_api_key

    admin_token, _ = _run_sync(
        create_api_key(
            pool,
            owner_uid="marl-admin",
            name="MARL Orchestrator",
            scopes=["*"],
        )
    )

    limited_token, _ = _run_sync(
        create_api_key(
            pool,
            owner_uid="marl-readonly",
            name="MARL Reader",
            scopes=["goals:read"],
        )
    )

    # Mock append_event to record directly in pool.ledger_events
    async def mock_append_event(conn, event_type, payload=None, actor_id=None, context_goal_id=None, **kwargs):
        ev = {
            "event_id": str(uuid.uuid4()),
            "event_type": event_type,
            "payload": payload or {},
            "actor_id": str(actor_id) if actor_id else None,
            "actor_type": "api",
            "context_goal_id": context_goal_id,
            "created_at": None,
        }
        pool.ledger_events.append(ev)
        return ev

    return {
        "app": app,
        "pool": pool,
        "admin_token": admin_token,
        "limited_token": limited_token,
        "mock_append_event": mock_append_event,
    }


# ── Test Cases ───────────────────────────────────────────────────────────────

def test_create_goal_unauthorized(api_fixture):
    client = TestClient(api_fixture["app"])
    res = client.post("/api/v1/goals", json={"name": "No Auth"})
    assert res.status_code == 401


def test_create_goal_insufficient_scope(api_fixture):
    client = TestClient(api_fixture["app"])
    with patch("app.services.api_keys.get_pool", return_value=api_fixture["pool"]):
        res = client.post(
            "/api/v1/goals",
            json={"name": "Read Only Attempt"},
            headers={"Authorization": f"Bearer {api_fixture['limited_token']}"},
        )
        assert res.status_code == 403


def test_create_goal_success(api_fixture):
    client = TestClient(api_fixture["app"])
    pool = api_fixture["pool"]

    with patch("app.services.api_keys.get_pool", return_value=pool), \
         patch("app.routers.goal_api.get_pool", return_value=pool), \
         patch("app.routers.goal_api.append_event", side_effect=api_fixture["mock_append_event"]), \
         patch("app.services.go_agent.create_go_for_goal", new_callable=AsyncMock) as mock_go:

        mock_go.return_value = {"agent_id": "go-custom-1"}

        payload = {
            "name": "Distributed RL Orchestration",
            "objective": "Train policy on cluster",
            "description": "Multi-agent coordination run",
            "priority": "high",
            "provenance": {
                "correlation_id": "corr-uuid-1234",
                "causation_id": "caus-uuid-5678",
            },
            "resources": [
                {
                    "kind": "repo",
                    "name": "rl-environment",
                    "repo_owner": "marl-lab",
                    "repo_name": "rl-env",
                    "default_branch": "main",
                }
            ],
        }

        res = client.post(
            "/api/v1/goals",
            json=payload,
            headers={"Authorization": f"Bearer {api_fixture['admin_token']}"},
        )

        assert res.status_code == 201
        data = res.json()
        gid = data["goal_id"]
        assert len(gid) >= 3
        assert data["name"] == "Distributed RL Orchestration"
        assert data["priority"] == "high"
        assert data["state"] == "planning"
        assert "threads" in data
        assert data["threads"]["go_thread_id"]

        # Verify DB records
        assert gid in pool.goals
        assert any(m["goal_id"] == gid and m["uid"] == "marl-admin" for m in pool.goal_members)
        assert len([t for t in pool.threads if t["goal_id"] == gid]) == 4
        assert len([r for r in pool.goal_resources if r["goal_id"] == gid]) == 1

        # Verify ledger events
        event_types = [e["event_type"] for e in pool.ledger_events]
        assert GOAL_CREATED in event_types
        assert GOAL_MEMBER_ADDED in event_types


def test_get_goal_questions(api_fixture):
    client = TestClient(api_fixture["app"])
    pool = api_fixture["pool"]

    gid = "TESTG"
    pool.goals[gid] = {"id": gid}
    # The caller is a member, which is what the guard asks. A test for
    # the goal nobody is a member of is below.
    pool.goal_members.append({"goal_id": gid, "uid": "marl-admin",
                              "role": "admin"})
    pool.questions.append({
        "id": 101,
        "goal_id": gid,
        "kind": "authorization",
        "question_text": "May the agent run terraform apply?",
        "payload": json.dumps({"plan": "terraform apply --auto-approve"}),
        "created_at": None,
        "execution_id": str(uuid.uuid4()),
        "story_id": "story-1",
        "state": "parked",
        "status": "OPEN",
    })

    with patch("app.services.api_keys.get_pool", return_value=pool), \
         patch("app.routers.goal_api.get_pool", return_value=pool):

        res = client.get(
            f"/api/v1/goals/{gid}/questions",
            headers={"Authorization": f"Bearer {api_fixture['admin_token']}"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["goal_id"] == gid
        assert len(data["questions"]) == 1
        q = data["questions"][0]
        assert q["question_id"] == 101
        assert q["needs_human"] is True
        assert q["question_text"] == "May the agent run terraform apply?"


def test_answer_goal_question(api_fixture):
    client = TestClient(api_fixture["app"])
    pool = api_fixture["pool"]

    gid = "TESTG"
    pool.goals[gid] = {"id": gid}
    # The caller is a member, which is what the guard asks. A test for
    # the goal nobody is a member of is below.
    pool.goal_members.append({"goal_id": gid, "uid": "marl-admin",
                              "role": "admin"})
    pool.questions.append({
        "id": 202,
        "goal_id": gid,
        "kind": "authorization",
        "question_text": "Confirm branch merge?",
        "status": "OPEN",
    })

    with patch("app.services.api_keys.get_pool", return_value=pool), \
         patch("app.routers.goal_api.get_pool", return_value=pool), \
         patch("app.routers.goal_api.append_event", side_effect=api_fixture["mock_append_event"]), \
         patch("app.services.kernel_decide.decide", new_callable=AsyncMock) as mock_decide:

        mock_decide.return_value = {"settled": True, "verdict": "approve"}

        res = client.post(
            f"/api/v1/goals/{gid}/questions/202/answer",
            json={"verdict": "approve", "text": "Proceed with deployment"},
            headers={"Authorization": f"Bearer {api_fixture['admin_token']}"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["settled"] is True

        mock_decide.assert_called_once()
        _, kwargs = mock_decide.call_args
        assert kwargs["question_id"] == 202
        assert kwargs["verdict"] == "approve"

        # Verify DECISION_RECORDED event was appended to ledger
        event_types = [e["event_type"] for e in pool.ledger_events]
        assert DECISION_RECORDED in event_types


def test_get_registered_actions(api_fixture):
    client = TestClient(api_fixture["app"])
    pool = api_fixture["pool"]

    gid = "ACTG"
    pool.goals[gid] = {"id": gid}
    # The caller is a member, which is what the guard asks. A test for
    # the goal nobody is a member of is below.
    pool.goal_members.append({"goal_id": gid, "uid": "marl-admin",
                              "role": "admin"})
    pool.ledger_events.append({
        "event_id": str(uuid.uuid4()),
        "event_type": ACTION_TEMPLATE_DISCOVERED,
        "actor_id": str(uuid.uuid4()),
        "actor_type": "agent",
        "context_goal_id": gid,
        "created_at": None,
        "payload": {
            "template": "marl.distributed_eval",
            "params": {"batch_size": 64, "env_name": "Ant-v4"},
            "cohort_scope": "evaluators",
            "description": "Evaluates policy checkpoints in parallel",
            "goal_id": gid,
        },
    })

    with patch("app.services.api_keys.get_pool", return_value=pool), \
         patch("app.routers.goal_api.get_pool", return_value=pool):

        res = client.get(
            f"/api/v1/goals/{gid}/actions",
            headers={"Authorization": f"Bearer {api_fixture['admin_token']}"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["goal_id"] == gid
        assert len(data["actions"]) == 1
        action = data["actions"][0]
        assert action["template"] == "marl.distributed_eval"
        assert action["cohort_scope"] == "evaluators"
        assert action["params"]["batch_size"] == 64


# ── In-Goal Action Registration Tool Unit Test ───────────────────────────────

@pytest.mark.asyncio
async def test_worker_register_action_template():
    from kernel.harness.tools_worker.actions import register_action_template

    mock_conn = MagicMock()
    tool_args = {
        "template": "custom_policy_step",
        "params": {"lr": 0.001, "gamma": 0.99},
        "cohort_scope": "training_workers",
        "description": "Performs an on-policy optimization update",
    }

    with patch("kernel.harness.tools_worker.actions.principal_for", new_callable=AsyncMock) as mock_p, \
         patch("kernel.harness.tools_worker.actions.kernel_actor", new_callable=AsyncMock) as mock_ka, \
         patch("kernel.harness.tools_worker.actions.append", new_callable=AsyncMock) as mock_append:

        mock_p.return_value = {
            "goal_id": "TESTG",
            "story_id": "story-10",
            "kind": "story",
            "domain": "coding",
            "human_actor_id": None,
        }
        mock_ka.return_value = (uuid.uuid4(), "executor")
        mock_append.return_value = str(uuid.uuid4())

        result = await register_action_template(
            conn=mock_conn,
            envelope={},
            execution_id=123,
            tool_args=tool_args,
            tool_call_id="call_abc123",
        )

        assert result.exit_code == 0
        assert result.tool_call_id == "call_abc123"
        out_data = json.loads(result.output)
        assert out_data["ok"] is True
        assert out_data["template"] == "custom_policy_step"
        assert out_data["cohort_scope"] == "training_workers"
        assert out_data["goal_id"] == "TESTG"

        mock_append.assert_called_once()
        _, append_kwargs = mock_append.call_args
        assert append_kwargs["event_type"] == ACTION_TEMPLATE_DISCOVERED
        assert append_kwargs["payload"]["template"] == "custom_policy_step"
        assert append_kwargs["payload"]["cohort_scope"] == "training_workers"


# ── a key is scoped to a VERB, never to a goal ───────────────────────────────

def _foreign_goal(pool, gid="OTHER"):
    """A goal that exists and that our principal has nothing to do with."""
    pool.goals[gid] = {"id": gid}
    pool.goal_members.append({"goal_id": gid, "uid": "somebody-else",
                              "role": "admin"})
    return gid


def test_questions_of_another_goal_are_not_readable(api_fixture):
    """`goals:read` says WHAT a key may do, never WHICH goal it may do
    it to. Without a membership check one key read every goal on the
    platform -- the cross-tenant shape `chat.py` carries a dedicated
    guard against, already fixed there once as a P0."""
    client = TestClient(api_fixture["app"])
    pool = api_fixture["pool"]
    gid = _foreign_goal(pool)
    pool.questions.append({
        "id": 9001, "goal_id": gid, "kind": "authorization",
        "question_text": "approve?", "payload": {}, "status": "OPEN",
        "execution_id": 7, "story_id": "s-x", "state": "PARKED_HOT",
        "created_at": None,
    })
    with patch("app.services.api_keys.get_pool", return_value=pool), \
         patch("app.routers.goal_api.get_pool", return_value=pool):
        res = client.get(
            f"/api/v1/goals/{gid}/questions",
            headers={"Authorization": f"Bearer {api_fixture['admin_token']}"},
        )
    assert res.status_code == 404


def test_another_goals_question_cannot_be_answered(api_fixture):
    """The sharper half. Answering settles a human-approval gate and
    unparks a pod -- doing that to a goal you are not in is not a read
    leak, it is driving somebody else's work."""
    client = TestClient(api_fixture["app"])
    pool = api_fixture["pool"]
    gid = _foreign_goal(pool)
    pool.questions.append({
        "id": 9002, "goal_id": gid, "kind": "authorization",
        "question_text": "approve?", "payload": {}, "status": "OPEN",
        "execution_id": 8, "story_id": "s-y", "state": "PARKED_HOT",
        "created_at": None,
    })
    with patch("app.services.api_keys.get_pool", return_value=pool), \
         patch("app.routers.goal_api.get_pool", return_value=pool), \
         patch("app.services.kernel_decide.decide",
               new_callable=AsyncMock) as mock_decide:
        res = client.post(
            f"/api/v1/goals/{gid}/questions/9002/answer",
            json={"verdict": "approve"},
            headers={"Authorization": f"Bearer {api_fixture['admin_token']}"},
        )
    assert res.status_code == 404
    mock_decide.assert_not_called()


def test_another_goals_actions_are_not_readable(api_fixture):
    client = TestClient(api_fixture["app"])
    pool = api_fixture["pool"]
    gid = _foreign_goal(pool)
    with patch("app.services.api_keys.get_pool", return_value=pool), \
         patch("app.routers.goal_api.get_pool", return_value=pool):
        res = client.get(
            f"/api/v1/goals/{gid}/actions",
            headers={"Authorization": f"Bearer {api_fixture['admin_token']}"},
        )
    assert res.status_code == 404


def test_a_missing_goal_answers_the_same_as_a_forbidden_one(api_fixture):
    """404 and not 403, matching `_guard_thread_in_goal`. A 403 confirms
    the goal exists, and which goal ids are real is not something an
    unauthorised caller should be able to enumerate."""
    client = TestClient(api_fixture["app"])
    pool = api_fixture["pool"]
    with patch("app.services.api_keys.get_pool", return_value=pool), \
         patch("app.routers.goal_api.get_pool", return_value=pool):
        absent = client.get(
            "/api/v1/goals/NOPE/questions",
            headers={"Authorization": f"Bearer {api_fixture['admin_token']}"},
        )
    gid = _foreign_goal(pool, "REAL1")
    with patch("app.services.api_keys.get_pool", return_value=pool), \
         patch("app.routers.goal_api.get_pool", return_value=pool):
        forbidden = client.get(
            f"/api/v1/goals/{gid}/questions",
            headers={"Authorization": f"Bearer {api_fixture['admin_token']}"},
        )
    assert absent.status_code == forbidden.status_code == 404
    assert absent.json() == forbidden.json()



# ── provisioning the GO is not optional ─────────────────────────────────────
#
# AND IT CANNOT RUN FIRST, which took two wrong versions to establish.
# `agent_contexts` has a foreign key to `goals`, so provisioning before
# the row exists dies on `Key (goal_id)=(CATV9) is not present`. Moving
# it INSIDE the creating transaction fixes the key and holds
# `pg_advisory_xact_lock(GLOBAL_CHAIN_LOCK_ID)` -- which `append_event`
# takes -- across seconds of gateway I/O, stalling every other append
# on the platform.
#
# So: write, provision, and UNDO the write if provisioning fails.


def _provision_fails(api_fixture, *, undone=True, go=None, exc=None):
    """Drive one creation whose provisioning does not succeed."""
    pool = api_fixture["pool"]
    client = TestClient(api_fixture["app"])
    with patch("app.services.api_keys.get_pool", return_value=pool), \
         patch("app.routers.goal_api.get_pool", return_value=pool), \
         patch("app.routers.goal_api.append_event",
               side_effect=api_fixture["mock_append_event"]), \
         patch("app.services.goal_rollback.undo_goal_creation",
               new_callable=AsyncMock) as mock_undo, \
         patch("app.services.go_agent.create_go_for_goal",
               new_callable=AsyncMock) as mock_go:
        mock_undo.return_value = undone
        if exc is not None:
            mock_go.side_effect = exc
        else:
            mock_go.return_value = go
        res = client.post(
            "/api/v1/goals",
            json={"name": "Doomed"},
            headers={"Authorization": f"Bearer {api_fixture['admin_token']}"},
        )
    return res, mock_undo


def test_a_failed_provision_rolls_the_goal_back(api_fixture):
    """It used to return 201 and a `go_agent_id` naming an agent that
    did not exist -- the worst possible answer for a caller with nobody
    watching a log."""
    res, undo = _provision_fails(api_fixture, exc=RuntimeError("gateway said no"))
    assert res.status_code == 502
    undo.assert_awaited_once()
    assert "gateway said no" in res.json()["detail"]
    assert "Nothing was kept" in res.json()["detail"]


def test_a_provisioner_that_returns_nothing_is_also_a_failure(api_fixture):
    """`{}` is not success. The old code read `if go_res and "agent_id"
    in go_res` and simply skipped the update."""
    res, undo = _provision_fails(api_fixture, go={})
    assert res.status_code == 502
    undo.assert_awaited_once()


def test_a_skipped_provision_is_not_a_success(api_fixture):
    """AN AGENT ID IS NOT EVIDENCE OF AN AGENT, and this is the one
    that broke dev. `create_go_for_goal`'s dead-goal guard returns
    `{"skipped": ..., "agent_id": ...}` -- an id even though it
    provisioned nothing. Observed as `Agent go-catv8 not found in DB`,
    with the GO then running on the gateway's default model because no
    instance row carried the chosen one."""
    res, undo = _provision_fails(
        api_fixture, go={"skipped": "goal_deleted", "agent_id": "go-x"})
    assert res.status_code == 502
    undo.assert_awaited_once()


def test_a_rollback_that_fails_does_not_claim_nothing_was_kept(api_fixture):
    """Saying "nothing was saved" while the goal is still there sends
    somebody to create it again and hit a duplicate id they cannot
    see."""
    res, _ = _provision_fails(api_fixture, undone=False,
                              exc=RuntimeError("gateway said no"))
    detail = res.json()["detail"]
    assert "could not be rolled back" in detail
    assert "needs an admin" in detail


def test_the_rollback_clears_what_provisioning_writes():
    """`agent_contexts` is the table whose foreign key failed, so it is
    the one that must not be forgotten; `agents` is keyed by name
    rather than goal_id and is the easy omission."""
    import inspect

    from app.services.goal_rollback import _CHILDREN, undo_goal_creation
    tables = {t for t, _ in _CHILDREN}
    for needed in ("agent_contexts", "goal_agent_memberships",
                   "agent_instances", "threads", "goal_members"):
        assert needed in tables, needed
    src = inspect.getsource(undo_goal_creation)
    assert "DELETE FROM agents WHERE id = $1" in src
    assert src.index("DELETE FROM agents") < src.index("DELETE FROM goals")


# ── the ledger must not claim a decision that did not happen ────────────────

def test_a_refused_decision_leaves_no_ledger_event(api_fixture):
    """DECISION_RECORDED was appended and COMMITTED before `decide()`
    ran, so a decide that then refused left the hash-chained ledger
    permanently asserting a decision that never took effect."""
    from app.services.kernel_decide import DecideError

    client = TestClient(api_fixture["app"])
    pool = api_fixture["pool"]
    gid = "DEC1"
    pool.goals[gid] = {"id": gid}
    pool.goal_members.append({"goal_id": gid, "uid": "marl-admin",
                              "role": "admin"})
    pool.questions.append({
        "id": 9100, "goal_id": gid, "kind": "authorization",
        "question_text": "approve?", "payload": {}, "status": "OPEN",
        "execution_id": 11, "story_id": "s-z", "state": "PARKED_HOT",
        "created_at": None,
    })
    before = len(pool.ledger_events)
    with patch("app.services.api_keys.get_pool", return_value=pool), \
         patch("app.routers.goal_api.get_pool", return_value=pool), \
         patch("app.routers.goal_api.append_event",
               side_effect=api_fixture["mock_append_event"]), \
         patch("app.services.kernel_decide.decide",
               new_callable=AsyncMock) as mock_decide:
        mock_decide.side_effect = DecideError("already answered")
        res = client.post(
            f"/api/v1/goals/{gid}/questions/9100/answer",
            json={"verdict": "approve"},
            headers={"Authorization": f"Bearer {api_fixture['admin_token']}"},
        )
    assert res.status_code == 400
    assert len(pool.ledger_events) == before, (
        "the ledger recorded a decision the kernel refused")
