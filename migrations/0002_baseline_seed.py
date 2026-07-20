"""0002_baseline_seed — seed the platform ship-set from the committed seed/ tree.

Read by apply.py (asyncpg) as a .py migration: `up(conn)` runs inside the
migration's transaction. Loads the human-readable files under <repo>/seed/ into
the DB so a fresh install comes up with every template, prompt, skill, tool, and
linkage already present. Idempotent (ON CONFLICT DO NOTHING).

Source of truth is the file tree — edit those files (or re-run publish's extractor)
to change what ships; this loader just ingests whatever is there.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

# migrations/ is at the repo root; the seed tree sits next to it at <repo>/seed.
SEED = Path(__file__).resolve().parent.parent / "seed"


def _sha256(text: str) -> str:
    # Must match app/routers/skills.py::_sha256 so the app doesn't see seeded
    # skills as "changed".
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a `---\\n ... \\n---\\n` YAML-lite header from the content body.

    Header holds simple `key: value` lines; `applicable_to` is a comma list.
    Content after the closing fence is returned verbatim (exact round-trip).
    """
    if not text.startswith("---\n"):
        raise ValueError("skill file missing frontmatter")
    end = text.index("\n---\n", 4)
    header = text[4:end]
    content = text[end + len("\n---\n"):]
    meta: dict = {}
    for line in header.splitlines():
        if not line.strip():
            continue
        key, _, val = line.partition(":")
        meta[key.strip()] = val.strip()
    return meta, content


