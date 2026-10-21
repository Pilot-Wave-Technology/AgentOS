"""FastAPI application entry point."""
import asyncio
import logging
import os
import sys

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.auth import SignupRequired, SignupPending, SignupRejected
from app.config import get_settings
from app.error_responses import unauthorized_response, wants_html
from app.database import init_db, close_db
from app.openclaw import session_manager, gateway_admin
from app.services.agent_registry import soft_delete_agent
from app.task_controller import task_controller
from app.routers import auth, goals, agents, chat, tasks, monitor, stories, orchestrator
from app.routers import council_thread, security_thread
from app.routers import admin_controls  # kill switch: pause/resume/force-kill
from app.routers import notifications, resources, memory, assistant, goal_thread, go_internal, internal_assistant, goal_resources, skills
# SHCA→Assistant rename compat — remove after zero /internal/shca/* traffic in prod (see documentation/SHCA_TO_ASSISTANT_RENAME.md)
from app.routers.internal_assistant import legacy_router as _internal_shca_legacy_router
from app.routers import agent_lifecycle
from app.routers import agent_auth  # POST /internal/agent-token (shared-token fix)
from app.routers import git_credential  # GET/POST /internal/git-credential (JIT git creds)
from app.routers import resource_object  # GET /internal/resource-object (granted document bytes)
from app.routers import artifact_object  # POST /internal/artifact (emitted artifact bytes)
from app.routers import artifact_exists  # GET /internal/artifact-exists (checks read back)
from app.routers import execution_jobs  # POST /internal/execution-jobs (what a long job is doing)
from app.routers import story_review  # POST /internal/story-review (the pod cannot write it)
from app.routers import admin_timings as admin_timings_router
from app.routers import model_registry as model_registry_router
from app.routers import local_execution  # /internal/local/* — Local Execution Mode (sign-in/checkout)
from app.routers import local_execution_run  # noqa: F401 — attaches exec/landing routes to the shared router
from app.routers import extension_dist  # /extension/* — VS Code extension manifest + .vsix download
from app.routers import platform_internal, platform_routes
from app.routers import gcp_oauth
from app.routers import templates as templates_router
from app.routers import template_authoring as template_authoring_router
from app.routers import tools as tools_router
from app.routers import agent_registry as agent_registry_router
from app.routers.custom_tools import admin_routes as custom_tools_admin
from app.routers.custom_tools import api_routes as custom_tools_api
from app.routers.custom_tools import internal_routes as custom_tools_internal
from app.routers.council import internal_routes as council_internal
from app.routers.council import routes as council_routes
from app.routers.security import internal_routes as security_internal
from app.routers.security import routes as security_routes
from app.routers import git_graph as git_graph_routes
from app.memory_v2 import routers as memory_v2_routers
from app.memory_v2 import snapshot as memory_v2_snapshot
from app.routers import user_stream as user_stream_router
from app import local_dev
from app import proxy_client
from app import proxy_routes

import json as _json

from app.config import get_settings as _get_settings
from kernel.obs import configure as _obs_configure


# ONE CONFIGURATION, SHARED. This used to be a hand-rolled formatter
# here, a verbatim copy of it in app/management_worker.py, and a bare
# basicConfig in app/council_worker.py, app/git_engine_worker.py and
# three kernel entrypoints -- so two of four Cloud Run services emitted
# plain text, every line landed as DEFAULT severity, and an ERROR in the
# git engine could not be filtered for.
#
# kernel/obs also stamps the ids: run_id, goal_id, story_id, work_kind,
# execution_id and leg, from a contextvar, onto every record -- so one
# query in Logs Explorer returns everything that happened to a run,
# without a single call site changing. It lives in kernel/ because
# kernel must never import from app/, and both halves need it.
_obs_configure(component="control-panel", level=_get_settings().log_level)

# The module logger. THERE WAS NONE, and five exception handlers in
# on_startup() called `_log.exception(...)` against a name bound only
# inside the dashboard refresh loop (since moved to
# app/services/dashboard_refresh.py) -- so each of them raised NameError at
# the moment it was trying to report a failure, turning a logged warning
# into an unhandled one. scripts/check_unbound_names.py has been
# reporting all five.
_log = logging.getLogger("app.main")

app = FastAPI(title="Multi-Agent Control Panel", docs_url=None, redoc_url=None)

# Static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Temporary test MCP server — remove after MCP integration testing

# Register routers
app.include_router(auth.router)
app.include_router(extension_dist.router)  # /extension/* — VS Code extension dist (before proxy)
app.include_router(admin_controls.router)  # kill switch: /settings/controls/*
# Settings sub-pages that use gateway_admin and must stay on the main service —
# registered BEFORE the proxy so /settings/* is intercepted here, not forwarded.
app.include_router(agent_registry_router.router)    # /settings/agent-registry*
app.include_router(skills.router)                   # /settings/skills*
# Refreshing a LIVE agent's prompts/tools/skills in place. Two routes, not
# one with a flag: the plan is a GET and the apply is a POST, so the
# fleet-wide write has to be asked for by name.
from app.routers import agent_refresh_routes            # noqa: E402
app.include_router(agent_refresh_routes.router)     # /settings/agents/refresh*
app.include_router(tools_router.router)             # /settings/tools* + /settings/tool-profiles*
# Template wizard uses gateway_admin (model list) and its routes are under
# /settings/agent-registry/new* — must be before proxy.
app.include_router(template_authoring_router.router)  # /settings/agent-registry/new*
# /settings/model-registry* (Phase A4 admin UI) + /internal/model_catalog
# (Phase A2 gateway pull). Must be BEFORE proxy so /settings/model-registry
# is served here; management service does not register this router. Unlike
# the other before-proxy routers, this one uses get_current_user + the DB
# pool directly (not gateway_admin), but the placement rule is the same.
app.include_router(admin_timings_router.router)  # /settings/timings
app.include_router(model_registry_router.router)  # /settings/model-registry* + /internal/model_catalog
# Management plane proxy — must be registered AFTER admin_controls + gateway-dependent
# routers so their /settings/* paths are matched first.
app.include_router(proxy_routes.router)
app.include_router(goals.router)
app.include_router(agents.router)
app.include_router(assistant.router)  # Must be before chat — pick-assistant route conflicts with chat/{agent_id}
app.include_router(goal_thread.router)
app.include_router(chat.router)
app.include_router(stories.router)
app.include_router(orchestrator.router)
app.include_router(council_thread.router)   # /goals/{pid}/council + /ws — Goal Review Council channel (Agent Mesh sibling)
app.include_router(security_thread.router)  # /goals/{pid}/security + /ws — Security Council channel (Agent Mesh sibling)
app.include_router(tasks.router)
app.include_router(monitor.router)
app.include_router(notifications.router)
app.include_router(goal_resources.router)  # Must be before resources — /browser route conflicts with /{rid}
app.include_router(gcp_oauth.router)
app.include_router(resources.router)
app.include_router(memory.router)
app.include_router(go_internal.router)
app.include_router(agent_auth.router)     # /internal/agent-token — per-agent JWT minting (inert until enabled)
app.include_router(git_credential.router)  # /internal/git-credential — JIT git creds for story workspaces
# The kernel builds workspaces in a container with no cloud
# credentials and a rule that it stays cloud-free, so a granted
# document's bytes come from here -- same reason as the line above.
app.include_router(resource_object.router)
app.include_router(artifact_object.router)
app.include_router(artifact_exists.router)
app.include_router(execution_jobs.router)
app.include_router(story_review.router)
app.include_router(local_execution.router)  # /internal/local/* — checkout lifecycle (M0)
# agent_lifecycle.router not mounted — its endpoint is superseded by
# tasks.agent_lifecycle_webhook which is registered first. Token tracking
# logic lives in agent_lifecycle.record_token_usage() called from there.
app.include_router(internal_assistant.router)
app.include_router(_internal_shca_legacy_router)  # SHCA rename compat — see import comment above + documentation/SHCA_TO_ASSISTANT_RENAME.md
app.include_router(templates_router.router)
app.include_router(custom_tools_admin.router)
app.include_router(custom_tools_api.router)
app.include_router(custom_tools_internal.router)
app.include_router(memory_v2_routers.router)  # /memory/prompts + /admin/memory/prompts
app.include_router(council_internal.router)    # /internal/council/review (RA submissions)
app.include_router(council_routes.router)      # /goals/{pid}/council/run + /status
app.include_router(security_internal.router)   # /internal/security/review (aspect submissions)
app.include_router(security_routes.router)     # /goals/{pid}/security/run + /status
app.include_router(platform_internal.router)   # /internal/platform/* (research council tools)
app.include_router(platform_routes.router)     # /goals/__platform__/research/run + /status
app.include_router(git_graph_routes.router)    # /goals/{gid}/git/reconcile + /git/nodes/* (engine surface)
app.include_router(user_stream_router.router)  # WS /stream/user-events (sidebar badge push)

# The legacy /slack/* router is GONE (2026-10-16). It was a second, divergent
# copy of the whole integration: its own channel-naming scheme, its own
# user-mapping table, and an action dispatcher that recognised only `card_*` --
# so every newer button (diff, recovery, story control) posted by the channels
# stack arrived at a handler that logged "Unrecognized action_id" and returned.
# All Slack traffic is /channels/slack/*, registered below.

