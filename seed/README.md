# `seed/` — what a fresh install comes up with

Every file here is loaded into the database once, by
[`../migrations/0002_baseline_seed.py`](../migrations/0002_baseline_seed.py), so
that a new installation has working agents the first time somebody logs in
rather than an empty dropdown.

**The file tree is the source of truth.** Editing a file here changes what ships.
The loader just ingests whatever it finds, and every insert is
`ON CONFLICT DO NOTHING` — so it is safe to re-run, and it will never overwrite
something you changed in the running system.

| | Count | What |
|---|---|---|
| `templates/` | 32 | agent templates — one directory each, holding `meta.json` and the prompt files |
| `skills/` | 9 | skills an agent can load on demand, as Markdown with a small frontmatter header |
| `tools/tools.json` | 69 | the tool registry: name, label, group, tags |
| `tools/tool_profiles.json` | 6 | named bundles of tools that templates reference |
| `memory_prompts/` | 18 | the prompts behind memory flush, compaction and story-complete |
| `claude_phases/` | 6 | the phase registry for coding-story DAGs |
| `model_pricing.json` | 22 | per-model input/output/cache rates, for cost accounting |
| `actors.json` | 2 | the system actors the ledger attributes its own writes to |
| `run_state_machine.json` | 5 versions | **the run state machine itself** |

---

## `run_state_machine.json` is not optional

Eight or more queries in the conductor and the assistant join against this
table. An INNER JOIN with no matching row returns **zero rows** rather than
failing — so an installation missing these reports no executions, no parks and
no transitions, and looks *idle* rather than broken.

All five versions ship. Every execution records which version it ran under, so
a run that started under version 4 keeps being judged by version 4's rules
after the platform moves on. Version 5 is current: ten states, twenty-five
transitions.

---

## Why it is files and not a `.sql` dump

A dump of these tables would be one unreviewable blob. As files, a change to a
shipped prompt shows up in a pull request as a diff of that prompt — which is
precisely how "21 of 30 templates are pinned to a retired model" became visible
rather than staying a column value nobody reads.

---

## Changing what ships

Edit the files, then apply the seed migration to a fresh database to check it
loads. For templates, `meta.json` carries the model, the tool allow/deny lists,
the context budget and the tool profiles; the sibling files are the prompt
itself.

Two constraints the loader will enforce for you:

- A tool with `source` other than `core` or `platform` is skipped. Tools created
  at runtime by a bootstrap are not baseline content, and seeding them without a
  tier would violate a check constraint.
- Native tools must be marked core. The database says so, not the loader.

If you are changing a *model* id, check that it exists in the catalogue
(`app/llm_models.py`) first. A template pinned to a model the registry no longer
offers produces an agent that looks configured and fails on its first turn.
