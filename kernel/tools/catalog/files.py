"""Reading and changing the tree — the base surface.

These ten are what an executor spends almost every step on, and
all of them run sandbox-side: nothing here needs a credential, a
database or the profile, so nothing here justifies crossing back
into the worker.
"""
from __future__ import annotations

from kernel.tools.catalog.spec import _B, _S, _schema

TOOLS: dict[str, dict] = {
    # ── Orientation — base surface ───────────────────────────────────────
    "read": _schema(
        "read",
        "Read a file, or a window of one. Prefer a window: pass `offset` "
        "and `limit` rather than pulling a whole large file into context "
        "to find one function. The result says how many lines exist and "
        "how many were returned, so you always know what you have not "
        "seen.",
        {"path": {"type": _S, "description": "File to read."},
         "offset": {"type": "integer",
                    "description": "First line to return, 0-based. "
                                   "Default 0."},
         "limit": {"type": "integer",
                   "description": "How many lines to return. Default "
                                  "2000."}},
        ["path"],
        side="sandbox", effect="contained",
    ),
    "glob": _schema(
        "glob",
        "Find files by name pattern. Scope it with `path` when you know "
        "roughly where to look — an unscoped search across a large tree "
        "costs more and answers less.",
        {"pattern": {"type": _S,
                     "description": "e.g. `**/*.py`, `test_*.py`."},
         "path": {"type": _S,
                  "description": "Directory to search under, relative to "
                                 "the workspace. Default: the whole "
                                 "workspace."}},
        ["pattern"],
        side="sandbox", effect="contained",
    ),
    "grep": _schema(
        "grep", "Search file contents by regular expression.",
        {"pattern": {"type": _S, "description": "Regular expression."},
         "path": {"type": _S, "description": "Directory or file to search."}},
        ["pattern"],
        side="sandbox", effect="contained",
    ),
    "list_dir": _schema(
        "list_dir",
        "List one directory. Use it when your idea of the layout might be "
        "wrong: `glob` answers 'where is the thing I can name', and this "
        "answers 'what is actually here'. Directories are marked with a "
        "trailing slash.",
        {"path": {"type": _S,
                  "description": "Directory, relative to the workspace. "
                                 "Default: the workspace root."}},
        [],
        side="sandbox", effect="contained",
    ),
    "skill": _schema(
        "skill", "Load one skill's full instructions by name. The prompt "
                 "lists available skills with one-line descriptions.",
        {"name": {"type": _S, "description": "Skill name as listed."}},
        ["name"],
        side="worker", effect="contained",
    ),
    "reply_to_human": _schema(
        "reply_to_human",
        "Answer a question a person asked you, which arrives in your "
        "messages as 'A person has asked you a question'. Answering "
        "changes nothing: no plan, no file, no job. It costs one step "
        "and then you carry on exactly where you were -- including "
        "waiting again on a background job, which never stopped. "
        "Answer from what YOU know: what you tried, what you rejected "
        "and why, what you are about to do. That is the whole reason "
        "the question came to you rather than being read off the "
        "record. If you genuinely do not know, say so plainly -- a "
        "guess presented as knowledge is worse than 'I have not "
        "looked at that yet'.",
        {"answer": {"type": _S,
                    "description": "Your answer, in plain words, for a "
                                   "person who is not reading your "
                                   "transcript."},
         "request_id": {"type": "integer",
                        "description": "The id given in the question "
                                       "message. Omit it only if the "
                                       "message carried none."}},
        ["answer"],
        side="worker", effect="contained",
    ),
    "ask_human": _schema(
        "ask_human",
        "Stop and ask a human. Use when blocked on something you cannot "
        "determine from the workspace: a missing credential or resource, an "
        "ambiguous requirement, or a decision that is not yours. The run "
        "parks and costs nothing while it waits. The ask is evidence, not "
        "a claim: you must show at least three DISTINCT things you already "
        "tried, and the answer you will assume if nobody replies.",
        {"question": {"type": _S, "description": "One specific question."},
         "attempts": {"type": "array", "items": {"type": _S},
                      "description": "At least three DISTINCT tool calls you "
                                     "already made, each as you made it "
                                     "(e.g. \"grep 'class Counter' src\"). "
                                     "The same failing call repeated is a "
                                     "loop, not evidence."},
         "proposed": {"type": _S, "description": "Your best answer if nobody "
                                                 "replies. Turns the human's "
                                                 "job into yes / no / "
                                                 "actually-this."},
         "blocking_on": {"type": _S, "description": "What this question "
                                                    "blocks. A question that "
                                                    "blocks nothing should "
                                                    "not stop the run."},
         "reversible": {"type": _B, "description": "Whether proceeding on "
                                                   "your proposed default "
                                                   "could be undone."},
         "context": {"type": _S, "description": "Anything else the human "
                                                "needs to decide."}},
        ["question", "attempts", "proposed", "blocking_on"],
        side="worker", effect="contained",
    ),

    "env_setup": _schema(
        "env_setup",
        "(Re)apply the workspace's committed environment: each mount's "
        ".agentos/setup.sh, or its manifest -- requirements.txt, "
        "pyproject.toml, uv.lock, poetry.lock, Pipfile.lock, "
        "package-lock.json, pnpm-lock.yaml, yarn.lock, package.json, "
        "Cargo.lock or Cargo.toml. It installs the project's DEV "
        "dependencies too -- a pyproject's dev/test extras, a "
        "package.json's devDependencies -- because your checks are "
        "usually tests and tests need test tooling. The report names "
        "the exact command it ran, so read that if the environment is "
        "not what you expected. "
        "Call it after editing a manifest. This is THE way to get "
        "dependencies, because it is the only one that is RECORDED: the "
        "tree is rebuilt from those same committed files between "
        "rounds, so anything you installed another way is gone by your "
        "next leg. Installs into the tree (.venv, node_modules) "
        "-- run tools via their in-tree path, e.g. .venv/bin/pytest.",
        {},
        [],
        side="worker", effect="reversible",
    ),

    "env_install": _schema(
        "env_install",
        "Install a package the work needs. Use this instead of running "
        "pip through the shell: what you install here is RECORDED and "
        "rebuilt with the tree, and what you install any other way is "
        "not — the tree is replaced between rounds, so your next leg "
        "runs without it and your checks fail for a reason that is not "
        "your work. NOT FOR SYSTEM PACKAGES: apt needs root and this "
        "sandbox is non-root by design, so it is refused. A system "
        "package belongs in the goal's base image — ask_human to have "
        "it added.",
        {"manager": {"type": _S, "enum": ["pip", "npm"],
                     "description": "Which package manager. apt is not "
                                    "offered — see above."},
         "packages": {"type": "array", "items": {"type": _S},
                      "description": "Package specifiers, e.g. "
                                     "[\"pytest==8.3.4\"]. Pin where the "
                                     "version matters: what is recorded "
                                     "is what gets rebuilt."}},
        ["manager", "packages"],
        side="worker", effect="reversible",
    ),

    # ── Editing ──────────────────────────────────────────────────────────
    "write": _schema(
        "write", "Write a file, creating or replacing it.",
        {"path": {"type": _S}, "content": {"type": _S}},
        ["path", "content"],
        side="sandbox", effect="contained",
    ),
    "edit": _schema(
        "edit",
        "Replace an exact string in a file. `old_string` must match "
        "EXACTLY ONCE or the edit is refused — an edit tool that changes "
        "the first of three occurrences corrupts a file you cannot see it "
        "corrupted. Include surrounding lines to make a match unique.",
        {"path": {"type": _S, "description": "File to edit."},
         "old_string": {"type": _S,
                        "description": "Exact text to replace, whitespace "
                                       "and indentation included."},
         "new_string": {"type": _S, "description": "What to put there."},
         "replace_all": {
             "type": "boolean",
             "description": "Replace EVERY occurrence instead of refusing "
                            "an ambiguous match. For a rename, this is the "
                            "right answer: renaming two of three by making "
                            "three careful edits is the same corruption "
                            "arriving by a safer-looking road."}},
        ["path", "old_string", "new_string"],
        side="sandbox", effect="contained",
    ),
}
