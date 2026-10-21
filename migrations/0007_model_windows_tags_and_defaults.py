"""0007 -- what the model pickers and the agents' model ids actually read.

The registry is three tables, not one. `models` carries a model; the
context windows a model may run at live in `model_context_windows`, and
its tags in `model_tags` (defined in `tags`). 0002 seeded only `models`
-- including an `available_context_windows` COLUMN the product no longer
reads -- so on a published install:

  * every context-window variant was unregistered, and the assistant and
    GO templates run on one: `google-flash38-500k/gemini-3.8-flash-500k`.
    Agents were provisioned on a model id nothing served.
  * no model was `go-selectable`, so the goal-setup picker fell back to
    every active model, and dropped each whose only window exceeded GO's
    500K cap -- leaving Haiku and the two CLI models.

This seeds the tag definitions (as the internal registry has them), the
windows (100K/200K/500K/1M) and `go-selectable` for Opus 5.5, Sonnet 5.5
and Gemini 3.8 Flash, all from files under seed/.

Also, for an install whose 0002 predates them:
  * the two claude-code phases that named the retired Sonnet 4.5 move to
    Sonnet 5.5 -- only where they still name it, never over an edit;
  * the DAG ceiling's singleton row, so the dashboard's edit of it has a
    row to update (the scheduler already falls back to the env defaults).

Every insert is ON CONFLICT DO NOTHING: an operator's change is never
overwritten.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

SEED = Path(__file__).resolve().parent.parent / "seed"
RETIRED = "anthropic/claude-sonnet-4-5"


def _load(name: str) -> list:
    p = SEED / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


async def up(conn) -> None:
    for t in _load("tags.json"):
        await conn.execute(
            "INSERT INTO tags (name, description) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            t["name"], t.get("description"))

    for w in _load("model_context_windows.json"):
        await conn.execute(
            "INSERT INTO model_context_windows (model_id, tokens) VALUES ($1, $2) "
            "ON CONFLICT DO NOTHING", w["model_id"], int(w["tokens"]))

    for t in _load("model_tags.json"):
        await conn.execute(
            "INSERT INTO model_tags (model_id, tag_name) VALUES ($1, $2) "
            "ON CONFLICT DO NOTHING", t["model_id"], t["tag_name"])

    # The phases the seed now names; only rows still on the retired model.
    for path in sorted((SEED / "claude_phases").glob("*.md")):
        text = path.read_text(encoding="utf-8")
        name = model = None
        for line in text.split("\n---", 1)[0].splitlines():
            if line.startswith("name:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("model:"):
                model = line.split(":", 1)[1].strip()
        if name and model and model != RETIRED:
            await conn.execute(
                "UPDATE claude_phases SET model = $2 WHERE name = $1 AND model = $3",
                name, model, RETIRED)

    n = int(os.environ.get("TASK_DAG_CEILING", "4"))
    enforce = os.environ.get("TASK_DAG_CEILING_ENFORCE", "false").strip().lower() == "true"
    await conn.execute(
        "INSERT INTO dag_ceiling_config (id, n, enforce) VALUES ('singleton', $1, $2) "
        "ON CONFLICT (id) DO NOTHING", n, enforce)
