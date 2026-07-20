"""Platform Research Council v2 inventory — segregated "what exists today" fact
sheets pushed into each worker's workspace so they reason over real data rather
than hallucinate.

Four builders, one focused doc per worker input (GO and SHCA kept SEPARATE):
  build_model_catalog_md   -> Model Research: supported families + pricing
  build_go_profile_md      -> Model Research: GO ONLY (model/context/prompts)
  build_shca_profile_md    -> Model Research: SHCA ONLY (kept separate)
  build_tool_catalog_md    -> Tool Research: catalog + tiers + usage gaps

TEMPLATE-LEVEL ONLY (never per-instance) — per-user SHCA instances live in
agents/agent_instances, not templates. Real pricing comes from the model_pricing
table; code-level detail is read by the reviewer from its own repo clone.
"""
from __future__ import annotations


# ─────────────────────────────────────────────────────────────────────────────
# v2 segregated builders — one focused doc per worker input, GO/SHCA kept apart.
#   build_model_catalog_md   -> Model Research: supported families + pricing
#   build_go_profile_md      -> Model Research: GO ONLY (model/context/prompts)
#   build_shca_profile_md    -> Model Research: SHCA ONLY (kept separate)
#   build_tool_catalog_md    -> Tool Research: catalog + tiers + usage gaps
# Real pricing comes from the model_pricing table (latest rate per provider+model).
# ─────────────────────────────────────────────────────────────────────────────

import re

from app.llm_models import SUPPORTED_MODELS, context_window_for, label_for

GO_TEMPLATE_ID = "go"
SHCA_TEMPLATE_ID = "default-shca"

# Provider families OpenClaw can run TODAY (the supportability gate). Synthetic
# context-window providers (google-flash-1m, anthropic-sonnet-200k, …) are
# wrappers around these same upstreams, so they count as the same family.
SUPPORTED_FAMILIES = ("anthropic", "google")


def _basename(model_id: str) -> str:
    return model_id.split("/")[-1]


def _strip_ctx(name: str) -> str:
    return re.sub(r"-\d+[km]$", "", name)


def _strip_date(name: str) -> str:
    return re.sub(r"-\d{8}$", "", name)


def _family_of(provider: str) -> str:
    """Map a (possibly synthetic) provider id to its upstream family."""
    p = (provider or "").lower()
    if p.startswith("anthropic"):
        return "anthropic"
    if p.startswith("google"):
        return "google"
    return p or "other"


async def _latest_pricing(pool) -> dict:
    """{normalized_model_name: (input_rate, output_rate)} — latest rate per model.
    Best-effort: returns {} if the table is missing/empty."""
    try:
        rows = await pool.fetch(
            """
            SELECT DISTINCT ON (provider, model) model, input_rate, output_rate
              FROM model_pricing
             ORDER BY provider, model, effective_from DESC
            """
        )
    except Exception:
        return {}
    out: dict = {}
    for r in rows:
        out[_strip_date(r["model"])] = (r["input_rate"], r["output_rate"])
    return out


def _price_for(model_id: str, pricing: dict) -> str:
    base = _strip_date(_basename(model_id))
    rate = pricing.get(base) or pricing.get(_strip_ctx(base))
    if not rate:
        return "pricing not on file"
    inp, out = rate
    return f"${inp}/M in · ${out}/M out"


async def build_model_catalog_md(pool) -> str:
    """The model fact sheet for Model Research: supported families (the
    supportability gate), every available model with context window + real
    pricing, and the synthetic context variants."""
    pricing = await _latest_pricing(pool)
    out: list[str] = ["# Model Catalog"]
    out.append(
        "\n## Deployability (context — NOT a research limit)\n"
        "Research the BEST models for each role **regardless of provider**, compare "
        "them to what we run today, and say how each would fit. Whether we can deploy "
        "one is decided LATER by the integration reviewer — not by you. Do NOT restrict "
        "yourself to, or bias toward, the families below.\n"
        f"- **anthropic/** and **google/** — runnable TODAY; a model swap is a pure "
        f"no-code change (`change_template_model`). Stamp `provider_family` accordingly.\n"
        "- **any other family** (OpenAI, Mistral, DeepSeek, xAI, Meta, …) — stamp "
        "`provider_family` as `other`. Still research and record it when it's genuinely "
        "better for a role; the reviewer marks it `out_of_scope` (we don't run that "
        "provider yet) and notes the provider-setup work. Surfacing a strong "
        "out-of-family option is useful signal, not noise."
    )

    real = [m for m in SUPPORTED_MODELS if m.get("tier") != "test"]
    synthetic = [m for m in SUPPORTED_MODELS if m.get("tier") == "test"]

    out.append("\n## Available models (real upstreams)")
    out.append("| model id | label | family | context | pricing |")
    out.append("|---|---|---|---|---|")
    for m in real:
        fam = _family_of(m.get("provider", ""))
        ctx = context_window_for(m["id"])
        out.append(
            f"| `{m['id']}` | {m.get('label','')} | {fam} | "
            f"{ctx:,} | {_price_for(m['id'], pricing)} |"
        )

    if synthetic:
        out.append("\n## Synthetic context-window variants")
        out.append(
            "`<base>-{50k,100k,200k,500k,1m}` providers pin a larger contextWindow "
            "around the SAME upstream model (same family, same pricing) — used to run "
            "an agent with more/less context. Not separate models."
        )
        for m in synthetic:
            out.append(f"- `{m['id']}` (ctx {context_window_for(m['id']):,})")
    out.append("")
    return "\n".join(out)