# Platform channel adapters (Notion + future platforms)
from app.channels.router import router as channels_router
app.include_router(channels_router)
logging.getLogger(__name__).info("Platform channels router registered")

# Git webhook receiver — POST /webhooks/git (GitHub + GitLab inbound events)
from app.services.webhooks import webhook_router
app.include_router(webhook_router)

# The parked-executor surface: what the kernel is waiting on, and the one
# path that may approve a plan (it is the only caller that knows which
# human is on the other end -- H8a).
from app.routers import kernel_park as _kernel_park_router  # noqa: E402
app.include_router(_kernel_park_router.router)
# I5f — deciding GO's errand proposals. Its own module: kernel_park is
# about a run already waiting, this is about work not yet authorised to
# start. Registered after, because it imports that router's membership
# helper rather than growing a fourth copy of it.
from app.routers import kernel_errands as _kernel_errands_router  # noqa: E402
app.include_router(_kernel_errands_router.router)
# D2 — seeing and downloading what a run produced. An errand's
# deliverable IS its artifacts, and until this the only way to reach one
# was to be an agent holding `read_artifact` — so a person could read an
# agent's summary of a report and never the report.
from app.routers import artifacts as _artifacts_router  # noqa: E402
app.include_router(_artifacts_router.router)

# Pause / Resume / Cancel for the executor working a thread's story. Thread
# id in the path, never a story id in the body — the conversation is what
# scopes the control (app/services/kernel_run_control.py).
from app.routers import kernel_control as _kernel_control_router  # noqa: E402
app.include_router(_kernel_control_router.router)
from app.routers import goal_api as _goal_api_router  # noqa: E402
app.include_router(_goal_api_router.router)
logging.getLogger(__name__).info("Git webhook receiver registered")

templates = Jinja2Templates(directory="app/templates")

# Expose role label maps to every template so role values can be displayed
# with their canonical namespaced labels (platform.admin, goal.member, etc.)
from app.roles import SYSTEM_ROLE_LABELS, GOAL_ROLE_LABELS  # noqa: E402
templates.env.globals["SYSTEM_ROLE_LABELS"] = SYSTEM_ROLE_LABELS
templates.env.globals["GOAL_ROLE_LABELS"] = GOAL_ROLE_LABELS


def _static_mtime(static_relative_path: str) -> int:
    """Cache-busting token for a file under static/, from its mtime at boot.

    Browsers cache /static/* indefinitely since app.css/app.js are served
    with no versioning in the URL. Appending ?v=<mtime> forces a fetch of
    the new file the moment a deploy changes it, instead of relying on
    users to hard-refresh. Read once at import time (not per-request) since
    every Cloud Run revision is a fresh container with immutable static files.
    """
    try:
        return int(os.path.getmtime(os.path.join("static", static_relative_path)))
    except OSError:
        return 0


# Templates are rendered via ~30 separate Jinja2Templates() instances across
# routers, each with its own isolated Environment — a global registered on
# main.py's `templates.env` would not be visible to templates rendered by
# other routers. request.state is the one thing every TemplateResponse call
# shares (Starlette requires `request` in the render context), so cache-bust
# tokens are threaded through there instead, alongside firebase config below.
_STATIC_CSS_VERSION = _static_mtime("css/app.css")
_STATIC_JS_VERSION = _static_mtime("js/app.js")
_STATIC_TOUR_JS_VERSION = _static_mtime("js/tour.js")

# ONE TOKEN PER FILE, ON DEMAND -- because three hand-written constants
# is how the other nineteen script tags came to have no token at all.
#
# WHAT THAT COST. `/static/js/kernel_park_card.js` is served with no
# versioning, so a browser that has it keeps it. A fix to the card
# renderer shipped, deployed green, and every returning tab went on
# running the old file -- the bug reproduced perfectly for the person
# testing it while the fix sat on the server. Indistinguishable from a
# fix that does not work, and the wrong thing gets changed next.
#
# Memoised: `getmtime` per file per process, and a Cloud Run revision is
# a fresh container with immutable static files, so one read is right.
_ASSET_VERSIONS: dict[str, int] = {}


def _asset_version(static_relative_path: str) -> int:
    """`?v=` token for any file under static/."""
    if static_relative_path not in _ASSET_VERSIONS:
        _ASSET_VERSIONS[static_relative_path] = _static_mtime(
            static_relative_path)
    return _ASSET_VERSIONS[static_relative_path]


@app.middleware("http")
async def inject_firebase_config(request: Request, call_next):
    """Make Firebase config + static-asset cache-bust versions available to all templates."""
    settings = get_settings()
    request.state.firebase_api_key = settings.firebase_web_api_key or ""
    request.state.firebase_project_id = settings.firebase_project_id or ""
    # host:port of the Firebase Auth Emulator ('' unless LOCAL_SERVER on). The
    # login/signup/base templates call auth.useEmulator(...) only when set.
    request.state.firebase_auth_emulator_host = local_dev.firebase_auth_emulator_host()
    request.state.static_css_version = _STATIC_CSS_VERSION
    request.state.static_js_version = _STATIC_JS_VERSION
    request.state.static_tour_js_version = _STATIC_TOUR_JS_VERSION
    # Templates call this for every other static file they pull in.
    request.state.asset_v = _asset_version
    return await call_next(request)


_chat_fanout_listener = None
_chat_dispatcher = None


def _scheduler_loops_run_here() -> bool:
    """Whether THIS process runs the loops the scheduler service owns.

    True unless something says otherwise, because a control panel with
    no scheduler beside it -- dev, feature, a laptop -- must keep
    working exactly as it did. cloudbuild-next sets
    AGENTOS_SCHEDULER_LOOPS=0 on the web tier once the scheduler is
    deployed alongside it.
    """
    import os as _os

    return _os.environ.get("AGENTOS_SCHEDULER_LOOPS", "1").strip() != "0"


