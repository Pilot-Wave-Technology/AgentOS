"""GCP Connect-via-Google OAuth flow for service account setup.

Operators click "Connect Google Cloud" in the SA modal, which opens this flow
in a popup window. After Google consent the operator fills a small form
(project ID + SA name) and the control panel creates the SA + tokenCreator
binding on their behalf using the short-lived OAuth token.

The access token is never persisted:
  - The callback receives it from Google, Fernet-encrypts it, and embeds it
    in a hidden form field (in-browser transit only).
  - The provision endpoint decrypts it, uses it for the two IAM API calls,
    and discards it. The Fernet key is derived from APP_SECRET_KEY (never
    written to the DB).

State token: HMAC-SHA256 signed JSON, 10-minute TTL. Contains goal_id,
env_resource_id, role, and uid so the callback can auth-check and pre-fill the
form without any server-side session.

Routes
------
GET /goals/{pid}/gcp/oauth/start?role=&rid=   — build auth URL, redirect
GET /gcp/oauth/callback                        — exchange code, render form
POST /gcp/oauth/provision-sa                   — create SA, register, close popup
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import time
import urllib.parse

import httpx
from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import get_current_user, get_goal_role
from app.config import get_settings
from app.database import get_pool
from app.routers.goal_resources import _insert_sa_child, _sync_goal_resources_md
from app.services.go_events import on_resource_registered
from app.services.gcp_setup import GcpSetupError, create_sa_and_bind

_log = logging.getLogger(__name__)

router = APIRouter()

_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
# cloud-platform scope is required for IAM SA creation + policy binding.
_GOOGLE_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
# SA account-ID regex: 6-30 chars, lowercase letters/digits/hyphens,
# must start with a letter, end with letter or digit.
_SA_NAME_RE = re.compile(r'^[a-z][a-z0-9\-]{4,28}[a-z0-9]$')


# ── crypto helpers ─────────────────────────────────────────────────────────────

def _fernet() -> Fernet:
    """Derive a Fernet key from APP_SECRET_KEY via SHA-256."""
    raw = hashlib.sha256(get_settings().app_secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(raw))


def _sign_state(payload: dict) -> str:
    """Return URL-safe signed token: base64(payload).hmac."""
    secret = get_settings().app_secret_key.encode()
    body = json.dumps(payload, separators=(',', ':')).encode()
    body_b64 = base64.urlsafe_b64encode(body).rstrip(b'=').decode()
    sig = hmac.new(secret, body_b64.encode(), hashlib.sha256).hexdigest()
    return f"{body_b64}.{sig}"


def _verify_state(token: str) -> dict:
    """Validate signature and expiry. Raises ValueError on failure."""
    try:
        body_b64, sig = token.rsplit('.', 1)
    except ValueError:
        raise ValueError("malformed state token")
    secret = get_settings().app_secret_key.encode()
    expected = hmac.new(secret, body_b64.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise ValueError("invalid state signature")
    padding = (4 - len(body_b64) % 4) % 4
    payload = json.loads(base64.urlsafe_b64decode(body_b64 + '=' * padding))
    if payload.get('exp', 0) < time.time():
        raise ValueError("state token expired")
    return payload


# ── routes ─────────────────────────────────────────────────────────────────────

@router.get("/goals/{pid}/gcp/oauth/start")
async def gcp_oauth_start(
    pid: str,
    rid: str,
    role: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Build the Google OAuth URL and redirect the popup to it."""
    if role not in ('test', 'deploy'):
        raise HTTPException(status_code=400, detail="role must be 'test' or 'deploy'")

    goal_role = await get_goal_role(user["uid"], pid)
    if goal_role is None:
        raise HTTPException(status_code=403)

    settings = get_settings()
    if not settings.gcp_oauth_client_id:
        raise HTTPException(
            status_code=503,
            detail="GCP OAuth is not configured. Set GCP_OAUTH_CLIENT_ID, "
                   "GCP_OAUTH_CLIENT_SECRET, and GCP_OAUTH_REDIRECT_URL.",
        )

    state_payload = {
        'pid': pid,
        'rid': rid,
        'role': role,
        'uid': user['uid'],
        'exp': int(time.time()) + 600,  # 10-min window
    }
    params = {
        'client_id': settings.gcp_oauth_client_id,
        'redirect_uri': settings.gcp_oauth_redirect_url,
        'response_type': 'code',
        'scope': _GOOGLE_SCOPE,
        'state': _sign_state(state_payload),
        'access_type': 'online',   # short-lived only; no refresh token
        'prompt': 'consent',       # always show consent so operator can pick the right account
    }
    auth_url = f"{_GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"
    _log.info(
        "gcp_oauth_start: user=%s goal=%s rid=%s role=%s → redirecting to Google",
        user['uid'], pid, rid, role,
    )
    return RedirectResponse(url=auth_url, status_code=302)


