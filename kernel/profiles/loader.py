"""Load profiles from disk.

A profile is a DIRECTORY, not a Python literal:

    profiles/story-coding/
        profile.toml          tools, workspace driver, flags
        prompt.md             the role — what this kind of executor is
        skills/<name>/SKILL.md

Editing a prompt is editing markdown. Editing the tool surface is editing
one list. Adding a profile is adding a directory. Nothing here needs a
Python change, which is what makes Eb6's proof literal: a diff between two
profiles touches only profile files, because profiles *are* files.

Validation is strict and fails at load, naming the file and the key. A typo
in `profile.toml` must not silently disable a guard — an unknown key is an
error, not something to ignore, and a missing required key names itself.
`tomllib` is stdlib on 3.12 and allows comments, so no dependency is added.
"""

from __future__ import annotations

import logging
import os
import tomllib

from kernel.profiles.record import Profile, UnknownProfileError

logger = logging.getLogger(__name__)

# Repo root / profiles. Overridable so tests can point at a fixture tree
# without writing into the real one.
DEFAULT_PROFILES_DIR = os.environ.get("AGENTOS_PROFILES_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "profiles",
)

BASE_FILE = "base.toml"
PROMPT_FILE = "prompt.md"
CONFIG_FILE = "profile.toml"
SKILLS_DIR = "skills"

_REQUIRED = {"kind", "domain", "workspace_driver"}
_OPTIONAL = {
    # Empty/absent is the work itself. See record.Profile.phase.
    "phase",
    "size",
    "tools_add",
    "tools_deny",
    "prompt_files",
    "allows_completion",
    "requires_plan",
    "has_verifier",
    "max_subagent_depth",
    # THESE WERE READ BY `_build` AND REFUSED BY THIS SET, which is the
    # worst arrangement of the two: `Profile` declares them, `_build`
    # pulls them out of `cfg`, and any profile that actually wrote one
    # failed to load with "unknown key(s): model". So the fields existed,
    # were plumbed, and could not be set by the only thing that sets them.
    #
    # `model` is the one that matters most. `record.py` argues for it —
    # "the cheapest model that can call a tool" for one agent type, a
    # stronger one for another — and both adapters have been built the
    # whole time. `adapter_for_model` resolves provider from model id, so
    # naming `claude-sonnet-5` here is all an agent type needs to run on
    # Anthropic instead of Gemini.
    "model",
    # Ec3d — H2's policy, per agent type. Validated below rather than
    # merely accepted: `requires_approval` reads exactly two keys, so a
    # profile writing `approve_reversable` would set a policy that does
    # nothing, and read as deliberate.
    "effect_policy",
    "leg_tokens",
    "leg_steps",
    "max_legs",
}
_KNOWN = _REQUIRED | _OPTIONAL


class ProfileLoadError(Exception):
    """A profile directory is malformed. Names the file and the problem."""


def _read_config(profile_dir: str) -> dict:
    path = os.path.join(profile_dir, CONFIG_FILE)
    if not os.path.isfile(path):
        raise ProfileLoadError(f"{path}: missing {CONFIG_FILE}")
    try:
        with open(path, "rb") as fh:
            cfg = tomllib.load(fh)
    except tomllib.TOMLDecodeError as e:
        raise ProfileLoadError(f"{path}: invalid TOML — {e}") from None

    missing = _REQUIRED - cfg.keys()
    if missing:
        raise ProfileLoadError(
            f"{path}: missing required key(s): {', '.join(sorted(missing))}"
        )
    policy_problem = _bad_effect_policy(cfg.get("effect_policy"))
    if policy_problem:
        raise ProfileLoadError(f"{path}: {policy_problem}")
    unknown = cfg.keys() - _KNOWN
    if unknown:
        # Strict on purpose: a mistyped `allows_completion` would otherwise
        # leave the default in place and read as deliberate policy.
        raise ProfileLoadError(
            f"{path}: unknown key(s): {', '.join(sorted(unknown))}. "
            f"Known keys: {', '.join(sorted(_KNOWN))}"
        )
    return cfg


