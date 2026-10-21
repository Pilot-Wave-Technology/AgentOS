"""VS Code extension distribution — the control plane fronts a PRIVATE GCS bucket.

Teammates install the AgentOS Local Execution extension from a `.vsix`. The bucket
that holds those `.vsix` files (+ a `latest.json` manifest) is private — only this
service's runtime service account can read it. The extension therefore fetches its
update manifest and downloads the binary from the CP, not from GCS directly:

    GET /extension/latest.json  → {version, url, notes}   (manifest, rewritten to point here)
    GET /extension/download     → the current .vsix bytes  (streamed from the private bucket)

Bucket layout — one bucket (`EXTENSION_DIST_BUCKET`), one CHANNEL per environment
(`EXTENSION_DIST_PREFIX`, e.g. prod/ dev/ feature/) so promoting never clobbers
another env's artifact:
    <prefix>latest.json                {"version": "0.1.0", "object": "agentos-vscode-0.1.0.vsix", "notes": "..."}
    <prefix>agentos-vscode-0.1.0.vsix  the packaged extension
`object` stays a bare filename; the prefix is applied here when resolving.

Publishing an update is bucket-only (no CP redeploy): upload the new .vsix under
this env's prefix, then overwrite <prefix>latest.json to name it. The extension
polls latest.json daily and offers the download. Because the feed URL derives from
each client's controlPlaneUrl AND each deployment reads only its own channel, dev
and prod clients automatically get their own environment's artifact.
"""
from __future__ import annotations

import json
import logging

import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

from app.auth import get_current_user
from app.config import get_settings
from app.storage import download_gcs_uri_bytes

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/extension", tags=["extension-dist"])
templates = Jinja2Templates(directory="app/templates")


def _control_plane_url(request: Request) -> str:
    """The externally-visible base URL of THIS deployment — dev shows dev, prod
    shows prod. Built from the forwarded proto + Host header rather than
    request.base_url, which can carry the wrong scheme behind Cloud Run's proxy."""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}".rstrip("/")


@router.get("", response_class=HTMLResponse)
async def install_page(request: Request, user: dict = Depends(get_current_user)):
    """In-app install/download page (sidebar entry, visible to every signed-in
    user). Shows the control-plane URL to paste into the extension + a download
    button for the .vsix. The URL reflects whichever deployment served the page."""
    version = ""
    try:
        version = str((await _read_manifest()).get("version", ""))
    except Exception:
        version = ""  # nothing published yet → page still renders
    return templates.TemplateResponse(
        "extension/install.html",
        {
            "request": request,
            "user": user,
            "control_plane_url": _control_plane_url(request),
            "version": version,
        },
    )


def _bucket() -> str:
    return get_settings().extension_dist_bucket


def _prefix() -> str:
    """Per-environment channel prefix within the bucket (e.g. "prod/"). Normalized
    to have no leading slash and exactly one trailing slash when set, so callers
    concatenate object keys directly. Empty = bucket root (legacy layout)."""
    p = (get_settings().extension_dist_prefix or "").strip().strip("/")
    return f"{p}/" if p else ""


async def _read_manifest() -> dict:
    """Fetch and parse this channel's latest.json from the private bucket (via the
    CP service account). The channel prefix keeps prod/dev/feature artifacts
    separate in one bucket."""
    raw = await download_gcs_uri_bytes(f"gs://{_bucket()}/{_prefix()}latest.json")
    return json.loads(raw)


@router.get("/latest.json")
async def latest() -> JSONResponse:
    """Update manifest for the VS Code extension. Never exposes the bucket: the
    download URL is rewritten to point back at this service's /extension/download."""
    try:
        m = await _read_manifest()
    except Exception:
        _log.exception("extension latest.json unavailable")
        # No manifest yet (nothing published) → 404 so the client silently skips.
        return JSONResponse({"error": "no extension published"}, status_code=404)
    # Relative download URL — the client resolves it against its own feed URL
    # (<controlPlaneUrl>/extension/latest.json). This stays correct behind Cloud
    # Run's proxy, where request.base_url can carry the wrong scheme/host.
    return JSONResponse(
        {
            "version": str(m.get("version", "")),
            "url": "/extension/download",
            "notes": str(m.get("notes", "")),
        },
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/download")
async def download() -> Response:
    """Stream the current .vsix from the private bucket. The object to serve is
    whatever latest.json's `object` field names — so a publish is bucket-only."""
    try:
        m = await _read_manifest()
        obj = str(m.get("object") or "").strip()
        if not obj:
            raise ValueError("latest.json has no `object`")
        data = await download_gcs_uri_bytes(f"gs://{_bucket()}/{_prefix()}{obj}")
    except Exception:
        _log.exception("extension download unavailable")
        return JSONResponse({"error": "extension download unavailable"}, status_code=404)
    # Sanitize the filename before it goes in a response header — `object` is
    # publisher-controlled (trusted) but a typo with a quote/CR/LF would corrupt
    # the header; keep only safe filename characters.
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", obj) or "agentos-vscode.vsix"
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-cache",
        },
    )
