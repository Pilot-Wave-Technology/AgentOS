"""Local-development mode — single source of truth.

Everything that swaps a cloud service for a local replica is gated here, behind
one flag: ``LOCAL_SERVER=true`` (``settings.local_server``). High-level modules
(``app/auth.py``, ``app/storage.py``, the middleware in ``app/main.py``) ask
THIS module whether a replica is active and how to reach it, rather than reading
env vars or branching on ``app_env`` themselves. That keeps the local-only
concern in one owned file — a new reader sees the entire local-dev surface here,
and there is exactly one place to audit before a deploy.

Replicas gated by the flag:
  • Firebase Auth Emulator  → stands in for real Firebase Auth

GCS is never emulated locally — the app reads/writes real buckets via ADC in
both modes; see local-development/README.md's "GCS (real buckets via ADC)".

The flag defaults to False, so none of this activates in a deployed environment.
See local-development/README.md for the surrounding stack.
"""
import logging
import os
from pathlib import Path

from app.config import get_settings

_log = logging.getLogger(__name__)


def is_local_server() -> bool:
    """True when the local dev stack is active (LOCAL_SERVER=true).

    The master gate. Every other helper in this module is a no-op / returns an
    empty value unless this is true.
    """
    return get_settings().local_server


def firebase_auth_emulator_host() -> str:
    """host:port of the Firebase Auth Emulator, or '' when disabled.

    Empty string in normal (cloud) operation → callers fall back to real
    Firebase. Only non-empty when local_server is on.
    """
    if not is_local_server():
        return ""
    return get_settings().firebase_auth_emulator_host.strip()


def agent_workspace_path(identifier: str) -> str:
    """Workspace path sent to the gateway (agents.create / SHCA / IA / council /
    security / GO provisioning) for one agent instance.

    Defaults to "~/.openclaw/workspace-<id>" -- correct in production, where the
    gateway runs on its own Ubuntu VM and "~" is a stable, isolated location.

    Locally the gateway runs on THIS SAME machine as this app, and there the
    literal "~" expands via the OS home dir regardless of OPENCLAW_STATE_DIR
    (that only redirects the gateway's own config root, a separate concern --
    see agent_os_openclaw/src/utils.ts resolveConfigDir vs resolveUserPath). Left
    unhandled, every agent workspace silently lands under the Windows user
    profile (C:) instead of the D: local-dev tree this stack otherwise keeps
    everything under. When local_server is on and local_workspace_root is set,
    substitute that instead of "~/.openclaw".
    """
    if is_local_server():
        root = get_settings().local_workspace_root.strip()
        if root:
            return f"{root}/workspace-{identifier}"
    return f"~/.openclaw/workspace-{identifier}"


def hook_command(hook_filename: str) -> str:
    """Command string for a Claude Code hook script (PreToolUse/PostToolUse/
    Stop -- see operator_provisioning.py's .claude/settings.json generation).

    In production these are plain bash scripts deployed to
    /etc/openclaw/hooks/<name> on the Ubuntu gateway VM (by agent_os_openclaw's
    infra tooling), and Claude Code invokes that absolute path directly as a
    real, executable, shebang'd script.

    Locally there is no /etc/openclaw/hooks/ at all, so every hook command
    silently fails to spawn and just no-ops -- Claude's turn proceeds either
    way since every hook here is written fail-open, so nothing blocks, but
    none of their side effects happen either (e.g. no live "Claude is doing
    X" progress cards in the SHCA chat, no kill-switch pause enforcement).
    Rather than maintaining a duplicate PowerShell/Python copy of each
    script, run the SAME bash scripts through Git Bash straight from their
    source location in the sibling agent_os_openclaw checkout (infra/hooks/).
    """
    if not is_local_server():
        return f"/etc/openclaw/hooks/{hook_filename}"

    bash_candidates = [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ]
    bash_exe = next((p for p in bash_candidates if os.path.isfile(p)), None)
    if not bash_exe:
        _log.warning(
            "local_dev.hook_command: no Git Bash found for hook %r (checked %s) -- "
            "hooks will silently no-op. Install Git for Windows to enable them.",
            hook_filename, bash_candidates,
        )
        return f"/etc/openclaw/hooks/{hook_filename}"  # fails closed, same as today

    repo_root = Path(__file__).resolve().parent.parent  # .../agentos
    hook_path = repo_root.parent / "agent_os_openclaw" / "infra" / "hooks" / hook_filename
    return f'"{bash_exe}" "{hook_path}"'


def firebase_emulator_credential():
    """A no-network Firebase credential for use against the Auth Emulator.

    Against the emulator, ``verify_id_token`` skips signature verification and
    never calls an authenticated Google API, so no real credential is needed —
    but ``firebase_admin.initialize_app`` still requires a credential object,
    and the usual ``ApplicationDefault()`` fallback would try (and fail) to
    resolve ADC. This adapter wraps anonymous credentials so init succeeds
    with zero network and zero secrets.
    """
    from firebase_admin import credentials
    from google.auth.credentials import AnonymousCredentials

    class _EmulatorCredential(credentials.Base):
        def get_credential(self):
            return AnonymousCredentials()

    return _EmulatorCredential()


def configure_local_environment() -> None:
    """Called once at app startup. Logs the active local-dev surface and keeps
    ``os.environ`` in sync for SDKs that auto-detect emulator env vars.

    firebase-admin routes to the Auth Emulator based on the
    ``FIREBASE_AUTH_EMULATOR_HOST`` process env var, so when the flag supplies a
    default we must publish it into os.environ (a value set only in the pydantic
    settings would be invisible to the SDK).
    """
    if not is_local_server():
        _log.info(
            "local_dev: LOCAL_SERVER=false → local mode OFF; using real cloud services"
        )
        return

    fb = firebase_auth_emulator_host()
    if fb:
        os.environ["FIREBASE_AUTH_EMULATOR_HOST"] = fb

    # WARNING level on purpose: this is a security-relevant posture change
    # (tokens not cryptographically verified). Make it loud in the logs so it
    # can never be mistaken for a real deployment.
    _log.warning(
        "local_dev: LOCAL_SERVER=true → LOCAL MODE ON. firebase_auth_emulator=%s. "
        "Firebase token signatures are NOT verified. GCS is real (ADC) in both "
        "modes. NEVER enable this against production.",
        fb or "<off>",
    )

    root = get_settings().local_workspace_root.strip()
    _log.info(
        "local_dev: agent workspaces -> %s",
        f"{root}/workspace-<id>" if root else "~/.openclaw/workspace-<id> (LOCAL_WORKSPACE_ROOT not set)",
    )
