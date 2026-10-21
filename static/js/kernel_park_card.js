/**
 * A parked executor's card: what it is waiting for, and the buttons.
 *
 * WHY THIS IS A FILE AND NOT PART OF A PAGE. It lived inside
 * chat/index.html, so it existed for the assistant's thread and nowhere
 * else. When an approved autonomous errand's conversation moved to the
 * GO's thread, the card arrived there as plain text with no buttons --
 * the plan was rendered, correct, and unapprovable, and the only way
 * forward was a POST typed into a browser console.
 *
 * Copying two hundred lines into the second page would have worked
 * until the first time somebody fixed one copy.
 *
 * WHAT A HOST PAGE MUST PROVIDE. Three globals, and nothing else:
 *
 *     PID          the goal id, for the decide endpoint
 *     escapeHtml   any HTML-escaper
 *     renderMd     markdown -> sanitised HTML (see safe_markdown.js)
 *
 * THE DECIDE ENDPOINT IS THE AUTHORITY, not this file. It checks goal
 * membership AND that the question belongs to that goal, and it is the
 * only thing that may approve a plan -- a person's act, carrying their
 * uid, which no agent can do. Everything here is presentation and a
 * couple of refusals that save a round trip.
 */

function buildParkHtml(card) {
  const plan = card.plan || {};
  const steps = Array.isArray(plan.steps) ? plan.steps : [];
  const checks = Array.isArray(plan.checks) ? plan.checks : [];
  let html = `<div class="card-title">${escapeHtml(card.title || 'Waiting on you')}</div>`;
  if (plan.rationale) {
    html += `<div class="card-body"><div class="agent-prose">${escapeHtml(plan.rationale)}</div></div>`;
  } else if (!steps.length && card.body) {
    html += `<div class="card-body"><div class="agent-prose">${renderMd(card.body)}</div></div>`;
  }
  if (steps.length) {
    const items = steps.map(s => `<li>${escapeHtml(s)}</li>`).join('');
    html += `<div class="card-body"><div class="text-2xs uppercase tracking-wider text-gray-400 mb-1">Steps (${plan.step_count || steps.length})</div>
      <ol class="list-decimal ml-5 space-y-0.5 text-gray-200">${items}</ol></div>`;
  }
  if (checks.length) {
    // The checks are the honest part of the card: a person reading the
    // command the verifier will run learns more than any prose.
    //
    // AND THEY ARE EDITABLE, while the decision is still open. Changing
    // one check used to mean revising the whole plan -- a full re-plan
    // to sharpen one command -- so nobody did it, and a guard reading
    // `test -f README.md` on a file the story never touched was
    // approved as written. Approving a plan you had to fix first is
    // still approving it; clearing a box drops that check.
    const open = card.needs_human && !card.decided
                 && card.kind === 'authorization';
    const qid = card.question_id;
    const items = checks.map((c, i) => {
      const tag = c.label ? `<span class="text-2xs text-gray-500 mr-1">[${escapeHtml(c.label)}]</span>` : '';
      if (!open) {
        return `<li>${tag}<code class="text-2xs" style="white-space:pre-wrap;word-break:break-all">${escapeHtml(c.run || '')}</code></li>`;
      }
      // A TEXTAREA, NOT AN INPUT. A check is a shell command and they
      // run long -- 175 characters is ordinary. A single-line field
      // showed the first sixty and hid the rest behind a scroll nobody
      // thinks to try, so a complete command read as a truncated one:
      // `pytest ... -k` with the pattern apparently missing. That cost
      // a wrong diagnosis out loud. You cannot edit what you cannot
      // see.
      return `<li class="flex items-start gap-1 mb-1">${tag}
        <textarea class="park-check input text-2xs font-mono flex-1 min-w-0"
               rows="2" spellcheck="false" wrap="soft"
               style="resize:vertical;white-space:pre-wrap;word-break:break-all"
               data-label="${escapeHtml(c.label || 'unlabelled')}"
               aria-label="check ${i + 1}">${escapeHtml(c.run || '')}</textarea></li>`;
    }).join('');
    html += `<div class="card-body"><div class="text-2xs uppercase tracking-wider text-gray-400 mb-1">Verified by (${plan.check_count || checks.length})</div>
      <ul class="ml-3 space-y-0.5" id="park-checks-${qid}">${items}</ul>
      ${open ? '<div class="text-2xs text-gray-500 mt-1">Edit a check before approving; empty removes it. An edited check is adopted as written.</div>' : ''}</div>`;
  }
  if (plan.truncated) {
    html += `<div class="text-2xs text-amber-400 italic mt-1">Showing the first steps — the full plan is longer.</div>`;
  }
  const dr = card.dryrun;
  if (dr && (dr.checks || []).length) {
    const rows = dr.checks.map(c => {
      const ok = !c.verdict || c.verdict === 'ok' || c.verdict === 'promote';
      const mark = ok ? '<span class="text-emerald-400">✓</span>'
                      : '<span class="text-amber-400">!</span>';
      const note = ok ? '' : ` <span class="text-2xs text-amber-300/80">${escapeHtml(c.verdict)}</span>`;
      return `<li>${mark} <code class="text-2xs">${escapeHtml(c.run)}</code>${note}</li>`;
    }).join('');
    const concerns = (dr.concerns || []).length;
    html += `<div class="card-body"><div class="text-2xs uppercase tracking-wider text-gray-400 mb-1">Dry-run${concerns ? ` — ${concerns} to look at` : ' — all proven'}</div>
      <ul class="ml-3 space-y-0.5">${rows}</ul>
      ${concerns ? `<div class="text-2xs text-gray-500 mt-1">Advisory only — approving is your call.</div>` : ''}</div>`;
  } else if (dr && dr.status === 'pending') {
    html += `<div class="text-2xs text-gray-500 mt-1 italic">Dry-run still running — you can approve now; it is advisory.</div>`;
  }
  const who = card.story_id ? `story ${escapeHtml(String(card.story_id))}` : `run ${escapeHtml(String(card.execution_id || ''))}`;
  html += `<div class="text-2xs text-gray-500 mt-2">${who} · run ${escapeHtml(String(card.execution_id || ''))} · approval is yours alone</div>`;
  return html;
}

