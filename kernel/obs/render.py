"""Record plus fields, out as a line. Two renderers, same fields.

WHY TWO. Cloud Logging wants one JSON object per line and reads it off
stdout with no SDK. A person at a terminal wants to see the message,
not hunt for it inside eighty columns of JSON. Same field set, two
renderings, chosen by AGENTOS_LOG_FORMAT -- so a laptop is readable and
production is queryable without either being a special case.

WHY NO SDK. google-cloud-logging would put a network call in the logging
path and bind the process to GCP. Cloud Run, GKE and the VM's ops agent
all read stdout. The two logging.googleapis.com keys in fields.py are
inert bytes anywhere else.
"""

from __future__ import annotations

import json
import logging
import os
import traceback

from kernel.obs import fields as F


class JsonRenderer(logging.Formatter):
    """One JSON object per line, shaped for Cloud Logging."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "severity": F.SEVERITY.get(record.levelno, "DEFAULT"),
            "message": record.getMessage(),
            "logger": record.name,
            F.SOURCE_KEY: {
                "file": record.pathname,
                "line": record.lineno,
                "function": record.funcName,
            },
        }
        payload.update(getattr(record, "agentos", {}) or {})

        run_id = payload.get("run_id")
        if run_id:
            trace = F.trace_value(str(run_id))
            if trace:
                payload[F.TRACE_KEY] = trace

        if record.exc_info:
            # A dedicated field, not newlines inside `message`: Error
            # Reporting groups on this and cannot parse an embedded one.
            payload["exception"] = "".join(
                traceback.format_exception(*record.exc_info))

        return json.dumps(payload, default=str)


class TextRenderer(logging.Formatter):
    """For a human at a terminal. Ids trail the message, not lead it."""

    _ORDER = ("event", "goal_id", "story_id", "execution_id", "leg", "tool")

    def format(self, record: logging.LogRecord) -> str:
        ctx = getattr(record, "agentos", {}) or {}
        shown = [f"{k}={ctx[k]}" for k in self._ORDER if ctx.get(k) is not None]
        tail = ("  [" + " ".join(shown) + "]") if shown else ""
        base = "%-8s %-34s %s%s" % (
            F.SEVERITY.get(record.levelno, "DEFAULT"),
            record.name.split(".")[-1][:34],
            record.getMessage(),
            tail,
        )
        if record.exc_info:
            base += "\n" + "".join(traceback.format_exception(*record.exc_info))
        return base


def renderer() -> logging.Formatter:
    """The one the environment asked for. Text is the local default.

    B1: no default inside environ.get -- the fallback is spelled out
    here so it is visible rather than hidden in a call.
    """
    want = (os.environ.get("AGENTOS_LOG_FORMAT") or "").lower()
    if want == "json":
        return JsonRenderer()
    if want == "text":
        return TextRenderer()
    # Unset: JSON wherever a log collector is reading stdout, text on a
    # laptop. K_SERVICE is Cloud Run's; KUBERNETES_SERVICE_HOST is k8s.
    in_cloud = bool(os.environ.get("K_SERVICE")
                    or os.environ.get("KUBERNETES_SERVICE_HOST"))
    return JsonRenderer() if in_cloud else TextRenderer()
