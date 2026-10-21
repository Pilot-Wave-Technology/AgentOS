# `task_runner/` — the Cloud Run Job for background work

A small, separate image. It is fired with no arguments whenever background work
*may* be pending; on each run it polls every registered handler in order and
runs the ones that find something to do.

Because it discovers its own work from the database, firing it when there is
nothing to do is free — so the trigger can be generous rather than clever.

```
task_runner/
  main.py      the entry point: poll each handler, run what has work
  config.py    settings
  tasks/       one module per kind of background work
```

It is deployed by `cloudbuild.yaml` as a Cloud Run **Job**, not a service, and
Terraform creates the job definition. `enable_task_runner = false` in
`terraform.tfvars` turns it off; the work then simply waits.

This is **not** the execution plane. Nothing here runs a story, holds a
workspace or talks to a model — for that, see
[`../kernel/README.md`](../kernel/README.md). It is the place for periodic
platform chores that do not belong in a request.
