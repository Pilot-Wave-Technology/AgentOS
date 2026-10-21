"""Issuing the next version of a story's contract.

There are two roads to a new definition of done — a person amending the
story (f10) and a person approving a plan that carries checks (F11) —
and they must not be two implementations. The first version of this
lived inside `amend.py`; the moment F11 needed the same thing with
labels attached, one copy would have grown a feature the other did not,
and the two would have started disagreeing about what a contract
version means.

WHY A NEW VERSION AND NEVER AN EDIT. Verdicts are keyed to claims, and a
claim was judged against a specific set of criteria. Edit one underneath
it and the verdict that says "passed" refers to something that no longer
exists — an audit trail that lies about work somebody may have shipped.
A new version keeps every past verdict truthful and says what changed,
when, and who changed it.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# Only reached when a story had no contract at all. The Verifier runs
# criteria in this image and must be able to run them; the shim image has
# no test runner, which is how F4's first pass produced six false greens.
#
# FROM THE DEPLOYMENT, not from a literal here. The literal was
# `agentos-kernel/fixture:dev`, which exists in one laptop's Docker cache
# and in no registry, so every contract minted in the cloud named an
# image the cluster could not pull -- ImagePullBackOff, pod never ready,
# dispatch timeout, reconcile recover, re-dispatch, for ever. See
# `kernel/base_image.py`.
from kernel.base_image import for_goal

# E6 -- the name the goal gives is a TAG, and a tag is what F6 has been
# warning about on every single run (205, 207, 208, 209, 210 on
# 2026-09-17). `base_digest` is written here, at approval, or the
# contract records WHY it could not be.
from kernel.image_digest import UNPINNED_KEY, pin, resolve

VALID_LABELS = ("goal", "guard", "unlabelled")


def normalise(checks) -> list[dict]:
    """Accept strings, {run, label}, or {expect, ...}; return one shape.

    Every check comes back as `{kind, run, spec, label}`. `kind` is
    `command` unless the check expects an artifact.

    Ec2d — WHY A SECOND KIND. A `command` check is a shell line run in a
    fresh container against the committed tree. That is right when the
    deliverable IS the tree, and wrong for an errand, whose tree is
    discarded and whose deliverable is a report: there is no command that
    proves a report was written. `artifact[role=report]` says the run must
    end holding one, which is a question about the manifest.

    Strings arrive from the console's amend box and from older callers.
    They become `unlabelled`, which is honest — nobody said what they
    were — and the dry-run reports them as uncheckable rather than
    guessing.
    """
    from kernel.verifier.artifact_kind import describe, parse

    out = []
    for c in checks or []:
        spec, kind = {}, "command"
        if isinstance(c, str):
            run, label = c, "unlabelled"
            # `goal: cmd` / `guard: cmd`, so a person typing into a
            # textarea can label without a second field.
            head, sep, rest = c.partition(":")
            if sep and head.strip().lower() in ("goal", "guard"):
                run, label = rest.strip(), head.strip().lower()
            # The notation the prompt teaches has to be the notation that
            # works — a model shown `artifact[role=report]` will type it.
            # `None` means "not an artifact check"; an empty dict means a
            # badly written one, which must still be recognised so it is
            # refused rather than shelled out.
            parsed = parse(run)
            if parsed is not None:
                kind, spec = "artifact", parsed
        else:
            run = str(c.get("run", "")).strip()
            label = str(c.get("label") or "unlabelled").lower()
            expect = str(c.get("expect") or "").strip().lower()
            if expect == "artifact":
                kind = "artifact"
                spec = {k: str(c[k]).strip() for k in ("role", "name",
                                                       "min_bytes")
                        if c.get(k) not in (None, "")}
                run = describe(spec)
            else:
                # THE SAME NOTATION, IN THE OTHER SHAPE. The branch above
                # applies `parse` to a check written as a bare STRING,
                # with the comment that "the notation the prompt teaches
                # has to be the notation that works". That reasoning was
                # applied to one branch and not this one — and this is
                # the shape the plan-approval path actually uses:
                # `questions.checks` is documented as
                # `[{run, label}]`, which lands here.
                #
                # So an executor wrote `artifact[role=report,name=X.md]`
                # exactly as taught, in the shape the system asked for,
                # and it was adopted as kind='command' — a shell command
                # that can never pass. Observed live on the first errand
                # ever to use an artifact goal check.
                #
                # `expect: artifact` still wins when given: an explicit
                # declaration beats inference from a string.
                parsed = parse(run)
                if parsed is not None:
                    kind, spec = "artifact", parsed
        # A DECLARED BUDGET HAS TO REACH THE CRITERION. `register_plan`
        # lets a check say `timeout_s`, and both rooms read it off the
        # SPEC — `harness/check_run.budget_for` and
        # `verifier/budget.timeout_for`. Dropping it here would make it a
        # field the model is invited to fill in and nothing honours,
        # which is the shape this file already documents twice above.
        #
        # BOTH SPELLINGS, because the model uses both. The schema asks
        # for `timeout_s` at the top of a check and run 255's executor
        # wrote `spec: {timeout_s: 1200}` instead -- reasonably, since
        # that is where the value ends up and where every reader looks
        # for it. Honouring only the documented one dropped the nested
        # one in silence, so the approved plan card showed a 1200s budget
        # and the stored criterion carried none.
        #
        # THAT IS WORSE THAN A REFUSAL, and worse than the bug this
        # comment block was already about. `run_check` resolves the
        # budget from the criterion, so a 13-minute suite would have been
        # started with the 300s default and killed at five minutes -- the
        # tool that exists to run a long check destroying the run it was
        # asked to measure, and reporting a timeout as the work's fault.
        declared = None
        if isinstance(c, dict):
            nested = c.get("spec")
            declared = c.get("timeout_s")
            if declared in (None, "") and isinstance(nested, dict):
                declared = nested.get("timeout_s")
        if declared not in (None, ""):
            try:
                spec = {**spec, "timeout_s": int(float(declared))}
            except (TypeError, ValueError):
                # A number nobody can read is not a budget. The default
                # applies, which is the same as not having said it.
                pass
        run = run.strip()
        if not run:
            continue
        out.append({"kind": kind, "run": run, "spec": spec,
                    "label": label if label in VALID_LABELS else "unlabelled"})
    return out


async def _insert_criteria(conn, contract_id: int, checks: list[dict]):
    """Returns the new criterion ids, in the order given."""
    ids = []
    for c in checks:
        kind = c.get("kind") or "command"
        # `run` is kept on an artifact criterion too. Every reader of a
        # contract — the dry-run, the card, the adjudicator's log — prints
        # it, and a criterion that renders as blank is one a person cannot
        # review. For an artifact it holds the notation back again.
        spec = {"run": c["run"], **(c.get("spec") or {})}
        ids.append(await conn.fetchval(
            "INSERT INTO criteria (contract_id, kind, spec, label) "
            "VALUES ($1, $2, $3::jsonb, $4) RETURNING id",
            contract_id, kind, json.dumps(spec), c["label"]))
    return ids


async def new_version(conn, execution_id: int, checks, note: str,
                      by: str) -> tuple[int, list[int]]:
    """Issue this story's next contract. Returns (contract_id, criterion_ids).

    `checks` is the COMPLETE new set, not an addition — which is what
    makes editing and removing possible at all.
    """
    checks = normalise(checks)
    # `base_image`/`base_digest` come back too: the superseding branch
    # below has to be able to pin a version that never was.
    old = await conn.fetchrow(
        "SELECT c.id, c.base_image, c.base_digest FROM contracts c "
        "JOIN executions e ON e.contract_id = c.id WHERE e.id = $1",
        execution_id)

    if old is None:
        # A story dispatched with NO checks. It can work and commit and
        # can never be judged, and the adjudicator says so every tick —
        # honest, and it leaves a person with nothing to do about it.
        # Giving it its first contract is the repair. Adding a definition
        # of done to a story that had none can only make finishing
        # harder, so there is nothing to protect against.
        run = await conn.fetchrow(
            "SELECT kind, domain, goal_id FROM executions WHERE id = $1",
            execution_id)
        image = await for_goal(conn, run["goal_id"])
        base_digest, spec = pin(image)
        contract_id = await conn.fetchval(
            "INSERT INTO contracts (goal_id, kind, domain, base_image, "
            "base_digest, workspace_spec, approved_at, approved_by, "
            "amendment) VALUES ($1, $2, $3, $4, $5, $6::jsonb, now(), $7, $8) "
            "RETURNING id",
            run["goal_id"], run["kind"], run["domain"], image, base_digest,
            json.dumps(spec), by,
            note or "first contract — this story was dispatched without one")
    else:
        # Everything the previous version settled is copied: image,
        # surface, and the hash taken at ITS approval. The surface hash
        # is deliberately NOT recomputed — it was taken over the tree as
        # approved, and re-taking it now would bless whatever the
        # executor has since done to the checks, which is the defence
        # agreeing with the attack.
        #
        # E6 — WITH ONE EXCEPTION, AND ONLY UPWARDS. A digest the
        # previous version settled is copied untouched; a NULL one is
        # not a settlement, it is the gap F6 has been reporting since
        # before this column was ever written. Every contract already
        # live — 205 through 210 — carries NULL and would carry it for
        # ever, because an amendment copies whatever it finds. Filling
        # it is not a change of image: the name is the same, and THIS
        # version is being approved right now, so the digest recorded is
        # the one being approved. It never overwrites.
        digest = old["base_digest"]
        if not digest:
            digest, _reason = resolve(old["base_image"])
        contract_id = await conn.fetchval(
            """
            INSERT INTO contracts (goal_id, kind, domain, base_image,
                                   base_digest, workspace_spec, tool_allow,
                                   budget, check_surface_hash, approved_at,
                                   approved_by, supersedes, amendment)
            SELECT goal_id, kind, domain, base_image, $4::text,
                   CASE WHEN $4::text IS NULL THEN workspace_spec
                        ELSE workspace_spec - $5::text END,
                   tool_allow, budget, check_surface_hash,
                   now(), $2, id, $3
            FROM contracts WHERE id = $1
            RETURNING id
            """, old["id"], by, note, digest, UNPINNED_KEY)

    ids = await _insert_criteria(conn, contract_id, checks)
    await conn.execute(
        "UPDATE executions SET contract_id = $2 WHERE id = $1",
        execution_id, contract_id)
    logger.info("execution %s: contract %s -> v%s, %d check(s), by %s",
                execution_id, old["id"] if old else "none", contract_id,
                len(checks), by)
    return contract_id, ids


async def current_checks(conn, execution_id: int) -> list[dict]:
    """What this story is currently checked against, with labels."""
    rows = await conn.fetch(
        "SELECT c.spec::text, c.label FROM criteria c JOIN executions e "
        "ON e.contract_id = c.contract_id WHERE e.id = $1 ORDER BY c.id",
        execution_id)
    return [{"run": json.loads(r["spec"]).get("run", ""), "label": r["label"]}
            for r in rows]