// ONE RENDERER FOR THE PARK BUTTONS, and the server decides what they say.
//
// There are two card renderers in this file -- one for a live push, one
// for a reload -- and the buttons were written out by hand in both. They
// drifted: the reload path learned that an ACCEPTANCE says "Mark done /
// More work" and the live path never did, so a freshly-arrived
// acceptance card offered "Approve plan / Request changes / Decline".
// Every word of that was wrong. Verification had already passed, there
// was no plan in front of anyone, and "Decline" is not a thing you can
// do to work that is finished -- the question was whether the STORY is
// done, which is a different question at a different level.
//
// So the labels come from `card.options`, which `build_card` already
// fills in per kind and is the only place that knows what is being
// asked. A new kind now arrives with correct buttons in both paths
// without touching this file -- which is what stopped being true the
// moment there were two copies.
const PARK_STYLE = { primary: 'opt-primary', secondary: 'opt-secondary',
                     danger: 'opt-danger' };
// Whitelisted because it is interpolated into an onclick. The server is
// ours, but a value that reaches executable script should never be
// taken on trust from a payload.
const PARK_VERDICTS = ['approve', 'revise', 'declined'];

function parkNotePlaceholder(kind) {
  if (kind === 'acceptance') return 'Describe the additional work (required for More work)';
  if (kind === 'direction') return 'Say which way to go (required to change the plan)';
  // A CLARIFICATION IS A QUESTION, not a verdict. "What should change?"
  // is the wrong prompt for "does this repo use pnpm or npm?", and the
  // answer IS the note -- there is nothing else on the card carrying it.
  if (kind === 'clarification') return 'Type your answer (required — this is what the executor gets)';
  return 'What should change? (required to revise or decline)';
}

