"""Ec3a — which shell commands cannot be undone.

WHAT WAS THERE, and why it was not enough. Sixteen substrings matched
against a lowercased command: `rm -rf /`, `mkfs`, `git push`, `drop table`
and a dozen more. It caught the classic ways to destroy a machine and
missed every way to destroy an ACCOUNT. `terraform apply`, `aws s3 rm`,
`gcloud … delete`, `helm uninstall`, `gh pr merge` all classified
`reversible` and ran silently — and those are precisely the commands an
errand with a cloud credential reaches for.

WHY NOT JUST A LONGER LIST OF SUBSTRINGS. Because the module this serves
is right that an over-eager list gets switched off:

    Narrow on purpose: an over-eager list turns every run into an approval
    queue and gets switched off, which is worse than not having one.

A substring cannot tell `terraform apply` from `echo "terraform apply"`, or
`truncate table t` from `truncate --help`, and it cannot see
`ls && terraform apply` as two commands. Every false positive spends a
person's attention on nothing, and the third one teaches everybody to
approve without reading. So the matching is done on TOKENS, per simple
command, after the wrappers are stripped.

WHAT IT STILL CANNOT SEE, stated rather than papered over. A command
inside `bash -c "…"`, a script invoked by name, an alias, a here-doc piped
to `psql`. This is a speed bump on the obvious road, not a sandbox — the
sandbox is the container, and Ec4 is where egress and credentials get
their own answer. Claiming more than that would be worse than claiming
less, because somebody would rely on it.
"""

from __future__ import annotations

import re

# Splits a command line into simple commands. `ls && terraform apply` is
# two, and matching the whole string would miss the second.
_SEPARATORS = re.compile(r"&&|\|\||;|\||\n")

# Prefixes that wrap a command without being one. `sudo terraform apply`
# is a terraform apply. `FOO=bar` is an assignment, not a verb.
_WRAPPERS = {"sudo", "time", "nohup", "exec", "command", "env", "nice",
             "ionice", "stdbuf", "xargs", "then", "do", "else"}
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# A flag, so `terraform -chdir=x apply` and `aws --profile p s3 rm` find
# their subcommand. Values that follow a space-separated flag are not
# skipped: doing so would let `aws --profile rm` read as a deletion.
_FLAG = re.compile(r"^-")

# ── WHAT CANNOT BE UNDONE, by what it reaches ────────────────────────────
#
# Each entry is a token SEQUENCE that must appear in order (not
# necessarily adjacent) within one simple command, after wrappers and
# flags are dropped. `("aws", "s3", "rm")` matches `aws --profile p s3 rm
# s3://b/k` and not `echo aws s3 rm`.
#
# `None` as the last element means "any token here", for the shape
# `gcloud <service> delete`.
_ANY = None

_RULES: tuple[tuple[tuple, str], ...] = (
    # The machine. Kept from the original list.
    (("rm", "-rf", "/"), "deletes the filesystem root"),
    (("rm", "-fr", "/"), "deletes the filesystem root"),
    (("mkfs",), "formats a filesystem"),
    (("dd", "if="), "writes raw blocks"),
    ((":(){",), "a fork bomb"),
    (("shutdown",), "stops the machine"),
    (("reboot",), "restarts the machine"),

    # Infrastructure as code. `apply` is included deliberately: it is
    # the command that CREATES billable, externally visible resources,
    # and "you can destroy them afterwards" is not the same as undo.
    (("terraform", "apply"), "changes real infrastructure"),
    (("terraform", "destroy"), "destroys real infrastructure"),
    (("tofu", "apply"), "changes real infrastructure"),
    (("tofu", "destroy"), "destroys real infrastructure"),
    (("pulumi", "up"), "changes real infrastructure"),
    (("pulumi", "destroy"), "destroys real infrastructure"),

    # Kubernetes and Helm.
    (("kubectl", "delete"), "deletes cluster objects"),
    (("kubectl", "drain"), "evicts a node's workloads"),
    (("kubectl", "apply"), "changes a live cluster"),
    (("helm", "uninstall"), "removes a release"),
    (("helm", "delete"), "removes a release"),
    (("helm", "rollback"), "changes a live release"),
    (("helm", "upgrade"), "changes a live release"),

    # AWS. `delete-*` and `terminate-*` are the whole family, so they are
    # matched by prefix below rather than enumerated here.
    (("aws", "s3", "rm"), "deletes objects from a bucket"),
    (("aws", "s3", "rb"), "removes a bucket"),
    (("aws", "s3", "sync", "--delete"), "deletes what the source lacks"),

    # Google Cloud.
    (("gcloud", _ANY, "delete"), "deletes a cloud resource"),
    (("gcloud", _ANY, _ANY, "delete"), "deletes a cloud resource"),
    (("gsutil", "rm"), "deletes objects from a bucket"),
    (("bq", "rm"), "deletes a dataset or table"),

    # Azure.
    (("az", _ANY, "delete"), "deletes a cloud resource"),
    (("az", _ANY, _ANY, "delete"), "deletes a cloud resource"),

    # (SQL is not here — it arrives as an ARGUMENT to a client, never as
    #  the first token, so it is matched separately below.)

    # Git and GitHub. A push is the boundary between local and shared.
    (("git", "push"), "publishes commits"),
    (("gh", "pr", "merge"), "merges a pull request"),
    (("gh", "release", "delete"), "deletes a release"),
    (("gh", "repo", "delete"), "deletes a repository"),

    # Containers and registries.
    (("docker", "rm"), "removes containers"),
    (("docker", "rmi"), "removes images"),
    (("docker", "push"), "publishes an image"),
    (("docker", "system", "prune"), "removes unused objects"),

    # Publishing a package cannot be taken back — most registries refuse
    # to re-use a version even after a yank.
    (("npm", "publish"), "publishes a package version"),
    (("twine", "upload"), "publishes a package version"),
    (("cargo", "publish"), "publishes a crate version"),

    # Anything that sends. The original had two curl spellings; this is
    # the family, and `-X` with a mutating method is the signal.
    (("sendmail",), "sends mail"),
    (("mailx",), "sends mail"),
)

