/**
 * tour.js — lightweight guided-tour engine (spotlight + step card).
 *
 * The shared overlay markup (#tour-overlay / #tour-spotlight / #tour-card)
 * lives once in base.html; this file only positions it and swaps step text.
 * Trigger a tour from anywhere with a plain onclick="startTour('id')" — same
 * "global function + inline onclick" convention as confirmAction()/
 * agentosTogglePopover() elsewhere in this app.
 *
 * TOURS registry: each entry is { label, steps }. A step is:
 *   { selector, title, body, beforeShow? }
 *     selector   - CSS selector for the element to spotlight
 *     beforeShow - optional async function run before the step is measured;
 *                  use it to open a modal/panel the step's target lives in
 *
 * If a step's selector isn't found on the page (wrong route, feature not
 * yet visible), the engine skips to the next step rather than getting stuck.
 */

// Generic "open a modal, then wait until its content is rendered" step
// helper — several tours need this (clicking a trigger button loads modal
// content via onclick/hx-get, which takes a tick to land in the DOM). Used
// as a step's `beforeShow`. No-ops if the modal is already open with the
// expected content present.
function _tourOpenModal({ triggerSelector, modalId, waitForSelector }) {
  return new Promise((resolve) => {
    const modal = document.getElementById(modalId);
    const ready = () =>
      modal && !modal.classList.contains('hidden') &&
      (!waitForSelector || document.querySelector(waitForSelector));
    if (ready()) { resolve(); return; }
    document.querySelector(triggerSelector)?.click();
    const start = Date.now();
    const poll = setInterval(() => {
      if (ready() || Date.now() - start > 2000) { clearInterval(poll); resolve(); }
    }, 50);
  });
}

// Pairs with _tourOpenModal for tours that walk out of a modal back onto
// the main page (e.g. from "fill this form" to "here's the row it created").
function _tourCloseModal(modalId) {
  return () => {
    document.getElementById(modalId)?.classList.add('hidden');
    return Promise.resolve();
  };
}

// Current goal id, parsed from the URL (/goals/{pid}/...) — for steps whose
// selector depends on which goal is open (e.g. the sidebar's per-goal
// subtree id, #goal-tree-{pid}), since TOURS is a single static registry
// shared across every goal, not rendered per-page like the rest of a step's
// markup. Used as `selector: () => '#goal-tree-' + _tourCurrentGoalId()`.
function _tourCurrentGoalId() {
  const m = location.pathname.match(/^\/goals\/([^/]+)/);
  return m ? m[1] : '';
}

// Picks a goal to hand the journey off into once platform-overview finishes:
// the sidebar's goal list, first entry other than whichever goal (if any) is
// currently open. Returns null when there's nothing to hand off to — no
// goals yet — so _finishTour() ends normally instead of navigating anywhere.
// Sidebar goal links carry the `goal-link` class (see /goals/sidebar-data in
// app/routers/goals.py); the tree loads via HTMX shortly after page load, so
// by the time a user finishes reading platform-overview's 5 steps it's had
// plenty of time to populate.
function _tourNextGoalUrl() {
  const currentGid = _tourCurrentGoalId();
  const links = Array.from(document.querySelectorAll('#sidebar-goals a.goal-link'));
  const next = links.find((a) => {
    const m = a.getAttribute('href').match(/^\/goals\/([^/]+)/);
    return m && m[1] !== currentGid;
  });
  return next ? next.getAttribute('href') : null;
}

