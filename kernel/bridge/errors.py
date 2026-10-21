"""G4 — one error vocabulary for every tool.

Today every tool failure is `{"error": "<a sentence>", "exit_code": 1}`.
`tool_edit` alone has six distinct failures — missing argument, identical
strings, no such file, unreadable, no match, ambiguous match — and all
six come back the same shape. So:

  * the model cannot branch. "Retrying is pointless" and "retry and it
    will work" are the same sentence, so the sensible strategy is to try
    again, which costs a leg.
  * the harness cannot count. `silent-wrong-edit rate` needs a wrong
    edit to be distinguishable from a right one, and the near-miss that
    would otherwise BE a silent wrong edit — an `old_string` matching
    three places — is indistinguishable from a typo in a path.
  * the brief cannot resume differently after a policy refusal than
    after a transient failure, because it cannot tell them apart.

THE PROJECT ALREADY DECIDED THIS TWICE and never applied it here. F2b's
refusals are typed codes with locators and never prose; the mailbox
refuses with `E_ASK_PREMATURE`, `E_EMPTY_PLAN`, `E_NOT_YET_STUCK`. The
convention exists. The tool surface is the last place still writing
sentences.

`retryable` IS THE POINT. Every other field is description; that one is
the decision. It is deliberately a property of the CODE and not of the
call site — a tool author who has to decide it per raise will get it
wrong under pressure, and two tools will answer differently for the same
condition.

ONE VOCABULARY, NOT ONE PER TOOL. `E_PATH_NOT_FOUND` means the same
thing to `read`, `edit`, `ls` and `grep`, and a model that learned it
once should not have to learn it again. A tool adds a code only where
the condition has no analogue elsewhere.

Stdlib only, and flat: `kernel/bridge/` is what gets mounted into the
sandbox and its modules import each other by bare name.
"""


class ToolError:
    """One failure kind: what it is, whether retrying helps, what to do."""

    __slots__ = ("code", "retryable", "meaning", "guidance")

    def __init__(self, code, retryable, meaning, guidance):
        self.code = code
        self.retryable = retryable
        self.meaning = meaning
        self.guidance = guidance


def _e(code, retryable, meaning, guidance):
    return ToolError(code, retryable, meaning, guidance)


