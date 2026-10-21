"""WHAT A REFRESH MAY WRITE, and the far longer list of what it may not.

THIS FILE IS THE SAFETY BOUNDARY. Everything else in the refresh is
recoverable by running it again; a write to the wrong filename is not.

THE GATEWAY WILL NOT STOP US. `agents.files.set` checks its own
allowlist -- `ALLOWED_FILE_NAMES` in openclaw's `server-methods/agents.ts`
-- and that list contains `MEMORY.md`, `memory.md` and `USER_MEMORY.md`,
because the gateway's memory tooling legitimately writes them. So a
refresh that named a memory file would be accepted without complaint and
would destroy the one thing in an agent's workspace nobody can
reconstruct. The narrow list below is the only thing in the way, which is
why it is a module of its own with a test that reads it.

AND THE RESTART WILL NOT STOP US EITHER. Agents write memory
continuously -- the flush runs inside async compaction, mid-turn -- so
there is no quiet moment when overwriting it would be harmless.

WHY SO FEW MANAGED FILES. A refresh exists to close the gap between the
prompts an agent was born with and the prompts in the repo today. That is
`AGENTS.md` and `SOUL.md`. Every other bootstrap file fails at least one
of "the same for every instance" and "safe to overwrite":

  IDENTITY.md    the GATEWAY appends to this itself at create time
                 (`- Name:`, `- Emoji:`, `- Avatar:`), and GO composes a
                 per-instance body. Re-pushing the template would drop
                 what the gateway wrote.
  USER.md        per-user content. `template_sync` already excludes it
                 from the admin cascade for this reason.
  HEARTBEAT.md   written empty at creation to disable a gateway flow;
                 nothing regenerates it and nothing should.
  BOOTSTRAP.md   deliberately stubbed at creation to send the model to
                 AGENTS.md. Stable, and not ours to re-assert.
  RESOURCES.md   story-scoped and change-driven, owned by
                 `resource_provisioning`. It has the same staleness
                 problem as the prompts and the restart will incidentally
                 fix it, but it is not this tool's to write.
  TOOLS.md       not written by provisioning at all.

`template_sync._PROTECTED_FILENAMES` already refuses IDENTITY, HEARTBEAT
and BOOTSTRAP. This list agrees with it and goes further.
"""

from __future__ import annotations

# The only files a refresh writes. Adding to this list is a decision
# about blast radius, not a convenience -- read the module docstring
# first, and the test that guards it.
MANAGED_FILES: tuple[str, ...] = ("AGENTS.md", "SOUL.md")

# Named explicitly rather than merely absent from MANAGED_FILES, so that
# a future edit adding one of these has to delete a line that says why
# not. `NEVER_WRITE` is checked at the point of the push as well, so a
# renderer bug cannot route around it.
NEVER_WRITE: frozenset[str] = frozenset({
    # The three that are unrecoverable.
    "MEMORY.md", "memory.md", "USER_MEMORY.md",
    # The four that belong to something other than the template.
    "IDENTITY.md", "USER.md", "HEARTBEAT.md", "BOOTSTRAP.md",
    # Owned elsewhere.
    "RESOURCES.md", "TOOLS.md",
})


def is_writable(filename: str) -> bool:
    """May a refresh write this file? Unknown names are refused.

    DENY BY DEFAULT, and not merely "not in NEVER_WRITE". A file this
    module has never heard of is one nobody has thought about, and the
    failure mode of refusing it is a missing update somebody notices,
    while the failure mode of allowing it is a workspace nobody can put
    back.
    """
    name = (filename or "").strip()
    if not name or name in NEVER_WRITE:
        return False
    return name in MANAGED_FILES


def refuse_reason(filename: str) -> str:
    """Why this file was not written, in words a person can act on."""
    name = (filename or "").strip()
    if not name:
        return "a file with no name"
    if name in NEVER_WRITE:
        return (f"{name} is never written by a refresh -- it holds memory, "
                f"per-user content, or state the gateway itself wrote")
    return (f"{name} is not in the managed set "
            f"({', '.join(MANAGED_FILES)}); a refresh writes only the "
            f"prompt files it can regenerate from the template")