@app.on_event("startup")
async def on_startup():
    # Publish local-dev posture (emulator hosts → os.environ) and log it loudly
    # before anything that reads those env vars. No-op unless LOCAL_SERVER=true.
    local_dev.configure_local_environment()
    proxy_client.create_client()
    await init_db()

    # The assistant's prompt files are authoritative on DISK, not in the
    # migration that seeded them. The migration embedded a copy as SQL
    # literals, so the bytes the model received and the bytes H8b gated
    # came from different places -- and drifted within a day, silently.
    # Reconciling here means the repository always wins, and the log
    # says so when it had to.
    #
    # AND ALL THE WAY TO THE AGENTS ALREADY RUNNING. Reconciling
    # `template_files` alone reaches goals created AFTER this boot and no
    # existing GO or assistant at all — so every prompt fix written
    # during a build missed the one goal being tested with it, which is
    # always an existing one. `kernel_prompt_resync` pushes what moved.
    #
    # OFF BY DEFAULT SINCE 2026-09-23, and the code is kept rather than
    # deleted. What it does is real and the gap it was written for is
    # real -- but on this gateway `session.reset` is
    # `{mode: idle, idleMinutes: 86400}`, so sessions never roll over,
    # so a pushed prompt is read by NOTHING until the gateway restarts.
    # This ran on every boot, wrote identical bytes, recorded nothing,
    # and churned the mtimes of the very files a person inspects to work
    # out who last wrote them.
    #
    # The refresh page at /settings/agents/refresh does the same
    # RECONCILE AND PUSH deliberately, with a preview, alongside tools
    # and skills, and tells you to restart.
    #
    # THAT SENTENCE USED TO SAY ONLY "push", AND THE WORD WAS LOAD-
    # BEARING. `resync` does two things -- reconcile the repository's
    # prompts into `template_files`, then push that table to live
    # agents -- and the page only ever did the second. So with this
    # switched off nothing wrote the repository's prompts into the
    # table at all, and the page could not see a prompt change however
    # often it was run. On 2026-09-24 a fleet refresh reported
    # `12 ok, 0 failed, 0 PARTIAL` and delivered the previous build's
    # prompts. `agent_refresh.run._reconcile_from_disk` now does the
    # first half there, which is what makes this switch genuinely
    # optional rather than quietly load-bearing.
    #
    # Switch this back on from that page if an unplanned gateway restart
    # -- an openclaw deploy done for its own reasons -- should pick up
    # current prompts without anybody having thought about it.
    try:
        from app.database import get_pool as _get_pool
        from app.services.kernel_prompt_resync import (
            RESYNC_ON_BOOT_KEY, resync as _resync)
        from app.services.platform_settings import get as _setting
        if not (await _setting(RESYNC_ON_BOOT_KEY))["value"]:
            _log.info(
                "kernel prompt resync: OFF (platform_settings.%s). Prompts "
                "are pushed from /settings/agents/refresh instead.",
                RESYNC_ON_BOOT_KEY)
        else:
            async with _get_pool().acquire() as _c:
                # force=True, because TWO processes reconcile these files:
                # this one and E0's assistant-api. Whichever boots first
                # absorbs the change, so "push only what I changed" means the
                # other one pushes nothing and the live agents keep the old
                # prompt — which is exactly what happened to KER25. The push
                # is idempotent and costs two files per template.
                await _resync(_c, force=True)
    except Exception:
        # A prompt that could not be reconciled must not stop the
        # service -- the previous content is still serviceable, and an
        # assistant with a stale prompt beats no control panel.
        _log.exception("assistant prompt sync failed; DB copy left as-is")

    # The kernel tool surface, on THIS app rather than its own port.
    # api.py's standalone server is right for E0 and cannot deploy --
    # Cloud Run gives a container one port, so the gateway would be
    # pointed at a URL nothing answers. Same handlers, same route sets,
    # generated rather than restated so a third declaration of the tool
    # surface cannot drift from the other two.
    try:
        from app.database import get_pool as _kp
        from kernel.assistant.asgi import build_router as _kernel_router
        from kernel.assistant.asgi import kernel_acquire as _kernel_acquire
        # The kernel schema is not always in the control panel's own
        # database -- locally it is a separate server, and both have a
        # `goal_resources` table with different columns.
        app.include_router(
            _kernel_router(_kernel_acquire(lambda: _kp().acquire())))
    except Exception:
        # The control panel must still serve if the kernel surface will
        # not mount; an assistant that cannot reach its tools reports a
        # typed E_KERNEL_UNREACHABLE, which is legible. A dead control
        # panel is not.
        _log.exception("kernel assistant surface did not mount")

    # G1: the GO wake router. Without it, ask_go writes a row nobody
    # reads and the assistant waits on an orchestrator that was never
    # told. Non-fatal on failure -- asks stay durable and unrouted, and a
    # restart picks them up.
    try:
        from app.services.go_wakes import run_forever as _go_wakes
        import asyncio as _aio
        _aio.get_event_loop().create_task(_go_wakes())
    except Exception:
        _log.exception("go_wakes router did not start")

    # The GO's read_resource fetchers. The kernel never imports the
    # control panel; the capability is injected where the control panel
    # hosts the tools -- its GitHub-App fetcher for repo files and its
    # GCS fetcher for uploaded originals. The standalone E0 surface
    # registers neither and refuses honestly.
    try:
        from kernel.go import read as _go_read
        from app.github_files import fetch_file_from_repo as _ff

        async def _repo_fetch(repo_name, path, ref):
            return await _ff(repo_name, path, ref)

        async def _doc_fetch(resource_id, filename):
            from app.storage import download_resource_file
            return await download_resource_file(resource_id, filename)

        async def _extract(filename, blob):
            from app.services.resource_scanner import (_extract_excel,
                                                       _extract_pdf,
                                                       _extract_word)
            low = (filename or "").lower()
            if low.endswith(".pdf"):
                got = await _extract_pdf(blob, filename)
            elif low.endswith((".xlsx", ".xls")):
                got = await _extract_excel(blob, filename)
            elif low.endswith(".docx"):
                got = await _extract_word(blob, filename)
            else:
                return None
            return got.get("text") or None

        _go_read.set_repo_fetcher(_repo_fetch)
        _go_read.set_doc_fetcher(_doc_fetch)
        _go_read.set_extractor(_extract)

        # AND THE WORKSPACE'S. A document granted to a story is copied
        # into the executor's workspace at docs/<name>, and the object
        # store it comes from is the control panel's -- so the same
        # credential seam serves both. Synchronous, because the
        # workspace drivers are: the worker calls acquire() off the
        # event loop.
        def _fetch_object(uri: str) -> bytes:
            from app.storage import download_resource_uri
            return download_resource_uri(uri)

        from kernel.workspace import bindings as _ws_bindings
        _ws_bindings.set_object_fetcher(_fetch_object)
    except Exception:
        _log.exception("GO read_resource fetchers not registered")

    # A parked executor announces itself in the story thread. Without
    # this the person was told to go and approve a plan in the E0
    # console -- a page built for driving tests -- and the plan text was
    # not in the message that told them to.
    try:
        from app.services.kernel_park_cards import run_forever as _park_cards
        # MOVED TO THE SCHEDULER, and gated rather than deleted.
        #
        # `app/scheduler_worker.py` runs these with CPU always allocated
        # and exactly one leader. Here they only advanced while a
        # request was in flight -- a browser WebSocket kept them warm,
        # so they worked while somebody watched and stalled overnight --
        # and every instance ran its own copy, up to maxScale.
        #
        # The env var rather than deleting the calls: dev, feature and a
        # laptop run the control panel with no scheduler beside it, and
        # for them the old behaviour is still the right one. Default ON,
        # so nothing changes for a deployment that has not opted out;
        # cloudbuild-next sets it to 0 on the control panel and the
        # scheduler picks them up.
        if _scheduler_loops_run_here():
            _aio.get_event_loop().create_task(_park_cards())
    except Exception:
        _log.exception("kernel park cards did not start")

    task_controller.start()
    import asyncio
    # chat_module fan-out (Phase 2): deliver queued user messages + dispatcher
    # replies to local browser subscribers across instances. Only when enabled.
    try:
        from app.chat_module.config import get_chat_config as _get_chat_cfg
        _ccfg = _get_chat_cfg()
        if _ccfg.queue_enabled:
            from app.chat_module.gateways import bootstrap_self as _gw_bootstrap
            await _gw_bootstrap()
            # Co-located dispatcher: drains the durable queue and drives the
            # EXISTING sessions (so on_message + process_go_response + all reply
            # side-effects fire). Every instance runs it; pg advisory locks
            # serialize per thread.
            from app.chat_module.dispatcher import Dispatcher
            global _chat_dispatcher
            _chat_dispatcher = Dispatcher()
            await _chat_dispatcher.start()
            # Cross-instance fan-out of queued user messages to local browser
            # subscribers (replies are delivered by on_message in-process).
            if _ccfg.fanout_enabled:
                from app.chat_module import fanout as _chat_fanout
                from app.database import get_pool as _get_pool

                async def _deliver(goal_id: str, agent_id: str,
                                   thread_id: str, frame: dict):
                    """To BOTH subscriber registries, because there are two.

                    A browser on the assistant chat page is registered
                    with `session_manager`; a browser on the GOAL THREAD
                    page is registered with `goal_thread_subs`, which
                    `session_manager` knows nothing about. This function
                    only ever reached the first, so a card posted into
                    the goal thread -- GO's errand proposal, above all --
                    arrived in the database, was pushed at an empty
                    audience, and appeared only when somebody reloaded.
                    Reported as "the approval card needs a refresh", and
                    it is: the poster was never at fault. It is
                    byte-identical to the one whose cards land live.

                    BOTH, UNCONDITIONALLY -- and the middle version of
                    this made it a fallback, which was a mistake worth
                    recording. Frames were arriving twice, so this sent
                    to the goal-thread registry only when the session
                    registry reached nobody. That does stop the double,
                    and it introduces a worse fault: a STALE session
                    subscriber -- a socket whose browser is gone but
                    which has not yet been reaped -- counts as "reached",
                    and the goal thread page then never gets the frame at
                    all. Intermittent, invisible, and identical in
                    appearance to the bug being fixed. Ask anyone who has
                    had to refresh to see a card that was already there.

                    THE DOUBLE WAS NEVER THIS FUNCTION'S FAULT. The
                    client de-duplicates on the frame's row id and has
                    all along; the guard was reading `msg_id` and
                    `message_id`, which cards carry and a plain `message`
                    frame does not -- it carries `msg.id`. So cards
                    stopped doubling and human messages kept doubling,
                    which is precisely the report. Fixed where it was,
                    in `handleMessage`.

                    Both registries no-op on an empty subscriber set, so
                    delivering to both costs a function call and cannot
                    be wrong.
                    """
                    import json as _j

                    # KEPT FOR THE LOG BELOW, and the last version of
                    # this dropped the assignment while leaving the log
                    # line that reads it. `NameError: reached` then
                    # raised BEFORE `_goal_bc` was ever called, the
                    # except swallowed it as a warning, and the
                    # goal-thread registry received nothing at all --
                    # for every frame, for the life of the deploy.
                    reached = await session_manager.broadcast_to_thread(
                        goal_id, agent_id, thread_id, frame)
                    try:
                        from app.routers.goal_thread_subs import (
                            broadcast as _goal_bc, watcher_count as _goal_n)
                        # DELIVER FIRST, MEASURE SECOND, and that order
                        # is the actual lesson. This logging call exists
                        # only to tell a delivery failure apart from a
                        # render failure -- and placed ahead of the
                        # broadcast it was measuring, it BECAME the
                        # delivery failure. A diagnostic must not sit on
                        # the path of the thing it observes.
                        _n = _goal_n(goal_id, thread_id)
                        await _goal_bc(goal_id, _j.dumps(frame), thread_id)
                        logging.getLogger("app.main").info(
                            "fanout: %s -> goal-thread %s (%d watcher(s), "
                            "session reached %d)",
                            frame.get("type"), thread_id[:12], _n, reached)
                    except Exception:               # noqa: BLE001
                        # A delivery failure is not a delivery: the row
                        # is committed and a reload shows it. Never let
                        # this take down the fan-out listener, which
                        # serves every other thread on this instance.
                        logging.getLogger("app.main").warning(
                            "goal-thread fan-out failed for %s",
                            thread_id[:12], exc_info=True)

                async def _chat_broadcaster(thread_id: str, row: dict):
                    pool = _get_pool()
                    tr = await pool.fetchrow(
                        "SELECT goal_id, agent_id FROM threads WHERE id = $1", thread_id,
                    )
                    if not tr:
                        return
                    _card = row.get("card_data")
                    if _card:
                        # Structured card (question/approval/completion, or the
                        # typed diff_review/dag_recovery cards). asyncpg returns
                        # JSONB as str — parse to a dict and emit the card frame
                        # the client expects, so cross-instance browsers render it
                        # (not a plain text bubble). Falls through to text on error.
                        import json as _json
                        try:
                            _cardobj = _card if isinstance(_card, dict) else _json.loads(_card)
                        except Exception:
                            _cardobj = None
                        if _cardobj is not None:
                            # diff_review / dag_recovery render via dedicated client
                            # handlers keyed on frame.type == the card kind; their
                            # live frame = card_data fields + the row's content
                            # (message body). Everything else is a generic agent_card.
                            _kind = _cardobj.get("kind")
                            if _kind in ("diff_review", "dag_recovery"):
                                _frame = {"type": _kind, "msg_id": row.get("id"),
                                          "content": row.get("content", ""), **_cardobj}
                            else:
                                _frame = {"type": "agent_card", "msg_id": row.get("id"),
                                          **_cardobj}
                            await _deliver(tr["goal_id"], tr["agent_id"],
                                           thread_id, _frame)
                            return
                    if row.get("role") == "agent":
                        frame = {"type": "token_stream_end",
                                 "full_content": row.get("content", ""),
                                 "message_id": row.get("id")}
                    else:
                        # Resolve display_name so the bubble never shows the raw UID.
                        # Also include sender so the JS can skip self-originated
                        # messages that were already optimistically rendered.
                        _sender = row.get("sender", "")
                        _display = await pool.fetchval(
                            "SELECT display_name FROM users WHERE uid = $1", _sender,
                        ) or ""
                        frame = {"type": "human_message", "message_id": row.get("id"),
                                 "sender": _sender,
                                 "sender_name": _display,
                                 "origin": "slack" if row.get("platform") == "slack" else "other",
                                 "content": row.get("content", "")}
                    await _deliver(tr["goal_id"], tr["agent_id"],
                                   thread_id, frame)

                _chat_fanout.set_broadcaster(_chat_broadcaster)
                global _chat_fanout_listener
                _chat_fanout_listener = _chat_fanout.FanoutListener()
                await _chat_fanout_listener.start()
    except Exception:
        import logging as _logging
        _logging.getLogger("app.main").warning(
            "chat_module fan-out init failed", exc_info=True,
        )
    # Goal Review Council dispatcher (Phase 1) — co-located, flag-gated.
    if get_settings().council_dispatcher_enabled:
        from app.services.council.dispatcher import council_dispatcher
        council_dispatcher.start()
        logging.getLogger("app.main").info("Council dispatcher started")

    # Goal Summary scheduler — co-located, flag-gated. Normally runs in the
    # council-worker service. Set GOAL_SUMMARY_ENABLED=true to run here instead.
    if get_settings().goal_summary_enabled:
        from app.services.goal_summary.scheduler import goal_summary_scheduler
        goal_summary_scheduler.start()
        logging.getLogger("app.main").info("Goal summary scheduler started")

    # Security Council dispatcher (P0) — co-located, flag-gated. Normally hosted in
    # the council-worker service; this lets the web tier run it during cutover.
    if get_settings().security_dispatcher_enabled:
        from app.services.security.dispatcher import security_dispatcher
        security_dispatcher.start()
        logging.getLogger("app.main").info("Security dispatcher started")

    # Git engine branch-ready listener (Component C) — web-tier only, flag-gated.
    # Re-provisions a story's assistant once the engine cuts its workstream branch.
    if get_settings().gitgraph_enabled:
        from app.services.git_graph.branch_ready_listener import branch_ready_listener
        branch_ready_listener.start()
        logging.getLogger("app.main").info("Git branch-ready listener started")

    # Assistant cleanup watchdog DISABLED — assistants now live as long as their
    # goal exists. They are destroyed only when the goal is deleted
    # (see goals.py:delete_goal). Reaping idle assistants caused the gateway
    # to delete the workspace including the V2-accumulated MEMORY.md;
    # on user return, the instance was re-provisioned and the agent
    # lost all durable memory. If you ever need to clean up orphans,
    # call cleanup manually or re-enable here with stricter conditions.
    # asyncio.create_task(_assistant_cleanup_watchdog())
    # IA/RA instance reaper — safety net for task-terminal paths that skip
    # the inline destroy_instance() call, e.g. dispatch-failure exceptions
    # and force-killed persistent-worker subtasks. No memory-loss risk
    # (IA/RA are ephemeral, unlike the assistant watchdog above).
    asyncio.create_task(_ia_instance_reaper())
    # Goal-delete orphan reconciler — runs every 5 min, catches gateway
    # agents that survived a failed goal-delete cascade.
    # DASHBOARD REFRESH MOVED TO THE SCHEDULER REGISTRY. It ran here on
    # every web instance, so every instance rebuilt every materialized
    # view -- the work done N times, contending for the same locks. See
    # app/services/dashboard_refresh.py.
    asyncio.create_task(_gateway_orphan_reconciler())
    # GitHub installation-token auto-refresh for active operator stories
    # — every 50 min (GitHub caps tokens at 1h).
    from app.services.token_refresh import run_loop as _tok_refresh_loop
    if _scheduler_loops_run_here():   # see the note at _park_cards
        asyncio.create_task(_tok_refresh_loop())
    # OpenClaw catalog sync — initial sync after 30s startup delay, then every 10 min.
    asyncio.create_task(_catalog_sync_loop())
    # Credential broker cleanup watchdog — backup to the strict-cleanup
    # hook in sentinel_parser/phase_dispatcher. Sweeps tasks whose
    # cleanup_handle the hook failed to revoke. Runs every 30 min.
    from app.services.credential_brokers.watchdog import run_loop as _cred_watchdog_loop
    if _scheduler_loops_run_here():   # see the note at _park_cards
        asyncio.create_task(_cred_watchdog_loop())
    # Deleted-goal session reaper — GO sessions never idle-evict, so a session for a
    # soft-deleted goal reconnects to the gateway forever (a reconnect storm on every
    # gateway restart). Sweep every 5 min: evict those sessions + finalize their
    # agent_instances. Also self-heals via the per-session zombie guard in openclaw.py.
    asyncio.create_task(_deleted_goal_session_reaper())
    # Local Execution Mode checkout reaper — frees crashed/abandoned checkouts and
    # cleans terminal local stories (§14.5 no-stuck-forever guarantee).
    asyncio.create_task(_local_checkout_reaper())
    # GO provisioning repair — catches goals whose GO agent creation failed
    # silently (e.g. gateway unreachable on cold-started Cloud Run instance).
    # Runs once at startup after a 45s delay to let the gateway auth settle.
    asyncio.create_task(_go_provisioning_repair())
    # Skill reconciliation — pushes current DB skill content to all live agent
    # instances on startup. Catches skill updates that landed via migration and
    # bypassed the push-on-write fan-out in skill_sync.py (migrations write
    # directly to the skills table, not through the skills router).
    asyncio.create_task(_skill_reconciliation_startup())
    # Model registry: startup task IS required — the comment that used to
    # sit here (Phase A2: "gateways fetch on their own schedule, no push
    # needed") is stale and was actively concealing a bug. AgentOS DOES
    # push provider auth config (`sync_gateway_model_catalog`,
    # `gateway_sync.py`) because `model-auth.ts` reads `cfg.models.providers`
    # directly, not the pulled catalog. Every `redeploy.sh` run wipes that
    # pushed config (`del(.models)`) and restores only the static
    # `models-synthetic.json` file — which has no native `anthropic`/
    # `google` entries at all. Until this loop's first tick (or an
    # incidental goal creation/admin edit re-triggers the push), a model
    # selection for a native provider silently falls back to the default
    # model, with no error. This self-heal loop bounds that window instead
    # of relying on chance. Safe to run every 5 min unconditionally now
    # that `sync_provider_auth_configs_to_gateway` skips the write (and the
    # restart) when the live config already matches — see that function's
    # docstring.
    # DETACHED, BECAUSE READINESS MUST NOT WAIT ON THE GATEWAY.
    #
    # This was `await`ed, and it is the last thing before startup
    # completes -- so the port stayed shut until two live gateway calls
    # came back (`models.reload`, then the provider-auth `config.set`).
    # Meanwhile the chat dispatcher, started ~250 lines above, has its
    # workers hydrating every live thread against that same gateway. The
    # control plane was racing itself to become ready.
    #
    # MEASURED ON DEV: Cloud Run's startup probe allows 4 minutes. The
    # deploy step took 8m43s on 8b339504 and FAILED outright on
    # af5fa6ac and on 9aa0d37d's first attempt -- "Container failed to
    # become healthy. Startup probes timed out after 4m" -- with the
    # container logs showing a healthy process still working through
    # startup when it was killed. The build list is littered with
    # FAILURE and TIMEOUT for this reason, and it gets worse as dev
    # accumulates threads.
    #
    # Nothing here needs to precede serving traffic. A stale model
    # catalogue degrades a model SELECTION until the first sync lands;
    # an unreachable control panel degrades everything. The periodic
    # self-heal loop below already exists to bound that window, and the
    # detached call is simply its first tick brought forward.
    #
    # The sibling calls on this path -- `_park_cards`, `_go_wakes`,
    # `_ia_instance_reaper`, `_catalog_sync_loop` and a dozen more --
    # are all `create_task` for the same reason. This one was the
    # exception.
    async def _mr_sync_detached() -> None:
        try:
            from app.services.model_registry.gateway_sync import (
                sync_gateway_model_catalog as _mr_sync)
            await _mr_sync()
        except Exception:
            logging.getLogger("app.main").warning(
                "model registry startup self-heal failed — will retry on "
                "the periodic loop", exc_info=True,
            )

    asyncio.create_task(_mr_sync_detached())
    asyncio.create_task(_model_registry_self_heal_loop())
    # Phase A3 (story b134ed98) adds a best-effort DB-vs-on-disk skew
    # check against the new `models` table populated by that story's
    # migrations. Purely observational: logs at INFO on drift, WARNING
    # on failure, and swallows every exception so it can never take
    # down startup. See app/llm_models_bootstrap.py.
    try:
        from app.llm_models import SUPPORTED_MODELS as _sm_supported_models
        from app.llm_models_bootstrap import check_skew as _sm_check_skew
        from app.database import get_pool as _sm_get_pool
        _sm_report = await _sm_check_skew(_sm_get_pool(), _sm_supported_models)
        logging.getLogger("app.main").info(
            "model registry skew check complete: "
            "only_in_db=%d only_on_disk=%d metadata_diff=%d",
            len(_sm_report.only_in_db),
            len(_sm_report.only_on_disk),
            len(_sm_report.metadata_diff),
        )
    except Exception:
        logging.getLogger("app.main").warning(
            "model registry skew check failed", exc_info=True,
        )
    # Provision-retry backstop (B) — retries operator-story workspaces stuck at
    # workspace_state='failed' (any story, git-graph or not), bounded by
    # stories.workspace_provision_attempts, then leaves + surfaces a permanent
    # failure. Turns "stuck until a human re-fires provisioning" into self-healing.
    asyncio.create_task(_provision_retry_backstop())
    # Platform Research Council — ensure the singleton goal + steward, run the
    # worker reaper, the event-driven review backstop, and the (off-by-default)
    # scheduler. All no-op unless platform_council_enabled (dev first).
    try:
        from app.services.platform_council import bootstrap as _pc_bootstrap
        if _pc_bootstrap.is_enabled():
            await _pc_bootstrap.ensure_platform_council()
            asyncio.create_task(_pc_bootstrap.worker_reaper_loop())
            asyncio.create_task(_pc_bootstrap.review_backstop_loop())
            asyncio.create_task(_pc_bootstrap.scheduler_loop())
    except Exception:
        logging.getLogger("app.main").exception("platform council bootstrap failed — non-fatal")
    # webhook_deliveries prune — delete rows older than 7 days every 24 h.
    asyncio.create_task(_webhook_deliveries_prune_loop())
    # DAG janitor — backstop for claude-code phase DAGs that stall silently:
    # rolls up parents whose subtasks are all terminal (missed rollup) and
    # re-dispatches (then fails) phase subtasks stuck in_progress with no live
    # turn. Every 5 min. See app/services/dag_janitor.py.
    from app.services.dag_janitor import run_loop as _dag_janitor_loop
    if _scheduler_loops_run_here():   # see the note at _park_cards
        asyncio.create_task(_dag_janitor_loop())
    # awaiting_merge janitor — a coding story holds its pod between push and
    # merge, so a PR nobody merges holds a pod, a workspace claim and an
    # admission slot indefinitely with nothing in an error state to notice it.
    # Moves the story to `blocked` past the deadline and lets reclaim collect
    # on the signal it already trusts. Same leader gate: two replicas sweeping
    # the same stories would race each other's CAS for no benefit.
    from app.services.awaiting_merge_janitor import run_loop as _awaiting_merge_loop
    if _scheduler_loops_run_here():   # see the note at _park_cards
        asyncio.create_task(_awaiting_merge_loop())
    # Unread badge listener — LISTEN on 'unread_updates' Postgres channel and
    # forward count changes to user-scoped WebSockets (/stream/user-events).
    # Non-critical: a failure only degrades real-time badge updates; the sidebar
    # still shows correct counts on page load from thread_participants directly.
    try:
        from app.unread_listener import start_unread_listener as _start_unread_listener
        _dsn = get_settings().database_url
        asyncio.create_task(_start_unread_listener(_dsn))
        logging.getLogger("app.main").info("Unread badge listener started")
    except Exception:
        logging.getLogger("app.main").warning(
            "Unread badge listener failed to start — real-time badge updates disabled",
            exc_info=True,
        )


