"""platform_registry data access — the curated v2 registry (research →
integration-review → act).

This module is the ONLY place that writes `platform_registry`. Three writers:
  - producers (model_research / tool_research) -> upsert_findings (batch, capped)
  - integration reviewer                       -> set_verdict (one row)
  - the steward marks lifecycle                -> mark_story_created

Anti-bloat rules enforced here (not just in prompts):
  - upsert on `fingerprint`: re-surfacing a known model/tool UPDATES its row
    instead of inserting a duplicate.
  - per-run cap: a single registry_write batch may carry at most TOP_N entries
    per (source, role): 5 per target_role for model_research (so GO can't starve
    SHCA), 5 flat for tool_research. The 6th+ is rejected.
  - re-review gate: an upsert that materially changes `detail` resets verdict to
    'pending' (so the reviewer re-checks); an unchanged re-surface keeps the
    existing verdict (no weekly re-review churn).
"""
from __future__ import annotations

import json
import logging
from uuid import uuid4

from app.database import get_pool

log = logging.getLogger(__name__)

TOP_N = 5               # max entries per research write batch (flat)
MODEL_PER_ROLE_CAP = 2  # model_research: at most 2 models recommended per agent role

_VALID_SOURCE = {"model_research", "tool_research", "skill_research"}
_VALID_CATEGORY = {"model", "tool", "mcp", "skill"}
_VALID_ROLE = {"GO", "SHCA", "IA", "RA", "council", "all"}
_VALID_VERDICT = {"pending", "adopt_now", "adopt_with_build", "not_applicable",
                  "rejected", "out_of_scope"}
# The 4 no-code operations an in-scope finding maps to (set alongside adopt_now).
_VALID_OP = {"change_template_model", "add_skill", "add_tool", "create_credential"}


class RegistryError(Exception):
    """Raised on a rejected write (cap exceeded, bad enum). Surfaced to the
    calling agent as a tool error so it self-corrects."""


def _coerce_role(role: str | None) -> str:
    role = (role or "all").strip()
    return role if role in _VALID_ROLE else "all"


def _enforce_cap(source: str, entries: list[dict]) -> None:
    """Reject a batch that exceeds the caps. A single research writes at most TOP_N
    entries total; model_research additionally may recommend at most
    MODEL_PER_ROLE_CAP models per agent role (GO/SHCA) — we never recommend more
    than 2 models for the same agent type."""
    if len(entries) > TOP_N:
        raise RegistryError(
            f"cap exceeded: a single research may write at most {TOP_N} entries; "
            f"got {len(entries)}. Submit only your best few."
        )
    if source == "model_research":
        per_role: dict[str, int] = {}
        for e in entries:
            r = _coerce_role(e.get("target_role"))
            per_role[r] = per_role.get(r, 0) + 1
        over = {r: n for r, n in per_role.items() if n > MODEL_PER_ROLE_CAP}
        if over:
            raise RegistryError(
                f"cap exceeded: recommend at most {MODEL_PER_ROLE_CAP} models per agent "
                f"role; got {over}. Submit only your top {MODEL_PER_ROLE_CAP} for each role."
            )


