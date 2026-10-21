"""E7e — a workspace is COMPOSED, and the kernel learns what a resource is.

E7d built the right seam — `acquire` · `checkpoint` · `publish`, three
bindings, named by the profile — and then gave it **one** `source` and
**one** `ref`. `executions.workspace_source` is a single scalar column,
the git binding clones one repo, `checkpoint` is `git add -A` at one path
and `publish` pushes one remote.

So multi-repo was not merely unbuilt, it was **inexpressible** — and that
is a regression against the system this replaces, which clones every
granted repo into `repos/<owner>__<name>/` and grants all of them to
every story. Nothing recorded said the new executor was less capable
here.

THE KERNEL HAD NO RESOURCE CONCEPT AT ALL. Nothing under `kernel/` read a
story's resource list, so `workspace_source` was set by whoever created
the execution and the link from *what the story was granted* to *what
its executor gets* did not exist. This module is that link.

A MOUNT, NOT A CLONE. Each entry says what to establish, where, by which
binding, and whether the executor may write to it. Adding a kind of
resource is then a binding plus a row — `repo` is git, `doc` is copyin
read-only, and whatever comes later is neither — rather than a change to
the shape of a workspace. That is what makes "a story could eventually
have anything in its resources" cheap.

WHY A SIBLING ROOT AND NEVER A CHILD. H0x seeded each run a directory
INSIDE the shared `/workspace`, which is itself a git repo — so the
seeded tree was untracked content, and the first story to run a clean at
that root deleted the workspace of the run beside it. Observed: execution
1541's leg 12 committed "Restore clean check surface" and took out both
1543's freshly seeded repo and two stages' worth of artifacts. A
workspace root is a place nothing else owns.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from typing import Optional

from kernel.workspace import errand_write

logger = logging.getLogger(__name__)

# Where a repo lands. Matches the layout the current system uses, so a
# story moving across does not have to be re-taught where its code is —
# and there is deliberately NO top-level `repo/`: a story that looks for
# one is looking for the single-repo layout this does not have.
REPO_LAYOUT = "repos/{owner}__{name}"
DOC_LAYOUT = "docs/{name}"

# Every kind that is a FILE the story reads. `doc` is the kernel's own
# older word and `document` is what the control panel writes on upload;
# both must land in docs/, and the mismatch between them cost a whole
# story planned against invented rules. `file` and `dataset` are the
# other write-backed non-repo kinds (see WRITE_BACKED_KINDS in
# kernel.go.tools) and behave identically as inputs.
DOC_KINDS = ("doc", "document", "file", "dataset")

_CLEAN = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class Mount:
    """One resource, established somewhere under the workspace root."""

    at: str                          # relative to the root; "" is the root
    binding: str                     # git | copyin | none
    source: Optional[str] = None
    ref: Optional[str] = None
    writable: bool = False
    kind: str = "repo"
    resource_id: Optional[str] = None

    @property
    def label(self) -> str:
        return self.at or "<root>"


def slug(text: str) -> str:
    return _CLEAN.sub("-", (text or "").strip()).strip("-")


def repo_path(url: str) -> str:
    """`repos/<owner>__<name>` for a clone URL, however it is written.

    Accepts the three forms that actually turn up — https, ssh and scp —
    because a story's resources are typed by people and by three
    different APIs, and normalising here is cheaper than at every reader.
    """
    text = (url or "").strip()
    text = re.sub(r"\.git$", "", text)
    text = re.sub(r"^[a-z+]+://", "", text)
    text = re.sub(r"^[^@/]+@", "", text)
    text = text.replace(":", "/")
    parts = [p for p in text.split("/") if p]
    owner = slug(parts[-2]) if len(parts) >= 2 else "local"
    name = slug(parts[-1]) if parts else "repo"
    return REPO_LAYOUT.format(owner=owner, name=name)


def mount_for(kind: str, source: str, ref: str | None = None,
              writable: bool = False, resource_id: str | None = None,
              name: str | None = None, qualify: bool = False) -> Mount:
    """The mount a resource of this kind becomes.

    One place decides, so a new kind is a branch here and a binding —
    never a special case at each caller.
    """
    if kind == "repo":
        return Mount(at=repo_path(source), binding="git", source=source,
                     ref=ref, writable=writable, kind=kind,
                     resource_id=resource_id)
    if kind == "branch":
        # A LANE. Writable = the story's working checkout at the
        # canonical path (the engine's feature branch resolves later);
        # read-only = a checkout of THE LANE ITSELF, branch-suffixed so
        # it can sit beside the working clone of the same repository.
        # Before this arm existed, a lane grant fell into the generic
        # `else` below: at=<resource id slug>, binding='none' -- a mount
        # that cloned nothing and told the executor nothing, which is
        # how a frontend story with `needs cal_backend` had no way to
        # see what the backend lane already contained.
        at = repo_path(source)
        if ref and (not writable or qualify):
            at = f"{at}@{ref}"
        # A READ lane pins its ref (that branch's landed state IS what
        # was granted). A WRITE lane must not: the story works on the
        # engine's branch for that lane, and a stored ref would pin the
        # checkout to the merge target instead.
        #
        # `qualify` GIVES A SECOND WRITE LANE ITS OWN DIRECTORY, and
        # READ THE NEXT PARAGRAPH BEFORE TRUSTING IT, because this
        # function is not always the thing that decides.
        #
        # THE SQL IN `story_resources` IS THE AUTHORITY FOR A TRUNK
        # LANE. It does not read the stored `at` for one -- `CASE WHEN
        # gr.kind = 'branch' AND gr.role = 'trunk' THEN NULL ELSE sr.at
        # END` throws it away -- and derives the path itself, suffixing
        # every write lane when a window function counting write grants
        # per (story, repo) exceeds one. That is SET-AWARE, which is the
        # property this function cannot have, and it means a two-lane
        # story got two directories before `qualify` existed.
        #
        # This was learned the expensive way: `qualify` was added to fix
        # a collision that the SQL had already solved, and the commit
        # that added it said so wrongly. What it actually buys is the
        # branch grants the SQL does NOT re-derive -- `role='feature'`
        # rather than `role='trunk'`, of which there are more in the
        # database than trunk ones -- where `sr.at` survives and this
        # function's answer is the one that lands.
        #
        # Keeping the two in agreement is the real reason to have it. A
        # helper that contradicted the SQL would be a trap for whoever
        # read only the helper, which is exactly how the wrong
        # conclusion got reached the first time.
        #
        # Everything downstream was already built for the plural case:
        # `publish_execution` iterates every writable mount,
        # `story_commit` runs per mount, `sync_trunk` merges into every
        # writable repository, and the git engine treats (repo, lane) as
        # a write target -- "two lanes of one repo are two targets
        # exactly like two repos" -- lane-qualifying its node key and
        # branch name whenever two targets share a repository. One path
        # name was the whole of what stood in the way.
        #
        # THE CALLER DECIDES, because this function sees one resource
        # and the collision is a property of the SET. `plan.py` makes
        # the same call the same way: it counts targets per repo and
        # qualifies only where the count exceeds one, so a single-lane
        # story keeps its historical name. Default False for exactly
        # that reason -- every existing caller is unchanged, and so is
        # every existing workspace layout.
        #
        # The path carries the lane while `ref` stays empty, and that
        # pairing is deliberate rather than an oversight: the directory
        # says WHICH lane this checkout serves, and the empty ref says
        # the engine's feature branch for that lane resolves later.
        return Mount(at=at, binding="git", source=source,
                     ref=("" if writable else ref),
                     writable=writable, kind=kind, resource_id=resource_id)
    # Ec4d — A CLOUD RESOURCE IS NOT A FILE, so it gets no mount.
    #
    # Observed on KER25 errand 91cdcd6e: an environment and an identity
    # granted to a run materialised as two EMPTY DIRECTORIES named by
    # raw resource id -- `baa405ff-496`, `c6566f47-187` -- sitting in
    # the workspace root. Nothing to open, nothing explaining them, and
    # the same "granted but not materialised" shape the DOC_KINDS arm
    # below was written to fix.
    #
    # What a cloud grant actually delivers is a LEASE, written by
    # `kernel/credentials/issue.py` and named by an environment
    # variable. That road is entirely separate from mounting, which is
    # why the honest mount here is none at all -- and WORKSPACE.md says
    # what the run can reach instead, because a directory cannot.
    if cloud_kind(kind):
        return Mount(at="", binding="none", source=source, writable=False,
                     kind=kind, resource_id=resource_id)
    if kind in DOC_KINDS:
        # Read-only, always. A doc is an input; a story that needs to
        # write one is producing a new resource, not editing its brief.
        #
        # `document`, NOT just `doc`. This tested `kind == "doc"` while
        # the control panel registers every upload as `document`, so all
        # three documents of a goal fell through to the catch-all below
        # and became binding='none' -- "granted, but not materialised".
        # WORKSPACE.md then told the executor to "reach it through its
        # own tools", and the executor has no document tool: read_
        # resource is GO's. So it planned the whole story from the story
        # text, inventing the rules the documents existed to state, and
        # nothing anywhere said a word about it.
        # THE SAME RULE THE SQL ABOVE APPLIES. An `artifact://` source
        # ends in a content hash, not a filename, so the tail of the URI
        # is 64 hex characters and tells a reader nothing.
        leaf = source.rsplit("/", 1)[-1]
        if source.startswith("artifact://") and name:
            leaf = name
        return Mount(at=DOC_LAYOUT.format(name=slug(leaf)),
                     binding="copyin", source=source, writable=False,
                     kind=kind, resource_id=resource_id)
    return Mount(at=slug(resource_id or kind), binding="none", source=source,
                 kind=kind, resource_id=resource_id, writable=False)


async def mounts_for(conn, execution_id: int) -> tuple:
    """What this execution's workspace is made of.

    Falls back to `workspace_source` as a single mount at the ROOT when
    no rows exist. That bridge is not politeness to old code: every
    execution created before this migration has one, the console still
    starts stories that way, and a fallback that produced an empty
    workspace would turn a missing feature into a broken one.
    """
    rows = await conn.fetch(
        """
        SELECT at, binding, source, ref, writable, kind, resource_id
          FROM execution_mounts WHERE execution_id = $1 ORDER BY at
        """,
        execution_id)
    if rows:
        by_at: dict = {}
        for r in rows:
            m = Mount(at=r["at"], binding=r["binding"], source=r["source"],
                      ref=r["ref"], writable=r["writable"],
                      kind=r["kind"], resource_id=r["resource_id"])
            held = by_at.get(m.at)
            # A COLLISION IS NOW WORTH SAYING OUT LOUD. The tie-break
            # below is deliberate and stays -- the same resource granted
            # twice under two relations should resolve to one mount, and
            # the one carrying a ref and write access is the right
            # winner. What was wrong was doing it in silence: two
            # writable lanes of one repository used to arrive here with
            # the same `at`, and a story lost half its workspace with
            # nothing written anywhere. `mount_for(qualify=True)` stops
            # that case from reaching this point; this stops the NEXT
            # one from being invisible too.
            if held is not None:
                logger.warning(
                    "execution %s: two mounts claim %r -- keeping the one "
                    "with ref=%r writable=%s over ref=%r writable=%s. A "
                    "second write lane of one repository needs "
                    "mount_for(qualify=True) so the two get separate "
                    "directories.",
                    execution_id, m.at,
                    *((m.ref, m.writable, held.ref, held.writable)
                      if (bool(m.ref), bool(m.writable))
                      > (bool(held.ref), bool(held.writable))
                      else (held.ref, held.writable, m.ref, m.writable)))
            if held is None or ((bool(m.ref), bool(m.writable))
                                > (bool(held.ref), bool(held.writable))):
                by_at[m.at] = m
        return tuple(by_at[k] for k in sorted(by_at))

    legacy = await conn.fetchval(
        "SELECT workspace_source FROM executions WHERE id = $1", execution_id)
    if not legacy:
        # NO ROWS AND NO SOURCE IS AN EMPTY WORKSPACE, NOT A BROKEN CLONE.
        #
        # This used to return the mount below unconditionally, with
        # `source=None`. Nothing can clone from None, so `prepare_workspace`
        # found no `.git` and raised `workspace incomplete` — and the
        # worker's dispatch sweep catches that, writes `stall_reason` and
        # NEVER DISPATCHES. The run sits at PENDING for ever.
        #
        # It reached everything with no mounts: 60 null-exec fixtures, 16
        # story/coding rows and the single errand. It stayed invisible
        # because the console calls `prepare_workspace` inside the contract
        # try/except, which swallows it, so the one path anybody drove by
        # hand was the one path that worked.
        #
        # The fallback itself is still right for what it was written for —
        # an execution that HAS a `workspace_source` predates
        # `execution_mounts` and must still get its repo. It is just not a
        # reason to invent a mount when there is nothing to mount.
        return ()
    return (Mount(at="", binding="git", source=legacy, writable=True,
                  kind="repo"),)


async def record(conn, execution_id: int, mounts) -> None:
    """Write the mounts this execution will be given. Idempotent by `at`."""
    for m in mounts:
        await conn.execute(
            """
            INSERT INTO execution_mounts
                (execution_id, at, binding, source, ref, writable, kind,
                 resource_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (execution_id, at) DO UPDATE
               SET binding = EXCLUDED.binding, source = EXCLUDED.source,
                   ref = EXCLUDED.ref, writable = EXCLUDED.writable,
                   kind = EXCLUDED.kind, resource_id = EXCLUDED.resource_id
            """,
            execution_id, m.at, m.binding, m.source, m.ref, m.writable,
            m.kind, m.resource_id)
    logger.info("execution %s: %d mount(s) — %s", execution_id, len(mounts),
                ", ".join(f"{m.label}{'' if m.writable else ' (ro)'}"
                          for m in mounts))


def writable(mounts) -> tuple:
    """The mounts an executor may commit in."""
    return tuple(m for m in mounts if m.writable)


def writable_scope_for(root: str, mounts) -> list:
    """The path prefixes an executor may write to. E7f.

    Interceptor stage 1 already contains writes by path prefix; what it
    was given was the workspace ROOT, so a story granted three repos and
    permitted to write one could edit any of them. Deriving the scope
    from the mounts turns a per-story permission the contract states into
    something the tool layer enforces, with no new mechanism.

    REFUSED AT THE TOOL, NOT AT COMMIT. A write caught at commit time has
    already happened: the edit is on disk, the model has moved on
    believing it worked, and the refusal arrives detached from the act
    that caused it. `checkpoint` skipping read-only mounts is the second
    line, not the first.

    A story with NO mounts recorded gets the root, which is the
    single-repo shape every execution created before E7e has -- narrowing
    that to nothing would stop those runs writing at all.
    """
    import os

    if not mounts:
        return [root]
    scope = [os.path.join(root, m.at) if m.at else root
             for m in mounts if m.writable]
    # Every mount read-only is a legitimate contract -- a review or an
    # audit -- and an empty scope is how the envelope says "writes are
    # refused", so it is returned as-is rather than widened to the root.
    return scope


# ── story-level resources, which a run inherits ──────────────────────

async def story_resources(conn, goal_id: str, story_id: str) -> tuple:
    """What this STORY is entitled to, whoever is running it."""
    # The catalogue owns `source`; the grant owns where and how it is
    # mounted. Joined rather than denormalised onto the grant, because a
    # clone URL copied at grant time is a clone URL that goes stale
    # silently -- and the whole point of one database is that there is
    # one place each fact lives.
    #
    # `writable` is DERIVED from relation, not stored: the control panel
    # already says needs | produces | modifies, and storing a boolean
    # beside it would be two columns for one decision.
    # TWO GRANTERS, ONE READER. The kernel's grant_resource writes the
    # mount fields (at, binding, ref); the GIT ENGINE's auto-link
    # (branches.ensure_branch_resource) writes only (story, resource,
    # relation) -- it predates the kernel and serves the legacy
    # provisioner, which derives layout itself. So every mount field is
    # COALESCEd here from the catalogue row: the engine's branch grant
    # becomes a git mount at repos/<owner>__<name> on the branch it cut,
    # which is exactly the layout the legacy provisioner would build.
    # Without this, an engine-granted story reads back a mount with
    # binding=None at=None, and provisioning dies on the first story the
    # engine ever handed the kernel.
    #
    # And a bare REPO grant defaults its ref to THE STORY'S OWN ENGINE
    # BRANCH when one is live: KER04's first run cloned `main` because
    # GO granted the repo (no branch on the row) and the engine's branch
    # was a separate resource -- so the executor would have committed to
    # trunk's checkout while the engine watched an untouched feature
    # branch. The story's live node is the ref that means "where this
    # story's work goes".
    rows = await conn.fetch(
        """
        SELECT COALESCE(
                   -- A DOCUMENT BELONGS IN docs/, whatever was cached.
                   -- The same stale grant that stored binding='none'
                   -- stored `at` as the bare resource id, so a document
                   -- would mount at `61666023-f8d` -- a path that tells
                   -- the executor nothing and matches no filename the
                   -- story mentions.
                   CASE WHEN gr.kind IN ('doc','document','file','dataset')
                             AND COALESCE(NULLIF(gr.clone_url, ''),
                                          NULLIF(gr.uri, '')) IS NOT NULL
                        -- AN ARTIFACT IS ADDRESSED BY HASH, so the
                        -- tail of its URI is 64 hex characters. Three
                        -- reports mounted as docs/089a908d43a7...,
                        -- docs/9c3c9139e3a9... and docs/ed72db885774...
                        -- and the executor had to open each one to find
                        -- out which was which -- the kind of workspace
                        -- that produces "I read the coverage report"
                        -- when the fuzzing one was opened. Its name is
                        -- in the row; use it.
                        THEN 'docs/' || CASE
                               WHEN COALESCE(NULLIF(gr.clone_url, ''),
                                             NULLIF(gr.uri, '')) LIKE 'artifact://%'
                               THEN regexp_replace(
                                      COALESCE(NULLIF(gr.metadata->>'original_name', ''),
                                               gr.name),
                                      '[^A-Za-z0-9._-]+', '_', 'g')
                               ELSE regexp_replace(
                                      COALESCE(NULLIF(gr.clone_url, ''),
                                               NULLIF(gr.uri, '')),
                                      '^.*/', '')
                             END
                        ELSE NULL END,
                   CASE WHEN gr.kind = 'branch' AND gr.role = 'trunk'
                        THEN NULL ELSE sr.at END,
                        'repos/' || COALESCE(gr.repo_owner, 'x') || '__'
                                 || COALESCE(gr.repo_name, sr.resource_id)
                        -- A READ of another lane of the SAME repo gets
                        -- its own checkout, branch-suffixed. Without
                        -- this, a frontend story's `needs cal_backend`
                        -- derived the same path as its own working
                        -- clone, the one-mount-per-path dedupe kept the
                        -- writable one, and the read access silently
                        -- vanished -- the executor had no way to see
                        -- what the backend lane already contains.
                        || CASE
                           -- reads of a lane always suffix; WRITES
                           -- suffix only when the story writes MORE
                           -- THAN ONE lane of this repo -- each lane
                           -- then gets its own working checkout, on its
                           -- own engine branch, committed and pushed
                           -- independently and landed as one merge
                           -- group. A single-lane story keeps the
                           -- canonical path every existing check names.
                           WHEN gr.kind = 'branch' AND gr.role = 'trunk'
                                AND NULLIF(gr.branch_name, '') IS NOT NULL
                                AND (sr.relation = 'needs'
                                     OR COUNT(*) FILTER (
                                          WHERE gr.kind = 'branch'
                                            AND gr.role = 'trunk'
                                            AND sr.relation IN
                                                ('produces', 'modifies'))
                                        OVER (PARTITION BY sr.story_id,
                                              gr.repo_owner,
                                              gr.repo_name) > 1)
                                THEN '@' || gr.branch_name
                                ELSE '' END)
                   AS at,
               -- 'none' IS NEVER A STORED ANSWER, only a stale one.
               -- The grant caches the mount fields it derived at grant
               -- time, so when `mount_for` did not recognise `document`
               -- and returned binding='none', that wrong answer was
               -- written down -- and fixing the code changed nothing
               -- for every grant already made. A resource that HAS a
               -- source is placeable by definition, so a stored 'none'
               -- on one can only be the old bug talking; ignore it and
               -- re-derive. (A genuinely unplaceable kind has no
               -- source, and still falls through to 'none' below.)
               CASE
                 WHEN gr.kind IN ('repo','branch') THEN
                      COALESCE(NULLIF(sr.binding,'none'), 'git')
                 WHEN COALESCE(gr.clone_url, gr.uri) IS NOT NULL THEN
                      COALESCE(NULLIF(sr.binding,'none'), 'copyin')
                 ELSE COALESCE(sr.binding, 'none')
               END AS binding,
               -- A TRUNK-LANE GRANT NEVER CHECKS OUT THE TRUNK. The
               -- lane row's branch_name is cal_backend or its like --
               -- the MERGE TARGET -- and resolving the workspace to it
               -- would have the story committing straight onto the
               -- lane, bypassing the engine's feature branch entirely.
               -- For role='trunk' the story's own engine-cut branch
               -- comes first, the lane last (a not-yet-cut branch still
               -- lands the clone on the right lane); for everything
               -- else the old order stands.
               CASE
               -- Reading another lane means reading THE LANE: its
               -- landed state, not this story's own feature branch
               -- (which the engine lookup below would wrongly match --
               -- same story, same repo).
               WHEN gr.role = 'trunk' AND sr.relation = 'needs' THEN
                   COALESCE(sr.ref, NULLIF(gr.branch_name, ''))
               -- NO sr.ref HERE. A write-lane grant stores the LANE as
               -- its ref (that is what was granted), and honouring it
               -- would check the story out on its own merge target --
               -- the exact accident that landed KER06 with no PR. The
               -- engine's branch for this lane is the only right answer;
               -- the lane name survives only as the last-resort
               -- fallback for a branch not yet cut.
               WHEN gr.role = 'trunk' THEN
                   COALESCE((SELECT gn.branch_name
                               FROM git_node_stories gns
                               JOIN git_nodes gn ON gn.id = gns.git_node_id
                               JOIN git_repos r2 ON r2.id = gn.git_repo_id
                              WHERE gns.story_id = sr.story_id
                                AND r2.name = gr.repo_name
                                AND lower(coalesce(r2.owner, '')) =
                                    lower(coalesce(gr.repo_owner, ''))
                                AND gn.trunk_branch = gr.branch_name
                                AND gn.state NOT IN ('MERGED', 'ABANDONED')
                              LIMIT 1),
                            NULLIF(gr.branch_name, ''))
               ELSE
                   COALESCE(sr.ref, NULLIF(gr.branch_name, ''),
                            (SELECT gn.branch_name
                               FROM git_node_stories gns
                               JOIN git_nodes gn ON gn.id = gns.git_node_id
                              WHERE gns.story_id = sr.story_id
                                AND gn.git_repo_id = gr.git_repo_id
                                AND gn.state NOT IN ('MERGED', 'ABANDONED')
                              LIMIT 1))
               END AS ref,
               COALESCE(sr.kind, gr.kind, 'repo') AS kind,
               sr.resource_id,
               sr.relation IN ('produces','modifies') AS writable,
               -- NULLIF, because `clone_url` on an uploaded document
               -- is the EMPTY STRING and COALESCE only skips NULL. So
               -- this returned '' for every document, and a copyin
               -- mount with no source silently copies nothing.
               COALESCE(NULLIF(gr.clone_url, ''),
                        NULLIF(gr.uri, '')) AS source
          FROM story_resources sr
          JOIN stories s ON s.id = sr.story_id
          LEFT JOIN goal_resources gr ON gr.id = sr.resource_id
         WHERE s.goal_id = $1 AND sr.story_id = $2
           AND COALESCE(gr.status, 'active') = 'active'
           -- Ec4d — A CLOUD GRANT IS NOT A MOUNT. An environment and an
           -- identity delivered a LEASE, not a tree, and leaving them in
           -- this query put two empty directories named by raw resource
           -- id in the workspace root of KER25 errand 91cdcd6e --
           -- nothing to open and nothing saying what they were. What the
           -- run can reach is stated in WORKSPACE.md instead, because a
           -- directory cannot say it.
           AND COALESCE(gr.kind, '') <> ALL($3::text[])
           -- A LANE GRANT IS THE WORKING CHECKOUT; the bare repo row
           -- and the engine's own feature-branch grant describe the
           -- same tree and would each derive a THIRD, redundant clone
           -- of it. Observed the moment a story held two lanes: three
           -- writable mounts for one repository, the extra one a
           -- duplicate of whichever lane the dedupe happened to keep.
           -- Where the story has an explicit trunk-lane WRITE grant for
           -- a repo, the lane rows own that repo's checkouts outright.
           AND NOT (
                (gr.kind = 'repo'
                 OR (gr.kind = 'branch'
                     AND COALESCE(gr.role, '') <> 'trunk'))
                AND EXISTS (
                    SELECT 1 FROM story_resources sr2
                      JOIN goal_resources gr2 ON gr2.id = sr2.resource_id
                     WHERE sr2.story_id = sr.story_id
                       AND gr2.kind = 'branch' AND gr2.role = 'trunk'
                       AND sr2.relation IN ('produces', 'modifies')
                       AND gr2.repo_name = gr.repo_name
                       AND lower(COALESCE(gr2.repo_owner, '')) =
                           lower(COALESCE(gr.repo_owner, ''))))
           -- F3/R3-P2 — READING THE LANE YOUR OWN BRANCH WAS CUT FROM is
           -- a second full clone of the same commit. `feat/KER25-1b06108e`
           -- was cut from `cal_kernel` at the lane head, so both mounts
           -- held byte-identical content and the executor got the same
           -- tree twice, once suffixed `@cal_kernel`.
           --
           -- Two mechanisms each supplied one and neither knew of the
           -- other: GO granted `needs` on the lane (correct for an
           -- investigation), and the engine's no-write-grant fallback
           -- placed the story on that same lane and derived the feature
           -- branch from it. So the `needs` grant is LOAD-BEARING -- it
           -- is what tells the fallback which lane to cut from -- and
           -- cannot simply be dropped. Only its MOUNT is redundant.
           --
           -- DELIBERATELY NARROW. A cross-lane read (a `cal_frontend`
           -- story granted `needs cal_backend`) has a different
           -- `trunk_branch` and is untouched: that arm exists because
           -- such a story "had no way to see what the backend lane
           -- already contained". And collapsing here loses nothing even
           -- when the lane advances under a long run, because it is the
           -- SAME REPOSITORY -- the lane is a `git fetch origin <lane>`
           -- away inside the checkout that remains.
           AND NOT (
                gr.kind = 'branch' AND COALESCE(gr.role, '') = 'trunk'
                AND sr.relation = 'needs'
                AND EXISTS (
                    SELECT 1 FROM git_node_stories gns3
                      JOIN git_nodes gn3 ON gn3.id = gns3.git_node_id
                      JOIN git_repos r3 ON r3.id = gn3.git_repo_id
                     WHERE gns3.story_id = sr.story_id
                       AND r3.name = gr.repo_name
                       AND lower(COALESCE(r3.owner, '')) =
                           lower(COALESCE(gr.repo_owner, ''))
                       AND gn3.trunk_branch = gr.branch_name
                       AND gn3.state NOT IN ('MERGED', 'ABANDONED'))
                -- AND SOMETHING ELSE OF THIS REPO ACTUALLY SURVIVES.
                --
                -- THE COLLAPSE IS ONLY SAFE AS A DEDUPE. It removes a
                -- SECOND checkout of content the working clone already
                -- has; with no other grant on the repo it removes the
                -- ONLY one, and the run gets an empty workspace.
                --
                -- Which is exactly what happened to errand 2bc47dfb: its
                -- single grant was `needs` on the cal_kernel lane, the
                -- engine's node existed but was still PLANNED so no
                -- feature-branch grant had been made, and the executor
                -- opened its plan with "clone the repository" because it
                -- had been given nothing. It was right; the mount query
                -- was wrong.
                AND EXISTS (
                    SELECT 1 FROM story_resources sr4
                      JOIN goal_resources gr4 ON gr4.id = sr4.resource_id
                     WHERE sr4.story_id = sr.story_id
                       AND sr4.resource_id <> sr.resource_id
                       AND gr4.repo_name = gr.repo_name
                       AND lower(COALESCE(gr4.repo_owner, '')) =
                           lower(COALESCE(gr.repo_owner, ''))
                       AND gr4.kind IN ('repo', 'branch')
                       AND COALESCE(gr4.status, 'active') = 'active'))
         ORDER BY 1
        """,
        goal_id, story_id, list(_cloud_kinds()))
    # ONE MOUNT PER PATH. A story often holds two grants that derive the
    # same `at` -- GO's repo/lane grant AND the engine's feature-branch
    # grant (gb_*) for the same repository. Two mounts at one path would
    # clone twice into one directory; the winner is the row that KNOWS
    # THE BRANCH (a resolved ref beats none -- the engine's grant beats
    # the bare repo row), then the writable one.
    by_at: dict = {}
    for r in rows:
        m = Mount(at=r["at"], binding=r["binding"], source=r["source"],
                  ref=r["ref"], writable=r["writable"], kind=r["kind"],
                  resource_id=r["resource_id"])
        held = by_at.get(m.at)
        if held is None or ((bool(m.ref), bool(m.writable))
                            > (bool(held.ref), bool(held.writable))):
            by_at[m.at] = m
    return tuple(by_at[k] for k in sorted(by_at))


async def grant(conn, goal_id: str, story_id: str, mount: Mount,
                granted_by: str = "go", relation: str = "",
                why: str = "") -> tuple[Mount, str]:
    """Attach a resource to a story. GO's act, not the assistant's.

    Recorded against the STORY so a re-run inherits it. An amendment
    reopens a story and dispatches a fresh execution; without this the
    second attempt would come up missing the repository somebody granted
    mid-flight, and fail for a reason unrelated to the work.

    RETURNS `(mount, relation)` AS GRANTED, which is not always what
    was passed in: an errand's checkout is upgraded to writable here
    (see below). The caller must use what comes back -- `execution_mounts`
    is written from the Mount object, not from the relation, so a caller
    that kept its own copy would record a read-only mount against a row
    that says `modifies`, and the live run would still be unable to
    write. Both halves are returned because neither can be derived from
    the other: `produces` and `modifies` are both writable.
    """
    # The primary key is the control panel's: (story_id, resource_id,
    # relation). Not (goal_id, story_id, at) as the kernel's copy had --
    # which allowed the same resource mounted twice at two paths, and no
    # caller ever wanted that.
    # The caller may say WHICH write relation (produces vs modifies --
    # different facts on the CP model); absent that, derive from the
    # mount the way the kernel always has.
    relation = relation or ("modifies" if mount.writable else "needs")

    # ── AN ERRAND'S CHECKOUT IS WRITABLE ───────────────────────────
    #
    # THE SECOND DOOR. `kernel/go/errand_resources.validate` applies
    # this to a PROPOSED errand, and its comment claimed every errand
    # came past it. A hand-started one does not: the GO creates the
    # story, then grants through `grant_resource` -> here. Errand
    # f87d306f (dev, 2026-10-07) arrived that way and both branches
    # mounted read-only, twenty minutes after proposal #9 came out
    # `modifies` through the other door.
    #
    # Asked HERE rather than at the caller because this is the one
    # place every grant passes, including whatever calls it next. The
    # rule and the reasoning are in `errand_write`, shared verbatim
    # with the proposal door so the two cannot drift.
    if await _is_errand(conn, story_id):
        relation, why, upgraded = errand_write.upgrade(
            mount.kind, relation, why)
        if upgraded:
            mount = replace(mount, writable=True)
            # THE STALE ROW GOES. ON CONFLICT keys on the relation, so
            # a resource already recorded `needs` would be joined by a
            # second `modifies` row rather than replaced, leaving one
            # resource with two answers. `mounts_for` prefers the
            # writable one, so this is tidiness rather than a bug --
            # but two answers is how the next reader gets it wrong.
            await conn.execute(
                "DELETE FROM story_resources WHERE story_id = $1 "
                "  AND resource_id = $2 AND relation = 'needs'",
                story_id, mount.resource_id)
    await conn.execute(
        """
        INSERT INTO story_resources
            (story_id, resource_id, relation, goal_id, at, binding, ref,
             kind, granted_by, notes)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,NULLIF($10,''))
        ON CONFLICT (story_id, resource_id, relation) DO UPDATE
           SET at = EXCLUDED.at, binding = EXCLUDED.binding,
               ref = EXCLUDED.ref, granted_by = EXCLUDED.granted_by,
               notes = COALESCE(EXCLUDED.notes, story_resources.notes)
        """,
        story_id, mount.resource_id, relation, goal_id, mount.at,
        mount.binding, mount.ref, mount.kind, granted_by, why)
    logger.info("story %s/%s granted %s%s by %s", goal_id, story_id,
                mount.label, "" if mount.writable else " (ro)", granted_by)
    return mount, relation


async def _is_errand(conn, story_id: str) -> bool:
    """Whether this story is an errand. NULL means story, as everywhere.

    One cheap lookup per grant, which happens when a person or the GO
    attaches a resource -- never in a hot path.
    """
    return bool(await conn.fetchval(
        "SELECT COALESCE(work_kind, 'story') = 'errand' FROM stories "
        " WHERE id = $1", story_id))


async def inherit(conn, execution_id: int, goal_id: str,
                  story_id: str) -> tuple:
    """Copy the story's resources onto a new run. Returns the mounts.

    A COPY, not a view. `execution_mounts` is what this particular
    workspace was built from and what a verdict was judged against; if it
    followed the story's list, granting a resource later would silently
    change what an old verdict claims to have been checked against.
    """
    mounts = await story_resources(conn, goal_id, story_id)
    if mounts:
        await record(conn, execution_id, mounts)
    return mounts


def cloud_kind(kind: str) -> bool:
    """Is this a cloud environment or identity?

    Asked of `kernel.credentials.clouds` rather than listed here: the
    kinds are defined once, and a second list is the thing this codebase
    keeps paying for.
    """
    from kernel.credentials.clouds import cloud_of_kind

    return cloud_of_kind(kind) is not None


def _cloud_kinds() -> tuple:
    """Every kind that is a cloud grant rather than a tree.

    From `kernel.credentials.clouds`, so adding a cloud does not mean
    remembering to exclude it here as well.
    """
    from kernel.credentials.clouds import (ALL_ENVIRONMENT_KINDS,
                                           ALL_IDENTITY_KINDS)

    return tuple(ALL_ENVIRONMENT_KINDS) + tuple(ALL_IDENTITY_KINDS)
