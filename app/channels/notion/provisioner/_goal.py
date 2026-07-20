"""Goal structure provisioning — create or refresh the Notion page hierarchy for a goal."""
import json as _json
import logging

from ._helpers import _shca_for_member

_log = logging.getLogger(__name__)


async def _ensure_parent_gallery_view(
    access_token: str,
    parent_database_id: str,
    integration_id: str,
    pool,
) -> None:
    """Add a gallery view to the workspace Goals DB, once per integration lifetime.

    Tracks completion in platform_integrations.config so we never add duplicate views.
    """
    from app.channels.notion.pages import setup_gallery_view_on_db

    row = await pool.fetchrow(
        "SELECT config FROM platform_integrations WHERE id = $1", integration_id,
    )
    if not row:
        return
    raw = row["config"] or {}
    cfg = _json.loads(raw) if isinstance(raw, str) else (raw or {})
    if cfg.get("parent_gallery_setup"):
        return

    try:
        from notion_client import AsyncClient
        notion = AsyncClient(auth=access_token)
        db = await notion.databases.retrieve(parent_database_id)
        sources = db.get("data_sources") or []
        ds_id = sources[0]["id"] if sources else parent_database_id
        await setup_gallery_view_on_db(access_token, parent_database_id, ds_id, "Goals")
        cfg["parent_gallery_setup"] = True
        await pool.execute(
            "UPDATE platform_integrations SET config = $1 WHERE id = $2",
            _json.dumps(cfg), integration_id,
        )
        _log.info("Notion: parent gallery view set up db=%s", parent_database_id)
    except Exception as exc:
        _log.warning("Notion: parent gallery view setup failed db=%s: %s", parent_database_id, exc)


