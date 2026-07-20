"""Generate SQL to update agent_files AND agents table from local template files."""
import os

TEMPLATES = [
    ("planner-ia", "AGENTS.md"),
    ("planner-ia", "SOUL.md"),
    ("backend-dev-ia", "AGENTS.md"),
    ("backend-dev-ia", "SOUL.md"),
    ("frontend-dev-ia", "AGENTS.md"),
    ("frontend-dev-ia", "SOUL.md"),
    ("test-ia", "AGENTS.md"),
    ("test-ia", "SOUL.md"),
    ("code-review-ia", "AGENTS.md"),
    ("code-review-ia", "SOUL.md"),
    ("shca", "AGENTS.md"),
    ("shca", "SOUL.md"),
]

BASE = os.path.join(os.path.dirname(__file__), "..", "..", "agent_files", "templates")
SQ = "'"

lines = [
    "-- Agent template update: .plan/ convention + branch/resource discipline",
    "-- Updates BOTH agent_files (source of truth for dispatch) AND",
    "-- agents.base_system_prompt / agents.soul (display columns).",
    "-- Run in Cloud SQL Studio.",
    "-- Verify after:",
    "-- SELECT agent_id, filename, length(content) AS bytes, written_at",
    "--   FROM agent_files WHERE written_by = 'plan-folder-migration'",
    "--   ORDER BY agent_id, filename;",
    "",
]

# Collect content per agent for the agents table update
agent_contents = {}  # agent_id -> {AGENTS.md: ..., SOUL.md: ...}

for i, (agent_id, filename) in enumerate(TEMPLATES):
    path = os.path.join(BASE, agent_id, filename)
    if not os.path.exists(path):
        lines.append(f"-- MISSING: {path}")
        continue
    with open(path, encoding="utf-8") as f:
        content = f.read()
    safe = agent_id.replace("-", "_") + "_" + filename.replace(".", "_")
    tag = f"$T{i}_{safe}$"

    # agent_files upsert — INSERT or UPDATE so this script works whether
    # the row exists (normal sync) or was wiped (rebuild from templates).
    lines.append(f"-- {agent_id} / {filename} ({len(content)} bytes)")
    lines.append(f"INSERT INTO agent_files (agent_id, filename, content, written_at, written_by)")
    lines.append(f"VALUES ({SQ}{agent_id}{SQ}, {SQ}{filename}{SQ}, {tag}")
    lines.append(content.rstrip())
    lines.append(f"{tag}, now(), {SQ}plan-folder-migration{SQ})")
    lines.append(f"ON CONFLICT (agent_id, filename) DO UPDATE")
    lines.append(f"   SET content = EXCLUDED.content,")
    lines.append(f"       written_at = EXCLUDED.written_at,")
    lines.append(f"       written_by = EXCLUDED.written_by;")
    lines.append("")

    # Collect for agents table
    agent_contents.setdefault(agent_id, {})
    agent_contents[agent_id][filename] = (content, tag, i)

# Now generate agents table UPDATEs (base_system_prompt from AGENTS.md, soul from SOUL.md)
lines.append("")
lines.append("-- ============================================================")
lines.append("-- Sync agents.base_system_prompt and agents.soul columns")
lines.append("-- (display copies — agent_files is the source of truth)")
lines.append("-- ============================================================")
lines.append("")

for agent_id, files in agent_contents.items():
    agents_md = files.get("AGENTS.md")
    soul_md = files.get("SOUL.md")

    set_parts = []
    if agents_md:
        content, _, idx = agents_md
        safe = agent_id.replace("-", "_") + "_agents_sync"
        tag = f"$S{idx}_{safe}$"
        set_parts.append(f"base_system_prompt = {tag}\n{content.rstrip()}\n{tag}")
    if soul_md:
        content, _, idx = soul_md
        safe = agent_id.replace("-", "_") + "_soul_sync"
        tag = f"$S{idx}_{safe}$"
        set_parts.append(f"soul = {tag}\n{content.rstrip()}\n{tag}")

    if set_parts:
        lines.append(f"-- agents table: {agent_id}")
        lines.append(f"UPDATE agents SET")
        lines.append(f"  {','.join(set_parts)}")
        lines.append(f" WHERE id = {SQ}{agent_id}{SQ};")
        lines.append("")

out_path = os.path.join(os.path.dirname(__file__), "update_agent_templates.sql")
with open(out_path, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print(f"Written {len(lines)} lines to {out_path}")
