# `scripts/` — provisioning, checking, and operating

Three kinds of thing live here, and they are not interchangeable.

| | |
|---|---|
| `infra/` | **Provisioning.** Creates clusters, identities, databases and the execution plane. Run from the repo root via `../execution-plane.sh`. |
| `check_*.{py,sh}` | **Build guards.** Run in CI, before anything is deployed. Each one refuses rather than warns. |
| the rest | **Operations.** Day-two things: smoke tests, rollback, audits. |

Most of the time you do not call anything here directly. The two entry points at
the repo root — [`../preflight.sh`](../preflight.sh) and
[`../execution-plane.sh`](../execution-plane.sh) — wrap these with the settings
a single-environment install needs, and getting those settings wrong is not
harmless. See the note at the end.

---

## The build guards

Four run in Cloud Build before the image is deployed:

| Script | Refuses |
|---|---|
| `../migrations/check_sequence.sh` | a `SEQUENCE` file and a migrations directory that disagree (it lives beside what it checks) |
| `check_expand_contract.py` | destructive DDL in a migration that has not been explicitly acknowledged |
| `check_ledger_actors.sh` | a hardcoded `'system'` actor id, which is how provenance quietly becomes useless |
| `check_unbound_names.py` | a name used before it is defined — a whole class of import-time crash |

A fifth, `check_infra_scope.py`, is **not** in the build. It is exercised by the
test suite and matters only to a multi-environment install, where it refuses an
infra build whose target names belong to **another environment**. Every
`gcloud` command takes a name, every name comes from a substitution, and a
substitution is one edit away from naming production. The script refuses before
the first command runs, on two rules: every target must be scoped to this
environment, and no target may be scoped to a *different* known one —
for a `staging` build, `agentos-prod-staging` contains "staging" but also names
production, and a check that only looked for the right word would pass it.

It will refuse an environment it has not been told about. Declare yours:

```bash
export AGENTOS_BUILDABLE_ENVS="myenv"
```

---

## `infra/`

| | |
|---|---|
| `names.sh` | **every name the execution plane is addressed by, in one place.** Sourced by everything else here. |
| `cluster_objects.sh` | the namespace, quota, service accounts and default-deny policy inside the cluster |
| `deploy_execution_plane.sh` | the conductor, the verifier, the egress proxy |
| `env_preflight.sh` + `preflight/` | is all of the above actually there? |
| `render.py` | substitutes variables into a manifest, and **fails on an unset one** |
| `gke_standard_pools.sh`, `gke_cluster.sh`, `db_bootstrap.sh` | **multi-environment installs only**: the cluster and its node pools; identities, custom roles and the artifact bucket; the execution pod's database role and grants |

A single install does not run the last three. Terraform creates the cluster,
the identities and the bucket, and migrations `0003`–`0005` create the database
roles and grants; `../execution-plane.sh` runs `cluster_objects.sh` and
`deploy_execution_plane.sh` against what terraform made.

`names.sh` exists because one environment's name (say `agentos-staging`) was
once spelled in two scripts and the artifact bucket in two more. The failure that produced is quiet and specific:
the conductor writes a tree to one bucket and the verifier reads from another,
so every check fails to start and the Job reports only that its init container
exited 1. Nothing is misconfigured in a way a person can see — two files simply
disagree.

`render.py` rather than `envsubst` for the same reason in miniature: `envsubst`
renders an unset variable as the empty string, and a manifest naming nothing is
applied successfully and then does nothing.

---

## Operations

| | |
|---|---|
| `smoke.sh` | is the deployed service answering? |
| `rollback.sh` | put the previous image back |
| `prune-revisions.sh` | old Cloud Run revisions — and they are not inert: a stale revision can keep running background loops, so a green build does not mean the old logic stopped |
| `podcheck.py` | why is this execution pod not running? |
| `egress_audit.py` | what did the egress proxy allow? |
| `council_run.py` | start a review council by hand |
| `sync-service-url.sh` | tell the gateway where the control plane is |

---

## One warning about running these directly

Everything in `infra/` derives its names from `names.sh`, which has two shapes:
with `AGENTOS_SINGLE_ENV` set it asks for `database-url` and `control_panel`,
and without it asks for `<env>-database-url` and `control_panel_<env>`.

A single-environment install that runs these bare will provision a second set of
resources under suffixed names, beside the ones it already has, and the preflight
will go on reporting exactly the same findings. That is why
`../execution-plane.sh` exists and why it sets the flag for you.