// ── Milestone auto-launch tracking ──────────────────────────────────────────
// The overall "platform journey" isn't one long tour — it's a chain of small
// tours, each auto-firing the first time the user reaches the real page/state
// it explains. "Has this milestone been seen before" and "record that it has
// now been seen" are decided ATOMICALLY, SERVER-SIDE, in the same request
// that renders the page (check_and_mark_milestone() in
// app/services/tour_progress.py) - the route passes the result down as
// `auto_fire_tour` in the template context, before any HTML (and thus any
// JS) reaches the browser.
//
// This replaces an earlier client-side design (check a bootstrapped
// completedMilestones array, then fire a follow-up POST to mark it) that
// raced page navigation: a user clicking away before that follow-up request
// finished meant the write silently never happened - confirmed for real via
// a milestone's completion row only ever landing on a user's *second* visit
// to a page, because their first visit's write got cut off by clicking
// away. Deciding and recording in the same request that renders the page
// closes that race entirely: there's no follow-up network call tied to
// navigation timing left to lose.
//
// Every milestone is account-wide, not per-goal: the whole journey plays out
// once ever, on whichever goal the user happens to open first — not once
// per goal they're ever added to.

// Call from a page's own inline script when the server has determined
// (via auto_fire_tour in the template context) that this is the user's
// first-ever visit to this milestone. No client-side "have I seen this"
// check needed here — that decision already happened server-side before
// this page was sent. `delayMs` gives async page content (an HTMX-loaded
// sidebar tree, a chat thread's initial render) a moment to settle before
// the tour measures its spotlight target.
function autoFireTour(tourId, delayMs) {
  if (window.userPrefs && window.userPrefs.startup_wizard_enabled === false) return;
  setTimeout(() => { if (window.startTour) startTour(tourId); }, delayMs || 600);
}

// "Restart the guided tour" action (Goals page button): clears every
// completed milestone server-side, then reloads so platform-overview fires
// through the exact same server-driven path as every other milestone.
//
// Deliberately NOT calling startTour('platform-overview') directly here -
// that was a confirmed bug: it showed the tour "for free" without ever
// calling check_and_mark_milestone_if_enabled(), so nothing was actually
// persisted. The *next* real visit to /goals (typically after finishing
// the rest of the chain) would then hit the real route, correctly find no
// row, and fire it again for real - which looked exactly like "the whole
// journey randomly retriggered," when it was actually this button's first
// showing that never counted. Reloading routes through list_goals() like
// any other page load, so there's only one path that ever shows or
// records this milestone, not two.
async function restartJourney() {
  try {
    await fetch('/settings/tour-progress/reset', { method: 'POST' });
  } catch (e) {
    console.warn('[tour] failed to reset progress server-side:', e);
  }
  window.location.reload();
}

