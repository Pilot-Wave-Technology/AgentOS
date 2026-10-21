# 11 · Your first story

File 09 proved the install works. This one is the product: a goal, a repository,
a story, and a pull request you accept.

Budget 20–30 minutes, most of it waiting for an agent to think. Do it on a
repository you do not mind receiving a branch and a PR — **a scratch repo is the
right choice for the first run.**

> If you are reading this before deploying, the vocabulary lives in
> [`../HOW_A_STORY_EXECUTES.md`](../HOW_A_STORY_EXECUTES.md). You do not need it
> to follow along, but every noun below is defined there.

---

## 11.1 Give the goal a repository

Agents cannot reach a repository you have not given them. Registering is a
deliberate, separate act from granting it to a story.

1. Open your goal → **Resources** → add a resource of kind **repository**.
2. Point it at a repo in the GitHub organisation your App is installed on
   (file 02). If it is not in that org, the App cannot see it and the agent will
   fail later, at branch time, with a 404 that does not mention this step.
3. Decide **writable** or not. This is a *ceiling*: it caps what any story may
   ever do with this repo, independently of what a particular story is granted.

**Only a person can register a repository.** The orchestrator may register
documents and is refused repositories outright — the row it would write has no
owner, no name and no default branch, so it would grant, mount, and then die at
branch derivation. Better to refuse where the error can name the cause.

---

## 11.2 Ask for work

In the goal's thread, tell the orchestrator what you want. Be concrete and
small for a first run:

> *Add a `--version` flag to the CLI that prints the version from
> `pyproject.toml`, and a test for it.*

The orchestrator proposes one or more **stories**. A story is the unit you
accept; it is not yet running and nothing has been created in your repository.

If the orchestrator does not answer at all, the control plane cannot reach the
gateway — go back to file 08.

---

## 11.3 Accept one

Accept the story you want. `proposed → accepted`.

Accepting is a person's act and there is no way around it. The valid moves from
each state are small and deliberate:

| State | What you can do |
|---|---|
| `proposed` | accept |
| `accepted` | start |
| `in_progress` | block, done |
| `blocked` | unblock, done |

---

## 11.4 Open it with your assistant

An **assistant** is your agent in this goal — one per person per goal, not one
per story. Open the accepted story with it ("work on this story" in the UI, or
`/work` from Slack).

Three things happen: the story moves to `in_progress`, a workspace is
provisioned, and the assistant starts a conversation with you about it.

**This is the conversation that matters.** The orchestrator wrote the story
thinly. What "done" means, which repository to touch, and what must not break
come out of talking to your assistant now — not from the sentence you typed in
11.2. Tell it anything it has not asked about.

Ask it to grant the repository to this story if it has not already. Grants are
per story, and a story with no grants has no workspace and cannot start.

---

## 11.5 Let it start, and approve the plan

When the scoping is settled, the assistant starts the run. That creates an
**execution** — one attempt at the story — which waits at `PENDING` until the
conductor admits it, then gets a pod.

The first thing the executor does is register a **plan** and *park*. A parked
run is not stuck and not broken: it is waiting for a person.

You will see an authorization card. Read the plan — this is the cheapest moment
in the whole cycle to change direction — and approve it.

Approving the plan **adopts its checks in the same act**, and the approval is
bound to a hash of the exact plan you were shown. An executor cannot re-register
the same steps with weaker checks and inherit your verdict.

---

## 11.6 Watch it work

The executor now works in **legs**: each one is a single conversation that
starts from an empty context and leaves a brief for the next. It may park again:

| You see | It means |
|---|---|
| a question | it is genuinely blocked — anyone can answer, including your assistant |
| a permission request | it wants to run something irreversible — **only a person** |
| nothing for a while | a long leg. Normal. |

You can steer a running execution without stopping it, and you can cancel it.

---

## 11.7 Accept the work

When the executor believes it is finished it claims completion. Before you are
asked anything, the work is **published** — the branch is pushed and a pull
request opened. That order is deliberate: the first live story on this system
passed review with seven commits still sitting in the sandbox.

Then the run parks for **acceptance**, and that one is yours alone. No autonomy
setting removes it. The reasoning is worth knowing: finishing is not only "was
the plan carried out", it is also "is this enough?" — and nobody can answer that
in advance, because the answer depends on what the work found.

Go and look at the pull request. Then either accept, or say what else you want
and let it continue.

---

## 11.8 Land it

Merge the pull request on GitHub, as you would any other.

If the git engine is on (it is, by default), the story then waits on a
`landing` park — the one kind **nobody answers**. The merge itself resolves it.
A card asking you to confirm what the engine is already watching for would be a
second authority over one fact, free to disagree with the first. What the card
is *for* is the other answer: "actually, more work".

Once it lands you get a **closeout**: the work is on trunk, and you decide
whether the story is finished. Then `done`, and — if review is on — a reviewer
reads what was actually written and the story reaches `reviewed`.

---

### ✅ You have done a full cycle when

- [ ] A story went `proposed → accepted → in_progress → done`
- [ ] You approved a plan and accepted a completion
- [ ] A branch and a pull request appeared in **your** repository
- [ ] You merged it and the story closed out

---

## If it stalls

**Nothing waits without a reason written on it.** That is an invariant, so the
first move is always to read the reason rather than guess.

| Symptom | Look at |
|---|---|
| execution stays `PENDING` | its `stall_reason` — it names which admission ceiling |
| the pod never starts | `./preflight.sh`, then `scripts/podcheck.py` |
| the orchestrator never answers | the gateway link (file 08) |
| work finished but never merged | the git engine — see file 10 |
| everything is quiet and nothing is wrong | a long leg; check the execution's steps |

[`10-operations-and-troubleshooting.md`](10-operations-and-troubleshooting.md)
has the day-two material.