async def _do_provision_goal_structure(
    goal_id: str,
    integration_id: str,
    access_token: str,
    parent_database_id: str,
    pool,
) -> str | None:
    """Create or refresh Notion page hierarchy for a goal. Idempotent.

    Branch A — existing GO binding found (re-enable or repeat call):
      Check if stories_board_db_id is absent (old integration). If so, create
      the databases now (upgrade path). Skip member subpages entirely.

    Branch B — no existing GO binding (first enable):
      Call create_goal_structure to build goal page + stories board + orchestrator DB,
      insert the GO binding, store config keys, set up gallery view on parent DB,
      then import any existing stories.

    Returns an error string on failure, None on success.
    """
    goal = await pool.fetchrow(
        "SELECT name, objective, status, go_thread_id FROM goals WHERE id = $1", goal_id,
    )
    if not goal:
        return "Goal not found"
    if not goal["go_thread_id"]:
        return "Goal thread not set up yet"

    members = [
        dict(r) for r in await pool.fetch(
            """SELECT gm.uid, u.display_name, u.email, u.actor_id
               FROM goal_members gm
               JOIN users u ON u.uid = gm.uid
               WHERE gm.goal_id = $1""",
            goal_id,
        )
    ]

    existing_go_binding = await pool.fetchrow(
        """SELECT external_channel_id FROM platform_channel_bindings
           WHERE goal_id = $1 AND integration_id = $2
             AND actor_id IS NULL AND archived_at IS NULL
           LIMIT 1""",
        goal_id, integration_id,
    )

    if existing_go_binding:
        # Branch A — update mode: check for old integrations missing the new databases.
        gpi_row = await pool.fetchrow(
            "SELECT config FROM goal_platform_integrations WHERE goal_id = $1 AND integration_id = $2",
            goal_id, integration_id,
        )
        raw_cfg = (gpi_row["config"] if gpi_row else None) or {}
        cfg = _json.loads(raw_cfg) if isinstance(raw_cfg, str) else (raw_cfg or {})

        if not cfg.get("stories_board_db_id"):
            # Old integration without sub-page databases — create them now.
            # Orchestrator DB creation DISABLED here too: a goal reaching this
            # branch doesn't have an orch_db_id yet either, so per the
            # "no new provisioning going forward" rule it stays unprovisioned
            # like a brand-new goal — see create_goal_structure in
            # app/channels/notion/pages/databases.py for the primary switch.
            from app.channels.notion.pages import (
                create_stories_board_database,
                # create_orchestrator_db,
            )
            from notion_client import AsyncClient
            notion = AsyncClient(auth=access_token)
            goal_page_id = existing_go_binding["external_channel_id"]
            try:
                stories_board_db_id, stories_board_ds_id = await create_stories_board_database(
                    goal_page_id, goal["name"], notion, access_token,
                )
                # orch_db_id, orch_ds_id = await create_orchestrator_db(
                #     goal_page_id, goal["name"], notion, access_token,
                # )
                orch_db_id, orch_ds_id = "", ""
                cfg["stories_board_db_id"] = stories_board_db_id
                cfg["stories_board_ds_id"] = stories_board_ds_id
                cfg["orch_db_id"]          = orch_db_id
                cfg["orch_ds_id"]          = orch_ds_id
                if gpi_row:
                    await pool.execute(
                        "UPDATE goal_platform_integrations SET config = $1 WHERE goal_id = $2 AND integration_id = $3",
                        _json.dumps(cfg), goal_id, integration_id,
                    )
                _log.info(
                    "Notion provision (upgrade): created databases goal=%s stories=%s orch=%s",
                    goal_id, stories_board_db_id, orch_db_id,
                )
                await _import_existing_stories(goal_id, stories_board_db_id, access_token, pool)
            except Exception as exc:
                _log.warning("Notion provision (upgrade): database creation failed goal=%s: %s", goal_id, exc)

        _log.info(
            "Notion provision (update): goal=%s page=%s — no member subpages in new model",
            goal_id, existing_go_binding["external_channel_id"],
        )
        return None

    # Branch B — create mode: build the full Notion structure for the first time.
    from app.channels.notion.pages import create_goal_structure

    try:
        result = await create_goal_structure(
            goal_id=goal_id,
            goal_name=goal["name"],
            goal_objective=goal["objective"] or "",
            goal_status=goal["status"] or "planning",
            members=members,
            parent_database_id=parent_database_id,
            access_token=access_token,
        )
    except Exception as exc:
        _log.error("Notion structure creation error goal=%s: %s", goal_id, exc)
        return str(exc)

    goal_page_id        = result["goal_page_id"]
    stories_board_db_id = result["stories_board_db_id"]
    stories_board_ds_id = result["stories_board_ds_id"]
    orch_db_id          = result["orch_db_id"]
    orch_ds_id          = result["orch_ds_id"]

    await pool.execute(
        """INSERT INTO platform_channel_bindings
               (goal_id, integration_id, external_channel_id, thread_id, resource_kind)
           VALUES ($1, $2, $3, $4, 'comment')
           ON CONFLICT (integration_id, external_channel_id) DO NOTHING""",
        goal_id, integration_id, goal_page_id, goal["go_thread_id"],
    )

    gpi_row = await pool.fetchrow(
        "SELECT config FROM goal_platform_integrations WHERE goal_id = $1 AND integration_id = $2",
        goal_id, integration_id,
    )
    if gpi_row:
        raw_cfg = gpi_row["config"] or {}
        cfg = _json.loads(raw_cfg) if isinstance(raw_cfg, str) else (raw_cfg or {})
        cfg["stories_board_db_id"] = stories_board_db_id
        cfg["stories_board_ds_id"] = stories_board_ds_id
        cfg["orch_db_id"]          = orch_db_id
        cfg["orch_ds_id"]          = orch_ds_id
        await pool.execute(
            "UPDATE goal_platform_integrations SET config = $1 WHERE goal_id = $2 AND integration_id = $3",
            _json.dumps(cfg), goal_id, integration_id,
        )

    if stories_board_db_id:
        await _import_existing_stories(goal_id, stories_board_db_id, access_token, pool)

    # Create a member page for each existing member so they have a chat surface.
    from app.channels.notion.provisioner._member import _do_provision_member_subpage
    for m in members:
        member_uid = m.get("uid", "")
        if not member_uid:
            continue
        try:
            await _do_provision_member_subpage(
                goal_id, member_uid, integration_id, access_token, pool,
            )
        except Exception as exc:
            _log.warning("Notion provision: member page failed uid=%s: %s", member_uid, exc)

    # Best-effort: add gallery view to the parent Goals DB (once per integration).
    await _ensure_parent_gallery_view(access_token, parent_database_id, integration_id, pool)

    _log.info(
        "Notion provision (create): goal=%s page=%s stories_db=%s orch_db=%s",
        goal_id, goal_page_id, stories_board_db_id, orch_db_id,
    )
    return None


async def _import_existing_stories(
    goal_id: str,
    stories_board_db_id: str,
    access_token: str,
    pool,
) -> None:
    """Create Notion story cards for stories that don't yet have a card.

    Queries the Stories Board for existing 'Story ID' values, then creates
    cards only for stories not already represented. Story pages are view-only;
    member pages (not story pages) serve as the SHCA chat surfaces.
    """
    from notion_client import AsyncClient
    from app.channels.notion.pages import create_story_card, fetch_existing_story_ids

    notion = AsyncClient(auth=access_token)
    try:
        existing_ids = await fetch_existing_story_ids(stories_board_db_id, notion)
        stories = await pool.fetch(
            "SELECT id, title, state, assigned_to, priority FROM stories WHERE goal_id=$1",
            goal_id,
        )
        created = 0
        for s in stories:
            if str(s["id"]) in existing_ids:
                continue
            try:
                await create_story_card(
                    stories_board_db_id,
                    str(s["id"]),
                    s["title"] or "Untitled",
                    s["state"] or "proposed",
                    s.get("assigned_to") or "",
                    s.get("priority") or "",
                    notion,
                    access_token,
                )
                created += 1
            except Exception as exc:
                _log.warning("Notion: failed to create story card story=%s: %s", s["id"], exc)
        _log.info("Notion import_existing_stories: goal=%s created=%d skipped=%d", goal_id, created, len(existing_ids))
    except Exception as exc:
        _log.warning("Notion _import_existing_stories failed goal=%s: %s", goal_id, exc)
