"""`_discard_plan_only_sibling_writes` (diff_review.py) — the guard against landing a
spurious commit in a write repo a story never actually touched.

Confirmed live TWICE: goal VKSDN story ds_a3b026e09e4b18 (the original defect — no
guard at all), then again on story ds_3b41012654528a AFTER this guard was added — a
first version of it assumed the combined `docs/diff.patch`'s `diff --git a/X b/X`
headers were prefixed with the repo's subdir (`repos/Owner__name/...`); they are NOT.
Every repo's segment reuses its OWN repo-relative paths verbatim, distinguished only
by the `=== repos/Owner__name ===` divider line the multi-repo capture inserts before
each one — these fixtures use that real, confirmed format, not a guessed one.
"""
from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

from app.services import diff_review as dr


STORY_ID = "ds_test"
CLONE_ROOT = "repos/acme__primary"
SIBLING = "repos/acme__sibling"
PLAN_REL = f"stories/{STORY_ID}/plans/cycle-01-x.md"
SCOPE_REL = f"stories/{STORY_ID}/scope.md"


def _segment(rel_paths: list[str]) -> str:
    """One repo's chunk of the combined patch: a `diff --git a/X b/X` header per
    file, same shape `begin_diff_review`'s real capture produces."""
    return "".join(
        f"diff --git a/{rel} b/{rel}\nnew file mode 100644\n" for rel in rel_paths
    )


def _combined_patch(*segments: tuple[str, list[str]]) -> str:
    return "".join(f"=== {subdir} ===\n{_segment(rels)}" for subdir, rels in segments)


def _gateway_call_stub(patch_text):
    async def _call(method, params=None, timeout=None):
        if method == "shca.workspace.readFile":
            return {"content": patch_text}
        if method == "shca.workspace.deleteFile":
            _call.deleted.append(params["path"])
            return {"ok": True}
        raise AssertionError(f"unexpected RPC: {method}")
    _call.deleted = []
    return _call


async def _run(clones, patch_text, current_plan_path):
    call_stub = _gateway_call_stub(patch_text)
    with ExitStack() as stack:
        stack.enter_context(patch("app.services.diff_review.get_pool", lambda: None))
        stack.enter_context(
            patch("app.services.story_plan_root.resolve_plan_root", AsyncMock(return_value=CLONE_ROOT)),
        )
        stack.enter_context(
            patch("app.services.diff_review.resolve_workspace_clones", AsyncMock(return_value=clones)),
        )
        stack.enter_context(patch("app.services.diff_review.write_clones", lambda cs: cs))
        stack.enter_context(patch("app.services.diff_review.gateway_admin.call", call_stub))
        await dr._discard_plan_only_sibling_writes(
            instance_id="agent-1", story_id=STORY_ID,
            current_plan_path=current_plan_path, diff_patch_path="docs/diff.patch",
        )
    return call_stub.deleted


@pytest.mark.asyncio
async def test_discards_a_sibling_whose_entire_diff_is_plan_and_scope():
    clones = [{"subdir": CLONE_ROOT}, {"subdir": SIBLING}]
    patch_text = _combined_patch(
        (CLONE_ROOT, ["src/real_work.py"]),
        (SIBLING, [PLAN_REL, SCOPE_REL]),
    )
    deleted = await _run(clones, patch_text, f"{CLONE_ROOT}/{PLAN_REL}")
    assert sorted(deleted) == sorted([f"{SIBLING}/{PLAN_REL}", f"{SIBLING}/{SCOPE_REL}"])


@pytest.mark.asyncio
async def test_leaves_a_sibling_alone_when_it_has_real_work_too():
    clones = [{"subdir": CLONE_ROOT}, {"subdir": SIBLING}]
    patch_text = _combined_patch(
        (CLONE_ROOT, ["src/real_work.py"]),
        (SIBLING, [PLAN_REL, "src/app.py"]),
    )
    deleted = await _run(clones, patch_text, f"{CLONE_ROOT}/{PLAN_REL}")
    assert deleted == [], "real work in the sibling must block the discard"


@pytest.mark.asyncio
async def test_never_touches_the_primary_repo_even_if_its_only_change_is_the_plan():
    # The single-repo exploratory-story case: the story's own primary repo is never a
    # candidate, regardless of what its diff looks like — there is no sibling loop to
    # even consider it since `sibling_subdirs` excludes `clone_root` by construction.
    clones = [{"subdir": CLONE_ROOT}]
    patch_text = _combined_patch((CLONE_ROOT, [PLAN_REL]))
    deleted = await _run(clones, patch_text, f"{CLONE_ROOT}/{PLAN_REL}")
    assert deleted == []


@pytest.mark.asyncio
async def test_leaves_a_sibling_alone_when_its_divider_segment_is_missing():
    # If the combined patch format ever changes again, missing a repo's divider
    # entirely must fail SAFE (leave it alone), not be mistaken for "no changes".
    clones = [{"subdir": CLONE_ROOT}, {"subdir": SIBLING}]
    patch_text = _combined_patch((CLONE_ROOT, ["src/real_work.py"]))
    deleted = await _run(clones, patch_text, f"{CLONE_ROOT}/{PLAN_REL}")
    assert deleted == []