const TOURS = {
  'create-goal': {
    label: 'How to create a Goal',
    steps: [
      {
        selector: '#btn-new-goal',
        title: 'Start here',
        body: 'Click "New Goal" in the sidebar to open the creation form — hit Next and we’ll open it for you.',
      },
      {
        selector: '#form-create-goal-global input[name="id"]',
        title: 'Goal ID',
        body: 'This becomes the goal’s permanent identifier — used in URLs, and as the Slack channel name if you connect Slack later. Pick something short; it can’t be changed afterwards.',
        beforeShow: () => _tourOpenModal({
          triggerSelector: '#btn-new-goal', modalId: 'modal-new-goal',
          waitForSelector: '#form-create-goal-global',
        }),
      },
      {
        selector: '#form-create-goal-global input[name="name"]',
        title: 'Name',
        body: 'A friendly display name for the goal, shown throughout the UI.',
      },
      {
        selector: '#go-new-model',
        title: 'Goal Orchestrator model',
        body: 'Every goal gets one Goal Orchestrator (GO) agent that breaks it down into stories for the other agents to work on. This picks which LLM it runs on — changeable later from Goal Settings.',
      },
      {
        selector: '#form-create-goal-global button[type="submit"]',
        title: 'Create it',
        body: 'When you’re ready, click "Create Goal". The GO agent is created automatically and starts planning.',
      },
    ],
  },

  'create-story': {
    // Trigger button lives inside modal-create-story itself now (next to its
    // "Create Story" heading), not next to the "New Story" button that opens
    // it — so by the time this tour can start, the modal is already open.
    // No "click here to open it" first step needed; _tourOpenModal below is
    // a no-op when the modal's already showing.
    label: 'How to create a Story',
    steps: [
      {
        selector: '#modal-create-story input[name="title"]',
        title: 'Title',
        body: 'A short summary of what needs to be done.',
        beforeShow: () => _tourOpenModal({
          triggerSelector: '#btn-new-story', modalId: 'modal-create-story',
          waitForSelector: '#modal-create-story input[name="title"]',
        }),
      },
      {
        selector: '#modal-create-story textarea[name="description"]',
        title: 'Description',
        body: 'Context, approach, or requirements for whoever picks this up.',
      },
      {
        selector: '#modal-create-story textarea[name="acceptance_criteria"]',
        title: 'Acceptance criteria',
        body: 'How you’ll know the story is actually done — this is what gets checked when its tasks finish.',
      },
      {
        selector: '#modal-create-story .grid.grid-cols-2',
        title: 'Assign & prioritize',
        body: 'Leave "Assign to" unset and any member can Accept it later, or assign it directly now. Priority affects ordering only — it doesn’t block anything.',
      },
      {
        selector: '#modal-create-story select[name="story_weight"]',
        title: 'Story weight',
        body: 'A rough size estimate, used for contribution tracking. Optional — leave "Not sized" if you’re unsure.',
      },
      {
        selector: '#modal-create-story button[type="submit"]',
        title: 'Create it',
        body: 'New stories start in "proposed". Any member (or the person it’s assigned to) can Accept it to start work.',
      },
    ],
  },

  'add-resource': {
    // Trigger button lives inside modal-add itself now (next to its "Add
    // Resource" heading), not next to the "Add Resource" button that opens
    // it — so by the time this tour can start, the modal is already open.
    // No "click here to open it" first step needed; _tourOpenModal below is
    // a no-op when the modal's already showing.
    label: 'How to add a Resource',
    steps: [
      {
        selector: '#add-kind',
        title: 'Resource type',
        body: 'A Git Repository or Branch gives agents code to work in. A Cloud Resource attaches GCP service accounts/projects so the integration-tester and deploy phases can run for real.',
        beforeShow: () => _tourOpenModal({
          triggerSelector: '#btn-add-resource', modalId: 'modal-add',
          waitForSelector: '#add-kind',
        }),
      },
      {
        selector: '#gh-repo-select',
        title: 'GitHub repository',
        body: 'Pick from repos the platform\'s GitHub App can already see. If yours is missing, hit Refresh, or install the app on it first.',
      },
      {
        selector: 'input[name="default_branch"]',
        title: 'Default branch',
        body: 'The branch agents read from by default. You can add more branches (feature/trunk/release) separately afterwards.',
      },
      {
        selector: '#default-footer button[type="submit"]',
        title: 'Add it',
        body: 'Once added, the GO can allocate this resource to stories that need it.',
      },
    ],
  },

  'manage-members': {
    label: 'How to manage members',
    steps: [
      {
        selector: '#btn-add-member',
        title: 'Invite someone',
        body: 'Click "Add member" to invite them by email — hit Next and we’ll open it for you.',
      },
      {
        selector: '#modal-add-member input[name="email"]',
        title: 'Email address',
        body: 'They must already have an Agent OS account; this doesn’t send a signup invite.',
        beforeShow: () => _tourOpenModal({
          triggerSelector: '#btn-add-member', modalId: 'modal-add-member',
          waitForSelector: '#modal-add-member input[name="email"]',
        }),
      },
      {
        selector: '#modal-add-member select[name="role"]',
        title: 'Role',
        body: 'goal.member can see and work everything in the goal. goal.manager can additionally invite/remove members, change roles, and edit goal settings.',
      },
      {
        selector: '#modal-add-member button[type="submit"]',
        title: 'Add them',
        body: 'They\'ll see this goal appear in their sidebar right away.',
      },
      {
        selector: 'table tbody select[name="role"]',
        title: 'Change a role anytime',
        body: 'Switching this dropdown saves immediately — no separate save button.',
        beforeShow: _tourCloseModal('modal-add-member'),
      },
      {
        selector: 'table tbody button.btn-secondary.btn-sm.text-red-400',
        title: 'Remove a member',
        body: 'Asks for confirmation first. If they own stories that haven’t started yet, you’ll be asked to reassign those before the removal completes.',
      },
    ],
  },

  'platform-overview': {
    label: 'How this all works',
    steps: [
      {
        selector: '#sidebar-header',
        title: 'Welcome to Agent OS',
        body: 'This platform is organized around Goals: everything you do here happens inside one. A few unfamiliar terms come up a lot — this is a two-minute plain-English tour of them.',
      },
      {
        // Only admins/editors have #btn-new-goal in the sidebar. For everyone
        // else it doesn't exist, so fall back to the goals tree area — the
        // step's own text already covers this case ("ask one to add you if
        // you don't see this button"). Without this fallback the engine
        // skips the step entirely (no selector match), which looked like a
        // broken step counter (1 of 5, then 3 of 5).
        selector: () => document.getElementById('btn-new-goal') ? '#btn-new-goal' : '#sidebar-goals',
        title: 'Goal = a project',
        body: 'A Goal is a project or objective with its own team, resources, and chat. (Only admins/editors create new ones — ask one to add you if you don\'t see this button.)',
      },
      {
        selector: '#sidebar-goals',
        title: 'Story = a chunk of work, Task = one step',
        body: 'Open any Goal you belong to and it expands here. Inside, work is broken into Stories (a scoped piece of the goal, like a feature) and each Story into Tasks (one concrete step an agent carries out).',
      },
      {
        selector: '#sidebar-goals',
        title: 'Who does the work: GO, Assistant, IA, RA',
        body: 'GO plans a goal automatically, turning it into Stories. IA (Independent Agent) executes one Task at a time. RA (Reviewer Agent) automatically checks finished work. Your Assistant is your own personal AI — the same one follows you across every goal, with a separate conversation in each — it\'s what breaks a Story into Tasks as you work through it, rather than a general-purpose chatbot.',
      },
      {
        selector: '#sidebar-settings-link',
        title: 'Make it yours',
        body: 'Settings > Help & Explainability lets you turn tooltips, hover help, or these guided tours off individually. On pages like Story board, Resources, or Members, look for a small compass icon near the main action — clicking it walks you through that specific screen. Once you\'ve created a goal, or are part of one, the rest of this journey continues automatically as you go.',
      },
    ],
  },

  'goal-thread-intro': {
    label: 'What is the Goal Thread?',
    steps: [
      {
        selector: '#goal-thread-header',
        title: 'The Goal Thread',
        body: 'This is the whole team\'s shared conversation with the Goal Orchestrator (GO) — the agent that plans this goal into Stories. Every member and the GO post here. Long conversations get automatically compacted (summarized) behind the scenes, so old history doesn\'t break things as it grows.',
      },
      {
        selector: '#msg-input',
        title: 'Talk to GO directly',
        body: 'Type here like you would to a teammate — ask questions, give direction, or answer something GO asked. GO reads it and updates the goal\'s Stories accordingly. This is also the primary place to ask GO to create and assign new Stories, rather than always adding them manually from the Story board.',
      },
      {
        selector: '#messages',
        title: 'Link resources and check status',
        body: 'Also use this thread to ask GO to link a resource (like a Git repo) to a Story, or to check on a Story\'s status as work progresses. When the Goal Review Council or Security Council report something, GO reads it and lets you know here if there\'s a next action for you.',
      },
    ],
  },

  'resources-intro': {
    label: 'What are Resources?',
    steps: [
      {
        selector: '#resources-header',
        title: 'What are resources?',
        body: 'Repos, documents, and cloud service accounts your agents can read or work in. A Git repository specifically gets scanned and summarized as soon as it\'s added — so agents already have an initial understanding of the codebase before any work starts on it.',
      },
      {
        selector: '#btn-add-resource',
        title: 'Add one',
        body: 'Click here to register a Git repository (or another kind) for this goal.',
      },
      {
        selector: '#resources-header',
        title: 'Ask GO to link it to a story',
        body: 'Adding a resource here doesn\'t automatically attach it to any story. Go to the Goal Thread and ask GO to allocate this repo to whichever story needs it.',
      },
    ],
  },

  'story-board-intro': {
    label: 'How Stories work',
    steps: [
      {
        selector: '#stories-header',
        title: 'How GO plans your work',
        body: 'GO breaks the goal\'s objective down into Stories on its own as it plans — you don\'t have to write every one by hand. It proposes them based on the goal\'s objective and what gets discussed in the Goal Thread.',
      },
      {
        selector: '#btn-new-story',
        title: 'Manual creation is the exception, not the norm',
        body: 'Asking GO in the Goal Thread is the intended way to get a Story created. This button is here for the odd case where you need to add one directly yourself — click here, or use the compass icon next to it for a full walkthrough of the form.',
      },
      {
        selector: '#story-board-columns',
        title: 'How a Story moves',
        body: 'Each column is a state: proposed (not picked up yet) → accepted (someone\'s taken it) → in_progress → done. blocked means it\'s stuck on a dependency or a question. A story\'s column is always its current state.',
      },
    ],
  },

  'assistant-thread-intro': {
    label: 'What is the Assistant?',
    steps: [
      {
        selector: '#chat-header',
        title: 'Your Assistant',
        body: 'This is your own personal AI, private to you — the same Assistant follows you across every goal, just with its own conversation here. Import a story into this thread to work through it with its help: it plans the story into Tasks, and can read that story\'s docs, resources, and this conversation\'s history.',
      },
      {
        selector: '#btn-add-participant',
        title: 'Share this conversation',
        body: 'Add another goal member here to bring them into this exact conversation — useful when someone else needs the same context you\'re working from.',
      },
      {
        selector: '#btn-delete-conversation',
        title: 'Start fresh',
        body: 'Delete this conversation to start over with a clean slate — useful if it\'s gotten off track or you just want a new beginning. This can\'t be undone.',
      },
      {
        selector: '#msg-input',
        title: 'Just talk to it',
        body: 'Ask it anything about the story, or bring one in to get started — it can update the story, check task status, and coordinate with GO on your behalf.',
      },
      {
        selector: '#messages',
        title: 'Cards, not just text',
        body: 'When it needs your input — approve a plan, review a diff, answer a question — it posts an interactive card here instead of plain text. Watch for these at each major decision point.',
      },
      {
        selector: '#messages',
        title: 'Wrapping up a story',
        body: 'When your work is ready, mark the story done. If it needs to push code to a branch, the Git Engine runs it through a merge story — you\'ll see a message here to review the diff and approve the merge, with a link to the PR you can click if one was raised.',
      },
      {
        selector: '#messages',
        title: 'Reviews happen too',
        body: 'A Story review (Goal Review Council) automatically happens for this work — check its thread anytime. A Security review (Security Council) may also run, depending on whether it\'s enabled in this deployment.',
      },
    ],
  },

  'tasks-board-intro': {
    label: 'How Tasks work',
    steps: [
      {
        selector: '#tasks-header',
        title: 'Stories become Tasks',
        body: 'Once your Assistant\'s plan for a story is approved, it\'s broken down into Tasks — the concrete steps an IA (Independent Agent) actually executes, one at a time.',
      },
      {
        selector: '#tasks-header',
        title: 'How a Task moves',
        body: 'pending → ready → dispatching → in_progress → automated_review → awaiting_approval (needs your approval) → approved/done. blocked means it\'s waiting on something. failed/rejected/cancelled/halted all stop it — halted specifically is one-way, no restart.',
      },
      {
        selector: '.space-y-3 .pill',
        title: 'That badge is the state',
        body: 'This is a live example — whatever it currently says is exactly where that task sits in the cycle above.',
      },
    ],
  },

  'council-intro': {
    label: 'What is the Goal Review Council?',
    steps: [
      {
        selector: '#council-header',
        title: 'Goal Review Council',
        body: 'An independent AI review of this goal — checks each finished story against its acceptance criteria and looks at overall goal health. Read-only for you: humans can\'t post here, only watch.',
      },
      {
        selector: '#messages',
        title: 'When does it run?',
        body: 'A quick per-story review runs automatically the moment a story is marked done. A fuller review of the whole goal also runs automatically, on a regular schedule. Either way, reports land here — GO reads them and lets you know in the Goal Thread if there\'s a next action for you.',
      },
    ],
  },

  'goal-dashboard-intro': {
    label: 'What is this page?',
    steps: [
      {
        selector: '#goal-dashboard-page',
        title: 'Welcome to this goal',
        body: 'This overview shows the objective and quick links to Monitor, Members, Integrations, and Model config. The day-to-day work — chatting with GO, browsing Stories and Tasks, adding Resources — happens in the sidebar.',
      },
      {
        selector: () => '#goal-tree-' + _tourCurrentGoalId(),
        title: 'The rest lives in the sidebar',
        body: 'This goal is already expanded in the sidebar. We\'ll walk through each of these next — starting with the Goal Thread.',
      },
    ],
  },

  'agent-mesh-intro': {
    label: 'What is the Agent Mesh?',
    steps: [
      {
        selector: '#agent-mesh-header',
        title: 'Agent Mesh',
        body: 'GO and everyone\'s Assistants coordinate with each other here — updates, questions, and requests passed between agents directly, not through you. Read-only for you: humans can\'t post here, only watch.',
      },
      {
        selector: '#messages',
        title: 'Why check it?',
        body: 'Come here to see important communication that happened between the different agents working on this goal.',
      },
    ],
  },

  'security-intro': {
    label: 'What is the Security Council?',
    steps: [
      {
        selector: '#security-header',
        title: 'Security Council',
        body: 'An independent scan of this goal\'s code for secrets, vulnerabilities, and access issues. Read-only for you: humans can\'t post here, only watch.',
      },
      {
        selector: '#messages',
        title: 'When does it run?',
        body: 'Unlike the Goal Review Council, nothing about finishing a story triggers this. It only runs on a schedule basis.',
      },
    ],
  },
};