# `aws <service> <verb>` where the verb destroys. Prefix-matched because
# the family is large and grows: delete-bucket, delete-db-instance,
# terminate-instances, deregister-*, remove-*.
_AWS_VERBS = ("delete-", "terminate-", "deregister-", "remove-",
              "disassociate-", "revoke-")

# HTTP methods that change something on the other end.
_MUTATING = {"post", "put", "delete", "patch"}

# DML is matched only INSIDE a database client, and that restriction is
# the whole reason it can be matched at all. `psql -c "drop table users"`
# is a dropped table; `grep -r "drop table" .` is a search, and a rule
# that fires on both is the false positive that gets the list ignored.
# Scoping to the client keeps the phrase meaningful.
_SQL_CLIENTS = {"psql", "mysql", "mariadb", "sqlite3", "sqlcmd", "mongosh",
                "clickhouse-client", "cqlsh", "duckdb", "usql", "pgcli",
                "mycli", "snowsql", "bq"}

_SQL_DML = (
    (r"\bdrop\s+table\b", "drops a table"),
    (r"\bdrop\s+database\b", "drops a database"),
    (r"\bdrop\s+schema\b", "drops a schema"),
    (r"\bdrop\s+index\b", "drops an index"),
    (r"\btruncate\s+(table\s+)?\w", "empties a table"),
    (r"\bdelete\s+from\b", "deletes rows"),
    # An UPDATE without a WHERE is the classic accident. With one it is
    # ordinary work, so the pattern needs the absence — checked in code
    # rather than in a regex, which cannot express it readably.
)

_UPDATE = re.compile(r"\bupdate\s+[\w.\"`]+\s+set\b")
_WHERE = re.compile(r"\bwhere\b")


def _simple_commands(command: str) -> list[list[str]]:
    """Tokenised simple commands, wrappers and assignments stripped."""
    out = []
    for part in _SEPARATORS.split(str(command or "").lower()):
        tokens = part.split()
        while tokens and (tokens[0] in _WRAPPERS
                          or _ASSIGNMENT.match(tokens[0])):
            tokens.pop(0)
        if tokens:
            out.append(tokens)
    return out


def _matches(rule: tuple, tokens: list[str]) -> bool:
    """Do the rule's tokens appear in order, ignoring flags between them?

    The FIRST element must be the command itself. Without that,
    `echo terraform apply` matches — and a rule that fires on prose is the
    false positive that gets the whole list switched off.
    """
    if not tokens or rule[0] is _ANY or not tokens[0].startswith(rule[0]):
        return False
    remaining = list(rule[1:])
    for token in tokens[1:]:
        if not remaining:
            return True
        if _FLAG.match(token) and remaining[0] is not _ANY:
            # A flag is not a subcommand, unless the rule names one.
            if not token.startswith(remaining[0]):
                continue
        want = remaining[0]
        if want is _ANY or token.startswith(want):
            remaining.pop(0)
    return not remaining


def _aws_family(tokens: list[str]) -> bool:
    return (tokens and tokens[0] == "aws"
            and any(t.startswith(_AWS_VERBS) for t in tokens[1:]))


def _mutating_request(tokens: list[str]) -> bool:
    """`curl -X POST`, `curl --request DELETE`, `wget --post-data`."""
    if not tokens or tokens[0] not in ("curl", "wget", "http", "httpie"):
        return False
    for i, token in enumerate(tokens):
        if token in ("-x", "--request") and i + 1 < len(tokens):
            if tokens[i + 1].strip("'\"") in _MUTATING:
                return True
        if token.startswith("--request="):
            if token.split("=", 1)[1].strip("'\"") in _MUTATING:
                return True
        if token.startswith("--post"):
            return True
    return False


def _sql_dml(tokens: list[str], raw: str) -> str:
    """DML inside a database client. Matched on the RAW text, not tokens.

    SQL is whitespace-formatted and quoted; `drop    table` and
    `drop\\ntable` are one statement and three tokens apart. The client
    check above is what makes searching the raw text safe.
    """
    if not tokens or tokens[0] not in _SQL_CLIENTS:
        return ""
    for pattern, why in _SQL_DML:
        if re.search(pattern, raw):
            return why
    if _UPDATE.search(raw) and not _WHERE.search(raw):
        return "updates every row in a table"
    return ""


def reason(command: str) -> str:
    """Why this command is irreversible, or "" if it is not.

    The REASON, not just a boolean, because it goes on the card a person
    reads. "this needs approval" tells them nothing they can decide with;
    "deletes objects from a bucket" does.
    """
    lowered = str(command or "").lower()
    for tokens in _simple_commands(command):
        for rule, why in _RULES:
            if _matches(rule, tokens):
                return why
        if _aws_family(tokens):
            return "deletes or terminates a cloud resource"
        if _mutating_request(tokens):
            return "sends a request that changes something remote"
        sql = _sql_dml(tokens, lowered)
        if sql:
            return sql
    return ""


def is_destructive(command: str) -> bool:
    return bool(reason(command))
