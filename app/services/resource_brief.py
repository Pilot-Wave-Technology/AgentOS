"""G4 — scanner v2: the brief GO reads instead of reading trees.

The old scanner gitingest-cloned the ENTIRE repository and sent every
file to Gemini — the exact read-everything cost GO's doctrine forbids,
performed on its behalf — and nothing ever re-scanned on an event, so
summaries described last week. v2 inverts the economics:

  IDENTITY — what the resource IS. Built from a SELECTION (file tree
  names, README, manifests), LLM-written, rarely: on registration and on
  explicit refresh. Stored as summary_type='identity'.

  DIGEST — what happened to it. One row per landing, appended by the
  MERGED wake with NO LLM: the engine already knows the branch, the
  stories, the merge sha; changed paths come from one best-effort
  compare call and are skipped, not faked, when unavailable. Stored as
  summary_type='digest', append-only. That half lives in
  `resource_digest.py`; the two subjects were one file until it stopped
  fitting in a reading.

Staleness is stamped, never hidden: the brief reports the identity's
as-of sha and how many landings have happened since. A summary that
admits its blind spots is trustworthy; one that does not is confident
and wrong.
"""

from __future__ import annotations

import logging

from app.database import get_pool

_log = logging.getLogger(__name__)

# Top-level files that define a repo's identity. If a landing touches
# one of these, the identity card is stale in a way the digest cannot
# absorb — mark it so the brief says so.
_IDENTITY_PATHS = ("readme", "pyproject.toml", "package.json",
                   "requirements", "dockerfile", "docker-compose",
                   "cloudbuild", "makefile", ".github/workflows")


def _touches_identity(paths: list) -> bool:
    return any(p.lower().startswith(_IDENTITY_PATHS)
               or "/" not in p and any(k in p.lower()
                                       for k in _IDENTITY_PATHS)
               for p in paths)


async def refresh_identity(resource_id: str, goal_id: str) -> None:
    """Rebuild the identity card from a SELECTION, not the tree.

    File tree (names only), README, manifests — a handful of requests
    and one model call. The old full scan shipped every file's contents;
    the identity of a repository lives in its structure and manifests.
    """
    pool = get_pool()
    r = await pool.fetchrow(
        "SELECT kind, repo_owner, repo_name,"
        " COALESCE(NULLIF(branch_name,''),"
        " default_branch, 'main') AS ref FROM goal_resources WHERE id = $1",
        resource_id)
    if r is None or not r["repo_owner"]:
        return

    # THE SAME RULE THE SCANNER FOLLOWS, or this is the way round it.
    #
    # A repo row is not summarised once the goal has declared trunk
    # branches: work happens at branch level, each lane is its own
    # resource with its own card, and a repo card can only show one of
    # them. This path is reached by `resource_brief(refresh: true)` --
    # which `kernel/go/brief.py` actively SUGGESTS when a card is
    # missing -- so without the check the GO's own remedy rebuilds the
    # card the scanner just declined to build, from `default_branch`,
    # which is the `main` this whole change exists to stop describing.
    #
    # Two paths to one fact, disagreeing, with the second one reachable
    # by following the advice printed by the first.
    if (r["kind"] or "") == "repo":
        from app.services.resource_scanner import declared_trunks

        lanes = await declared_trunks(goal_id, r["repo_name"] or "",
                                      r["repo_owner"] or "")
        if lanes:
            _log.info("resource %s: identity refresh declined; this goal "
                      "works on %s, each with its own card",
                      resource_id, ", ".join(sorted(lanes)))
            return
    import httpx

    from app.github_tokens import generate_installation_token
    from app.services.resource_scanner import _store_summary, \
        _summarize_with_gemini

    tok = await generate_installation_token([r["repo_name"]])
    if isinstance(tok, dict):
        tok = tok.get("token", "")
    h = {"Authorization": f"Bearer {tok}",
         "Accept": "application/vnd.github+json"}
    base = f"https://api.github.com/repos/{r['repo_owner']}/{r['repo_name']}"
    async with httpx.AsyncClient(timeout=20) as c:
        tree_r = await c.get(f"{base}/git/trees/{r['ref']}?recursive=1",
                             headers=h)
        tree = [e["path"] for e in tree_r.json().get("tree", [])
                if e.get("type") == "blob"][:400]
        picked = [p for p in tree
                  if _touches_identity([p]) and p.count("/") <= 1][:8]
        bodies = []
        for p in picked:
            fr = await c.get(f"{base}/contents/{p}?ref={r['ref']}",
                             headers={**h,
                                      "Accept": "application/vnd.github.raw"})
            if fr.status_code == 200:
                bodies.append(f"--- {p} ---\n{fr.text[:6000]}")
        head_r = await c.get(f"{base}/commits/{r['ref']}", headers=h)
        head_sha = (head_r.json().get("sha", "") or "")[:12]

    prompt = (
        "Write an identity card for this repository, for a PLANNER that "
        "never reads code. Fixed sections, nothing else:\n"
        "purpose: (two sentences)\nstack:\nlayout: (dir - role, one line "
        "each)\ninterfaces: (what it exposes; known consumers)\nrun:\n"
        "unread: (what this card was not built from)\n\n"
        f"File tree ({len(tree)} files):\n" + "\n".join(tree[:200])
        + "\n\n" + "\n\n".join(bodies))
    summary = await _summarize_with_gemini(prompt)
    await _store_summary(
        resource_id=resource_id, goal_id=goal_id, summary_type="identity",
        content=summary,
        metadata={"selection": picked, "files_seen": len(tree)},
        previous_scan_sha=head_sha)
    await pool.execute(
        "UPDATE goal_resources SET summary_stale = false, "
        "last_summary_at = now(), head_sha = $2 WHERE id = $1",
        resource_id, head_sha)
    _log.info("resource_brief: identity rebuilt for %s @ %s", resource_id,
              head_sha)