// Every milestone that's part of the overall guided journey, in order —
// platform-overview through security-intro. Used to decide whether the tour
// a user just closed early (✕) was a journey milestone at all (cascading
// "mark the rest done" only makes sense for these — standalone tours like
// create-story/add-resource/manage-members/create-goal aren't tracked
// server-side and have nothing to cascade). Kept in sync with
// JOURNEY_MILESTONE_IDS in app/services/tour_progress.py.
const JOURNEY_MILESTONE_IDS = [
  'platform-overview', 'goal-dashboard-intro', 'goal-thread-intro', 'agent-mesh-intro',
  'resources-intro', 'story-board-intro', 'assistant-thread-intro', 'tasks-board-intro',
  'council-intro', 'security-intro',
];

// ── Cross-page journey chaining ─────────────────────────────────────────────
// Finishing one milestone tour via "Next"/"Continue" (NOT the ✕ close
// button) auto-navigates to the next page in the journey. Closing early
// always stops the whole chain (✕ calls abandonTour(), which cascades the
// rest of the journey as done rather than just closing — see the button
// wiring in base.html).
//
// Just a tourId -> next URL function map — nothing here decides or records
// whether the next milestone has been "seen"; that's the destination page's
// own server-side check_and_mark_milestone() call (see autoFireTour() above),
// which fires correctly regardless of whether the user arrives here via
// this chain or by clicking a sidebar link directly. No sessionStorage,
// no client-side completion tracking needed for this — an earlier design
// (pre-mark the next milestone + a sessionStorage flag to resume on load)
// added complexity purely to avoid a double-fire race that server-side
// atomicity makes moot.
//
// platform-overview hands off into whichever goal the sidebar shows first
// (or ends normally if the user has none yet) — the account-wide "overall
// journey" is platform-overview followed by one full walkthrough of a goal,
// not a dead end that leaves the user to click into a goal themselves.
// security-intro has no entry: it's the end of that one goal's walkthrough,
// and every milestone before it is account-wide (marked once, ever) so
// there's nothing left for a second goal to still need showing.
const JOURNEY_CHAIN = {
  'platform-overview': _tourNextGoalUrl,
  'goal-dashboard-intro': (gid) => `/goals/${gid}/thread`,
  'goal-thread-intro': (gid) => `/goals/${gid}/orchestrator`,
  'agent-mesh-intro': (gid) => `/goals/${gid}/resources/browser`,
  'resources-intro': (gid) => `/goals/${gid}/stories`,
  'story-board-intro': (gid) => `/goals/${gid}/chat/pick-assistant`,
  'assistant-thread-intro': (gid) => `/goals/${gid}/tasks`,
  'tasks-board-intro': (gid) => `/goals/${gid}/council`,
  'council-intro': (gid) => `/goals/${gid}/security`,
};

