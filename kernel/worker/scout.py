"""The setup scout: reads a repository like a new teammate would.

Fires when a git mount has NO committed environment definition, or when
the one it has keeps failing. It reads what a person would read -- the
README, the manifests, the repo's own CI workflows (which literally
spell out how the project tests itself) -- and DRAFTS two things: a
`.agentos/setup.sh` and the check commands that fit this repo.

THE DRAFT IS A SUGGESTION, NEVER AN ACT. It lands in the workspace as
SETUP_SUGGESTED.md for the executor to read, adapt and COMMIT like any
other work product. Only after that commit does either room build from
it -- so verification stays a function of committed text, chosen once,
repeated mechanically. A scout that wrote directly into the build would
make verification depend on a model's mood at verify time, which is the
one property the whole verifier exists to rule out.
"""

from __future__ import annotations

import glob
import logging

from kernel.base_image import environment_text
import os

from kernel.harness.models.quick import available, complete_text

logger = logging.getLogger(__name__)

NL = chr(10)

# What a new teammate reads first, in the order they'd read it. CI
# workflows earn their place: they are the repo's own executable answer
# to "how do you test this".
INTERESTING = (
    "README.md", "README.rst", "README",
    "CONTRIBUTING.md", "Makefile", "justfile",
    "package.json", "pyproject.toml", "requirements.txt",
    "pnpm-workspace.yaml", "tsconfig.json",
    ".github/workflows/*.yml", ".github/workflows/*.yaml",
)
PER_FILE_CHARS = 40_000
TOTAL_CHARS = 900_000  # well inside the 500K-token reading budget

PROMPT = """You are the setup scout inside an agent platform. A story's
workspace mount has no working environment definition, and the platform
builds environments ONLY from committed text: a `.agentos/setup.sh` run
with cwd at the repo root, in a container with network. The result must
land INSIDE the tree (.venv/, node_modules/, vendor/) because the
checks later run offline as a different user.
{environment}

Read the repository excerpts below and produce EXACTLY this structure:

## Suggested .agentos/setup.sh
```sh
(the script)
```

## Suggested check commands
- (one shell command per line, runnable from the repo root offline
  after setup, using in-tree executables like .venv/bin/pytest or
  node_modules/.bin/vitest -- never the bare package manager)

## Notes
(1-3 sentences: anything unusual -- monorepo layout, required env vars,
tests that download at runtime and how to pre-fetch them in setup.)

Repository mount: {at}
Why the scout ran: {reason}

REPOSITORY EXCERPTS:
{files}
"""


def _gather(root: str) -> str:
    """The readable excerpts, budgeted. Missing files are simply absent."""
    parts, spent = [], 0
    for pattern in INTERESTING:
        for path in sorted(glob.glob(os.path.join(root, pattern))):
            if spent >= TOTAL_CHARS:
                return NL.join(parts)
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    body = fh.read(PER_FILE_CHARS)
            except OSError:
                continue
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            parts.append(f"===== {rel} ====={NL}{body}{NL}")
            spent += len(body)
    return NL.join(parts)


async def suggest_setup(workspace: str, at: str, reason: str) -> str:
    """Draft a setup for one mount into SETUP_SUGGESTED.md. Returns a
    one-line note for the action log, or "" when nothing was drafted."""
    if not available():
        return ""
    root = os.path.join(workspace, *at.split("/")) if at else workspace
    files = _gather(root)
    if not files.strip():
        return ""
    draft = await complete_text(
        PROMPT.format(at=at or ".", reason=reason, files=files,
                      # ONE DESCRIPTION OF THE IMAGE, shared with the
                      # executor's own system prompt. The scout drafts a
                      # setup script FOR that image, so a scout
                      # describing it from memory drafts against an
                      # environment nobody runs.
                      environment=environment_text().strip()))
    if not draft:
        return ""
    note_path = os.path.join(workspace, "SETUP_SUGGESTED.md")
    try:
        with open(note_path, "w", encoding="utf-8") as fh:
            fh.write("# Suggested environment setup (scout draft)" + NL + NL
                     + "A scout read this repository and drafted the "
                     + "setup below. REVIEW IT, adapt it, then commit it "
                     + "as `" + (at + "/" if at else "")
                     + ".agentos/setup.sh` and call `env_setup`. Nothing "
                     + "uses this file until you commit the script."
                     + NL + NL + draft + NL)
    except OSError:
        return ""
    logger.info("scout drafted a setup for %s (%s)", at or ".", reason)
    return f"scout drafted SETUP_SUGGESTED.md for {at or '.'}"
