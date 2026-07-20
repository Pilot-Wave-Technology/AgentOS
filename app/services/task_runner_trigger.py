"""Fire-and-forget trigger for the task-runner Cloud Run Job — the generic
async executor (see `task_runner/`). Extracted from `app/routers/
goal_resources.py` (its original, only caller) so the "get a GCP access
token, POST to the Cloud Run Jobs API" logic lives in exactly one place —
the goal-weightage recompute watchdog
(`app/services/taximeter/weightage_watchdog.py`) needs the same trigger
and would otherwise have to duplicate it.
"""
import asyncio
import logging

_log = logging.getLogger(__name__)


async def trigger_task_runner(context: str) -> None:
    """Fire-and-forget: trigger the task-runner Cloud Run Job.

    The task runner is a generic async executor: it polls the DB for
    pending work across every registered handler — no env var overrides
    needed. `run.jobs.run` permission is sufficient (no `runWithOverrides`
    required). Does nothing if `TASK_RUNNER_JOB_NAME` is not configured
    (local dev / disabled). `context` is a short label for log lines only
    (e.g. a resource_id, or "weightage-watchdog") — it has no effect on
    what the job actually does, since the job self-discovers its own work.
    """
    _log.info("trigger_task_runner: enter — context=%s", context)

    import google.auth  # noqa: PLC0415
    import google.auth.transport.requests as _gauth_req  # noqa: PLC0415
    import httpx  # noqa: PLC0415

    from app.config import get_settings  # noqa: PLC0415
    settings = get_settings()
    job_name = settings.task_runner_job_name
    if not job_name:
        _log.warning(
            "trigger_task_runner: TASK_RUNNER_JOB_NAME not set — skipping context=%s",
            context,
        )
        return

    project = settings.firebase_project_id
    region = "${GCP_REGION}"
    url = (
        f"https://run.googleapis.com/v2/projects/{project}/locations/{region}"
        f"/jobs/{job_name}:run"
    )
    _log.info(
        "trigger_task_runner: fetching access token for project=%s job=%s",
        project, job_name,
    )

    def _get_access_token() -> str:
        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        creds.refresh(_gauth_req.Request())
        return creds.token  # type: ignore[attr-defined]

    try:
        token = await asyncio.to_thread(_get_access_token)
        _log.info("trigger_task_runner: token obtained, posting to %s", url)
        # No overrides — the job queries the DB for pending work itself.
        # This avoids needing run.jobs.runWithOverrides (run.jobs.run suffices).
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                url, json={},
                headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code in (200, 201):
            _log.info(
                "trigger_task_runner: execution created — context=%s job=%s HTTP %d",
                context, job_name, resp.status_code,
            )
        else:
            _log.error(
                "trigger_task_runner: API rejected trigger — context=%s "
                "HTTP %d body=%s",
                context, resp.status_code, resp.text[:500],
            )
    except Exception:
        _log.error(
            "trigger_task_runner: unhandled exception — context=%s",
            context, exc_info=True,
        )