async def _template_profile_md(pool, template_id: str, role_label: str) -> str:
    """Shared builder for a single role's profile doc (model + context + tools +
    prompt bodies). Used for GO and SHCA SEPARATELY so the two never mix."""
    tmpl = await pool.fetchrow(
        "SELECT id, type, name, model, tools_allow FROM templates WHERE id = $1",
        template_id,
    )
    out: list[str] = [f"# {role_label} profile"]
    if not tmpl:
        out.append(f"\n_(template `{template_id}` not found)_")
        return "\n".join(out)

    files = await pool.fetch(
        "SELECT filename, content FROM template_files "
        "WHERE template_id = $1 AND filename IN ('AGENTS.md', 'SOUL.md')",
        template_id,
    )
    fmap = {r["filename"]: r["content"] for r in files}
    allow = list(tmpl["tools_allow"] or [])

    out.append(
        f"\nThis is the **{role_label} ONLY**. Recommend the best model for THIS "
        f"role's workload — do not generalize across roles.\n"
    )
    out.append(f"- **template id:** `{tmpl['id']}`")
    out.append(f"- **current model:** `{tmpl['model']}` ({label_for(tmpl['model'])})")
    out.append(f"- **current context window:** {context_window_for(tmpl['model']):,} tokens")
    out.append(f"- **tools granted:** {', '.join(allow) if allow else '(none)'}")
    out.append(
        "\n**Workload profile:** infer from the prompt + tools below — is this role "
        "reasoning-heavy, long-context, high-volume/cost-sensitive, tool-heavy? Pick "
        "the model that best fits THAT profile from ANY provider, and note how it "
        "compares to the current model (better/worse on which axes, and the trade-off)."
    )
    if fmap.get("AGENTS.md"):
        out.append(f"\n## {role_label} AGENTS.md\n```\n{fmap['AGENTS.md'][:8000]}\n```")
    if fmap.get("SOUL.md"):
        out.append(f"\n## {role_label} SOUL.md\n```\n{fmap['SOUL.md'][:4000]}\n```")
    out.append("")
    return "\n".join(out)


async def build_go_profile_md(pool) -> str:
    return await _template_profile_md(pool, GO_TEMPLATE_ID, "GO (Goal Orchestrator)")


async def build_shca_profile_md(pool) -> str:
    return await _template_profile_md(pool, SHCA_TEMPLATE_ID,
                                      "SHCA (System Human Coordination Agent)")


async def build_tool_catalog_md(pool) -> str:
    """The tool fact sheet for Tool Research: current catalog by group, the
    hot-addable custom-tool tiers, and a per-template usage map for gap analysis."""
    tools = await pool.fetch(
        "SELECT name, label, group_name, source, tier FROM tools "
        "WHERE status = 'active' ORDER BY group_name, name"
    )
    templates = await pool.fetch(
        "SELECT id, type, tools_allow FROM templates WHERE status = 'active' ORDER BY type, id"
    )

    out: list[str] = ["# Tool Catalog"]
    out.append(
        "\n## How tools are added (the no-redeploy lanes)\n"
        "New tools/integrations are DB rows in the `tools` table + a `tools.reload` "
        "— **adding or editing a tool never provisions/redeploys a VM.** Custom-tool "
        "tiers you can add hot:\n"
        "- **mcp_server** — wrap an MCP server (this is how new MCP servers get in)\n"
        "- **http_action** — a single templated HTTP call\n"
        "- **openapi** — an OpenAPI spec (one aggregated tool)\n"
        "- **typescript** — a Deno-sandboxed function (net only)\n\n"
        "So a candidate is `adopt_now` if it fits one of these tiers; it needs "
        "`adopt_with_build` only if it requires new control-panel/gateway code."
    )

    out.append("\n## Current tools by group")
    by_group: dict = {}
    for r in tools:
        by_group.setdefault(r["group_name"] or "(ungrouped)", []).append(r)
    if not tools:
        out.append("\n_(no active tools)_")
    for group in sorted(by_group):
        out.append(f"\n### {group}")
        for r in by_group[group]:
            label = f" — {r['label']}" if r["label"] else ""
            tier = f", tier `{r['tier']}`" if r["tier"] else ""
            out.append(f"- `{r['name']}`{label}  _(source `{r['source'] or ''}`{tier})_")

    out.append("\n## Tool usage by agent role (for gap analysis)")
    out.append("Which template is granted which tools — find roles that are "
               "under-equipped relative to their job.")
    for r in templates:
        allow = list(r["tools_allow"] or [])
        out.append(f"- **{r['id']}** ({r['type']}): {', '.join(allow) if allow else '(none)'}")
    out.append("")
    return "\n".join(out)
