"""Tool credential CRUD endpoints — /api/custom-tools/credentials/."""
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from app.auth import require_admin, get_current_user
from app.database import get_pool
from app.models.custom_tool import ToolCredential, ToolCredentialCreateRequest, ToolCredentialRotateRequest
from app.services.custom_tools.crud import (
    count_tools_using_credential,
    create_tool_credential,
    delete_tool_credential,
    list_tool_credentials,
    rotate_tool_credential,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/credentials")


@router.get("", response_model=list[ToolCredential])
async def api_list_credentials(_user=Depends(get_current_user)):
    pool = get_pool()
    return await list_tool_credentials(pool)


@router.post("", status_code=201, response_model=ToolCredential)
async def api_create_credential(body: ToolCredentialCreateRequest, user=Depends(require_admin)):
    pool = get_pool()
    log.info(
        "credential create requested: name=%s type=%s header_name=%s",
        body.name, body.credential_type, body.header_name,
    )
    try:
        cred = await create_tool_credential(
            pool,
            name=body.name,
            credential_type=body.credential_type,
            secret_value=body.secret_value,
            header_name=body.header_name,
            created_by=user.get("email"),
        )
    except ValueError as exc:
        # Domain errors (duplicate name, validation) — clean 400.
        log.warning("credential create rejected: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        # Encrypt-key missing, DB constraint, anything else — log the real
        # cause server-side and return a JSON error envelope. Without this
        # the client sees an HTML 500 page that breaks res.json() parsing.
        log.exception("credential create failed: name=%s", body.name)
        raise HTTPException(
            status_code=500,
            detail=f"credential create failed: {type(exc).__name__}: {exc}",
        )
    return cred


@router.post("/{credential_id}/rotate", response_model=ToolCredential)
async def api_rotate_credential(
    credential_id: str, body: ToolCredentialRotateRequest, _user=Depends(require_admin)
):
    pool = get_pool()
    try:
        cred = await rotate_tool_credential(pool, credential_id, body.secret_value)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return cred


@router.delete("/{credential_id}", status_code=204)
async def api_delete_credential(credential_id: str, _user=Depends(require_admin)):
    pool = get_pool()
    try:
        await delete_tool_credential(pool, credential_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(None, status_code=204)


@router.get("/{credential_id}/usage")
async def api_credential_usage(credential_id: str, _user=Depends(get_current_user)):
    pool = get_pool()
    count = await count_tools_using_credential(pool, credential_id)
    return JSONResponse({"count": count})