function parkOptionsHtml(card) {
  const decided = card.decided || '';
  const qid = card.question_id;
  if (decided) {
    const LBL = card.kind === 'acceptance'
      ? { approve: '✓ Story marked done', revise: '↺ More work requested' }
      : card.kind === 'direction'
      ? { approve: '▶ Carried on as planned', revise: '✎ Plan being changed' }
      : { approve: '✓ Approved', revise: '✎ Changes requested', declined: '✕ Declined' };
    return `<div class="card-responded">${escapeHtml(LBL[decided] || decided)}</div>`;
  }
  // NOT `needs_human` ALONE, and this is where the last fix stopped.
  // That field answers an AUTHORIZATION question -- "is a person the
  // only one who may answer this?" -- and using it to decide whether to
  // DRAW anything conflated it with "may anyone reply here at all?".
  //
  // A `clarification` sits outside HUMAN_ANSWERED_KINDS on purpose,
  // because the ASSISTANT is allowed to answer it; that is what makes
  // `ask_human` cheap. So `needs_human` is false, and this line threw
  // away the `options` and `allow_custom` that `build_card` had just
  // filled in for exactly this case. Watched on NCAL3 run 189: a
  // question on screen, PARKED_HOT, nothing to click, and the only exit
  // cancelling the run.
  //
  // `needs_human` is KEPT in the condition: a card that needs a human
  // and carries no options still gets the default three buttons below.
  const hasControls = card.needs_human || card.allow_custom
                      || (Array.isArray(card.options) && card.options.length);
  if (!hasControls) return '';
  const opts = (Array.isArray(card.options) && card.options.length)
    ? card.options
    : [{ value: 'approve', label: '✓ Approve plan', style: 'primary' },
       { value: 'revise', label: '✎ Request changes', style: 'secondary' },
       { value: 'declined', label: '✕ Decline', style: 'danger' }];
  const btns = opts
    .filter(o => PARK_VERDICTS.indexOf(o.value || o.id) !== -1)
    .map(o => {
      const v = o.value || o.id;
      const cls = PARK_STYLE[o.style] || 'opt-secondary';
      return `<button class="card-option ${cls}" onclick="decidePark(${qid},'${v}')">${escapeHtml(o.label || v)}</button>`;
    }).join('');
  const custom = card.allow_custom === false ? '' : `
    <div class="card-custom-input">
      <input type="text" id="park-note-${qid}" placeholder="${escapeHtml(parkNotePlaceholder(card.kind))}">
    </div>`;
  // The kind travels on the element rather than through the onclick:
  // it is server-supplied and an onclick is executable script.
  return `<div class="card-options" data-park-q="${qid}" `
       + `data-park-kind="${escapeHtml(card.kind || '')}">${btns}</div>${custom}`;
}

async function decidePark(qid, verdict) {
  const note = (document.getElementById(`park-note-${qid}`) || {}).value || '';
  const box0 = document.querySelector(`[data-park-q="${qid}"]`);
  const parkKind = (box0 && box0.dataset.parkKind) || '';
  if ((verdict === 'revise' || verdict === 'declined') && !note.trim()) {
    alert('Say what should change — a revise or a decline with no reason gives the executor nothing to act on.');
    return;
  }
  // THE NOTE IS THE WHOLE ANSWER on a clarification -- the verdict is
  // just the send button. Letting it through empty resolves the question
  // with nothing in it, which un-parks the run and tells the executor
  // exactly as much as never answering did.
  if (parkKind === 'clarification' && !note.trim()) {
    alert('Type your answer first — on a question, the box is the answer.');
    return;
  }
  // THE CHECKS AS THEY STAND, if the person edited any. Sent only with
  // an approve: a revise hands the plan back for the executor to
  // rewrite, and editing checks on a plan about to be replaced would
  // put words in the mouth of the next one. Omitted entirely when
  // nothing was touched, because an empty list means "approved with no
  // checks" and that is a very different thing from "as proposed".
  let checks;
  if (verdict === 'approve') {
    const boxes = document.querySelectorAll(`#park-checks-${qid} .park-check`);
    if (boxes.length) {
      const edited = [...boxes].map(b => ({ run: b.value,
                                            label: b.dataset.label }));
      if (edited.some((c, i) => c.run !== boxes[i].defaultValue)) {
        checks = edited;
      }
    }
  }

  const box = document.querySelector(`[data-park-q="${qid}"]`);
  if (box) box.innerHTML = '<span class="text-2xs text-gray-400 italic">Sending…</span>';
  try {
    const resp = await fetch(`/goals/${PID}/kernel/questions/${qid}/decide`, {
      method: 'POST', credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(checks ? { verdict, text: note, checks }
                                  : { verdict, text: note }),
    });
    if (!resp.ok) {
      const j = await resp.json().catch(() => ({}));
      throw new Error(j.detail || `${resp.status} ${resp.statusText}`);
    }
    const LBL = parkKind === 'clarification'
      ? { approve: '↩ Answer sent' }
      : { approve: '✓ Approved', revise: '✎ Changes requested', declined: '✕ Declined' };
    if (box) box.outerHTML = `<div class="card-responded">${LBL[verdict] || verdict}</div>`;
    const inp = document.getElementById(`park-note-${qid}`);
    if (inp && inp.parentElement) inp.parentElement.remove();
  } catch (e) {
    if (box) box.innerHTML = `<span class="text-2xs text-red-400">${escapeHtml(e.message)}</span>`;
  }
}