async def _go_provisioning_repair():
    """Repair goals whose GO agent creation failed during goal creation.

    Runs once at startup after a 45s delay (enough for the gateway auth
    handshake to complete). Finds active goals with go_thread_id set but
    go_agent_id empty — the signature of a half-provisioned goal — and
    calls create_go_for_goal for each. Safe to run on every restart because
    create_go_for_goal is idempotent.
    """
    _log = logging.getLogger("app.go_repair")
    await asyncio.sleep(45)
    try:
        from app.database import get_pool
        from app.services.go_agent import create_go_for_goal
        pool = get_pool()
        rows = await pool.fetch(
            """SELECT id, name, objective, created_by
               FROM goals
               WHERE status = 'active'
                 AND go_thread_id != ''
                 AND (go_agent_id IS NULL OR go_agent_id = '')""",
        )
        if not rows:
            _log.info("GO provisioning repair: no half-provisioned goals found")
            return
        _log.warning(
            "GO provisioning repair: found %d goal(s) with missing go_agent_id — re-provisioning",
            len(rows),
        )
        for row in rows:
            pid = row["id"]
            try:
                _log.info("GO provisioning repair: re-provisioning GO for goal %s (%s)", pid, row["name"])
                go_result = await create_go_for_goal(
                    goal_id=pid,
                    goal_name=row["name"],
                    objective=row.get("objective") or "",
                    created_by_uid=row.get("created_by") or "",
                )
                await pool.execute(
                    "UPDATE goals SET go_agent_id = $1 WHERE id = $2",
                    go_result["agent_id"], pid,
                )
                _log.info("GO provisioning repair: goal %s repaired — go_agent_id=%s", pid, go_result["agent_id"])
            except Exception as exc:
                _log.error("GO provisioning repair: failed for goal %s: %s", pid, exc)
    except Exception as exc:
        _log.error("GO provisioning repair watchdog error: %s", exc)