@router.get("/gcp/oauth/callback", response_class=HTMLResponse)
async def gcp_oauth_callback(
    request: Request,
    code: str = None,
    state: str = None,
    error: str = None,
    user: dict = Depends(get_current_user),
):
    """Receive the OAuth code from Google, exchange for token, render setup form."""
    if error:
        _log.warning("gcp_oauth_callback: Google returned error=%s", error)
        return HTMLResponse(_error_page(f"Google OAuth error: {error}"))

    if not code or not state:
        return HTMLResponse(_error_page("Missing code or state from Google."))

    try:
        state_payload = _verify_state(state)
    except ValueError as exc:
        _log.warning("gcp_oauth_callback: bad state: %s", exc)
        return HTMLResponse(_error_page(f"Invalid or expired flow state: {exc}. Please start over."))

    # The state token was minted for a specific uid at /start. Without this
    # check, anyone who intercepts a signed-but-not-yet-used state value
    # (e.g. via a shared/logged redirect URL) could complete the flow as
    # themselves and provision a service account into someone else's goal.
    if state_payload.get('uid') != user['uid']:
        _log.warning(
            "gcp_oauth_callback: state uid=%s does not match session uid=%s — rejecting",
            state_payload.get('uid'), user['uid'],
        )
        return HTMLResponse(_error_page("This authorization link belongs to a different user. Please start over."))

    pid = state_payload['pid']
    rid = state_payload['rid']
    role = state_payload['role']

    goal_role = await get_goal_role(user["uid"], pid)
    if goal_role is None:
        return HTMLResponse(_error_page("You are not a member of this goal."))

    settings = get_settings()

    # Exchange code → access_token.
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(_GOOGLE_TOKEN_URL, data={
            'code': code,
            'client_id': settings.gcp_oauth_client_id,
            'client_secret': settings.gcp_oauth_client_secret,
            'redirect_uri': settings.gcp_oauth_redirect_url,
            'grant_type': 'authorization_code',
        })

    if resp.status_code != 200:
        _log.error(
            "gcp_oauth_callback: token exchange failed %d: %s",
            resp.status_code, resp.text[:300],
        )
        return HTMLResponse(_error_page("Token exchange with Google failed. Please try again."))

    access_token = resp.json().get('access_token')
    if not access_token:
        return HTMLResponse(_error_page("No access_token in Google response. Please try again."))

    # Encrypt the token for safe passage through the browser form.
    encrypted_token = _fernet().encrypt(access_token.encode()).decode()

    # Load the env resource to pre-fill the form.
    pool = get_pool()
    env_row = await pool.fetchrow(
        "SELECT name, metadata FROM goal_resources WHERE id = $1 AND goal_id = $2",
        rid, pid,
    )
    env_meta: dict = {}
    env_name = ""
    if env_row:
        env_name = env_row['name'] or ""
        raw = env_row['metadata']
        if isinstance(raw, str) and raw:
            try:
                env_meta = json.loads(raw)
            except Exception:
                pass
        elif isinstance(raw, dict):
            env_meta = raw

    prefill_project = env_meta.get('target_project_id', '')
    prefill_sa_name = 'test-runner' if role == 'test' else 'deployer'
    sa_label = 'Test' if role == 'test' else 'Deploy'

    _log.info(
        "gcp_oauth_callback: state OK goal=%s rid=%s role=%s — rendering form",
        pid, rid, role,
    )
    return HTMLResponse(_setup_form(
        pid=pid, rid=rid, role=role, sa_label=sa_label,
        env_name=env_name, encrypted_token=encrypted_token,
        prefill_project=prefill_project, prefill_sa_name=prefill_sa_name,
    ))


