"""Installing through the shell is unrecorded, so it is refused.

THE DIVERGENCE THIS CLOSES. The Verifier reproduces the executor's
environment from `env_deltas` and from nothing else -- that is what
makes a passing check mean something, because a check that passes only
on the machine that did the work has measured the machine. `env_install`
records; the shell does not. Both install.

So the first live story installed FastAPI with
`bash: pip install fastapi uvicorn pytest httpx`, recorded nothing, and
its own test suite then failed verification with
`ModuleNotFoundError: No module named 'fastapi'` -- a refusal that told
the author nothing about their work. The model then wrote a `conftest.py`
that shelled out to pip at test time, which is what a competent agent
does when the environment keeps disappearing underneath it.

The `env_install` description already said, in as many words, "use this
instead of running pip or apt through the shell ... what you install any
other way is not [recorded]". It was ignored, and being ignored is the
argument: a rule that only the prompt enforces is a rule the system does
not have. This one is enforced where the two doors meet.

DELIBERATELY NARROW. It matches installs that MUTATE the environment,
and it is a refusal with an instruction rather than a silent rewrite --
guessing package names out of arbitrary shell (`&&` chains, `-r` files,
`-e .`) and recording the guess would put a wrong environment on the
record, which is worse than none. Everything else the shell can do is
untouched.
"""

from __future__ import annotations

import re

# OPTIONS MAY SIT BETWEEN THE MANAGER AND THE VERB, and requiring them to
# be adjacent is how `pip --quiet install requests` walked through a guard
# written to stop exactly that. Every one of these is ordinary usage:
#
#     pip --quiet install requests
#     pip --cache-dir /tmp install requests
#     apt-get -y install curl
#     npm --silent install
#
# A flag, optionally followed by one non-flag value, repeated. Narrow
# enough that `pip download -d install_dir` still does not match — `\b`
# after `install` fails against the underscore — and that a subcommand
# which is not an install is not swept up.
_OPTS = r"(?:\s+-\S+(?:\s+[^-\s]\S*)?)*"

# `pip`/`pip3`/`python -m pip` install, `apt-get install`, `npm install`
# (bare `npm install` in a package dir counts -- it mutates node_modules
# from package.json, which the Verifier cannot see).
_PATTERNS = (
    (re.compile(r"\b(?:python[0-9.]*\s+-m\s+)?pip[0-9.]*" + _OPTS
                + r"\s+install\b"), "pip"),
    (re.compile(r"\bapt(?:-get)?" + _OPTS + r"\s+install\b"), "apt"),
    (re.compile(r"\bnpm" + _OPTS + r"\s+(?:install|i|ci|add)\b"), "npm"),
    (re.compile(r"\b(?:yarn|pnpm)" + _OPTS + r"\s+(?:install|add)\b"), "npm"),
    (re.compile(r"\bpoetry" + _OPTS + r"\s+(?:install|add)\b"), "pip"),
    (re.compile(r"\buv" + _OPTS + r"\s+(?:pip" + _OPTS + r"\s+)?"
                r"(?:install|add|sync)\b"), "pip"),
)

# A read-only inspection is not an install, whatever it mentions.
# A hyphen is not a word character, so `\b` cannot anchor before `--`:
# `\b--help\b` never matched and `pip install --help` read as an
# install. Anchor on whitespace instead.
_INSPECT = re.compile(
    r"(?:^|\s)(?:--help|-h)(?:\s|$)"
    r"|\bpip[0-9.]*\s+(?:list|show|freeze)\b"
    r"|--version\b")


def install_manager(command: str) -> str:
    """The manager an install command would use, or '' if it is not one.

    KNOWN FALSE POSITIVE, left in deliberately. A command that MENTIONS an
    install inside quotes — `grep -r 'pip install' docs/` — is refused.
    Stripping quoted segments before matching would fix it and open a
    hole the other way: `bash -c "pip install x"` is a real install and
    would then walk straight through. A recoverable refusal on a grep is
    the cheaper of the two, and the model can rephrase.
    """
    text = " ".join(str(command or "").split())
    if not text or _INSPECT.search(text):
        return ""
    for pattern, manager in _PATTERNS:
        if pattern.search(text):
            return manager
    return ""


def refusal(command: str, manager: str, tools=None) -> str:
    """What to tell the executor. Names a tool the caller actually has.

    A DEAD-END CORRECTION IS WORSE THAN NONE. This used to say "call
    `env_setup` to apply it" unconditionally, and the errand profile did
    not hold `env_setup` — so the only route out of the refusal was a tool
    that would be refused by name at chain stage 2. The model is then
    correct to conclude the system is contradicting itself, and what it
    does next is work around the environment, which is how the first live
    story ended up shelling to pip from inside its own `conftest.py`.

    `tools` is the caller's effective surface. `None` means "unknown", and
    unknown offers both routes — the old behaviour, and right for a caller
    that cannot say, because naming too much is a smaller error than
    telling somebody their only option is a tool they do not have.
    """
    have = None if tools is None else set(tools)

    def held(name: str) -> bool:
        return have is None or name in have

    hint = {
        "pip": 'env_install(manager="pip", packages=["fastapi", "uvicorn"])',
        "apt": 'env_install(manager="apt", packages=["curl"])',
        "npm": 'env_install(manager="npm", packages=["typescript"])',
    }[manager]

    routes = []
    if held("env_setup"):
        routes.append(
            "declare the dependency in the mount's manifest "
            "(requirements.txt / package-lock.json) or .agentos/setup.sh, "
            "then call env_setup to apply it")
    if held("env_install"):
        routes.append(f"env_install records a one-off: {hint}")

    if not routes:
        # Neither tool. Say so plainly rather than inventing a route — the
        # honest answer is that this profile cannot install anything, and
        # `ask_human` is the way out.
        return (
            f"E_UNRECORDED_INSTALL: installing through the shell leaves no "
            f"RECORD, so it is refused -- and this profile holds neither "
            f"env_setup nor env_install, so there is no recorded route "
            f"either. Work with what the environment already has, or "
            f"ask_human for the dependency to be added to the mount's "
            f"manifest.")

    extra = ""
    if manager == "pip" and re.search(r"-r\s+\S+", command):
        extra = (" For a requirements file, read it and pass the pinned "
                 "specifiers as `packages` -- what is recorded is what "
                 "gets rebuilt, so naming them is what makes them "
                 "reproducible.")
    # TRUE IN BOTH VERIFICATION MODES, and that is why it says RECORD
    # rather than "what the Verifier can rebuild". Verification is a
    # platform switch that is off by default, so naming the Verifier
    # made this refusal read as a warning about something that is not
    # running -- and a model that believes the reason does not apply
    # argues with the refusal instead of taking the route.
    #
    # The record is what matters either way. A shell install is invisible
    # to `env_deltas`, so the NEXT LEG of this same story rebuilds the
    # tree without it -- the tree is replaced between rounds -- and a
    # Verifier, if one ever runs, rebuilds without it too. One sentence
    # that is true whichever way the switch is set.
    return (
        f"E_UNRECORDED_INSTALL: installing through the shell leaves no "
        f"RECORD of what you installed -- the next leg rebuilds this tree "
        f"without it, and your checks then fail for a reason that is not "
        f"your work. Instead, {'; or '.join(routes)}.{extra}")
