"""The profile record — everything that differs between executor types.

Eb5 fixes the rule this module exists to keep: **everything that differs is
data, not a branch in the worker.** One executor, three profiles. If the
harness ever has to ask which profile it is running, that claim is false and
Eb6's diff proof fails.

A profile is selected by `(kind, domain)`, both of which are already columns
on `executions` and already bucket admission (C7). Nothing new names a type.

What a profile carries:

    system prompt      the stable prefix — AGENTS.md / SOUL.md (E6e)
    tool set           becomes envelope.allowed_tools, chain stage 2
    workspace driver   git, copy-in, or none (E7d)
    completion         whether propose_completion exists at all (Eb3)
    planning           whether work needs a registered, approved plan

Deliberately a frozen dataclass and not a dict: a typo in a profile field
should fail at construction, in one place, not silently disable a guard
somewhere downstream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


class UnknownProfileError(Exception):
    """Raised when (kind, domain) matches no registered profile.

    Fails closed and by name. A default profile would hand an unknown
    execution the coding tool surface, which is the one with write and
    bash in it.
    """


@dataclass(frozen=True)
class Profile:
    """One executor type, as data."""

    # Identity — matches executions.kind / executions.domain.
    kind: str
    domain: str

    # WHICH PHASE OF THAT WORK, matching executions.phase. Empty is the
    # work itself; "review" is the read-only pass that runs in the same
    # pod after the work is finished and a person has accepted it.
    #
    # A THIRD KEY COMPONENT RATHER THAN A FOURTH PROFILE DIRECTORY NAMED
    # story-review. `kind` and `domain` say what the work IS, and a
    # review of a coding story is still coding work — it reads the same
    # tree, needs the same workspace driver, and differs only in what it
    # may do to it. Keying on a phase keeps "one agent type per (kind,
    # domain)" true and lets the review surface be derived from the same
    # base.
    phase: str = ""

    # E6e: files read from the workspace, in order, to build the stable
    # system-prompt prefix. Missing files are skipped, not an error — a
    # repo without a SOUL.md is normal, a repo without any of them is a
    # repo whose conventions we do not know, which is also normal.
    prompt_files: tuple[str, ...] = ()

    # Prepended above the prompt files. The harness's own contract with
    # the model: how tools behave, what a refusal means, when to stop.
    # Not the place for durability or progress instructions — E3 and E6c
    # keep those harness-owned precisely because prompts do not hold.
    prompt_preamble: str = ""

    # Chain stage 2. The EFFECTIVE surface: (base + added) - denied,
    # already composed by the loader. None would mean "open surface"; a
    # profile always has an explicit surface, so this is a tuple, never None.
    #
    # The three fields under it record how the surface was arrived at.
    # Composition keeps profiles short to edit; carrying the parts keeps
    # the resolved list printable, so "what can this profile actually
    # call?" never has to be answered by merging two files in your head.
    # The model this AGENT TYPE is built around, when it has an opinion.
    #
    # Optional, and precedence puts it third: a user's choice for a
    # particular run beats it, and an explicit caller beats them both.
    # A profile that names nothing takes the harness default, which is
    # the right answer for most of them — a coding executor is not
    # inherently a Sonnet or a Flash.
    #
    # Where it matters: an errand that only runs a shell command wants
    # the cheapest model that can call a tool, and a business story that
    # writes prose does not want the same one. That is a property of the
    # agent type, so it is declared with the agent type.
    model: str = ""

    # Leg size for this agent type, when it has an opinion. Same
    # precedence as `model`: a user's choice for a run beats it.
    #
    # An errand that runs one command does not want a 200,000-token leg,
    # and a coding story that reads a large tree does not want a small
    # one. That is a property of the agent type, so it is declared with
    # the agent type — 0 means "no opinion, take the default".
    leg_tokens: int = 0
    leg_steps: int = 0
    max_legs: int = 0

    tools: tuple[str, ...] = ()
    base_tools: tuple[str, ...] = ()
    added_tools: tuple[str, ...] = ()
    denied_tools: tuple[str, ...] = ()

    # E7d. "git" | "copyin" | "none".
    workspace_driver: str = "none"

    # I7 -- how much compute this kind of work gets. A WORD, resolved by
    # kernel.sizes, because the alternative is four numbers per profile
    # that drift out of Autopilot's memory:CPU ratio one profile at a
    # time. Empty means the default, which is `small`: a generous
    # default is one nobody revisits, and the quota is shared.
    size: str = ""

    # Eb3: absent at dispatch, never discouraged in a prompt.
    allows_completion: bool = True

    # Whether the executor must register a plan and have it approved
    # before doing work. An errand has no plan to approve.
    requires_plan: bool = False

    # Eb1: an errand has no contract, no criteria, no Verifier.
    has_verifier: bool = True

    # Ec3d — H2's policy, per agent type. `{"approve_reversible": true}`
    # makes an ordinary shell command need a person, which is the right
    # posture for an agent type let loose on a production account and the
    # wrong one for story-coding, which would then park on every `pytest`.
    #
    # IT CANNOT LOOSEN `irreversible`, and that is enforced where it is
    # read rather than here: `effects.requires_approval` returns True for
    # that class as an EARLY RETURN, before the policy is consulted at
    # all. A lookup with a default would be one typo away from
    # overridable.
    #
    # `None` means "no opinion" and behaves exactly as before this field
    # existed, which is what keeps the two story profiles unchanged.
    #
    # The plan recorded this as a field the loader already read and the
    # validator refused. It did not exist at all — the other four in that
    # note (`model`, `leg_tokens`, `leg_steps`, `max_legs`) did.
    effect_policy: dict | None = None

    # Skills. Directories searched, in order, for skill definitions. The
    # STABLE prompt lists only each skill's name and one-line description;
    # the body is loaded by the `skill` tool when the model asks for it.
    # Progressive disclosure, the way Claude Code and OpenClaw both do it:
    # a dozen skill bodies inlined would evict the work from the window,
    # and most of them are irrelevant to any given story.
    skill_dirs: tuple[str, ...] = ()

    # Sub-agents (E7b/E7c). A profile that cannot dispatch has no
    # `subagent` tool in its surface, so chain stage 2 refuses it by name
    # rather than a prompt asking the model not to try — the same shape
    # as propose_completion on an errand (Eb3).
    max_subagent_depth: int = 0

    def tool_names(self) -> list[str]:
        """The surface as envelope.allowed_tools wants it."""
        return list(self.tools)

    def __post_init__(self):
        # AT LOAD, not at dispatch. A typo resolved to `small` gives a
        # large story a small pod, and the OOM that follows reads as a
        # broken test rather than as a misspelled profile.
        from kernel.sizes import resolve as _resolve_size  # noqa: PLC0415

        _resolve_size(self.size)
        if self.workspace_driver not in ("git", "copyin", "none"):
            raise ValueError(
                f"unknown workspace_driver {self.workspace_driver!r} for "
                f"profile {self.kind}/{self.domain}"
            )
        if self.requires_plan and not self.allows_completion:
            # A profile that must plan but can never claim completion has
            # no way to finish; that is a construction error, not a policy.
            raise ValueError(
                f"profile {self.kind}/{self.domain} requires a plan but "
                "cannot propose completion — it could never finish"
            )