# ── The closed set ────────────────────────────────────────────────────
# Grouped by what a reader needs to know, not by which tool raises them.
CODES = {

    # Arguments. Always the caller's mistake, never worth repeating.
    "E_BAD_ARGUMENT": _e(
        "E_BAD_ARGUMENT", False,
        "an argument was missing, empty or the wrong type",
        "read the tool's schema and call it again with the argument it "
        "names. Repeating the same call will fail the same way."),

    # Paths. The commonest family, and the one where retrying is most
    # tempting and least useful.
    "E_PATH_NOT_FOUND": _e(
        "E_PATH_NOT_FOUND", False,
        "no file or directory at that path",
        "do not guess another path. Use `glob` or `ls` to find out what "
        "is actually there — a path you inferred from a name is the "
        "commonest way to spend three calls learning nothing."),
    "E_IS_A_DIRECTORY": _e(
        "E_IS_A_DIRECTORY", False,
        "the path is a directory and this tool takes a file",
        "use `ls` to see what is in it."),
    "E_PERMISSION": _e(
        "E_PERMISSION", False,
        "the filesystem refused access",
        "this will not change by trying again. If you need this file, "
        "say so with ask_human."),
    "E_OUTSIDE_SCOPE": _e(
        "E_OUTSIDE_SCOPE", False,
        "the write was refused by the execution envelope",
        "this is POLICY, not a mistake you can correct by rephrasing. "
        "The path is outside what this run may write. Work inside the "
        "workspace, or ask a human to widen it."),

    # Editing. E_MATCH_AMBIGUOUS is the one that matters most: it is the
    # near-miss that would otherwise be a silent wrong edit.
    "E_MATCH_NOT_FOUND": _e(
        "E_MATCH_NOT_FOUND", False,
        "old_string does not appear in the file",
        "re-READ the region before editing again. Either the file is "
        "not what you remember, or your copy of the text differs in "
        "whitespace — indentation is the usual culprit."),
    "E_MATCH_AMBIGUOUS": _e(
        "E_MATCH_AMBIGUOUS", False,
        "old_string appears more than once, so the edit is undecidable",
        "include surrounding lines until the match is unique — or, if "
        "you genuinely mean every occurrence, pass replace_all."),
    "E_NO_CHANGE": _e(
        "E_NO_CHANGE", False,
        "old_string and new_string are identical",
        "nothing was written because nothing would have changed."),

    # Size. Never a failure of the work.
    "E_TOO_LARGE": _e(
        "E_TOO_LARGE", False,
        "the result exceeds the bound this tool will return",
        "narrow it: `read` takes offset and limit, `grep` and `glob` "
        "take a path to search under. Reading a whole large file to "
        "find one function is the cost this bound exists to make "
        "visible."),

    # Transient. THE ONLY FAMILY WHERE RETRYING IS THE RIGHT ANSWER, and
    # it is deliberately small — a code that is retryable when it should
    # not be sends a model round a loop it cannot leave.
    "E_TRANSIENT": _e(
        "E_TRANSIENT", True,
        "something failed in a way that may not fail again",
        "retry once. If it fails the same way twice it is not "
        "transient — say so rather than continuing."),
    "E_TIMEOUT": _e(
        "E_TIMEOUT", True,
        "the operation did not finish in the time allowed",
        "retry with less work: a narrower search, a smaller read, or a "
        "command that does one thing."),

    # The bridge itself.
    "E_UNKNOWN_TOOL": _e(
        "E_UNKNOWN_TOOL", False,
        "no tool by that name on this surface",
        "the surface is decided by the profile. Use one of the tools "
        "you were given."),
    "E_TOOL_CRASHED": _e(
        "E_TOOL_CRASHED", False,
        "the tool raised instead of returning",
        "a defect in the harness, not in what you asked for. Report it "
        "with ask_human rather than working around it."),
}


def fail(code, detail="", exit_code=1, **extra):
    """A typed failure, in the shape every bridge tool returns.

    `error` stays a sentence because that is what reaches the model's
    transcript, and it now carries the guidance rather than each call
    site inventing its own. `error_code` and `retryable` are what the
    HARNESS reads.
    """
    known = CODES.get(code)
    if known is None:
        # An unknown code is a bug in the caller, and silently passing
        # it through would let the vocabulary drift back open.
        known = CODES["E_TOOL_CRASHED"]
        detail = f"unknown error code {code!r}; {detail}".strip()
    message = detail.strip() or known.meaning
    out = {
        "output": "",
        "error": f"[{known.code}] {message} — {known.guidance}",
        "error_code": known.code,
        "retryable": known.retryable,
        "exit_code": exit_code,
    }
    out.update(extra)
    return out


def ok(output="", **extra):
    """The successful shape, so both paths are written the same way."""
    out = {"output": output, "error": None, "error_code": None,
           "retryable": False, "exit_code": 0}
    out.update(extra)
    return out


def from_oserror(exc, path=""):
    """Map an OSError onto the vocabulary. One place, not per tool."""
    import errno

    number = getattr(exc, "errno", None)
    if number == errno.ENOENT:
        return fail("E_PATH_NOT_FOUND", f"no such file: {path or exc}")
    if number == errno.EISDIR:
        return fail("E_IS_A_DIRECTORY", f"{path or exc} is a directory")
    if number in (errno.EACCES, errno.EPERM):
        return fail("E_PERMISSION", f"permission denied: {path or exc}")
    return fail("E_TRANSIENT", f"{type(exc).__name__}: {exc}")