async def _skill_reconciliation_startup():
    """Push all active DB skills to live agent instances once on startup.

    Runs after a 45s delay (same as _go_provisioning_repair) to let the
    gateway auth handshake complete before making RPC calls. Catches skill
    content updates that landed via migration — migrations write directly to
    the skills table and bypass the push-on-write fan-out in skill_sync.py.
    """
    _log = logging.getLogger("app.skill_reconciliation")
    await asyncio.sleep(45)
    try:
        from app.services.skill_sync import reconcile_skills_on_startup
        await reconcile_skills_on_startup()
    except Exception as exc:
        _log.error("skill reconciliation startup error: %s", exc)


async def _model_registry_self_heal_loop():
    """Re-push the model catalog + provider auth config every 5 min.

    Bounds the post-deploy window where `redeploy.sh`'s `del(.models)` has
    wiped everything AgentOS previously pushed into the gateway's
    `models.providers.*` (see the startup-block comment above this loop's
    call site for the full failure mode). Without this, that window is
    bounded only by luck — whenever someone next creates a goal or edits
    the registry.

    5 minutes matches the `model-registry` gateway plugin's own
    `REFRESH_INTERVAL_MS` self-heal cadence, so both halves of the
    catalog/auth sync are on the same schedule.

    Cheap in the steady state: `sync_provider_auth_configs_to_gateway`
    skips its write (and the restart) whenever the live gateway config
    already matches what we'd push, so most ticks are one `config.get`
    and nothing else — see that function's docstring for why this is
    required before running this loop unconditionally (an unconditional
    push here, before that skip existed, would have restarted the gateway
    every 5 minutes, permanently).
    """
    _log = logging.getLogger("app.model_registry_self_heal")
    consecutive_failures = 0
    while True:
        await asyncio.sleep(300)
        try:
            from app.services.model_registry.gateway_sync import sync_gateway_model_catalog
            await sync_gateway_model_catalog()
            if consecutive_failures:
                _log.info("Model registry self-heal recovered after %d failed tick(s)", consecutive_failures)
            consecutive_failures = 0

            # Pricing-coverage check (item C3): warn, don't block. Catches
            # the next model added through the admin UI with no rate on
            # file, instead of it silently reporting $0.00 until someone
            # notices by accident.
            try:
                from app.database import get_pool
                from app.services.model_pricing import count_unpriced_active_models
                unpriced_count = await count_unpriced_active_models(get_pool())
                if unpriced_count:
                    _log.warning(
                        "Model registry: %d active model(s) have no pricing row — "
                        "their cost reports as $0.00", unpriced_count,
                    )
            except Exception as exc:
                _log.debug("Model registry pricing-coverage check failed: %s", exc)
        except Exception as exc:
            consecutive_failures += 1
            # First failure logs loud; a gateway outage doesn't then flood
            # the log every 5 minutes for its whole duration.
            if consecutive_failures == 1:
                _log.warning("Model registry self-heal failed: %s", exc)
            else:
                _log.debug("Model registry self-heal still failing (%d in a row): %s", consecutive_failures, exc)


