# AgentOS

A platform for running long software goals with LLM agents, where a person
stays in the loop at the points that matter and the work happens in isolated
containers rather than on somebody's laptop.

You describe a goal. An orchestrator agent proposes stories. You accept one. An
assistant scopes it with you and starts it. The work then runs in its own pod —
with its own workspace, its own database login and no credentials it does not
need — and comes back as a branch and a pull request for you to accept.

> ### Read this before you run it
>
> **AgentOS agents write to your repositories.** They create branches, push
> commits and open pull requests in whatever GitHub organisation you point them
> at, through a GitHub App you install. They do not merge without a person, and
> every story's work goes to a branch of its own — but the write access is real,
> and it is the point. Give it an organisation you are comfortable with.
>
> It also spends money on LLM APIs, with your keys, in the background.

---

## What it is made of

AgentOS is **two branches of this one repository**, deployed in that order:

| Branch | Role |
|---|---|
| **`agent_os_openclaw`** | the gateway — hosts the agents that hold a conversation. **Deploy first**; it creates the shared network. |
| **`agent_os_platform`** (this one) | the control plane, the scheduler and the execution plane. |

Neither is useful without the other.

This branch contains four planes, and only one of them thinks:

```
  CONTROL PLANE    app/      Cloud Run, stateless    the UI, the API, the board
  GATEWAY PLANE              one Node process        the agents that TALK
  KERNEL PLANE     kernel/   conductor + verifier    decides WHEN work runs.
                                                     Contains no model call.
  EXECUTION PLANE  a pod     one per execution       the workspace, the shell, the git

                        PostgreSQL — the only shared state
```

There is no message broker: fan-out is `LISTEN/NOTIFY` and advisory locks.

---

## How work actually flows

```
  goal ──► story ──► execution ──► leg ──► step
   │         │           │
   │         │           └─ one ATTEMPT at running it, in a pod
   │         └─ the unit a PERSON accepts
   └─ the unit of accountability
```

A story is not the thing that runs; an **execution** is. Inside the pod an
executor works in **legs** — one conversation each, starting from an empty
context and leaving a brief for the next — until it claims it is finished. A
person accepts the claim. The work lands on trunk.

**[`HOW_A_STORY_EXECUTES.md`](HOW_A_STORY_EXECUTES.md) is the document to read
next.** It defines every one of those words and shows where each lives.

---

## What is actually enforced

Most "human in the loop" claims are prompts. These are not:

- **An agent cannot approve anything.** Resolving an approval raises
  `E_APPROVAL_NOT_HUMAN` without a human uid, and that uid defaults to nothing.
- **An assistant cannot create a story, grant a resource, provision, commit or
  verify** — because no such function is reachable from its surface. The
  boundary is a missing function, not an instruction.
- **The model's shell runs as a different user** from the process holding the
  database, in a container that holds no cloud credential at all.
- **A reviewer cannot change what it is reviewing**, and cannot run the test
  suite it is judging — a reviewer that can run the suite can report its own
  measurement as a finding about somebody else's work.
- **A tool not on a profile's list is refused by name.** The chain is
  fail-closed, so a mistake is a clear refusal rather than a silent hole.
- **`irreversible` effects can never be pre-approved.** Naming one in a profile
  stops the profile loading.

---

## What you get

**Goal-oriented work** — everything traces to a goal, with dependencies between
stories tracked and the graph closing over cancellations rather than leaving
dangling edges.

**Isolated execution** — one pod per execution, two containers, two users, a
default-deny network and an egress proxy. Required: every executor runs on the
GKE cluster that Terraform creates.

**A git engine** — cuts each story's branch, tracks what depends on what, and
runs a merge queue so dependent work lands in an order that builds.

**Review councils** — goal review, security review, and a platform research
council that studies AgentOS itself. Goal review ships on; security review and
platform research ship off and are switched on in `terraform.tfvars`.

**Persistent agent memory** — assistants accumulate memory across sessions,
built from the ledger rather than from the chat transcript.

**Custom tools** — define HTTP endpoints, OpenAPI specs or MCP servers in the
UI; agents pick them up within a minute, with a credential broker that only
hands over what the tool declared.

**An append-only ledger** — every state change is a hash-chained event.
Projections are derived from events, not the other way round.

**A kill switch** — pause or halt work at system or goal scope.

---

## Tech

| | |
|---|---|
| API | Python 3.12 · FastAPI · uvicorn |
| Database | PostgreSQL, asyncpg, **raw SQL — no ORM** |
| Frontend | Jinja2 + HTMX, no build step |
| Auth | Firebase |
| Execution | Kubernetes (GKE Standard), one pod per execution |
| Models | Anthropic Claude · Google Gemini |
| Deployment | Cloud Run · Cloud Build · Cloud SQL · GKE |

Raw SQL is a deliberate choice, held up by build-time sweeps rather than a type
system: the suite PREPAREs every SQL literal against a real schema, so a column
that does not exist fails the build instead of a request. See
[`tests/README.md`](tests/README.md).

---

## Deploying it

Start at **[`deploy/README.md`](deploy/README.md)** and work through the files
in order. Budget about two hours the first time.

You will need a Google Cloud project with billing, a GitHub organisation and an
App, a Firebase project, and API keys for both Anthropic and Gemini.
[`deploy/warmup.md`](deploy/warmup.md) is the checklist.

**Run [`./preflight.sh`](preflight.sh) between stages.** It checks that what the
last step was supposed to create actually exists, and it treats "could not
determine" as a failure rather than a pass. A deploy into a half-provisioned
project is the expensive case: it comes up, serves some requests, and fails on
the ones you did not test.

The execution plane — the GKE cluster every executor runs on — is created by
the same `terraform apply` and finished by the build
([`./execution-plane.sh`](execution-plane.sh)).

Each organisation runs AgentOS from its **own fork** — see
[`FORKING.md`](FORKING.md).

---

## Finding your way around

| | |
|---|---|
| [`HOW_A_STORY_EXECUTES.md`](HOW_A_STORY_EXECUTES.md) | the vocabulary, end to end — **start here** |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | the same system by component |
| [`CONFIG.md`](CONFIG.md) | every setting and where it comes from |
| [`deploy/`](deploy/README.md) | standing it up |
| [`app/`](app/README.md) · [`kernel/`](kernel/README.md) | the two halves of the code |
| [`FORKING.md`](FORKING.md) · [`VISION.md`](VISION.md) | why it is shaped this way |