# The only two keys `effects.requires_approval` reads. `irreversible` is
# absent on purpose and a profile naming it is refused: it cannot be
# loosened, and silently ignoring the key would let somebody believe they
# had turned the ceiling off — which is worse than either answer.
_POLICY_KEYS = frozenset({"approve_contained", "approve_reversible"})


def _bad_effect_policy(policy) -> str:
    """What is wrong with an `effect_policy` table, or ""."""
    if policy is None:
        return ""
    if not isinstance(policy, dict):
        return (f"effect_policy must be a table of "
                f"{', '.join(sorted(_POLICY_KEYS))}, got "
                f"{type(policy).__name__}")
    unknown = sorted(set(policy) - _POLICY_KEYS)
    if unknown:
        extra = (" `irreversible` can never be loosened, by any profile or "
                 "any setting — that is an early return in "
                 "effects.requires_approval, not a policy lookup."
                 if any("irrevers" in u for u in unknown) else "")
        return (f"effect_policy has unknown key(s): {', '.join(unknown)}. "
                f"Known: {', '.join(sorted(_POLICY_KEYS))}.{extra}")
    wrong = sorted(k for k, v in policy.items() if not isinstance(v, bool))
    if wrong:
        return f"effect_policy value(s) must be true or false: {', '.join(wrong)}"
    return ""


def _read_prompt(profile_dir: str) -> str:
    path = os.path.join(profile_dir, PROMPT_FILE)
    if not os.path.isfile(path):
        raise ProfileLoadError(
            f"{path}: missing {PROMPT_FILE} — a profile with no role "
            "statement would run with whatever the model assumes"
        )
    with open(path, encoding="utf-8") as fh:
        body = fh.read().strip()
    if not body:
        raise ProfileLoadError(f"{path}: empty")
    return body


def _read_base_tools(root: str) -> tuple[str, ...]:
    """The tools every executor gets, from profiles/base.toml."""
    path = os.path.join(root, BASE_FILE)
    if not os.path.isfile(path):
        raise ProfileLoadError(
            f"{path}: missing {BASE_FILE} — every profile composes its "
            "surface from the base set"
        )
    try:
        with open(path, "rb") as fh:
            cfg = tomllib.load(fh)
    except tomllib.TOMLDecodeError as e:
        raise ProfileLoadError(f"{path}: invalid TOML — {e}") from None
    tools = cfg.get("tools")
    if not tools:
        raise ProfileLoadError(f"{path}: no `tools` list")
    return tuple(tools)


def _compose_tools(base: tuple[str, ...], cfg: dict, path: str) -> tuple[str, ...]:
    """(base + tools_add) - tools_deny, order preserved, typos rejected.

    Every mistake here is caught rather than absorbed, because the failure
    mode of a silently-absorbed one is a tool surface that differs from
    what the file appears to say — and this list IS the security boundary
    chain stage 2 enforces.
    """
    add = list(cfg.get("tools_add", ()))
    deny = list(cfg.get("tools_deny", ()))

    dup = [t for t in add if t in base]
    if dup:
        raise ProfileLoadError(
            f"{path}: tools_add re-lists base tool(s): {', '.join(sorted(dup))}. "
            "Remove them — they are already granted by base.toml"
        )
    inert = [t for t in deny if t not in base]
    if inert:
        raise ProfileLoadError(
            f"{path}: tools_deny names non-base tool(s): {', '.join(sorted(inert))}. "
            "A deny that removes nothing is a typo, not a policy"
        )
    both = set(add) & set(deny)
    if both:
        raise ProfileLoadError(
            f"{path}: tool(s) both added and denied: {', '.join(sorted(both))}"
        )

    effective = [t for t in base if t not in deny] + add
    seen = {t for t in effective if effective.count(t) > 1}
    if seen:
        raise ProfileLoadError(
            f"{path}: duplicate tool(s) in effective surface: {', '.join(sorted(seen))}"
        )
    return tuple(effective)