async def _catalog_sync_loop():
    """Sync the OpenClaw tool catalog into the tools table on startup and periodically.

    Waits 30 s for the gateway to authenticate before the first sync, then repeats
    every 10 minutes so new catalog tools are picked up without a manual trigger.
    Custom tools (tier IS NOT NULL) are never touched.
    """
    _log = logging.getLogger("app.catalog_sync")
    await asyncio.sleep(30)
    while True:
        try:
            from app.database import get_pool
            from app.openclaw import gateway_admin
            from app.services.tool_registry.catalog_sync import sync_openclaw_catalog
            result = await sync_openclaw_catalog(get_pool(), gateway_admin)
            if "error" not in result:
                _log.info(
                    "Catalog sync complete: added=%d updated=%d deprecated=%d total=%d",
                    result["added"], result["updated"], result["deprecated"], result["total"],
                )
        except Exception as exc:
            _log.warning("Catalog sync loop error: %s", exc)
        await asyncio.sleep(600)

    # memory_v2 periodic workspace snapshot — safety net so memory files
    # survive crashes/restarts between reaps. Gated by its OWN flag (default OFF) so
    # enabling event-driven snapshot/restore for the local↔web memory sync doesn't
    # turn on this heavy 6h loop.
    if getattr(get_settings(), "memory_v2_periodic_snapshot_enabled", False):
        interval = (
            getattr(get_settings(), "memory_v2_periodic_snapshot_hours", 6) * 3600
        )
        asyncio.create_task(memory_v2_snapshot.periodic_snapshot_loop(interval))


async def _deleted_goal_session_reaper():
    """Evict in-memory gateway sessions for soft-deleted goals + finalize their
    agent_instances. GO sessions never idle-evict, so without this a deleted goal's
    session reconnects to the gateway forever. Runs shortly after boot, then every 5 min."""
    import asyncio
    import logging
    _log = logging.getLogger("app.main")
    from app.openclaw import session_manager
    await asyncio.sleep(30)  # let the pool + sessions settle after boot
    while True:
        try:
            await session_manager.reap_deleted_goal_sessions()
        except Exception as exc:
            _log.warning("deleted-goal session reaper error: %s", exc)
        await asyncio.sleep(300)


async def _local_checkout_reaper():
    """Local Execution Mode (§14.5): free checkouts whose heartbeat expired (crash/
    abandon) so no story stays "checked out forever", and clean terminal stories still
    marked local. Runs shortly after boot, then every 60s."""
    import asyncio
    import logging
    _log = logging.getLogger("app.main")
    from app.services.local_checkout import reap_local_checkouts
    await asyncio.sleep(45)
    while True:
        try:
            result = await reap_local_checkouts()
            if result["reclaimed"] or result["cleaned"]:
                _log.info(
                    "local checkout reaper: reclaimed=%s cleaned=%s",
                    result["reclaimed"], result["cleaned"],
                )
        except Exception as exc:
            _log.warning("local checkout reaper error: %s", exc)
        await asyncio.sleep(60)


async def _provision_retry_backstop():
    """(B) Auto-retry operator-story workspaces stuck at workspace_state='failed'.

    Covers ANY operator story (git-graph or not) whose shca.workspace.setup failed —
    including cases the inline retry (A, operator_provisioning) exhausted. Bounded by
    stories.workspace_provision_attempts: retried up to _CAP times, then LEFT failed
    (with its workspace_state_error surfaced on the row) so a genuinely broken
    provision — e.g. a repo the GitHub App can't access — doesn't loop forever.

    Reuses the branch-ready listener's advisory-locked _reprovision_story so it can't
    race the git-graph poll on the same story; that path re-runs the standard kickoff
    (dispatch_story_kickoff_to_assistant → provision_story_workspace) for the bound
    story. We only sweep BOUND stories (they always are once setup has failed) with a
    short backoff since the last attempt. Every 60s. Independent of gitgraph_enabled."""
    import asyncio
    import logging
    from app.database import get_pool
    _log = logging.getLogger("app.main")
    _CAP = 5
    await asyncio.sleep(90)  # let startup + the gateway auth settle
    while True:
        try:
            pool = get_pool()
            rows = await pool.fetch(
                """SELECT s.id AS story_id, s.goal_id
                     FROM stories s
                     JOIN threads t ON t.id = s.active_thread_id
                    WHERE s.execution_mode = 'operator'
                      AND s.workspace_state = 'failed'
                      AND COALESCE(s.workspace_provision_attempts, 0) < $1
                      AND s.state NOT IN ('done', 'reviewed', 'cancelled', 'rejected')
                      AND s.updated_at < now() - interval '45 seconds'
                      AND EXISTS (SELECT 1 FROM goals g
                                   WHERE g.id = s.goal_id AND g.status = 'active')
                    LIMIT 50""",
                _CAP,
            )
            if rows:
                from app.services.git_graph.branch_ready_listener import branch_ready_listener
                for r in rows:
                    _log.info("provision-retry backstop: retrying story=%s", r["story_id"])
                    await branch_ready_listener._reprovision_story(r["goal_id"], r["story_id"])
        except Exception:
            _log.exception("provision-retry backstop error — non-fatal")
        await asyncio.sleep(60)


async def _assistant_cleanup_watchdog():
    """Global watchdog: every 60s, find assistant instances whose story is DONE
    and destroy them.

    Story-mode assistant instances are persistent — they live as long as the story
    is in progress, regardless of how long the human is idle. Cleanup only
    fires when the linked story is marked done (or cancelled / rejected) so
    the workspace can be reaped without losing in-progress work context.

    An assistant instance is eligible for cleanup when ALL of these are true:
      1. Active in agent_instances (status='active')
      2. Type is the assistant DB type (SHCA in the current schema)
      3. Created more than 30 minutes ago
      4. No messages on the thread in the last 30 minutes (truly idle)
      5. The story whose active_thread_id matches this instance's thread is
         in a terminal state (done | cancelled | rejected) — OR there is no
         story linked to this thread (general-mode conversation, eligible
         under normal idle rules)

    This means: while a story is accepted / in_progress / blocked, the assistant
    instance for that story is NEVER reaped, even if the human walks away
    for hours.
    """
    import asyncio
    import logging
    _log = logging.getLogger("app.assistant_cleanup")

    # Wait a few seconds for gateway_admin to connect on startup
    await asyncio.sleep(10)
    _log.info("Assistant cleanup watchdog started (story-aware: only reaps when story is done)")

    while True:
        try:
            from app.database import get_pool
            from app.openclaw import gateway_admin, session_manager

            pool = get_pool()
            # Story state gate: a thread linked to a story that is NOT done
            # is protected from cleanup. We use a LEFT JOIN LATERAL so we
            # only consider the MOST RECENT story per thread — preventing
            # an old completed story from masking a fresh in-progress one
            # if their active_thread_id values overlap (which can happen
            # if old code paths didn't clear active_thread_id on terminal
            # transition). The terminal-transition sites now also clear
            # active_thread_id (defense in depth).
            stale = await pool.fetch("""
                SELECT ai.id, ai.gateway_agent_id, ai.thread_id, ai.template_id,
                       ai.goal_id,
                       s.id AS story_id, s.state AS story_state
                  FROM agent_instances ai
                  JOIN agents a ON a.id = ai.template_id
             LEFT JOIN LATERAL (
                       SELECT id, state
                         FROM stories
                        WHERE goal_id = ai.goal_id
                          AND active_thread_id = ai.thread_id
                        ORDER BY updated_at DESC NULLS LAST, created_at DESC
                        LIMIT 1
                       ) s ON TRUE
                 WHERE ai.status = 'active' AND a.type = 'SHCA'  -- DB constant, see agent_types.py
                   AND ai.created_at < now() - interval '30 minutes'
                   AND NOT EXISTS (
                         SELECT 1 FROM messages m
                          WHERE m.thread_id = ai.thread_id
                            AND m.created_at > now() - interval '30 minutes'
                   )
                   AND (
                         s.id IS NULL                                        -- general-mode (no live story linked)
                         OR s.state IN ('done', 'cancelled', 'rejected')     -- latest linked story is terminal
                   )
            """)

            # Log assistant instances we're protecting (latest linked story is alive)
            protected = await pool.fetch("""
                SELECT ai.gateway_agent_id, s.id AS story_id, s.state, s.title
                  FROM agent_instances ai
                  JOIN agents a ON a.id = ai.template_id
                  JOIN LATERAL (
                       SELECT id, state, title
                         FROM stories
                        WHERE goal_id = ai.goal_id
                          AND active_thread_id = ai.thread_id
                        ORDER BY updated_at DESC NULLS LAST, created_at DESC
                        LIMIT 1
                       ) s ON TRUE
                 WHERE ai.status = 'active' AND a.type = 'SHCA'  -- DB constant, see agent_types.py
                   AND ai.created_at < now() - interval '30 minutes'
                   AND s.state NOT IN ('done', 'cancelled', 'rejected')
            """)
            for p in protected:
                _log.debug(
                    "Assistant cleanup: protecting %s — story=%s state=%s title=%r",
                    p["gateway_agent_id"], p["story_id"], p["state"],
                    (p["title"] or "")[:60],
                )

            for inst in stale:
                gw_id = inst["gateway_agent_id"]
                thread_id = inst["thread_id"]
                story_id = inst["story_id"]
                story_state = inst["story_state"]
                reason = (
                    f"story={story_id} state={story_state}"
                    if story_id else "general-mode (no story)"
                )

                # memory_v2: pre-reap flush + snapshot before destroying
                # the gateway agent. Both feature-flagged; failures are
                # non-fatal so the reap always proceeds.
                try:
                    from app.memory_v2 import flush as _mv2_flush
                    if _mv2_flush.is_enabled() and thread_id:
                        goal_id_for_flush = inst.get("goal_id") or ""
                        await _mv2_flush.trigger_pre_reap_flush(
                            agent_id=gw_id, agent_type="ASSISTANT",
                            thread_id=thread_id,
                            goal_id=goal_id_for_flush,
                        )
                except Exception:
                    _log.exception("memory_v2 pre-reap flush failed for %s — proceeding", gw_id)

                try:
                    if getattr(get_settings(), "memory_v2_snapshot_enabled", False):
                        await memory_v2_snapshot.snapshot_agent_workspace(
                            gw_id, reason="pre_reap",
                        )
                except Exception:
                    _log.exception("memory_v2 pre-reap snapshot failed for %s — proceeding", gw_id)

                # Archive-and-delete: MEMORY.md + memory/ + sessions/ go
                # to GCS first, then the gateway destroys the workspace.
                # If archive fails, SKIP the DB destroyed-mark so the next
                # watchdog pass can retry — never lose a session by marking
                # destroyed without confirmed archive.
                archive_ok = False
                try:
                    await gateway_admin.archive_and_delete_agent(
                        gw_id, inst.get("goal_id") or "",
                    )
                    archive_ok = True
                    _log.info(
                        "Watchdog destroyed stale assistant instance %s (thread %s, %s)",
                        gw_id, thread_id, reason,
                    )
                except Exception as exc:
                    _log.warning(
                        "Watchdog archive-and-delete FAILED for %s — leaving instance "
                        "active for next pass: %s", gw_id, exc,
                    )

                if not archive_ok:
                    continue  # next stale instance — retry this one later

                # Mark as destroyed in DB
                await pool.execute("""
                    UPDATE agent_instances SET status = 'destroyed', destroyed_at = now()
                    WHERE id = $1 AND status = 'active'
                """, inst["id"])

                # Clear thread instance link
                if thread_id:
                    await pool.execute(
                        "UPDATE threads SET instance_id = NULL WHERE id = $1",
                        thread_id,
                    )

                # Clear operator workspace state — story should no longer appear
                # in token-refresh or provisioning queries.
                if thread_id and inst.get("goal_id"):
                    try:
                        from app.services.operator_provisioning import (
                            clear_operator_workspace_on_assistant_delete,
                        )
                        await clear_operator_workspace_on_assistant_delete(
                            pool, inst["goal_id"], thread_id,
                        )
                    except Exception:
                        pass  # non-fatal; workspace state is advisory

                # Evict from SessionManager if still there
                try:
                    session_manager.evict_thread(
                        inst["goal_id"], inst["template_id"], thread_id or "",
                    )
                except Exception:
                    pass

                # If this was the assistant's last binding to an active goal, mark
                # the agents row deleted too — otherwise the /agent-registry
                # instances page keeps counting it. Assistants are per-user and
                # shared across goals via agent_contexts, so we only soft-
                # delete when zero active-goal bindings remain.
                try:
                    assistant_agent_id = inst["template_id"]
                    still_bound = await pool.fetchval(
                        """SELECT 1
                             FROM agent_contexts ac
                             JOIN goals g ON g.id = ac.goal_id AND g.status = 'active'
                            WHERE ac.agent_id = $1
                            LIMIT 1""",
                        assistant_agent_id,
                    )
                    if not still_bound:
                        await soft_delete_agent(pool, assistant_agent_id)
                        _log.info(
                            "Watchdog: marked assistant agent %s deleted (no remaining "
                            "active-goal bindings)",
                            assistant_agent_id,
                        )
                except Exception as exc:
                    _log.warning(
                        "Watchdog: agents-table status reconcile failed for %s: %s",
                        inst["template_id"], exc,
                    )

            if stale:
                _log.info("Assistant cleanup: destroyed %d stale instances", len(stale))

        except Exception as exc:
            _log.warning("Assistant cleanup watchdog error: %s", exc)

        await asyncio.sleep(60)


