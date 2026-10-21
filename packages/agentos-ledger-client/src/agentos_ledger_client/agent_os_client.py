"""Client SDK for external AgentOS goal and action orchestration."""

from __future__ import annotations

import json
from typing import Any, Optional
import urllib.error
import urllib.request

try:
    import httpx

    _HAVE_HTTPX = True
except ImportError:
    _HAVE_HTTPX = False


class AgentOSClientError(Exception):
    """Base exception for AgentOSClient errors."""

    def __init__(self, status_code: int, message: str, body: Any = None):
        super().__init__(f"AgentOSClientError({status_code}): {message}")
        self.status_code = status_code
        self.message = message
        self.body = body


class AgentOSClient:
    """Client for AgentOS External API suite."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request_sync(self, method: str, path: str, json_body: Any = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = self._headers()

        if _HAVE_HTTPX:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.request(method, url, headers=headers, json=json_body)
                if not resp.is_success:
                    try:
                        err_data = resp.json()
                    except Exception:
                        err_data = resp.text
                    raise AgentOSClientError(resp.status_code, str(err_data), body=err_data)
                return resp.json()

        # Stdlib urllib fallback
        data = json.dumps(json_body).encode("utf-8") if json_body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw_err = exc.read().decode("utf-8", "replace")
            try:
                err_data = json.loads(raw_err)
            except Exception:
                err_data = raw_err
            raise AgentOSClientError(exc.code, str(err_data), body=err_data) from exc
        except Exception as exc:
            raise AgentOSClientError(500, str(exc)) from exc

    async def _request_async(self, method: str, path: str, json_body: Any = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = self._headers()

        if _HAVE_HTTPX:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.request(method, url, headers=headers, json=json_body)
                if not resp.is_success:
                    try:
                        err_data = resp.json()
                    except Exception:
                        err_data = resp.text
                    raise AgentOSClientError(resp.status_code, str(err_data), body=err_data)
                return resp.json()

        import asyncio

        return await asyncio.to_thread(self._request_sync, method, path, json_body)

    # ── Synchronous Methods ──

    def create_goal(
        self,
        name: str,
        objective: str = "",
        description: str = "",
        priority: str = "medium",
        provenance: Optional[dict[str, Any]] = None,
        resources: Optional[list[dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Initiate work with Merkle provenance and resource mounting."""
        body = {
            "name": name,
            "objective": objective,
            "description": description,
            "priority": priority,
            "provenance": provenance,
            "resources": resources,
            **kwargs,
        }
        return self._request_sync("POST", "/api/v1/goals", body)

    def get_pending_questions(self, goal_id: str) -> dict[str, Any]:
        """Surface active human approval gates inside the goal."""
        return self._request_sync("GET", f"/api/v1/goals/{goal_id}/questions")

    def answer_question(
        self,
        goal_id: str,
        question_id: int | str,
        verdict: str = "approve",
        text: str = "",
        checks: Optional[list[dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Record decision and settle human gate."""
        body = {
            "verdict": verdict,
            "text": text,
            "checks": checks,
            **kwargs,
        }
        return self._request_sync(
            "POST",
            f"/api/v1/goals/{goal_id}/questions/{question_id}/answer",
            body,
        )

    def get_registered_actions(self, goal_id: str) -> dict[str, Any]:
        """Query/harvest new action templates produced by the goal."""
        return self._request_sync("GET", f"/api/v1/goals/{goal_id}/actions")

    def get_deliverables(self, goal_id: str) -> dict[str, Any]:
        """Alias for get_registered_actions."""
        return self.get_registered_actions(goal_id)

    # ── Asynchronous Methods ──

    async def acreate_goal(
        self,
        name: str,
        objective: str = "",
        description: str = "",
        priority: str = "medium",
        provenance: Optional[dict[str, Any]] = None,
        resources: Optional[list[dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        body = {
            "name": name,
            "objective": objective,
            "description": description,
            "priority": priority,
            "provenance": provenance,
            "resources": resources,
            **kwargs,
        }
        return await self._request_async("POST", "/api/v1/goals", body)

    async def aget_pending_questions(self, goal_id: str) -> dict[str, Any]:
        return await self._request_async("GET", f"/api/v1/goals/{goal_id}/questions")

    async def aanswer_question(
        self,
        goal_id: str,
        question_id: int | str,
        verdict: str = "approve",
        text: str = "",
        checks: Optional[list[dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        body = {
            "verdict": verdict,
            "text": text,
            "checks": checks,
            **kwargs,
        }
        return await self._request_async(
            "POST",
            f"/api/v1/goals/{goal_id}/questions/{question_id}/answer",
            body,
        )

    async def aget_registered_actions(self, goal_id: str) -> dict[str, Any]:
        return await self._request_async("GET", f"/api/v1/goals/{goal_id}/actions")

    async def aget_deliverables(self, goal_id: str) -> dict[str, Any]:
        return await self.aget_registered_actions(goal_id)