/**
 * Turn every `.card-container` in `root` into a rendered card.
 *
 * The assistant's thread has its own richer hydrator (it handles diff
 * reviews, DAG recovery and the rest); this is the small one the GO's
 * thread uses, where the only cards are the kernel's.
 *
 * IDEMPOTENT, because it runs on load, after "load earlier" prepends
 * rows, and after a live card is appended: it reads the <script> then
 * removes it, so a container it has already filled is skipped.
 */
function hydrateParkCards(root) {
  (root || document).querySelectorAll('.card-container').forEach(el => {
    const scriptEl = el.querySelector('script.card-json');
    if (!scriptEl) return;                        // already hydrated
    let card;
    try {
      // `</` was escaped server-side so it could not close the tag.
      //
      // SPLIT/JOIN, NOT A REGEX, AND THIS IS WHY. The regex here was
      // `/<\\//g` -- whose THIRD `/` closes the literal, leaving the
      // regex `<\\` followed by `/g`, which is a DIVISION by an
      // undefined `g`. Every call threw `ReferenceError: g is not
      // defined`, inside this try, caught by the silent `return` below.
      //
      // So no card on this page has EVER hydrated. Reported four times
      // over as "plan cards disappear after I refresh", "the mark story
      // done card is lost", "the verification card does not persist" --
      // and each time it looked like a persistence bug, because cards
      // still ARRIVE live through `buildCardElement`, which never
      // touches this function.
      //
      // Measured in the browser: 3 `.card-container` elements, 3
      // surviving `script.card-json` tags, before and after calling
      // `hydrateParkCards()` by hand.
      //
      // `chat/index.html` has the same unescape spelled `/<\\\//g` --
      // one backslash more, and correct -- which is exactly why the
      // assistant's thread never had this. A literal string cannot be
      // mis-escaped the same way twice, so this uses one.
      card = JSON.parse(
        scriptEl.textContent.trim().split('<\\/').join('</'));
      if (typeof card === 'string') card = JSON.parse(card);
    } catch (e) { return; }
    if (!card || typeof card !== 'object') return;

    // BUILD FIRST, THEN SWAP, AND NEVER LET ONE CARD TAKE THE PAGE.
    //
    // THE BUG THIS REPLACES, reported as "plan cards disappear after I
    // refresh". The old order was: remove the <script> holding the
    // JSON, then build the HTML from it. A throw anywhere in the
    // builder -- one unexpected field on one card kind out of sixteen
    // -- landed between those two steps, and the result was an EMPTY
    // container whose data had already been deleted. Worse, the throw
    // escaped the forEach, so every card BELOW it was never hydrated
    // either and stayed an invisible <script> tag.
    //
    // So: whole page of cards, gone, from one bad field. In the DB, in
    // the HTML, rendering as nothing. And silent -- an exception in a
    // DOMContentLoaded handler leaves no trace on the page at all,
    // which is why this survived being looked at three times.
    //
    // The three changes are independent and all three are needed: the
    // JSON outlives the build, a failure is contained to its own card,
    // and a failed card still shows something a person can read and
    // report rather than a blank.
    let html;
    try {
      // EVERY KIND, through one renderer. This used to branch on
      // `kernel_park` and give anything else a title and a body, which
      // was the same allowlist thinking that stopped sixteen card
      // types reaching the page at all.
      const chrome = cardChrome(card);
      html =
        '<div class="agent-card ' + chrome.colour
          + (card.responded ? ' responded' : '') + '">' +
          '<div class="card-header">' +
            '<span>' + chrome.icon + '</span>' +
            '<span class="font-medium">' +
              escapeHtml(card.agent_name || card.agent || 'Executor') + '</span>' +
            '<span>' + '·' + '</span>' +
            '<span class="font-mono">' +
              escapeHtml(card.story_id || card.task_id || '') + '</span>' +
          '</div>' + buildCardInnerHtml(card) +
          (card.responded ? '<div class="card-responded">Responded</div>' : '') +
        '</div>';
    } catch (err) {
      // READABLE, NOT BLANK. The buttons are gone -- whatever threw may
      // have been what built them -- but the card's own words are
      // plain text and cannot throw, so the person sees what is
      // waiting and can say which card is broken.
      if (window.console) {
        console.error('card failed to render', card && card.card_type, err);
      }
      html =
        '<div class="agent-card">' +
          '<div class="card-header"><span>!</span>' +
          '<span class="font-medium">' +
            escapeHtml(card.agent_name || card.agent || 'Executor') +
          '</span></div>' +
          '<div class="card-title">' +
            escapeHtml(card.title || card.card_type || 'Card') + '</div>' +
          '<div class="card-body">' + escapeHtml(card.body || '') + '</div>' +
          '<div class="card-responded">This card could not be drawn.' +
          ' Reload, and report it if it happens again.</div>' +
        '</div>';
    }
    // ONLY NOW is the JSON safe to drop.
    scriptEl.remove();
    el.innerHTML = html;
  });
}


