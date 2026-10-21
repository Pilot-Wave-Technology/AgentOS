from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from agentos_ledger_client import AgentOSClient, AgentOSClientError


def test_agent_os_client_headers():
    client = AgentOSClient("http://localhost:8000/", "agos_live_sk_test123")
    assert client.base_url == "http://localhost:8000"
    headers = client._headers()
    assert headers["Authorization"] == "Bearer agos_live_sk_test123"
    assert headers["Content-Type"] == "application/json"
    assert headers["Accept"] == "application/json"


def test_agent_os_client_create_goal():
    client = AgentOSClient("http://localhost:8000", "agos_live_sk_test123")

    mock_resp = MagicMock()
    mock_resp.is_success = True
    mock_resp.json.return_value = {"id": "g-123", "status": "created"}

    with patch("httpx.Client.request", return_value=mock_resp) as mock_req:
        res = client.create_goal(
            name="Test Goal",
            objective="Do things",
            description="Detailed desc",
            priority="high",
            provenance={"correlation_id": "c-1", "causation_id": "ca-1"},
            resources=[{"resource_id": "r-1", "access_level": "read"}],
        )
        assert res["id"] == "g-123"
        mock_req.assert_called_once()
        args, kwargs = mock_req.call_args
        assert args[0] == "POST"
        assert args[1] == "http://localhost:8000/api/v1/goals"
        body = kwargs["json"]
        assert body["name"] == "Test Goal"
        assert body["objective"] == "Do things"
        assert body["provenance"]["correlation_id"] == "c-1"


def test_agent_os_client_get_pending_questions():
    client = AgentOSClient("http://localhost:8000", "agos_live_sk_test123")

    mock_resp = MagicMock()
    mock_resp.is_success = True
    mock_resp.json.return_value = {"questions": [{"id": 42, "text": "Approve this?"}], "count": 1}

    with patch("httpx.Client.request", return_value=mock_resp) as mock_req:
        res = client.get_pending_questions("g-123")
        assert res["count"] == 1
        assert res["questions"][0]["id"] == 42
        mock_req.assert_called_once()
        args, kwargs = mock_req.call_args
        assert args[0] == "GET"
        assert args[1] == "http://localhost:8000/api/v1/goals/g-123/questions"


def test_agent_os_client_answer_question():
    client = AgentOSClient("http://localhost:8000", "agos_live_sk_test123")

    mock_resp = MagicMock()
    mock_resp.is_success = True
    mock_resp.json.return_value = {"settled": True, "verdict": "approve"}

    with patch("httpx.Client.request", return_value=mock_resp) as mock_req:
        res = client.answer_question("g-123", 42, verdict="approve", text="Looks good")
        assert res["settled"] is True
        mock_req.assert_called_once()
        args, kwargs = mock_req.call_args
        assert args[0] == "POST"
        assert args[1] == "http://localhost:8000/api/v1/goals/g-123/questions/42/answer"
        assert kwargs["json"]["verdict"] == "approve"


def test_agent_os_client_get_registered_actions():
    client = AgentOSClient("http://localhost:8000", "agos_live_sk_test123")

    mock_resp = MagicMock()
    mock_resp.is_success = True
    mock_resp.json.return_value = {"actions": [{"template": "foo", "params": {}}], "count": 1}

    with patch("httpx.Client.request", return_value=mock_resp) as mock_req:
        res = client.get_registered_actions("g-123")
        assert res["count"] == 1
        mock_req.assert_called_once()
        args, kwargs = mock_req.call_args
        assert args[0] == "GET"
        assert args[1] == "http://localhost:8000/api/v1/goals/g-123/actions"

    # Also test get_deliverables alias
    with patch("httpx.Client.request", return_value=mock_resp) as mock_req:
        res2 = client.get_deliverables("g-123")
        assert res2["count"] == 1


def test_agent_os_client_error_handling():
    client = AgentOSClient("http://localhost:8000", "agos_live_sk_test123")

    mock_resp = MagicMock()
    mock_resp.is_success = False
    mock_resp.status_code = 403
    mock_resp.json.return_value = {"detail": "Insufficient permissions"}

    with patch("httpx.Client.request", return_value=mock_resp):
        with pytest.raises(AgentOSClientError) as exc_info:
            client.get_pending_questions("g-123")
        assert exc_info.value.status_code == 403
        assert "Insufficient permissions" in str(exc_info.value.body)


@pytest.mark.asyncio
async def test_agent_os_client_async_methods():
    client = AgentOSClient("http://localhost:8000", "agos_live_sk_test123")

    mock_resp = MagicMock()
    mock_resp.is_success = True
    mock_resp.json.return_value = {"status": "ok"}

    with patch("httpx.AsyncClient.request", return_value=mock_resp) as mock_req:
        res = await client.acreate_goal(name="Async Goal")
        assert res["status"] == "ok"

        res = await client.aget_pending_questions("g-1")
        assert res["status"] == "ok"

        res = await client.aanswer_question("g-1", 10, verdict="deny")
        assert res["status"] == "ok"

        res = await client.aget_registered_actions("g-1")
        assert res["status"] == "ok"

        res = await client.aget_deliverables("g-1")
        assert res["status"] == "ok"

        assert mock_req.call_count == 5