async def up(conn) -> None:
    if not SEED.exists():
        raise FileNotFoundError(f"seed tree not found at {SEED}")

    # 1. tools registry -----------------------------------------------------
    tools = json.loads(_read(SEED / "tools" / "tools.json"))
    for t in tools:
        # Only native built-in tools belong in the baseline. source='custom' tools
        # (registry_*/discovery_*/research_*/apply_*) are created idempotently at
        # runtime by the platform-council bootstrap; seeding them here (without a
        # tier/source_code) violates tools_source_tier_consistency. Native tools
        # (tier IS NULL) must have is_core=true per tools_native_is_core.
        if t["source"] not in ("core", "platform"):
            continue
        await conn.execute(
            """INSERT INTO tools (name,label,description,group_name,tags,source,status,is_core)
               VALUES ($1,$2,$3,$4,$5,$6,$7,true) ON CONFLICT (name) DO NOTHING""",
            t["name"], t["label"], t["description"], t["group_name"],
            t["tags"], t["source"], t["status"],
        )

    # 2. tool profiles + their tools ----------------------------------------
    profiles = json.loads(_read(SEED / "tools" / "tool_profiles.json"))
    for p in profiles:
        await conn.execute(
            """INSERT INTO tool_profiles (id,name,description,status)
               VALUES ($1,$2,$3,$4) ON CONFLICT (id) DO NOTHING""",
            p["id"], p["name"], p["description"], p["status"],
        )
        for tool_name in p.get("tools", []):
            await conn.execute(
                """INSERT INTO tool_profile_tools (profile_id,tool_name)
                   VALUES ($1,$2) ON CONFLICT (profile_id,tool_name) DO NOTHING""",
                p["id"], tool_name,
            )

    # 3. templates + files + tool-profile linkage ---------------------------
    tdir = SEED / "templates"
    for d in sorted(p for p in tdir.iterdir() if p.is_dir()):
        meta = json.loads(_read(d / "meta.json"))
        await conn.execute(
            """INSERT INTO templates
                 (id,type,name,description,model,emoji,tools_allow,tools_deny,
                  memory_enabled,max_tokens,notes,status,version)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
               ON CONFLICT (id) DO NOTHING""",
            meta["id"], meta["type"], meta["name"], meta["description"],
            meta["model"], meta["emoji"], meta["tools_allow"], meta["tools_deny"],
            meta["memory_enabled"], meta["max_tokens"], meta["notes"],
            meta["status"], meta["version"],
        )
        for f in sorted(p for p in d.iterdir() if p.is_file() and p.name != "meta.json"):
            await conn.execute(
                """INSERT INTO template_files (template_id,filename,content)
                   VALUES ($1,$2,$3) ON CONFLICT (template_id,filename) DO NOTHING""",
                meta["id"], f.name, _read(f),
            )
        for tp in meta.get("tool_profiles", []):
            await conn.execute(
                """INSERT INTO template_tool_profiles (template_id,profile_id,position)
                   VALUES ($1,$2,$3) ON CONFLICT (template_id,profile_id) DO NOTHING""",
                meta["id"], tp["profile_id"], tp["position"],
            )

    # 4. skills -------------------------------------------------------------
    sdir = SEED / "skills"
    for f in sorted(sdir.glob("*.md")):
        meta, content = _parse_frontmatter(_read(f))
        applicable = [x.strip() for x in meta.get("applicable_to", "").split(",") if x.strip()]
        await conn.execute(
            """INSERT INTO skills
                 (name,title,description,content,content_hash,version,applicable_to,source_path,status)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
               ON CONFLICT (name) DO NOTHING""",
            meta["name"], meta["title"], meta["description"], content,
            _sha256(content), int(meta["version"]), applicable, None, meta["status"],
        )

    # 5. memory prompts (memory-v2 flush / compaction / story-complete / reap) --
    mdir = SEED / "memory_prompts"
    if mdir.exists():
        for f in sorted(mdir.glob("*.md")):
            meta, content = _parse_frontmatter(_read(f))
            await conn.execute(
                """INSERT INTO memory_prompts
                     (agent_type, prompt_kind, version, content, is_active, created_by, notes)
                   VALUES ($1,$2,$3,$4,$5,$6::uuid,$7)
                   ON CONFLICT DO NOTHING""",
                meta["agent_type"], meta["prompt_kind"], int(meta["version"]),
                content, (meta.get("is_active", "true").lower() == "true"),
                # created_by is NOT NULL UUID (no FK) — sentinel marks a seeded row.
                "00000000-0000-0000-0000-000000000000", meta.get("notes") or "",
            )

    # 6. platform council bootstrap rows (static system seed) ------------------
    # The __platform__ singleton goal hosts the Platform Research Council. Without
    # it, ensure_platform_council() logs "goal missing" and never provisions the
    # platform steward / platform assistant.
    await conn.execute(
        """INSERT INTO goals (id, name, description, color_tag, created_by, status)
           VALUES ('__platform__', 'Platform Improvement',
                   'Standing Platform Research Council — an admins-only advisory goal where the research Convener and admins discuss platform improvements (models, agent design, coding tooling, hosting/infra), grounded in the repo + template inventory + web research.',
                   '#0ea5e9', 'system', 'active')
           ON CONFLICT (id) DO NOTHING"""
    )
    # Single-row state for the "recreate platform agents once per build" feature.
    await conn.execute(
        """INSERT INTO platform_recreate_state (id, last_build_sha)
           VALUES ('singleton', '') ON CONFLICT (id) DO NOTHING"""
    )

    # 7. ledger system actors (static; user/agent actors are created at runtime) -
    af = SEED / "actors.json"
    if af.exists():
        for a in json.loads(_read(af)):
            await conn.execute(
                """INSERT INTO actors (id, type, uid, agent_id, display_name)
                   VALUES ($1::uuid,$2,$3,$4,$5) ON CONFLICT (id) DO NOTHING""",
                a["id"], a["type"], a.get("uid"), a.get("agent_id"), a["display_name"],
            )

    # 8. claude-code phase registry --------------------------------------------
    cpdir = SEED / "claude_phases"
    if cpdir.exists():
        for f in sorted(cpdir.glob("*.md")):
            meta, body = _parse_frontmatter(_read(f))
            await conn.execute(
                """INSERT INTO claude_phases
                     (name, description, system_prompt, allowed_tools, denied_tools, model, status)
                   VALUES ($1,$2,$3,$4,$5,$6,$7) ON CONFLICT (name) DO NOTHING""",
                meta["name"], meta.get("description", ""), body,
                json.loads(meta.get("allowed_tools", "[]")),
                json.loads(meta.get("denied_tools", "[]")),
                meta.get("model", ""), meta.get("status", "active"),
            )

    # 9. model pricing ---------------------------------------------------------
    mp = SEED / "model_pricing.json"
    if mp.exists():
        for r in json.loads(_read(mp)):
            await conn.execute(
                """INSERT INTO model_pricing
                     (provider, model, provider_label, notes, input_rate, output_rate,
                      cache_read_rate, cache_write_rate, thinking_rate)
                   VALUES ($1,$2,$3,$4,$5::numeric,$6::numeric,$7::numeric,$8::numeric,$9::numeric)
                   ON CONFLICT DO NOTHING""",
                r["provider"], r["model"], r.get("provider_label"), r.get("notes"),
                r["input_rate"], r["output_rate"], r["cache_read_rate"],
                r["cache_write_rate"], r.get("thinking_rate"),
            )
