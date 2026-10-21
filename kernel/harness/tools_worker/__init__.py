"""Worker-side tool implementations.

These cannot run in the sandbox, and each for a reason the sandbox cannot
satisfy: `story_commit` needs git and the real workspace; `skill` reads the
profile, which is the tool surface itself and so must not cross the bridge;
`ask_human` and `register_plan` write to the database and park the run;
`web_fetch` needs the SSRF guard, and the sandbox shares the worker's
network namespace. The sandbox holds no credentials, and that is the line.

Split out of `executor.py` so that file stays the routing and interceptor
layer, and split again by family when it reached 400 lines. Each function
returns a ToolResult; none raises across the call, because an exception
here becomes an opaque failure the model cannot act on.

RE-EXPORTED FLAT, deliberately. `executor.py` dispatches `tools_worker.X`
and `test_tool_bindings` asserts every such name is defined — moving the
functions between modules must not change either. The names are the
contract; the files are housekeeping.
"""
from __future__ import annotations

from kernel.harness.tools_worker.actions import register_action_template
from kernel.harness.tools_worker.ask import ask_human
from kernel.harness.tools_worker.commit import story_commit
from kernel.harness.tools_worker.plan import register_plan
from kernel.harness.tools_worker.read import artifact_read, web_fetch
from kernel.harness.tools_worker.reply import reply_to_human
from kernel.harness.tools_worker.review import submit_story_review
from kernel.harness.tools_worker.skills import skill

__all__ = ["actions", "ask_human", "artifact_read", "register_action_template",
           "register_plan", "reply_to_human", "skill", "story_commit",
           "submit_story_review", "web_fetch"]
