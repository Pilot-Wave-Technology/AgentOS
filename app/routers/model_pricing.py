"""Admin UI for model pricing management.

Pricing rows are append-only — updating a rate means inserting a new row
with a later effective_from. The cost query in dashboard_metrics uses a
LATERAL join to find the rate in effect at each token record's recorded_at,
so historical costs automatically use the correct rate without any backfill.
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import require_admin, get_current_user
from app.roles import SystemRole
from app.database import get_pool

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/settings/model-pricing")
templates = Jinja2Templates(directory="app/templates")


@router.get("", response_class=HTMLResponse)
async def pricing_page(request: Request, user: dict = Depends(get_current_user)):
    """Viewable by anyone logged in; only platform.admin can add new rates."""
    pool = get_pool()

    # Most recent rate per provider/model for the "current rates" table
    current_rates = await pool.fetch("""
        SELECT DISTINCT ON (provider, model)
               provider, model, provider_label,
               input_rate, output_rate, cache_read_rate, cache_write_rate,
               thinking_rate, effective_from, notes
        FROM   model_pricing
        ORDER  BY provider, model, effective_from DESC
    """)

    # Full history newest-first for the history table
    history = await pool.fetch("""
        SELECT provider, model, provider_label,
               input_rate, output_rate, cache_read_rate, cache_write_rate,
               thinking_rate, effective_from, notes
        FROM   model_pricing
        ORDER  BY effective_from DESC, provider, model
        LIMIT  100
    """)

    _log.info("model pricing page: %d current, %d history rows", len(current_rates), len(history))
    return templates.TemplateResponse("admin/model_pricing.html", {
        "request": request,
        "user": user,
        "current_rates": current_rates,
        "history": history,
        "error": None,
        "can_edit": user.get("system_role") == SystemRole.PLATFORM_ADMIN,
    })


@router.post("", response_class=HTMLResponse)
async def add_pricing(request: Request, user: dict = Depends(require_admin)):
    form = await request.form()
    pool = get_pool()
    error = None

    try:
        provider = (form.get("provider") or "").strip().lower()
        model = (form.get("model") or "").strip().lower()
        if not provider or not model:
            raise ValueError("Provider and model are required.")

        input_rate       = float(form.get("input_rate") or 0)
        output_rate      = float(form.get("output_rate") or 0)
        cache_read_rate  = float(form.get("cache_read_rate") or 0)
        cache_write_rate = float(form.get("cache_write_rate") or 0)
        thinking_rate_raw = (form.get("thinking_rate") or "").strip()
        thinking_rate    = float(thinking_rate_raw) if thinking_rate_raw else None
        provider_label   = (form.get("provider_label") or "").strip() or None
        notes            = (form.get("notes") or "").strip() or None

        effective_from_raw = (form.get("effective_from") or "").strip()
        if effective_from_raw:
            effective_from = datetime.fromisoformat(effective_from_raw).replace(tzinfo=timezone.utc)
        else:
            effective_from = datetime.now(timezone.utc)

        await pool.execute("""
            INSERT INTO model_pricing
                (provider, model, provider_label, notes,
                 input_rate, output_rate, cache_read_rate, cache_write_rate,
                 thinking_rate, effective_from)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
        """, provider, model, provider_label, notes,
             input_rate, output_rate, cache_read_rate, cache_write_rate,
             thinking_rate, effective_from)

        _log.info(
            "model pricing added: provider=%s model=%s in=%.4f out=%.4f "
            "cr=%.4f cw=%.4f thinking=%s effective_from=%s",
            provider, model, input_rate, output_rate,
            cache_read_rate, cache_write_rate,
            f"{thinking_rate:.4f}" if thinking_rate else "output_rate",
            effective_from.isoformat(),
        )
        return RedirectResponse("/settings/model-pricing", status_code=303)

    except Exception as exc:
        _log.warning("model pricing add failed: %s", exc)
        error = str(exc)

    # Re-render with error
    current_rates = await pool.fetch("""
        SELECT DISTINCT ON (provider, model)
               provider, model, provider_label,
               input_rate, output_rate, cache_read_rate, cache_write_rate,
               thinking_rate, effective_from, notes
        FROM   model_pricing
        ORDER  BY provider, model, effective_from DESC
    """)
    history = await pool.fetch("""
        SELECT provider, model, provider_label,
               input_rate, output_rate, cache_read_rate, cache_write_rate,
               thinking_rate, effective_from, notes
        FROM   model_pricing
        ORDER  BY effective_from DESC, provider, model
        LIMIT  100
    """)
    return templates.TemplateResponse("admin/model_pricing.html", {
        "request": request,
        "user": user,
        "current_rates": current_rates,
        "history": history,
        "error": error,
        "can_edit": True,
    })
