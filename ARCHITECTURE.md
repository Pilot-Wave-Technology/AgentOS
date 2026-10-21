# Architecture

What AgentOS is made of, and why each boundary sits where it does.

For the same system told as a story — a sentence a person types becoming a
commit — read [`HOW_A_STORY_EXECUTES.md`](HOW_A_STORY_EXECUTES.md) first. It
defines *execution*, *leg*, *park* and *profile*, which this document uses
throughout.

Everything here was read from the source on the branch it ships from. Where a
number appears, the file holding it is named.

---

## 1. Four planes

```
   CONTROL PLANE      app/        Cloud Run, stateless, five services
                                  the UI, the API, the board, the loops
          │
   GATEWAY PLANE      OpenClaw    one Node process on a VM
                                  the agents that TALK: orchestrator, assistants
          │
   KERNEL PLANE       kernel/     conductor x2, verifier x1, on Kubernetes
                                  decides WHEN work runs. Contains no model call.
          │
   EXECUTION PLANE    the pod     one pod per execution
                                  the workspace, the shell, the git
          │
                   PostgreSQL — the only shared state
```

Each boundary is enforced by something stronger than convention: a database
grant, a Kubernetes NetworkPolicy, or a function that does not exist.

| Plane | Holds | Cannot |
|---|---|---|
| Control | nothing durable | execute work |
| Gateway | agent sessions, workspaces, memory files | dispatch a run |
| Kernel | no LLM at all | decide what work is worth doing |
| Execution | the workspace, the shell, the git | reach the cloud, or delete itself |

**There is no message broker.** Fan-out is PostgreSQL `LISTEN/NOTIFY`, claims
are `FOR UPDATE SKIP LOCKED`, and coordination is advisory locks. That is why
two conductor replicas need no leader election, and why the design ports to
another cloud without a queue rewrite.

---

## 2. Control plane

One image, five Cloud Run services, each on a different entrypoint:

| Service | Entrypoint | Runs |
|---|---|---|
| `<prefix>-web` | `app.main` | UI, chat, API, agent callbacks |
| `<prefix>-council` | `app.council_worker` | the review-council dispatchers |
| `<prefix>-git` | `app.git_engine_worker` | the dependency-graph engine |
| `<prefix>-management` | `app.management_worker` | settings, dashboard, admin |
| `<prefix>-scheduler` | `app.scheduler_worker` | the background loops, behind one leader |

The web tier is deployed with `AGENTOS_SCHEDULER_LOOPS=0`, so it leaves those
loops to the scheduler. Locally the variable is unset and `uvicorn app.main:app`
runs the ones it starts itself.

Eight background loops are declared in one list
(`app/services/scheduler/registry.py`). The test for belonging there is whether
running two of them does harm.

See [`app/README.md`](app/README.md).

---

## 3. Gateway plane

A single Node process (OpenClaw, a separate branch of this repository) holding
the agents that hold a conversation. It owns their sessions, their workspaces
and their memory files; it cannot dispatch an execution.

The control plane and the gateway speak over one authenticated WebSocket. The
control plane tells the gateway which models exist and what their real context
windows are — that projection is generated from the model registry rather than
hand-maintained on both sides.

---

## 4. Kernel plane

`kernel/` is a **library**. The Deployment named `conductor` runs
`python -m kernel.worker.service` at **two replicas**; the verifier runs
`python -m kernel.verifier.service` at one.

With no leader election, correctness rests on four independent mechanisms:

1. **Row claims** — `FOR UPDATE SKIP LOCKED`, so a second replica sees nothing
   to claim rather than racing for it.
2. **Generation-fenced leases** — a stale holder's write no longer matches.
3. **Compare-and-set on every transition** — `WHERE id = $1 AND state = $2`;
   losing the race writes nothing.
4. **One global advisory lock** for the reconcile sweep.

See [`kernel/README.md`](kernel/README.md).

---

## 5. Execution plane

One pod per execution, with two containers running as **different users**:

