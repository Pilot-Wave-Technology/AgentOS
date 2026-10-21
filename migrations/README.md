# `migrations/` — the schema, and how it changes

A fresh install applies the nine migrations listed in `SEQUENCE`, in order:

| File | What |
|---|---|
| `0001_baseline_schema.sql` | the whole schema, `pg_dump --schema-only` from a live database |
| `0002_baseline_seed.py` | loads the human-readable [`../seed/`](../seed/) tree — templates, skills, tools, prompts, the run state machine |
| `0003_kernel_roles_and_grants.sql` | the kernel's roles, and what each may touch |
| `0004_execution_pod_login.sql` | the login every execution pod connects as, with exactly its grants |
| `0005_kernel_role_grants.sql` | the full set of grants for the kernel roles |
| `0006_platform_settings_defaults.sql` | the operator switches' default values |
| `0007_model_windows_tags_and_defaults.py` | model context windows, model tags, and the model defaults the pickers read |
| `0008_goal_leg_budget.sql` | a goal's own tokens per leg and max legs, and `executions.leg_budget` they are frozen onto |
| `0009_retire_legacy_templates.sql` | removes the legacy `go`, `default-shca` and IA templates when nothing uses them |

Everything you add afterwards is a new file listed in `SEQUENCE`.

```bash
python migrations/apply.py "$DATABASE_URL"      # or set DATABASE_URL and omit it
```

The runner is asyncpg, not psql. Each migration runs **once**, inside its own
transaction, and is recorded in a `_migrations` table with its sha256, its
duration and who applied it. Cloud Build runs this as a job before every deploy,
so you rarely invoke it by hand.

---

## `SEQUENCE` decides the order, not the filename

Order is an explicit human decision recorded in git, not something inferred from
a sort. Two branches can each add a migration without colliding over a number
prefix, and the merge conflict happens in `SEQUENCE` — where a person has to
look at it — rather than silently resolving into the wrong order.

`check_sequence.sh` validates that the file and the directory agree. It runs in
CI before anything touches a database.

**A migration that has been applied is finished.** Editing it changes nothing:
the runner has already recorded it and will not run it again. Every environment
that applied the old text keeps the old behaviour while the file claims
otherwise — which is a genuinely confusing afternoon. If you need to change
what an applied migration did, add a new one.

---

## Two schemas

| Schema | Holds | Addressed as |
|---|---|---|
| `public` | the control plane: goals, stories, agents, resources, the ledger | bare names |
| `kernel` | executions, legs, the step journal, parks, the run state machine | `kernel.`-qualified from `app/`, bare from `kernel/` |

Code in `kernel/` connects with `search_path=kernel,public`, so it writes bare
names. Code in `app/` does not, and must say `kernel.executions`. Getting this
wrong produces "relation does not exist" against a table that is plainly there.

---

## Writing one

**Expand, then contract.** Add the new column or table, deploy, backfill, move
the readers, and only then drop the old one — in a later migration. A single
migration that renames a column breaks every instance still running the previous
image, which during a rolling deploy is most of them.

`scripts/check_expand_contract.py` fails the build on destructive DDL that has
not been explicitly acknowledged. Acknowledging it is a comment in the file; the
point is that it was a decision rather than an oversight.

**Grants are not in the dump.** `0001` is `--no-owner --no-privileges`
deliberately: machine-generated grants are unreviewable, they re-break on every
regeneration, and they would bake one database's role names into a public
artifact. Roles and grants come from migrations `0003`–`0005` instead, each a
reviewed file in its own right.

---

## Regenerating the baseline

Only maintainers of AgentOS itself need this. The baseline is produced from a
real database that has every migration applied — not from the files — so the
published schema is a thing that demonstrably works rather than a replay nobody
has run. The seed tree is extracted alongside it, one file per template, so a
change to a shipped prompt is reviewable in a pull request.

The test suite builds a database from this baseline and PREPAREs every SQL
literal in the codebase against it. A baseline missing a table fails the build
loudly, which is how it is kept honest. See [`../tests/README.md`](../tests/README.md).

---

## `reset_db.py`

Drops and rebuilds. It exists for development and for the `db-reset.sh`
Cloud Run job. It does exactly what its name says, so read
[`../deploy/10-operations-and-troubleshooting.md`](../deploy/10-operations-and-troubleshooting.md)
before pointing it at anything you care about.