@router.post("/gcp/oauth/provision-sa", response_class=HTMLResponse)
async def gcp_oauth_provision_sa(
    request: Request,
    pid: str = Form(...),
    rid: str = Form(...),
    role: str = Form(...),
    target_project_id: str = Form(...),
    sa_name: str = Form(...),
    encrypted_token: str = Form(...),
    user: dict = Depends(get_current_user),
):
    """Create the SA via GCP IAM, bind tokenCreator, register in DB, close popup."""
    # Auth — re-check goal membership; the user must still be a member.
    goal_role = await get_goal_role(user["uid"], pid)
    if goal_role is None:
        return HTMLResponse(_error_page("You are not a member of this goal."))

    if role not in ('test', 'deploy'):
        return HTMLResponse(_error_page("Invalid SA role."))

    # Validate SA name before touching GCP.
    if not _SA_NAME_RE.match(sa_name):
        return HTMLResponse(_error_page(
            "SA name must be 6–30 characters: lowercase letters, digits, hyphens; "
            "start with a letter; end with a letter or digit."
        ))

    # Decrypt the short-lived access token.
    try:
        access_token = _fernet().decrypt(encrypted_token.encode()).decode()
    except Exception:
        _log.warning(
            "gcp_oauth_provision_sa: token decryption failed for user=%s — likely expired",
            user['uid'],
        )
        return HTMLResponse(_error_page(
            "Your session token has expired or is invalid. Please close this window and start over."
        ))

    settings = get_settings()

    # Create SA and bind tokenCreator in GCP.
    try:
        sa_email = await create_sa_and_bind(
            access_token=access_token,
            project_id=target_project_id,
            sa_name=sa_name,
            cloud_run_sa=settings.gcp_vm_sa_email,
        )
        _log.info(
            "gcp_oauth_provision_sa: GCP SA created %s for goal=%s role=%s",
            sa_email, pid, role,
        )
    except GcpSetupError as exc:
        _log.warning("gcp_oauth_provision_sa: GCP error: %s", exc)
        return HTMLResponse(_error_page(f"GCP error: {exc}"))
    except Exception:
        _log.exception("gcp_oauth_provision_sa: unexpected error for user=%s", user['uid'])
        return HTMLResponse(_error_page(
            "Unexpected error while creating the service account. Please try again."
        ))

    # Load env row for name + duplicate check.
    sa_kind = 'gcp_test_sa' if role == 'test' else 'gcp_deploy_sa'
    pool = get_pool()

    env_row = await pool.fetchrow(
        "SELECT id, name FROM goal_resources WHERE id = $1 AND goal_id = $2",
        rid, pid,
    )
    if not env_row:
        return HTMLResponse(_error_page("Environment resource not found."))

    env_name = env_row['name']

    # Duplicate guard — one active SA per role per env.
    existing = await pool.fetchrow(
        """SELECT id FROM goal_resources
            WHERE goal_id = $1 AND parent_id = $2 AND kind = $3 AND status = 'active'""",
        pid, rid, sa_kind,
    )
    if existing:
        return HTMLResponse(_error_page(
            f"An active {role} SA is already attached to this environment. "
            "Archive it first, then try again."
        ))

    # Register in DB.
    async with pool.acquire() as conn:
        new_rid = await _insert_sa_child(
            conn,
            pid=pid,
            env_id=rid,
            env_name=env_name,
            user=user,
            sa_kind=sa_kind,
            sa_email=sa_email,
        )

    _log.info(
        "gcp_oauth_provision_sa: registered %s as %s (resource_id=%s) in goal=%s",
        sa_email, sa_kind, new_rid, pid,
    )

    # Best-effort downstream sync — failures must not block the success response.
    try:
        await _sync_goal_resources_md(pid)
    except Exception:
        _log.exception(
            "gcp_oauth_provision_sa: _sync_goal_resources_md failed for goal=%s", pid,
        )

    try:
        await on_resource_registered(pid, new_rid, user['uid'])
    except Exception:
        _log.exception(
            "gcp_oauth_provision_sa: on_resource_registered failed for goal=%s", pid,
        )

    return HTMLResponse(_success_page(sa_email, role))


# ── page fragments ─────────────────────────────────────────────────────────────

def _h(text: str) -> str:
    """Minimal HTML entity escape for safe interpolation into HTML text/attributes."""
    return (
        text.replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;')
    )


def _error_page(message: str) -> str:
    """Minimal HTML error page rendered inside the OAuth popup."""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Error</title>
<style>
body{{font-family:system-ui,sans-serif;background:#0f1117;color:#f87171;
     padding:2rem;max-width:480px;margin:auto}}