async def upsert_findings(
    *, source: str, entries: list[dict], run_id: str | None = None,
    source_agent_id: str | None = None, branch: str = "",
) -> dict:
    """Batch-upsert findings from a producer. Enforces the top-N cap, then
    upserts each on `fingerprint`. Returns {ok, written:[{id,fingerprint,action}]}.

    Each entry: {fingerprint, category, title, summary?, detail?, evidence?,
    impact?, provider_family?, target_role?}.
    """
    if source not in _VALID_SOURCE:
        raise RegistryError(f"invalid source {source!r} (expected one of {sorted(_VALID_SOURCE)})")
    if not isinstance(entries, list) or not entries:
        raise RegistryError("entries must be a non-empty list")
    _enforce_cap(source, entries)

    pool = get_pool()
    written: list[dict] = []
    for e in entries:
        fingerprint = str(e.get("fingerprint") or "").strip()
        category = str(e.get("category") or "").strip()
        title = str(e.get("title") or "").strip()
        if not fingerprint or category not in _VALID_CATEGORY or not title:
            raise RegistryError(
                f"each entry needs a fingerprint, a category in {sorted(_VALID_CATEGORY)}, "
                f"and a title (got fingerprint={fingerprint!r}, category={category!r}, "
                f"title={'set' if title else 'empty'})"
            )
        evidence = e.get("evidence")
        impact = e.get("impact")
        row = await pool.fetchrow(
            """
            INSERT INTO platform_registry
                (id, source, category, fingerprint, target_role, provider_family,
                 title, summary, detail, evidence, impact, finding_status,
                 source_agent_id, run_id, branch)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, 'new', $12, $13, $14)
            ON CONFLICT (fingerprint) DO UPDATE SET
                source          = EXCLUDED.source,
                category        = EXCLUDED.category,
                target_role     = EXCLUDED.target_role,
                provider_family = EXCLUDED.provider_family,
                title           = EXCLUDED.title,
                summary         = EXCLUDED.summary,
                detail          = EXCLUDED.detail,
                evidence        = EXCLUDED.evidence,
                impact          = EXCLUDED.impact,
                source_agent_id = EXCLUDED.source_agent_id,
                run_id          = EXCLUDED.run_id,
                branch          = EXCLUDED.branch,
                finding_status  = 'updated',
                -- re-review only when the finding materially changed:
                verdict     = CASE WHEN platform_registry.detail IS DISTINCT FROM EXCLUDED.detail
                                   THEN 'pending' ELSE platform_registry.verdict END,
                reviewed_at = CASE WHEN platform_registry.detail IS DISTINCT FROM EXCLUDED.detail
                                   THEN NULL ELSE platform_registry.reviewed_at END,
                updated_at  = NOW()
            RETURNING id, (xmax = 0) AS inserted
            """,
            uuid4().hex, source, category, fingerprint, _coerce_role(e.get("target_role")),
            (str(e.get("provider_family")).strip() if e.get("provider_family") else None),
            title, str(e.get("summary") or ""), str(e.get("detail") or ""),
            json.dumps(evidence if evidence is not None else []),
            json.dumps(impact if impact is not None else {}),
            source_agent_id, run_id, branch,
        )
        written.append({
            "id": row["id"], "fingerprint": fingerprint,
            "action": "inserted" if row["inserted"] else "updated",
        })
    log.info("platform_registry: %s wrote %d findings (run=%s)", source, len(written), run_id)
    return {"ok": True, "count": len(written), "written": written}