/* -- EVERY CARD, NOT JUST THE PARKED ONE -----------------------------
 *
 * The first cut of this module carried only the parked-executor card,
 * and the server passed only four card kinds through to it. There are
 * sixteen: approval, completion, question, review, progress, failure,
 * kernel_answer, kernel_verdict, kernel_pr, kernel_stall,
 * git_pr_opened, git_node_error and the rest. Anything not on the list
 * reached the thread and rendered as nothing at all -- which is how a
 * card is provably in the database and invisible to the person it is
 * addressed to.
 *
 * So the allowlist is gone on both sides. `buildCardInnerHtml` renders
 * whatever it is handed: a parked executor gets its buttons, a review
 * gets its verdict block, and an unknown kind gets its title and body,
 * which is a card somebody can read rather than a message that
 * vanished.
 *
 * MOVED HERE FROM chat/index.html, where these also existed twice --
 * `renderAgentCard` for a live push and a near-identical block inside
 * `hydrateChatCards` for a reload. That duplication has already caused
 * one bug the comments there record: "patching only the live path is
 * why the approval card vanished on refresh."
 */
const cardIcons = { question: '❓', progress: '📋', completion: '✅', failure: '❌', approval: '🎯', review: '🔍', kernel_park: '⏸️', kernel_verdict: '🧪', kernel_pr: '🔀', kernel_stall: '🚧', kernel_stall_cleared: '✅' };
const cardColors = {
  question: 'card-question', progress: 'card-progress',
  completion: 'card-completion', failure: 'card-failure', approval: 'card-approval', review: 'card-review',
  kernel_park: 'card-approval',
  kernel_verdict: 'card-review',
  kernel_pr: 'card-completion',
  kernel_stall: 'card-failure',
  // Green, not red. It reports that the red card above it no longer
  // describes the present -- rendering it as another failure would
  // double the alarm it exists to retire.
  kernel_stall_cleared: 'card-completion',
};