async def _ia_instance_reaper():
    """Global watchdog: every 60s, destroy IA/RA agent_instances whose task
    has already reached a terminal state.

    Unlike the (disabled) assistant watchdog above, this carries no memory-
    loss risk: IA/RA instances are per-task ephemeral by design and never
    archive durable memory (see task_controller.destroy_instance docstring).

    This is a safety net, not the primary cleanup path — task_controller
    already calls destroy_instance() inline on most terminal transitions.
    It exists because a few paths skip that call on purpose or by omission
    (e.g. a dispatch failure that raises before destroy_instance is reached,
    or a force-killed persistent-worker subtask that defers cleanup to
    cleanup_parent_task, which itself never fires for halted/failed/rejected
    parents). Without this reaper those instances stay status='active'
    forever and inflate the goal monitor's "Active Agents" count.

    A row is eligible when ALL of these are true:
      1. Active in agent_instances (status='active')
      2. Template type is IA or RA (SHCA/GO are out of scope here)
      3. The linked task's state is terminal (done|failed|rejected|cancelled|halted)
      4. The task was last updated more than 2 minutes ago — gives the normal
         inline destroy_instance() call a wide margin to run first; destroy_instance
         is idempotent/best-effort so a race here would be harmless anyway.
    """
    import logging
    _log = logging.getLogger("app.ia_instance_reaper")

    await asyncio.sleep(15)
    _log.info("IA/RA instance reaper started")

    TERMINAL_TASK_STATES = ("done", "failed", "rejected", "cancelled", "halted")

    while True:
        try:
            from app.database import get_pool
            from app.task_controller import destroy_instance

            pool = get_pool()
            leaked = await pool.fetch(
                """
                SELECT ai.id, ai.gateway_agent_id, ai.task_id, ai.goal_id, t.state
                  FROM agent_instances ai
                  JOIN agents a ON a.id = ai.template_id
                  JOIN tasks t ON t.id = ai.task_id
                 WHERE ai.status = 'active'
                   AND a.type IN ('IA', 'RA')
                   AND t.state = ANY($1)
                   AND t.updated_at < now() - interval '2 minutes'
                """,
                list(TERMINAL_TASK_STATES),
            )

            for inst in leaked:
                task = {
                    "id": inst["task_id"],
                    "instance_id": inst["gateway_agent_id"],
                    "goal_id": inst["goal_id"],
                }
                try:
                    await destroy_instance(task)
                    _log.info(
                        "IA/RA reaper destroyed leaked instance %s (task=%s state=%s)",
                        inst["gateway_agent_id"], inst["task_id"], inst["state"],
                    )
                except Exception as exc:
                    _log.warning(
                        "IA/RA reaper failed to destroy %s (task=%s): %s",
                        inst["gateway_agent_id"], inst["task_id"], exc,
                    )

            if leaked:
                _log.info("IA/RA instance reaper: destroyed %d leaked instances", len(leaked))

        except Exception as exc:
            _log.warning("IA/RA instance reaper error: %s", exc)

        await asyncio.sleep(60)


