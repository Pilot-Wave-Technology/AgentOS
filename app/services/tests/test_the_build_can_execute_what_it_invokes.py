"""A build step must not exec a repo script directly.

WHY THIS IS A TEST. `core.filemode` is false on Windows, so `chmod +x`
changes the working tree and NOTHING in the index: the file commits as
100644, every local check still passes -- because locally it is run as
`bash <path>` -- and Cloud Build fails with

    bash: line 3: /workspace/scripts/infra/env_preflight.sh: Permission denied

after the entire build has run. It cost a full dev build to find, and
there is nothing on a Windows workstation that can notice.

THE RULE IS THE CONVENTION, NOT THE MODE BIT. Seven of the ten shell
scripts these builds reference are committed 100644 and work fine,
because every other step invokes them through an interpreter --
`entrypoint: bash` with the path as args[0], or `bash <path>` inside a
`-c` body. Requiring the bit everywhere would flag seven working
files; requiring the convention flags exactly the mistake, and leaves
the outcome independent of a git setting that varies by platform.
"""

import pathlib
import re

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[3]

# A line in a `-c` body that STARTS with a repo path is an exec.
BARE_EXEC = re.compile(r"^/workspace/\S+\.(?:sh|py)\b")


def _script_body(args: list) -> str | None:
    """The text of a `-c` body, or None if this step has none.

    SCOPED TO THE BODY, and that distinction is the whole of it. A
    step whose args[0] IS the script is the safe form -- the
    entrypoint runs it -- and an earlier version of this check read
    every arg line-by-line, so `args: ['/workspace/scripts/smoke.sh',
    ...]` looked identical to a bare exec inside a body. It failed six
    working build files before it failed the one broken step.
    """
    for i, a in enumerate(args):
        if a == "-c" and i + 1 < len(args):
            return args[i + 1]
    return None


def _violations(path: pathlib.Path) -> list[str]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out = []
    for step in doc.get("steps") or []:
        args = [str(a) for a in (step.get("args") or [])]
        entry = (step.get("entrypoint") or "").strip()

        # Form A: args[0] is the script. Safe when the entrypoint runs it.
        if args and args[0].startswith("/workspace/")                 and args[0].endswith((".sh", ".py"))                 and not re.fullmatch(r"bash|sh|python3?|py", entry):
            out.append(f"{path.name}:{step.get('id')}: "
                       f"execs {args[0]} with entrypoint {entry or '<none>'}")

        # Form B: a command line inside a `-c` body that IS the path.
        body = _script_body(args)
        if body:
            for line in body.splitlines():
                if BARE_EXEC.match(line.strip()):
                    out.append(f"{path.name}:{step.get('id')}: "
                               f"execs {line.strip().split()[0]} in a -c body")
    return out


@pytest.mark.parametrize(
    "cb", sorted(ROOT.glob("cloudbuild*.yaml")), ids=lambda p: p.name)
def test_no_step_execs_a_repo_script_directly(cb):
    bad = _violations(cb)
    assert not bad, (
        "a repo script is exec'd rather than run through an interpreter. "
        "It works only if the file is committed 100755, which `chmod +x` "
        "does NOT achieve on Windows, so this fails in Cloud Build and "
        "nowhere else:\n  " + "\n  ".join(bad)
        + "\n\nFix: prefix it with `bash `, as every other step does.")
