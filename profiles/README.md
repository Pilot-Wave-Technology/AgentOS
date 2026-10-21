# `profiles/` — what an executor is allowed to be

A profile is **data, not a class**: a TOML file, a prompt, and optionally some
skills. Changing what an executor can do, or who it is, is an edit here. None of
it needs a code change.

```
profiles/
  base.toml                  the 8 tools EVERY executor gets
  story-coding/
    profile.toml             kind, domain, tool surface, budgets
    prompt.md                who it is
    skills/commit-hygiene/   loaded on demand, not always in context
  errand-ops/
  story-business/
  story-coding-review/
```

---

## The four that ship

| Profile | kind / domain / phase | Tools | What it is for |
|---|---|---|---|
| `story-coding` | story / coding | **29** | the full executor — shell, commit, subagents |
| `errand-ops` | errand / ops | **25** | one-off operational work; no verifier, the exit code is the verdict |
| `story-business` | story / business | **18** | documents and analysis; **no shell, no commit** |
| `story-coding-review` | story / coding / **review** | **10** | reads what was written; cannot change it |

A profile is selected by `(kind, domain, phase)`. **There is no default.** An
unknown combination fails at dispatch, before a container exists — because
falling back to the coding profile would hand an unclassified execution `write`
and `bash`.

---

## The tool arithmetic

**effective = (base + `tools_add`) − `tools_deny`**

`base.toml` holds the eight an executor of any kind would be broken without:
`read`, `glob`, `grep`, `list_dir`, `skill`, `ask_human`, `reply_to_human`,
`register_action_template`. Anything a profile could reasonably not want belongs
in that profile instead — `write` is the example, because an errand has no
business editing the workspace.

Stage 2 of the interceptor chain is **fail-closed on exactly this list**. A tool
not named is refused *by name*, so a profile that forgets to add something gets
a clear refusal rather than a silent hole.

Anomalies in the arithmetic are **load errors**, not runtime failures: denying
something that was never added, or naming an unknown tool, stops the profile
loading at all. `./run.sh profiles` prints each resolved surface, so the list a
reviewer needs is never something they assemble in their head.

---

## The review profile is worth reading

It is the same pod, the same workspace, a later phase — and it is the clearest
statement of intent in the tree.

- **Every writing tool is refused**: `write`, `edit`, `bash`, `story_commit`,
  and the whole background-job set. Including `run_check`, because a reviewer
  that can run the suite can report its own measurement as a finding about
  someone else's work.
- **No diff tool, deliberately.** What moved is computed once and handed over in
  the brief, by code that reads trunk from the git graph. A fact in the brief
  cannot be browsed with and cannot guess.
- **It loads `AGENTS.md` but not `SOUL.md`.** The project's conventions are
  context a reviewer needs; the author's personality is not an independent read.
- **`ask_human` is denied but `reply_to_human` is not.** Parking would hold the
  pod and the admission slot waiting for an answer nobody is coming to give.
  Answering a question costs nothing and holds nothing.

---

## Settings a profile can carry

| Key | Means |
|---|---|
| `kind`, `domain`, `phase` | how it is selected |
| `tools_add`, `tools_deny` | the surface |
| `model` | name it rather than inherit, when the model is a property of the role |
| `max_legs`, `leg_tokens`, `leg_steps` | the budget (defaults: 12 legs, 120K context, 200 steps) |
| `allows_completion` | may it claim the story is finished? |
| `requires_plan` | must it register a plan and have it approved? |
| `has_verifier` | are its checks re-run in a clean container? |
| `max_subagent_depth` | may it delegate, and how deep |
| `workspace_driver` | `git` for a composed checkout, `none` for work needing no tree |
| `prompt_files` | which of the project's files it loads |
| `effect_policy` | may it pre-approve contained or reversible calls |

`effect_policy` can be loosened only as far as contained or reversible.
**`irreversible` can never be loosened**, and naming it is a refusal to start.

---

## Adding one

Create a directory, write `profile.toml` and `prompt.md`, and give it a
`(kind, domain)` nothing else claims. Nothing registers it in code.

Two things to decide deliberately: whether it can claim completion, and whether
its checks are verified. An executor that can claim but is not verified is
trusting the model's own account of whether the work is finished — which is fine
for an errand whose terminal command exits zero, and not fine for code that
merges.
