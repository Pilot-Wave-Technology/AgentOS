"""One artifact store, reachable from both processes that read it.

THE BUG. `kernel/artifacts/store.py` falls back to the literal
"/artifacts" when ARTIFACT_ROOT is unset. Inside E0's container that was
a NAMED DOCKER VOLUME; on a Windows host the same string resolves to
`C:\artifacts`. Two different directories wearing one path.

That matters because the control panel — which runs natively, NOT in the
container — is where GO's tools actually land: `KERNEL_ASSISTANT_URL`
points at `/internal/kernel` on the CP, and the artifact download route
lives there too. So `read_artifact` refused EVERY artifact any errand had
ever emitted, with E_NO_SUCH_ARTIFACT, while the bytes sat in the
manifest, catalogued as a resource, and present in a directory the reader
could not open.

GO reported this accurately — "returned unavailable when queried with
read_artifact" — and named the error code when asked. It was disbelieved
twice, because the obvious reading was the round-2 confabulation and
because the first two attempts to check it looked at the wrong surface:
the E0 assistant-api log on :8790 (GO does not use it) and then a
grep over a UTF-16 log that silently matched nothing.

The fix is a bind mount plus an explicit ARTIFACT_ROOT, so both
processes open one tree.
"""
from __future__ import annotations

import io
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _read(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8-sig").read()


def test_the_store_is_a_bind_mount_not_a_named_volume():
    """A named volume is invisible to every process outside the
    container, which is the whole defect."""
    compose = _read("infra/e0/docker-compose.yml")
    line = next(l for l in compose.splitlines()
                if l.strip().endswith(":/artifacts"))
    src = line.strip().split(":/artifacts")[0].lstrip("- ").strip()
    assert "/" in src or "." in src, (
        f"the artifact store is mounted from {src!r}, a bare name — that "
        f"is a named volume, which the host cannot open")


def test_the_control_panel_is_pointed_at_the_same_tree():
    """It serves /internal/kernel and the download route, and its
    fallback resolves somewhere else entirely."""
    ps1 = _read("local-development/scripts/run/control-panel.ps1")
    assert "ARTIFACT_ROOT" in ps1, (
        "the control panel does not set ARTIFACT_ROOT, so it falls back "
        "to the literal /artifacts and reads a different store")
    assert "infra" in ps1 and "e0" in ps1


def test_the_fallback_is_still_visible_in_the_code():
    """B1 — no default hidden inside an environ.get call. If the
    fallback moves into the call, the next person cannot see that there
    IS one, which is how this stayed invisible."""
    src = _read("kernel/artifacts/store.py")
    assert 'os.environ.get("ARTIFACT_ROOT")' in src
    assert re.search(r'_ROOT_ENV\s+or\s+"/artifacts"', src), (
        "the fallback root is no longer spelled out beside the lookup")


def test_no_control_characters_in_the_compose_or_launcher():
    """Both files were written through a shell heredoc and both picked
    up a literal BEL from an unescaped `\a` in a Windows path. The
    compose failed to parse — loudly — and the launcher silently pointed
    at `infra\e0<BEL>rtifacts`, which would have recreated the same
    two-stores bug with a stranger name."""
    for rel in ("infra/e0/docker-compose.yml",
                "local-development/scripts/run/control-panel.ps1"):
        text = _read(rel)
        bad = sorted({hex(ord(c)) for c in text
                      if ord(c) < 32 and c not in "\r\n\t"})
        assert not bad, f"{rel} contains control characters {bad}"