(function () {
  let _tour = null; // { id, stepIndex }
  let _resizeHandler = null;

  function _steps() {
    return TOURS[_tour.id].steps;
  }

  async function _showStep(i) {
    const steps = _steps();
    const step = steps[i];
    if (!step) { endTour(); return; }
    if (step.beforeShow) await step.beforeShow();

    // selector is usually a plain CSS string, but can be a function for
    // steps whose target depends on runtime state (e.g. the current goal id)
    // rather than something fixed at authoring time.
    const resolvedSelector = typeof step.selector === 'function' ? step.selector() : step.selector;
    const target = resolvedSelector ? document.querySelector(resolvedSelector) : null;
    if (!target) {
      // Not on this page / not rendered yet — skip forward rather than stall.
      if (i < steps.length - 1) { await _showStep(i + 1); return; }
      _finishTour();
      return;
    }

    const overlay = document.getElementById('tour-overlay');
    const spotlight = document.getElementById('tour-spotlight');
    const card = document.getElementById('tour-card');

    target.scrollIntoView({ block: 'center', behavior: 'smooth' });
    await new Promise((r) => setTimeout(r, 150)); // let scroll settle before measuring

    const rect = target.getBoundingClientRect();
    const pad = 6;
    spotlight.style.top = `${rect.top - pad}px`;
    spotlight.style.left = `${rect.left - pad}px`;
    spotlight.style.width = `${rect.width + pad * 2}px`;
    spotlight.style.height = `${rect.height + pad * 2}px`;

    document.getElementById('tour-step-title').textContent = step.title;
    document.getElementById('tour-step-body').textContent = step.body;
    document.getElementById('tour-step-counter').textContent = `Step ${i + 1} of ${steps.length}`;
    document.getElementById('tour-btn-prev').classList.toggle('hidden', i === 0);
    const isLast = i === steps.length - 1;
    document.getElementById('tour-btn-next').textContent =
      !isLast ? 'Next' : (!_tour.standalone && JOURNEY_CHAIN[_tour.id] ? 'Continue →' : 'Done');

    overlay.classList.remove('hidden');

    // Position the card below the spotlight, flipping above if it would
    // overflow the viewport bottom, and clamped horizontally — same
    // technique as the info-popover panel in app.js.
    const cardRect = card.getBoundingClientRect();
    let top = rect.bottom + pad + 12;
    if (top + cardRect.height > window.innerHeight - 8) {
      top = Math.max(8, rect.top - pad - cardRect.height - 12);
    }
    let left = rect.left;
    if (left + cardRect.width > window.innerWidth - 8) left = window.innerWidth - cardRect.width - 8;
    if (left < 8) left = 8;
    card.style.top = `${top}px`;
    card.style.left = `${left}px`;

    _tour.stepIndex = i;
  }

  // standalone=true (set by the tour_trigger() macro's onclick — see
  // partials/_tour_trigger.html) means this is a one-off manual replay, not
  // a step in the auto-fire journey: the last step reads "Done" instead of
  // "Continue →", finishing never navigates to the next milestone's page,
  // and cancelling early never cascades the rest of the journey as done
  // (see JOURNEY_CHAIN/abandonTour() below) — none of that applies when the
  // user just wanted to re-watch this one page's tour.
  window.startTour = function (id, standalone) {
    if (window.userPrefs && window.userPrefs.startup_wizard_enabled === false) return;
    if (!TOURS[id]) { console.warn('[tour] unknown tour id:', id); return; }
    _tour = { id, stepIndex: 0, standalone: !!standalone };
    _resizeHandler = () => _tour && _showStep(_tour.stepIndex);
    window.addEventListener('resize', _resizeHandler);
    _showStep(0);
  };

  window.endTour = function () {
    document.getElementById('tour-overlay')?.classList.add('hidden');
    if (_resizeHandler) window.removeEventListener('resize', _resizeHandler);
    _resizeHandler = null;
    _tour = null;
  };

  // True only when leaving _tour mid-flow should cascade "the rest of the
  // journey is abandoned" — a standalone replay (create-story, add-resource,
  // a manually re-opened milestone) never cascades, and neither does a tour
  // that isn't part of the tracked journey at all.
  function _shouldCascadeAbandon() {
    return !!(_tour && !_tour.standalone && JOURNEY_MILESTONE_IDS.includes(_tour.id));
  }

  // Closing a journey milestone early (✕ or Escape) means "I don't want to
  // see the rest of this guided journey" — not just "not right now for this
  // one page." Marks every remaining milestone done server-side in one call
  // so none of them auto-fire later, then closes exactly like endTour().
  window.abandonTour = function () {
    if (_shouldCascadeAbandon()) {
      fetch('/settings/tour-progress/skip-remaining', { method: 'POST' }).catch((e) => {
        console.warn('[tour] failed to mark remaining journey milestones done:', e);
      });
    }
    endTour();
  };

  // Leaving the page mid-tour some other way — refresh, closing the tab, or
  // navigating elsewhere without touching the tour's own Next/✕ controls —
  // is the same "didn't finish, don't want the rest" signal as abandonTour(),
  // so it cascades identically. fetch() issued from pagehide is not reliably
  // delivered before the browser tears the page down; sendBeacon is built
  // specifically to survive that. _finishTour() always calls endTour() (nulls
  // _tour) before navigating on a legitimate chain-continue, so this never
  // misfires on the normal "finished the tour, moving to the next milestone's
  // page" path — only on a genuinely interrupted one.
  window.addEventListener('pagehide', () => {
    if (_shouldCascadeAbandon()) {
      navigator.sendBeacon('/settings/tour-progress/skip-remaining');
    }
  });

  // Called whenever a tour reaches its natural end (last step finished, or
  // skipped past because its target never appeared). If this tour is part
  // of JOURNEY_CHAIN, continues to the next page automatically instead of
  // just closing — see the chain comment above TOURS' closing brace. No
  // network call here: the destination page decides for itself, server-side,
  // whether to auto-fire its own milestone. endTour() runs before navigating
  // (not just on the no-chain path) so _tour is already cleared by the time
  // pagehide fires above — otherwise a normal finish-and-continue would look
  // identical to an interrupted one and wrongly cascade-abandon the rest.
  function _finishTour() {
    const chainFn = _tour && !_tour.standalone && JOURNEY_CHAIN[_tour.id];
    const nextUrl = chainFn ? chainFn(_tourCurrentGoalId()) : null;
    endTour();
    if (nextUrl) {
      window.location.href = nextUrl;
    }
  }

  window.tourNext = function () {
    if (!_tour) return;
    const steps = _steps();
    if (_tour.stepIndex >= steps.length - 1) { _finishTour(); return; }
    _showStep(_tour.stepIndex + 1);
  };

  window.tourPrev = function () {
    if (!_tour || _tour.stepIndex === 0) return;
    _showStep(_tour.stepIndex - 1);
  };

  document.addEventListener('keydown', (e) => {
    if (_tour && e.key === 'Escape') abandonTour();
  });
})();