```
  pod exec-<id>                     process namespace NOT shared
  ├── worker    kernel image. The DB connection, the git credential,
  │             the model key. Runs the leg loop.
  └── sandbox   the goal's base image, different uid, no secrets.
                This is where bash runs.
```

Sharing the process namespace would let the sandbox read
`/proc/<worker>/environ` and take the database password, so it is not shared.
The SQL proxy listens on a unix socket on a worker-only volume rather than TCP,
because both containers share a network namespace and a TCP listener would be
reachable from the model's own shell.

Port **9100** (worker → sandbox) has no authentication: reachability is the
boundary, restricted by a NetworkPolicy. Port **9101** (conductor → worker) is
authenticated with a token *derived* from the execution id rather than stored,
so a leak is one execution's and cannot mint another.

**The pod holds no cloud credential.** Artifacts, git credentials and granted
objects all cross through the control plane, authenticated with the execution's
own token.

This plane is **required**: every executor runs here. Terraform creates it and
the build deploys onto it. See [`execution-plane.sh`](execution-plane.sh).

---

## 6. Domain model

```
  goal ──► story ──► execution ──► leg ──► step
   │         │           │
   │         │           └─ one ATTEMPT at running the story
   │         └─ the unit a PERSON accepts
   └─ the unit of accountability; owns the agents and the resources
```

A story is not the thing that runs. An execution is. A story can have several.

**Resources** are registered on the goal (what it owns, and whether a story may
ever write it) and granted to a story (what it holds now). An execution's mounts
are **copied** from the grants at creation, so granting something later cannot
change what an old verdict claims to have been checked against.

---

## 7. Agent types

Definitions are **templates**; running agents are **instances**.

| Type | Scope | Lifecycle |
|---|---|---|
| **Orchestrator** (`GO`) | one per goal | lives as long as the goal |
| **Assistant** | one per member per goal | lives with that binding |
| **Reviewer** (`RA`) | one per council review | ephemeral — spawned per review, reaped after its verdict |
| **Platform**, **PlatformResearch**, **PlatformAssistant** | one installation-wide goal | the self-improvement council |

The orchestrator proposes stories and scopes work; it cannot start a run. The
assistant starts runs, steers them and answers questions; it cannot create a
story or grant a resource. Neither can approve anything — approval requires a
human uid.

> **Independent Agents (`IA`) are replaced by executor profiles.** Earlier
> versions created an Independent Agent per task. Story work now runs as an
> execution in the execution plane, and what that executor may do is defined
> by its **executor profile** (next section). The IA templates are no longer
> seeded, and migration `0009_retire_legacy_templates` removes them from an
> existing install; the `IA` type constant remains only so old database rows
> resolve.
>
> `RA` is **live**, in a different job: it is the type the review councils
> provision their reviewers as — `ra-story`, `ra-progress`, `ra-convener`, `ra-goal-health`,
> and the `sec-*` set. The old model's RA template, `task-reviewer-ia`, is
> retired with the IA templates.
>
> If you find documentation describing a task controller dispatching IAs, it
> predates the current system.

---

## 8. Executor profiles

Data, not classes. Effective surface is **(base + add) − deny**, and an anomaly
in that arithmetic is a *load* error.

| Profile | Tools | |
|---|---|---|
| `story-coding` | 29 | the full executor |
| `errand-ops` | 25 | no verifier; the exit code is the verdict |
| `story-business` | 18 | no shell, no commit |
| `story-coding-review` | 10 | read-only; its own model; 3 legs |

There is no default profile: an unknown `(kind, domain)` fails at dispatch,
before a container exists. See [`profiles/README.md`](profiles/README.md).

---

## 9. The run state machine

Ten states, twenty-five transitions, versioned and immutable. **Version 5** is
current and every execution records the version it ran under
(`seed/run_state_machine.json`).

```
  PENDING → ADMITTED → DISPATCHING → RUNNING → {PARKED_HOT, PARKED_COLD,
                                                VERIFYING, DONE, FAILED}
  cancel reaches every non-terminal state.      DONE → PENDING on reopen.
  terminal: DONE, FAILED, CANCELLED             FAILED is a dead end.
```