def _skill_dirs(profile_dir: str) -> tuple[str, ...]:
    root = os.path.join(profile_dir, SKILLS_DIR)
    if not os.path.isdir(root):
        return ()
    return (root,)


def load_profile_dir(profile_dir: str, base_tools: tuple[str, ...]) -> Profile:
    """Build one Profile from one directory, over the base tool set."""
    cfg = _read_config(profile_dir)
    cfg_path = os.path.join(profile_dir, CONFIG_FILE)
    try:
        return _build(cfg, cfg_path, profile_dir, base_tools)
    except ValueError as e:
        # Profile.__post_init__ validates field VALUES and raises without
        # knowing which file it came from. Someone editing a profile needs
        # the path, not a bare "unknown workspace_driver 'svn'".
        raise ProfileLoadError(f"{cfg_path}: {e}") from None


def _build(cfg, cfg_path, profile_dir, base_tools) -> Profile:
    return Profile(
        kind=cfg["kind"],
        domain=cfg["domain"],
        phase=cfg.get("phase", "") or "",
        prompt_preamble=_read_prompt(profile_dir),
        model=str(cfg.get("model", "")),
        leg_tokens=int(cfg.get("leg_tokens", 0)),
        leg_steps=int(cfg.get("leg_steps", 0)),
        max_legs=int(cfg.get("max_legs", 0)),
        prompt_files=tuple(cfg.get("prompt_files", ())),
        tools=_compose_tools(base_tools, cfg, cfg_path),
        base_tools=base_tools,
        added_tools=tuple(cfg.get("tools_add", ())),
        denied_tools=tuple(cfg.get("tools_deny", ())),
        workspace_driver=cfg["workspace_driver"],
        size=cfg.get("size", ""),
        allows_completion=cfg.get("allows_completion", True),
        requires_plan=cfg.get("requires_plan", False),
        has_verifier=cfg.get("has_verifier", True),
        effect_policy=cfg.get("effect_policy") or None,
        skill_dirs=_skill_dirs(profile_dir),
        max_subagent_depth=cfg.get("max_subagent_depth", 0),
    )


class ProfileMap(dict):
    """Keyed by `(kind, domain, phase)`, readable by `(kind, domain)`.

    THE KEY GREW A THIRD COMPONENT and roughly twenty call sites index
    this map by two. Every one of them means the work itself — they were
    written before a phase existed — so a two-part lookup resolves the
    empty phase rather than raising. New code should pass all three.

    `__missing__` and not a `get` override: `dict.__getitem__` calls it
    only on a miss, so a real three-part key costs nothing, and the
    fallback cannot shadow a phase that genuinely exists.
    """

    def __missing__(self, key):
        if isinstance(key, tuple) and len(key) == 2:
            return self[(key[0], key[1], "")]
        raise KeyError(key)


def load_all(profiles_dir: str | None = None
             ) -> dict[tuple[str, str, str], Profile]:
    """Load every profile directory. Raises on the first malformed one.

    Loading nothing is an error too: an empty profiles/ directory means
    every execution would fail to resolve, and it should say so once here
    rather than once per dispatch.
    """
    root = profiles_dir or DEFAULT_PROFILES_DIR
    if not os.path.isdir(root):
        raise ProfileLoadError(f"{root}: profiles directory does not exist")

    base_tools = _read_base_tools(root)
    out: ProfileMap = ProfileMap()
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        if not os.path.isdir(d) or name.startswith("."):
            continue
        profile = load_profile_dir(d, base_tools)
        key = (profile.kind, profile.domain, profile.phase)
        if key in out:
            raise ProfileLoadError(
                f"{d}: duplicate profile for kind={key[0]!r} "
                f"domain={key[1]!r} phase={key[2]!r}"
            )
        out[key] = profile

    if not out:
        raise ProfileLoadError(f"{root}: no profiles found")
    logger.info("loaded %d profiles from %s", len(out), root)
    return out