async def _gateway_orphan_reconciler():
    """Safety net for the rare case where goal-delete couldn't tear down
    gateway agents (gateway unreachable, transient error, etc.).

    Every 5 minutes:
      1. Fetch the live agent list from the gateway.
      2. For each live agent, check three "should be gone" conditions:
         a. Matches an agent_instances row marked status='destroyed' —
            the goal-delete UPDATE succeeded but the gateway delete
            failed. Retry the gateway delete.
         b. Matches an agent_instances row whose goal is deleted but
            the instance is still status='active' — the cascade
            partially completed. Delete from gateway AND mark destroyed.
         c. Matches a goals.go_agent_id where the goal is deleted —
            the GO survived the delete. (Covered by destroy_goal_agent_
            instances on first call, but this catches retries.)
      3. Anything that doesn't match a known DB record is left alone —
         we never delete an agent we don't recognise.

    Errors are logged; the loop keeps running.
    """
    import logging
    _log = logging.getLogger("app.gateway_reconciler")

    # Give gateway_admin time to authenticate after startup; longer than
    # the SHCA watchdog because this one is a safety net, not a hot path.
    await asyncio.sleep(60)
    _log.info("Gateway orphan reconciler started (interval=5min)")

    while True:
        try:
            from app.database import get_pool
            from app.openclaw import gateway_admin

            pool = get_pool()

            # Kill switch — don't delete gateway agents while the system is
            # frozen; the orphan reconciler deleting paused agents would break
            # resume (M3). Skip the reconcile tick and retry after the interval.
            try:
                from app.services import execution_control as _ec
                _blk = await _ec.is_system_paused()
                if _blk:
                    _log.debug(
                        "orphan reconciler: system paused (reason=%s) — skipping tick",
                        _blk.reason or "<none>",
                    )
                    await asyncio.sleep(300)
                    continue
            except Exception as exc:
                _log.debug("orphan reconciler: pause-check failed, proceeding anyway: %s", exc)

            try:
                live = await gateway_admin.call("agents.list")
            except Exception as exc:
                _log.debug("agents.list unreachable; skipping tick: %s", exc)
                await asyncio.sleep(300)
                continue

            gateway_ids = [
                a.get("id", "") for a in (live or {}).get("agents", [])
                if a.get("id")
            ]
            if not gateway_ids:
                await asyncio.sleep(300)
                continue

            # Candidates to delete (gateway_agent_id, reason).
            to_delete: list[tuple[str, str]] = []

            # Pass A — agent_instances marked destroyed but still on the gateway
            rows = await pool.fetch("""
                SELECT id, gateway_agent_id
                  FROM agent_instances
                 WHERE status = 'destroyed'
                   AND gateway_agent_id = ANY($1::text[])
            """, gateway_ids)
            for r in rows:
                to_delete.append((r["gateway_agent_id"], "stale_destroyed_marker"))

            # Pass B — agent_instances active but the owning goal is deleted
            rows = await pool.fetch("""
                SELECT ai.id, ai.gateway_agent_id
                  FROM agent_instances ai
                  JOIN goals g ON g.id = ai.goal_id
                 WHERE ai.status = 'active'
                   AND g.status = 'deleted'
                   AND ai.gateway_agent_id = ANY($1::text[])
            """, gateway_ids)
            for r in rows:
                to_delete.append((r["gateway_agent_id"], "owning_goal_deleted"))
                # Mark destroyed in DB so the next pass doesn't repeat work.
                try:
                    await pool.execute(
                        """UPDATE agent_instances
                              SET status = 'destroyed', destroyed_at = now()
                            WHERE id = $1""",
                        r["id"],
                    )
                except Exception as exc:
                    _log.debug("Failed to mark agent_instance destroyed: %s", exc)

            # Pass C — GO agents whose goal is deleted
            rows = await pool.fetch("""
                SELECT go_agent_id
                  FROM goals
                 WHERE status = 'deleted'
                   AND go_agent_id <> ''
                   AND go_agent_id = ANY($1::text[])
            """, gateway_ids)
            for r in rows:
                to_delete.append((r["go_agent_id"], "go_for_deleted_goal"))

            # Deduplicate (a single gateway agent could match multiple passes).
            seen: set[str] = set()
            unique: list[tuple[str, str]] = []
            for gw_id, reason in to_delete:
                if gw_id and gw_id not in seen:
                    seen.add(gw_id)
                    unique.append((gw_id, reason))

            if not unique:
                await asyncio.sleep(300)
                continue

            destroyed = 0
            errors = 0
            for gw_id, reason in unique:
                # ALWAYS archive-and-delete — never plain-delete. archive_and_delete_agent
                # is the generic canonical removal path (archives MEMORY.md/memory/
                # sessions to GCS, then deletes) and is safe for ANY agent type, so
                # defaulting to it means we can NEVER silently lose an SHCA/assistant/GO's
                # memory here. The only cost is a cheap GCS write for the rare IA/RA
                # orphan. This replaces a gateway-id string pattern ('-shca-') that did
                # NOT match the current 'assistant' naming and would have plain-deleted
                # assistants — losing their memory. is_go is derived from the pass reason
                # (Pass C is the ONLY GO source, and GOs are never in agent_instances),
                # so it stays authoritative without an id pattern.
                is_go = reason == "go_for_deleted_goal"
                try:
                    derived_goal = gateway_admin.extract_goal_id_from_agent_id(gw_id)
                    await gateway_admin.archive_and_delete_agent(
                        gw_id, derived_goal,
                    )
                    _log.info(
                        "Reconciler archived+destroyed orphan %s (reason=%s, goal=%s)",
                        gw_id, reason, derived_goal,
                    )
                    destroyed += 1
                    # For GO orphans: clean up the agents-table row and emit
                    # AGENT_DELETED ledger event. Idempotent — no-op if
                    # teardown_goal_agents already soft-deleted this agent.
                    if is_go:
                        from app.services.agent_registry import soft_delete_agent
                        try:
                            await soft_delete_agent(pool, gw_id)
                        except Exception as sda_exc:
                            _log.debug(
                                "Reconciler: soft_delete_agent for %s failed: %s",
                                gw_id, sda_exc,
                            )
                except Exception as exc:
                    _log.warning(
                        "Reconciler failed to archive+delete %s (reason=%s): %s",
                        gw_id, reason, exc,
                    )
                    errors += 1

            _log.info(
                "Reconciler tick: %d candidates, %d destroyed, %d errors",
                len(unique), destroyed, errors,
            )

        except Exception as exc:
            _log.warning("Gateway orphan reconciler error: %s", exc)

        await asyncio.sleep(300)


async def _webhook_deliveries_prune_loop():
    """Delete webhook_deliveries rows older than 7 days. Runs every 24 hours.

    Calls prune_webhook_deliveries() introduced in webhook_deliveries_prune migration.
    Errors are non-fatal — the dedup table just grows a little; no correctness impact.
    """
    _log = logging.getLogger("app.webhook_prune")
    _log.info("webhook_deliveries prune loop started (24h interval)")
    while True:
        await asyncio.sleep(86400)  # 24 hours
        try:
            from app.database import get_pool  # noqa: PLC0415
            pool = get_pool()
            deleted = await pool.fetchval("SELECT prune_webhook_deliveries()")
            _log.info("webhook_deliveries prune: deleted %d rows older than 7 days", deleted or 0)
        except Exception:
            _log.exception("webhook_deliveries prune failed — non-fatal, will retry next cycle")


@app.on_event("shutdown")
async def on_shutdown():
    await task_controller.stop()
    await session_manager.shutdown()
    await gateway_admin.shutdown()
    if get_settings().council_dispatcher_enabled:
        try:
            from app.services.council.dispatcher import council_dispatcher
            await council_dispatcher.stop()
        except Exception:
            pass
    if get_settings().security_dispatcher_enabled:
        try:
            from app.services.security.dispatcher import security_dispatcher
            await security_dispatcher.stop()
        except Exception:
            pass
    if get_settings().gitgraph_enabled:
        try:
            from app.services.git_graph.branch_ready_listener import branch_ready_listener
            await branch_ready_listener.stop()
        except Exception:
            pass
    if _chat_dispatcher is not None:
        try:
            await _chat_dispatcher.stop()
        except Exception:
            pass
    if _chat_fanout_listener is not None:
        try:
            await _chat_fanout_listener.stop()
        except Exception:
            pass
    await close_db()
    await proxy_client.close_client()


@app.get("/")
async def root(request: Request):
    """Serve goals list at root — same content as /goals, URL stays at /."""
    token = request.cookies.get("firebase_token")
    if not token:
        return RedirectResponse(url="/login")
    from app.auth import get_current_user
    from app.routers.goals import _get_user_goals, templates as goals_templates
    try:
        user = await get_current_user(request)
    except Exception:
        return RedirectResponse(url="/login")
    from app.roles import SystemRole as _SystemRole
    goal_list = await _get_user_goals(user["uid"], user.get("system_role") == _SystemRole.PLATFORM_ADMIN)
    return goals_templates.TemplateResponse(
        "goals/list.html",
        {
            "request": request,
            "user": user,
            "goals": goal_list,
        },
    )


# ── Signup gate handlers ─────────────────────────────────────────────────────

@app.exception_handler(SignupRequired)
async def signup_required_handler(request: Request, exc: SignupRequired):
    return RedirectResponse(url="/signup")


@app.exception_handler(SignupPending)
async def signup_pending_handler(request: Request, exc: SignupPending):
    return RedirectResponse(url="/signup/pending")


@app.exception_handler(SignupRejected)
async def signup_rejected_handler(request: Request, exc: SignupRejected):
    return RedirectResponse(url="/signup/rejected")


# ── HTTP error handlers ──────────────────────────────────────────────────────

@app.exception_handler(401)
async def unauthorized_handler(request: Request, exc):
    # A browser gets the login page; a bearer-token caller gets a 401 it
    # can read. The unconditional redirect turned every refused
    # /internal/* call into "405 Method Not Allowed" -- see
    # app/error_responses.py.
    if not wants_html(request):
        _log.warning("401 on %s %s (bearer present=%s)",
                     request.method, request.url.path,
                     bool(request.headers.get("authorization")))
    return unauthorized_response(request)


@app.exception_handler(400)
async def bad_request_handler(request: Request, exc):
    # API / fetch callers (Accept: */*, application/json) keep JSON so their
    # error-parsing logic isn't broken.  Browser form submissions send
    # Accept: text/html explicitly, so they get a readable HTML error page.
    if "text/html" in request.headers.get("accept", ""):
        detail = getattr(exc, "detail", "Bad request")
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "code": 400, "message": str(detail), "user": None, "goals": []},
            status_code=400,
        )
    from fastapi.responses import JSONResponse as _JSONResponse
    return _JSONResponse({"detail": getattr(exc, "detail", "Bad request")}, status_code=400)


@app.exception_handler(403)
async def forbidden_handler(request: Request, exc):
    # Auth dependencies (require_admin/require_editor/require_goal_member, etc.)
    # already set exc.detail to a specific reason ("Editor access required",
    # "Not a goal member", ...) and log it — surface that reason to the user
    # instead of one generic message for every permission denial.
    detail = getattr(exc, "detail", None) or "You don't have permission to access this page."
    return templates.TemplateResponse(
        "error.html",
        {"request": request, "code": 403, "message": detail, "user": None, "goals": []},
        status_code=403,
    )


@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    path = request.url.path
    if path.startswith("/static") or path == "/favicon.ico":
        return PlainTextResponse("Not found", status_code=404)
    return templates.TemplateResponse(
        "error.html",
        {"request": request, "code": 404, "message": "Page not found.", "user": None, "goals": []},
        status_code=404,
    )