async def set_verdict(
    *, finding_id: str, verdict: str, adoption_path: str = "",
    supported_today: bool | None = None, reviewer_notes: str = "",
    op_type: str | None = None, op_params: dict | None = None,
) -> dict:
    """Integration reviewer writes its verdict into the finding's own row. For an
    in-scope (adopt_now) finding it also records op_type + op_params — the
    structured execution spec (which of the 4 no-code ops + its args)."""
    if verdict not in _VALID_VERDICT:
        raise RegistryError(f"invalid verdict {verdict!r} (expected one of {sorted(_VALID_VERDICT)})")
    op_type = (op_type or "").strip() or None
    if op_type is not None and op_type not in _VALID_OP:
        raise RegistryError(f"invalid op_type {op_type!r} (expected one of {sorted(_VALID_OP)})")
    pool = get_pool()
    # Auto-derive for MODELS: a model finding maps to exactly ONE op
    # (change_template_model), so a reviewer that marks it adopt_now without an op_type
    # must not 400 — fill it deterministically. template_id follows from target_role;
    # model is best-guessed from the fingerprint ('model:<id>:<role>') and validated at
    # apply time. (tool/skill genuinely need reviewer-supplied op_params, so they still
    # require an explicit op_type below.)
    if verdict == "adopt_now" and op_type is None:
        frow = await pool.fetchrow(
            "SELECT category, target_role, fingerprint FROM platform_registry WHERE id=$1", finding_id)
        if frow and frow["category"] == "model":
            op_type = "change_template_model"
            if not op_params:
                role = (frow["target_role"] or "").strip().upper()
                tmpl = {"GO": "go-v2", "SHCA": "kernel-assistant"}.get(role, "")
                fp = (frow["fingerprint"] or "").split(":")
                model_guess = fp[1] if len(fp) >= 2 and fp[0] == "model" else ""
                op_params = {k: v for k, v in (("template_id", tmpl), ("model", model_guess)) if v}
    if verdict == "adopt_now" and op_type is None:
        raise RegistryError("adopt_now requires an op_type (one of "
                            f"{sorted(_VALID_OP)}) — an in-scope finding must map to an operation")
    updated = await pool.fetchval(
        """
        UPDATE platform_registry
           SET verdict         = $1,
               adoption_path   = $2,
               supported_today = $3,
               reviewer_notes  = $4,
               op_type         = $5,
               op_params       = $6,
               reviewed_at     = NOW(),
               updated_at      = NOW()
         WHERE id = $7
        RETURNING id
        """,
        verdict, adoption_path or "", supported_today, reviewer_notes or "",
        op_type, json.dumps(op_params or {}), finding_id,
    )
    if not updated:
        raise RegistryError(f"finding {finding_id!r} not found")
    return {"ok": True, "id": finding_id, "verdict": verdict, "op_type": op_type}


async def search(
    *, category: str | None = None, verdict: str | None = None,
    target_role: str | None = None, impl_status: str | None = None,
    fingerprint: str | None = None, limit: int = 100,
) -> list[dict]:
    """Filtered read. Empty/None filters are ignored. Newest-updated first."""
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT id, source, category, fingerprint, target_role, provider_family,
               title, summary, finding_status, verdict, adoption_path,
               supported_today, impl_status, story_id, reviewed_at, updated_at
          FROM platform_registry
         WHERE ($1 = '' OR category    = $1)
           AND ($2 = '' OR verdict     = $2)
           AND ($3 = '' OR target_role = $3)
           AND ($4 = '' OR impl_status = $4)
           AND ($5 = '' OR fingerprint = $5)
         ORDER BY updated_at DESC
         LIMIT $6
        """,
        (category or "").strip(), (verdict or "").strip(), (target_role or "").strip(),
        (impl_status or "").strip(), (fingerprint or "").strip(), max(1, min(limit, 500)),
    )
    return [_serialize(r) for r in rows]


async def get(finding_id: str) -> dict | None:
    """Full row (incl. detail/evidence/impact) by id."""
    pool = get_pool()
    row = await pool.fetchrow("SELECT * FROM platform_registry WHERE id = $1", finding_id)
    return _serialize(row, full=True) if row else None


async def list_pending(limit: int = 50) -> list[dict]:
    """Rows awaiting an integration verdict — the reviewer's work queue."""
    return await search(verdict="pending", limit=limit)


async def mark_story_created(finding_id: str, story_id: str) -> None:
    """Stamp a finding once the steward cuts a story for it."""
    pool = get_pool()
    await pool.execute(
        "UPDATE platform_registry SET impl_status='story_created', story_id=$1, "
        "updated_at=NOW() WHERE id=$2",
        story_id, finding_id,
    )


def _serialize(row, *, full: bool = False) -> dict:
    d = dict(row)
    for k in ("reviewed_at", "updated_at", "created_at"):
        if d.get(k) is not None:
            d[k] = d[k].isoformat()
    if full:
        for k in ("evidence", "impact"):
            v = d.get(k)
            if isinstance(v, str):
                try:
                    d[k] = json.loads(v)
                except Exception:
                    pass
    return d
