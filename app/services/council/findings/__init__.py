"""The council's findings ledger — identity for things a review says.

One import surface over three modules: `parse` reads findings out of a
reviewer's markdown, `store` writes them and records what happened to
them, `render` turns them back into the two documents the council needs.

The sequence for one stage is always the same:

    items    = parse.findings(text, raised_by=stage)
    verdicts = parse.accounted(text)
    await store.record(goal_id, run_id, items)
    await store.apply_accounting(goal_id, run_id, verdicts)

and for the next run's inputs:

    doc = render.open_findings_doc(await store.open_findings(goal_id))
"""

from app.services.council.findings import parse, render, store

__all__ = ["parse", "render", "store", "ingest_stage"]


async def ingest_stage(goal_id: str, run_id: str, stage: str,
                       review_text: str) -> dict:
    """Fold one finished stage's review into the ledger.

    CALLED THE MOMENT A STAGE FINISHES, not at the end of the run. If the
    convener then crashes, `render.packet` can still draw a real packet
    from what the earlier stages recorded — which is what makes "every
    goal review produces its findings" true rather than aspirational.

    Never raises: the stage has already done its work and been recorded
    as done, and losing the ledger write must not retroactively fail it.
    """
    items = parse.findings(review_text, raised_by=stage)
    verdicts = parse.accounted(review_text)
    recorded = await store.record(goal_id, run_id, items)
    closed = await store.apply_accounting(goal_id, run_id, verdicts)
    return {"recorded": recorded, "closed": closed,
            "parsed": len(items), "accounted": len(verdicts)}