function buildReviewHtml(card) {
  const verdictLabels = { approved: 'Approved', changes_required: 'Changes Required', rejected: 'Rejected' };
  const verdict = card.verdict || '';
  const label = verdictLabels[verdict] || verdict;
  const score = card.quality_score ?? '?';
  const findings = (card.findings || []).map(f => `<li>${escapeHtml(f)}</li>`).join('');
  const recs = (card.recommendations || []).map(r => `<li>${escapeHtml(r)}</li>`).join('');
  return `
    <div class="card-review-body">
      <div class="review-task-ref">Re: ${escapeHtml(card.reviewed_task_title || '')}</div>
      <div class="review-verdict-row">
        <span class="review-verdict-badge verdict-${escapeHtml(verdict)}">${escapeHtml(label)}</span>
        <span class="review-score">${score}/100</span>
      </div>
      <div class="review-summary agent-prose">${renderMd(card.summary || '')}</div>
      ${findings ? `<details><summary>Findings (${(card.findings||[]).length})</summary><ul>${findings}</ul></details>` : ''}
      ${recs ? `<details><summary>Recommendations (${(card.recommendations||[]).length})</summary><ul>${recs}</ul></details>` : ''}
      ${card.updated_spec ? `<details class="review-updated-spec"><summary>Updated spec</summary><div class="agent-prose">${renderMd(card.updated_spec)}</div></details>` : ''}
    </div>`;
}
function buildRetryHtml(card) {
  if (!card.retry_run || card.retried) return '';
  return `
    <div class="card-options" data-retry-run="${card.retry_run}">
      <button class="card-option opt-secondary" onclick="retryRun(${card.retry_run})">↻ Retry — I fixed the cause</button>
    </div>`;
}
async function retryRun(eid) {
  const box = document.querySelector(`[data-retry-run="${eid}"]`);
  if (box) box.innerHTML = '<span class="text-2xs text-gray-400 italic">Retrying…</span>';
  try {
    const resp = await fetch(`/goals/${PID}/kernel/executions/${eid}/retry`, {
      method: 'POST', credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ note: 'retried from the story thread' }),
    });
    const j = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(j.detail || `${resp.status} ${resp.statusText}`);
    const what = (j.cleared || []).length
      ? `↻ Retried — cleared ${(j.cleared || []).join('; ')}`
      : '↻ Nothing was holding this run back';
    if (box) box.outerHTML = `<div class="card-responded">${escapeHtml(what)}</div>`;
  } catch (e) {
    if (box) box.innerHTML = `<span class="text-2xs text-red-400">${escapeHtml(e.message)}</span>`;
  }
}
function respondToCard(taskId, optionId, value) {
  // Grey out the card
  const cardEl = document.querySelector(`[data-card-task="${taskId}"] .agent-card`);
  if (cardEl) {
    cardEl.classList.add('responded');
    const optionsEl = cardEl.querySelector('.card-options');
    if (optionsEl) optionsEl.innerHTML = `<div class="card-responded">✓ ${escapeHtml(value)}</div>`;
    const customEl = cardEl.querySelector('.card-custom-input');
    if (customEl) customEl.remove();
  }

  // Show user's response as a bubble
  appendBubble('human', `[Re: ${taskId}] ${value}`, currentUserName);

  // Send via WebSocket
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({
      type: 'card_response',
      task_id: taskId,
      option_id: optionId,
      value: value,
    }));
  }
}

function respondToCardCustom(taskId) {
  const input = document.getElementById(`custom-${taskId}`);
  if (!input || !input.value.trim()) return;
  respondToCard(taskId, 'custom', input.value.trim());
}

function respondToCardReplan(taskId) {
  const input = document.getElementById(`custom-${taskId}`);
  if (!input || !input.value.trim()) {
    showToast('Please type your answer before clicking Reply & Re-plan', 'info');
    return;
  }
  respondToCard(taskId, 'replan', input.value.trim());
}

/**
 * The inside of a card: everything between `<div class="agent-card">`
 * and its close. Shared by the live path, the reload path and the GO
 * thread, so a card looks the same however it arrived.
 */
