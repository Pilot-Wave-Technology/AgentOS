"""The full test suite of one repository, as a single check line.

WHY A CHECK AND NOT AN INSTRUCTION. A trunk integration is judged by
`run_check`, which runs what the CONTRACT names -- and approving a plan
with checks replaces the contract (`contracts.new_version`). An
instruction to "run the full suite" that is not a check is never run by
the verifier, and the run's own word for it is not evidence.

WHY THE PLATFORM WRITES IT. The integration plan is written before any
executor has looked at the repository, for every repository a story
writes, in any language. So the line detects the suite rather than
naming it, in the order a repository most plausibly declares one:

  package.json with a real `test` script   npm test
  *.test.js / *_test.js / test/*.js        node --test (Node's own runner)
  any test_*.py / *_test.py                python -m pytest
  go.mod                                   go test ./...
  Cargo.toml                               cargo test
  none of those                            passes, and says so

A REPOSITORY WITH NO TESTS PASSES, because "tests may or may not be
present" is the normal case for a young branch, and failing it would
park every integration of an untested repo on a check nobody can make
pass. pytest's exit 5 ("no tests collected") is the same fact said by
the runner, so it passes too. npm's generated placeholder
(`"test": "echo \\"Error: no test specified\\" && exit 1"`) is not a
suite and is treated as none.

THE LINE RUNS FROM THE WORKSPACE ROOT, which is where every check runs,
so it `cd`s into the mount first. Vendored trees are skipped so a
dependency's own tests are not mistaken for this repository's.
"""
from __future__ import annotations

# Generous: a whole suite, not one targeted check. Read off the spec by
# both rooms (`check_run.budget_for`, `verifier/budget.timeout_for`).
SUITE_TIMEOUT_S = 2400

_PRUNE = ("-path ./.venv -prune -o -path ./venv -prune -o "
          "-path ./node_modules -prune -o -path ./.git -prune -o")


def _quote(text: str) -> str:
    return "'" + text.replace("'", "'\"'\"'") + "'"


def suite_command(mount: str) -> str:
    """One POSIX shell line: run `mount`'s suite, or pass if it has none."""
    where = _quote(mount)
    py_tests = (f"find . {_PRUNE} \\( -name 'test_*.py' -o -name "
                f"'*_test.py' \\) -print | grep -q .")
    # BMIF1's frontend: tests run with `node --test` and no npm script,
    # which read as "no test suite" and passed without running anything.
    js_tests = (f"find . {_PRUNE} \\( -name '*.test.js' -o -name "
                f"'*.test.mjs' -o -name '*.test.cjs' -o -name '*_test.js' "
                f"-o -name '*-test.js' -o -path './test/*.js' \\) -print "
                f"| grep -q .")
    return (
        f"cd {where} && "
        "if [ -f package.json ] "
        "&& grep -q '\"test\"[[:space:]]*:' package.json "
        "&& ! grep -q 'no test specified' package.json; then npm test; "
        f"elif {js_tests}; then node --test; "
        f"elif {py_tests}; then "
        "python -m pytest -q; rc=$?; [ \"$rc\" -eq 5 ] && rc=0; exit $rc; "
        "elif [ -f go.mod ]; then go test ./...; "
        "elif [ -f Cargo.toml ]; then cargo test; "
        f"else echo \"no test suite in {mount} -- nothing to run\"; fi")


def marker_check(mount: str) -> dict:
    """No merge-conflict marker left in any tracked file of `mount`.

    WHY A CHECK OF ITS OWN. On BMIF1 run 3 a README conflict was
    "resolved" by an edit that never landed (the file was not writable),
    `story_commit` committed the markers, and every test still passed --
    a README is not code -- so they were merged to trunk. Only the two
    marker lines that cannot be anything else are matched: `=======` on
    its own is an ordinary Markdown underline. `git grep` searches
    tracked files only, so vendored trees are not judged.
    """
    return {"run": (f"cd {_quote(mount)} && ! git grep -n -E "
                    "'^(<<<<<<< |>>>>>>> )' -- ."),
            "label": "guard"}


def suite_check(mount: str) -> dict:
    """The check a trunk-integration plan carries for one merged mount."""
    return {"run": suite_command(mount), "label": "guard",
            "timeout_s": SUITE_TIMEOUT_S}