**The machine is logged, not enforced.** A trigger records every transition;
nothing validates one before writing. Validating would turn an undeclared
transition into a hard failure, so the console flags them instead. Three places
consult it voluntarily: accept, cancel-when-no-leg, and reopen.

---

## 10. Admission

| Ceiling | Value | Mechanism | Exact? |
|---|---|---|---|
| System-wide concurrent executions | 20 | a count before the claim | may overshoot by one per replica |
| Per goal, per owner — orchestrator | 10 | a row claim | exact |
| Per goal, per owner — assistant | 5 | a row claim | exact |

Only the system ceiling reads an environment variable, `AGENTOS_SYSTEM_CEILING`
(`kernel/conductor/admit.py`). The per-owner ceilings are code defaults
(`kernel/conductor/owner.py`), written into the goal's `admission_buckets` row
when the bucket is created; to widen one goal, edit that row — the row's
ceiling wins over the default. The bucket key is the **owner**, not the work
type.

When full, nothing is dropped: the row stays `PENDING` with a `stall_reason`
naming which ceiling. **Nothing waits without a reason written on it.**

---

## 11. Authorization

Four layers, and the useful thing is that they are different *kinds* of
mechanism:

| Layer | Enforced by |
|---|---|
| Who may read or change a goal | roles checked in the request path |
| What an agent may call | the tool surface — **a missing function**, not a prompt |
| What an execution may touch | its granted mounts and the interceptor chain |
| Who may authorise | SQL: the control plane *lowers itself* to a restricted role for the two authority-carrying writes |

Goal membership is goal-admin only, **with no platform-admin bypass** — a
deliberate asymmetry, since platform admins can read. There is a last-admin
guard, and removing a member holding stories returns `409 needs_reassignment`
rather than orphaning work.

Approving raises `E_APPROVAL_NOT_HUMAN` on a blank human uid, which defaults to
nothing unless a call site supplies one. The single exemption — an autonomous
errand's *plan*, settled by the orchestrator — re-checks the grant rather than
trusting a flag. Acceptance is never covered.

---

## 12. The kill switch

`app/services/execution_control.py` is the single source of truth for whether
work may start. It is consulted on the way in, so a pause stops new executions
without killing running ones.

---

## 13. Councils

Three, all switchable in `terraform.tfvars`. Only goal review ships on; security
and platform research ship off:

- **Goal review** — reads finished stories and reports to the orchestrator.
- **Security** — reviews a branch across several aspects.
- **Platform research** — researches improvements to AgentOS itself, in one
  installation-wide goal.

---

## 14. Channels

One platform is registered, Slack, behind an adapter / provisioner / relay
interface. Adding another is three registry entries and no change to the goal
routes.

---

## 15. Deployment

```
  git push → Cloud Build → build image
                        → run the migration job
                        → deploy five Cloud Run services
                        → deploy the execution plane
```

Terraform owns the service *configuration* — env vars, secrets, service
account, connector, Cloud SQL, sizing. Cloud Build owns the *image and the
migrations*. Re-running either does not fight the other.

The gateway deploys first and creates the shared VPC; the control plane reuses
it. See [`deploy/README.md`](deploy/README.md), and run
[`preflight.sh`](preflight.sh) between stages.

---

## 16. Repository layout

| | |
|---|---|
| [`app/`](app/README.md) | the control plane |
| [`kernel/`](kernel/README.md) | scheduling, execution, verification |
| [`migrations/`](migrations/README.md) | the schema and how it changes |
| [`seed/`](seed/README.md) | what a fresh install comes up with |
| [`profiles/`](profiles/README.md) | what an executor is allowed to be |
| [`scripts/`](scripts/README.md) | provisioning, build guards, operations |
| [`tests/`](tests/README.md) | the whole-codebase sweeps |
| [`terraform/`](terraform/README.md) | the infrastructure |
| [`deploy/`](deploy/README.md) | the deployment guide |
| [`task_runner/`](task_runner/README.md) | the background-work Job |
| [`static/`](static/README.md) | browser assets |
| `packages/` | the ledger client, published separately |