function buildCardInnerHtml(card) {
  const ct = card.card_type || 'progress';

  const body = ct === 'review' || ct === 'kernel_verdict'
    ? buildReviewHtml(card)
    : ct === 'kernel_park'
    ? buildParkHtml(card)
    : '<div class="card-title">' + escapeHtml(card.title || '') + '</div>' +
      '<div class="card-body"><div class="agent-prose">' +
      renderMd(card.body || '') + '</div></div>';

  let context = '';
  if (card.context) {
    context = '<details class="card-context"><summary>Show details</summary>'
      + '<div class="detail-content agent-prose">'
      + renderMd(card.context) + '</div></details>';
  }

  let artifacts = '';
  if (card.artifacts && card.artifacts.length) {
    artifacts = '<div class="card-artifacts">'
      + card.artifacts.map(function (a) {
          return escapeHtml(typeof a === 'string'
            ? a : (a.name || a.path || ''));
        }).join(' - ')
      + '</div>';
  }

  // A PARKED EXECUTOR'S BUTTONS ARE NOT THE GENERIC ONES. The generic
  // options post to the card-response endpoint, which answers an
  // AGENT's question; a plan approval goes to the kernel with the
  // signed-in human's uid attached, and nothing else may approve one.
  let options = '';
  if (ct === 'kernel_park') {
    options = parkOptionsHtml(card);
  } else if (card.options && card.options.length && !card.responded) {
    options = '<div class="card-options">' + card.options.map(function (opt) {
      const style = opt.style === 'primary' ? 'opt-primary'
                  : opt.style === 'danger' ? 'opt-danger' : 'opt-secondary';
      return '<button class="card-option ' + style
        + '" onclick="respondToCard(&#39;' + escapeHtml(card.task_id)
        + '&#39;,&#39;' + escapeHtml(opt.id)
        + '&#39;,&#39;' + escapeHtml(opt.value) + '&#39;)">'
        + escapeHtml(opt.label) + '</button>';
    }).join('') + '</div>';
  }

  // The free-text reply, for a card that invites one.
  let custom = '';
  if (card.allow_custom && !card.responded && ct !== 'kernel_park') {
    const tid = escapeHtml(card.task_id);
    custom = '<div class="card-custom-input">'
      + '<input type="text" placeholder="Custom answer..." id="custom-' + tid
      + '" onkeydown="if(event.key===&#39;Enter&#39;)respondToCardCustom(&#39;'
      + tid + '&#39;)">'
      + '<button onclick="respondToCardCustom(&#39;' + tid + '&#39;)">Reply</button>'
      + '</div>';
  }

  return body + context + artifacts + options + custom
       + buildRetryHtml(card);
}

/** The icon and colour for a card kind, with a sane fallback. */
function cardChrome(card) {
  const ct = card.card_type || 'progress';
  return { icon: cardIcons[ct] || '📌',
           colour: cardColors[ct] || 'card-progress' };
}

/**
 * A card that arrived live, as a ready-to-insert element.
 *
 * ONE RENDERER FOR BOTH THREADS, and for both ways a card can reach
 * one. Before this the assistant's page had `renderAgentCard` for a
 * live push and a near-identical block inside `hydrateChatCards` for a
 * reload, and the GO's page had a third for reloads and NOTHING for a
 * live push -- the `agent_card` frame was relayed to it and dropped on
 * the floor, so a parked executor's card sat correctly in the database
 * and appeared only if you refreshed.
 *
 * The caller decides where it goes: the assistant's page inserts before
 * its typing indicator, the GO's appends to the transcript.
 */
function buildCardElement(card) {
  const chrome = cardChrome(card);
  const el = document.createElement('div');
  el.className = 'flex justify-start';
  el.setAttribute('data-card-task', card.task_id || '');
  el.setAttribute('data-utc', new Date().toISOString());
  el.innerHTML =
    '<div class="agent-card ' + chrome.colour
      + (card.responded ? ' responded' : '') + '">' +
      '<div class="card-header">' +
        '<span>' + chrome.icon + '</span>' +
        '<span class="font-medium">' +
          escapeHtml(card.agent_name || card.agent || 'Agent') + '</span>' +
        '<span>\u00b7</span>' +
        '<span class="font-mono">' +
          escapeHtml(card.story_id || card.task_id || '') + '</span>' +
      '</div>' + buildCardInnerHtml(card) +
      (card.responded ? '<div class="card-responded">Responded</div>' : '') +
    '</div>';
  return el;
}