h2{{margin-bottom:.5rem}}p{{color:#d1d5db}}a{{color:#818cf8}}
</style></head>
<body>
<h2>Something went wrong</h2>
<p>{_h(message)}</p>
<p><a href="javascript:window.close()">Close this window</a></p>
</body></html>"""


def _success_page(sa_email: str, role: str) -> str:
    """Popup page that fires postMessage to the parent window then auto-closes."""
    sa_email_js = json.dumps(sa_email)
    role_js = json.dumps(role)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Service Account Attached</title>
<style>
body{{font-family:system-ui,sans-serif;background:#0f1117;color:#d1fae5;
     padding:2rem;max-width:480px;margin:auto}}
h2{{color:#34d399;margin-bottom:.5rem}}code{{color:#6ee7b7}}
p{{color:#9ca3af;font-size:.9rem}}
</style></head>
<body>
<h2>Service Account Attached</h2>
<p><code>{sa_email}</code> has been created in GCP and registered as the
{role} SA. The page will refresh automatically.</p>
<p>Closing this window…</p>
<script>
try {{
  window.opener && window.opener.postMessage(
    {{type:'gcp_sa_provisioned', sa_email:{sa_email_js}, role:{role_js}}},
    window.location.origin
  );
}} catch(_) {{}}
setTimeout(function(){{ window.close(); }}, 1800);
</script>
</body></html>"""


def _setup_form(
    pid: str, rid: str, role: str, sa_label: str, env_name: str,
    encrypted_token: str, prefill_project: str, prefill_sa_name: str,
) -> str:
    """SA configuration form rendered inside the OAuth popup."""
    env_name_escaped = _h(env_name)
    prefill_project_escaped = _h(prefill_project)
    prefill_sa_name_escaped = _h(prefill_sa_name)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Connect Google Cloud — {sa_label} SA</title>
<style>
*{{box-sizing:border-box}}
body{{font-family:system-ui,sans-serif;background:#0f1117;color:#d1d5db;
     padding:2rem;max-width:480px;margin:auto}}
h2{{color:#e5e7eb;margin-bottom:.2rem}}
.sub{{color:#6b7280;font-size:.85rem;margin:.25rem 0 1.5rem}}
label{{display:block;font-size:.875rem;color:#9ca3af;margin-bottom:.375rem}}
input{{width:100%;background:#1f2937;border:1px solid #374151;color:#f3f4f6;
       padding:.5rem .75rem;border-radius:.375rem;font-size:.875rem}}
input:focus{{outline:none;border-color:#6366f1;box-shadow:0 0 0 1px #6366f1}}
.field{{margin-bottom:1rem}}
.hint{{font-size:.75rem;color:#6b7280;margin:.3rem 0 0}}
.btn{{background:#4f46e5;color:#fff;border:none;padding:.625rem 1.25rem;
      border-radius:.375rem;cursor:pointer;font-size:.875rem;width:100%;
      margin-top:.5rem;transition:background .15s}}
.btn:hover{{background:#4338ca}}
.btn:disabled{{opacity:.6;cursor:default}}
</style></head>
<body>
<h2>Attach {sa_label} Service Account</h2>
<p class="sub">Environment: <strong>{env_name_escaped}</strong></p>
<form id="f" method="post" action="/gcp/oauth/provision-sa">
  <input type="hidden" name="pid" value="{pid}">
  <input type="hidden" name="rid" value="{rid}">
  <input type="hidden" name="role" value="{role}">
  <input type="hidden" name="encrypted_token" value="{encrypted_token}">
  <div class="field">
    <label>GCP Project ID</label>
    <input name="target_project_id" required value="{prefill_project_escaped}"
           placeholder="my-gcp-project" autocomplete="off">
  </div>
  <div class="field">
    <label>Service Account Name</label>
    <input name="sa_name" required value="{prefill_sa_name_escaped}"
           placeholder="test-runner" autocomplete="off">
    <p class="hint">6–30 chars · lowercase letters, digits, hyphens · starts with a letter.
      Will become <em>name@project.iam.gserviceaccount.com</em>.</p>
  </div>
  <button type="submit" id="btn" class="btn">Create &amp; Attach SA</button>
</form>
<script>
document.getElementById('f').addEventListener('submit', function() {{
  var b = document.getElementById('btn');
  b.disabled = true;
  b.textContent = 'Creating…';
}});
</script>
</body></html>"""
