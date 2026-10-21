/**
 * Pause / Resume / Cancel a run, and watch what it is doing.
 *
 * WHY THIS IS A FILE. It lived inside chat/index.html, so it existed
 * for the assistant's thread and nowhere else. When an approved
 * autonomous errand's conversation moved to the GO's thread, the bar,
 * the ticker and all three buttons stayed behind -- and the only way to
 * pause a run was to ask the GO, which relayed it as an instruction the
 * executor turned into a plan and parked on.
 *
 * NOTHING ON THE SERVER HAD TO CHANGE. The endpoint is
 * `/goals/{pid}/threads/{thread_id}/run`: thread-scoped, it derives the
 * story from the thread and gates on goal membership. It does not know
 * or care which agent owns the thread.
 *
 * WHAT A HOST PAGE MUST PROVIDE, before loading this:
 *
 *     RUN_THREAD_ID   the thread whose run this is ("" disables it all)
 *     RUN_BASE        `/goals/${pid}/threads/${RUN_THREAD_ID}/run`
 *     escapeHtml      any HTML-escaper
 *
 * plus the markup in _shared/run_controls.html and _shared/run_ticker.html.
 * Those two carry Jinja, and the two consts above do too, which is why
 * they stay in the page rather than moving here.
 */
let runPollTimer = null;
let RUN_STORY_LABEL = '';

function runSetNote(text, tone) {
  const el = document.getElementById('run-note');
  if (!el) return;
  el.textContent = text || '';
  el.className = tone === 'bad' ? 'text-rose-400'
               : tone === 'busy' ? 'text-amber-300' : 'text-gray-500';
}

async function refreshRunControls() {
  if (!RUN_THREAD_ID) return;
  const bar = document.getElementById('run-controls');
  if (!bar) return;
  let c;
  try {
    const res = await fetch(`${RUN_BASE}/controls`, {cache: 'no-store'});
    if (!res.ok) return;
    c = await res.json();
  } catch (e) { return; }

  if (!c.available) { bar.classList.add('hidden'); return; }
  bar.classList.remove('hidden');
  document.getElementById('run-state').textContent = (c.state || '').toLowerCase();
  // Named in the dialog so a person can see WHICH story they are about to
  // stop — the control is thread-scoped, and that should be visible.
  RUN_STORY_LABEL = c.story_title || c.story_id || '';
  document.getElementById('run-pause-btn').disabled = !c.can_pause;
  document.getElementById('run-resume-btn').disabled = !c.can_resume;
  document.getElementById('run-cancel-btn').disabled = !c.can_cancel;
  // A pause is not instant — it lands at the executor's next step boundary.
  // Saying "paused" before the state moves would be a claim we cannot back.
  if (c.state === 'RUNNING') runSetNote('working', '');
  else if (c.can_resume) runSetNote('paused — waiting on you', 'busy');
  // A DISABLED BUTTON MUST SAY WHY. In VERIFYING there is no leg and no step
  // boundary — the adjudicator is running the checks, not a model — so there
  // is nothing to pause. Cancel still works. Without this the greyed Pause
  // reads as broken.
  else if (c.state === 'VERIFYING')
    runSetNote('verifying — no leg to pause; Cancel still works', '');
  else if (c.state === 'PENDING' || c.state === 'ADMITTED')
    runSetNote('waiting for a slot — not started yet', '');
  else runSetNote('', '');
}

async function runPost(action, body) {
  const res = await fetch(`${RUN_BASE}/${action}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body || {}),
  });
  let data = {};
  try { data = await res.json(); } catch (e) {}
  if (!res.ok) {
    // 409 means the RUN was in the wrong state, not that the request was bad —
    // usually because it moved between the poll and the click. Say which,
    // in the dialog's own voice, because a red word next to a state field is
    // what made this look like a button that does nothing.
    const why = data.detail || `could not ${action}`;
    runSetNote(why, 'bad');
    if (res.status === 409) window.alert(why);
    return null;
  }
  return data;
}

// EVERY CONTROL GOES THROUGH THE DIALOG. Pause and Cancel are both easy to
// press by accident — they sit next to each other on a bar that appears on its
// own while you are reading — and one of them cannot be undone. So none of the
// three fires straight from a click; each states what it will do, and Cancel
// says plainly that it is final.
//
// The confirm button is never autofocused, so Enter cannot complete an action
// the person only opened to read.
const RUN_DIALOG = {
  pause: {
    title: 'Pause this executor?',
    body: 'It stops at its next step boundary — usually seconds, occasionally ' +
          'as long as one tool call. The sandbox stays up and the journal is ' +
          'kept, so Resume carries on from where it stopped. Nothing is lost.',
    label: 'Why are you pausing? (optional, recorded with the hold)',
    go: 'Pause', cls: 'bg-amber-600 hover:bg-amber-500', warn: '',
  },
  cancel: {
    title: 'Cancel this run for good?',
    body: 'A running leg stops at its next step boundary; a parked one ends on ' +
          'the next sweep. Any open question is closed.',
    warn: 'This cannot be undone. A cancelled run has no way back — the story ' +
          'stays and can be started again, but this run and its progress are over.',
    label: 'Why are you cancelling? (optional, recorded on the run)',
    go: 'Cancel the run', cls: 'bg-rose-700 hover:bg-rose-600',
  },
  resume: {
    title: 'Resume this executor',
    body: 'It continues from where it stopped — it will not redo the last leg. ' +
          'Anything you write below is delivered as a STEER: the executor ' +
          'reads it before its next model call and acts on it, and it stays ' +
          'in front of the model for the rest of the run.',
    warn: 'This is the next instruction, and it can change the plan. If what ' +
          'you ask for changes what "done" means, the executor writes a new ' +
          'plan including it and parks for your approval — so it lands, but ' +
          'you get to see it as a plan first.',
    label: 'What should it do? (optional — blank just resumes)',
    go: 'Resume', cls: 'bg-emerald-600 hover:bg-emerald-500',
  },
};

function runAsk(action) {
  const spec = RUN_DIALOG[action];
  const dlg = document.getElementById('run-dialog');
  if (!dlg || !spec) return Promise.resolve(null);

  document.getElementById('run-dialog-title').textContent = spec.title;
  document.getElementById('run-dialog-body').textContent = spec.body;
  document.getElementById('run-dialog-label').textContent = spec.label;
  document.getElementById('run-dialog-story').textContent =
    RUN_STORY_LABEL ? `Story: ${RUN_STORY_LABEL}` : '';
  const warn = document.getElementById('run-dialog-warn');
  warn.textContent = spec.warn || '';
  warn.classList.toggle('hidden', !spec.warn);
  const go = document.getElementById('run-dialog-go');
  go.textContent = spec.go;
  go.className = 'px-3 py-1.5 rounded text-xs font-semibold text-white ' + spec.cls;
  const box = document.getElementById('run-dialog-text');
  box.value = '';

  return new Promise((resolve) => {
    dlg.addEventListener('close', function once() {
      dlg.removeEventListener('close', once);
      resolve(dlg.returnValue === 'go' ? (box.value || '') : null);
    });
    dlg.showModal();
    // Focus the note, never the confirm button.
    box.focus();
  });
}

async function runControl(action) {
  const text = await runAsk(action);
  if (text === null) return;                 // dismissed — nothing happened
  // The bar can be up to one poll stale, and the dialog adds however long the
  // person spent reading it. Re-check before submitting so a run that moved
  // in the meantime is reported as such instead of 409-ing behind the scenes.
  await refreshRunControls();
  const btn = document.getElementById(`run-${action}-btn`);
  if (btn && btn.disabled) {
    runSetNote(`the run moved on — it can no longer be ${action}d`, 'bad');
    return;
  }
  runSetNote(action === 'pause' ? 'pausing…' : 'cancelling…', 'busy');
  const out = await runPost(action, {reason: text});
  if (out) runSetNote(out.detail || '', 'busy');
  refreshRunControls();
}

async function runResume() {
  const text = await runAsk('resume');
  if (text === null) return;
  runSetNote('resuming…', 'busy');
  const out = await runPost('resume', {message: text});
  if (out) runSetNote(out.detail || 'resumed', '');
  refreshRunControls();
}

// ── Live executor activity ─────────────────────────────────────────────
//
// A story runs for minutes between cards, and from the thread that looks
// exactly like a story that has stalled. This says "read this, wrote that,
// ran this" — enough to see movement, not enough to be a second copy of the
// work.
//
// IT NEVER TOUCHES THE ASSISTANT. Nothing here posts a message, so nothing
// enters the assistant's context and nothing wakes it. Lines are appended to
// the DOM and live only as long as the page does.
//
// OFF BY DEFAULT, and the choice is remembered per thread — somebody who
// wants to watch one story usually does not want every story narrating.
const RUN_TICKER_KEY = `agentos.ticker.${RUN_THREAD_ID}`;
let runTickerOn = false;
let runTickerSeq = 0;
let runTickerTimer = null;
let runTickerCheck = 0;

// One colour per FAMILY, not per tool: a person is scanning for a change of
// kind — it started writing, it started running things — not memorising a
// palette of fifteen.
// Tuned for the DARK surface this sits on: the 300/400 band reads clearly on
// `bg-bg`, while the 500/600 band that looks right on white goes muddy here.
const TICKER_TONE = {
  read:   'text-sky-400',        // looking around
  write:  'text-amber-400',      // changing files
  exec:   'text-violet-400',     // running things
  commit: 'text-emerald-400',    // landing work
  plan:   'text-indigo-300',     // 300, not 400 — 400 sits too close to violet
  ask:    'text-rose-400',       // wants a person
  // Alive but not acting. Its own tone, not `other`, so a run that is ONLY
  // ever idle reads as a pattern rather than as silence.
  idle:   'text-yellow-500',
  // EVERY UNRECOGNISED TOOL, one fixed colour. A tool added to the kernel
  // tomorrow appears here the day it ships — named, uncoloured-by-family, and
  // never an error. Slate rather than gray so it is still distinguishable
  // from the detail text beside it.
  other:  'text-slate-400',
  // Verification outcomes. Not tools — the adjudicator writes these — but
  // they share the strip, and they are the lines a person is waiting for.
  pass:   'text-emerald-400',
  fail:   'text-red-400',
};

// How a run's state reads to a person waiting on it. Phrased as what is
// happening, not as the machine's state name — "verifying" is a word people
// use; PARKED_HOT is not.
const TICKER_PHASE = {
  RUNNING:      () => 'executor working…',
  DISPATCHING:  () => 'starting the executor…',
  VERIFYING:    (n) => n ? `verifying · running ${n} check${n === 1 ? '' : 's'}…`
                         : 'verifying…',
  PARKED_HOT:   () => 'parked — waiting for you',
  PARKED_COLD:  () => 'parked — waiting for you',
  DONE:         () => 'finished',
  CANCELLED:    () => 'cancelled',
  FAILED:       () => 'failed',
};

function tickerPhase(text) {
  const el = document.getElementById('run-ticker-phase');
  if (el) el.textContent = text || '';
}

function tickerLines() { return document.getElementById('run-ticker-lines'); }

function runTickerToggled() {
  const box = document.getElementById('run-ticker-toggle');
  const strip = document.getElementById('run-ticker');
  runTickerOn = !!(box && box.checked);
  try { localStorage.setItem(RUN_TICKER_KEY, runTickerOn ? '1' : '0'); } catch (e) {}
  if (!strip) return;
  strip.classList.toggle('hidden', !runTickerOn);
  if (runTickerOn) {
    tickerLines().innerHTML = '';
    tickerPhase('connecting…');
    pollRunTicker(true);
    if (!runTickerTimer) runTickerTimer = setInterval(pollRunTicker, 2000);
  } else {
    clearInterval(runTickerTimer);
    runTickerTimer = null;
    tickerLines().innerHTML = '';
    tickerPhase('');
    runTickerSeq = 0;
    runTickerCheck = 0;
  }
}

async function pollRunTicker(firstCall) {
  if (!runTickerOn || !RUN_THREAD_ID) return;
  // SEED on the first call. Jumping to the tail cursor and waiting, which is
  // what this did, showed an empty strip until the executor's next tool call
  // — and parked, or between legs, or verifying, that never comes.
  const q = firstCall
    ? 'after=0&after_check=0&seed=8'
    : `after=${runTickerSeq}&after_check=${runTickerCheck}`;
  let data;
  try {
    const res = await fetch(`${RUN_BASE}/activity?${q}`, {cache: 'no-store'});
    if (!res.ok) return;
    data = await res.json();
  } catch (e) { return; }

  const lines = tickerLines();
  if (!lines) return;
  if (!data.available) {
    tickerPhase('no run in flight');
    return;
  }

  const fresh = (data.steps || []).concat(data.checks || []);
  for (const s of fresh) appendTickerLine(lines, s);

  runTickerSeq = data.last_seq != null ? data.last_seq : runTickerSeq;
  runTickerCheck = data.last_check != null ? data.last_check : runTickerCheck;

  const phase = TICKER_PHASE[data.state];
  tickerPhase(phase ? phase(data.criteria || 0)
                    : String(data.state || '').toLowerCase());

  if (fresh.length) {
    while (lines.childElementCount > 80) lines.removeChild(lines.firstChild);
    lines.scrollTop = lines.scrollHeight;
  }
}

function appendTickerLine(lines, s) {
  const line = document.createElement('div');
  line.className = 'flex gap-1.5 items-baseline';

  // The dot carries the colour and the name stays legible. Colour on the
  // name alone made a wall of amber whenever the executor wrote three files
  // in a row; a small mark reads as punctuation instead.
  const tone = TICKER_TONE[s.family] || TICKER_TONE.other;
  const dot = document.createElement('span');
  dot.className = `${tone} flex-shrink-0 leading-none`;
  dot.textContent = s.family === 'pass' ? '✓' : s.family === 'fail' ? '✕' : '⏺';

  const name = document.createElement('span');
  name.className = 'text-gray-300 flex-shrink-0';
  name.textContent = s.tool;

  const detail = document.createElement('span');
  detail.className = 'text-gray-500 truncate';
  // textContent, never innerHTML: this is model-written text and a path is
  // a place somebody could put a tag.
  detail.textContent = s.detail || '';

  line.append(dot, name, detail);
  lines.append(line);
}

if (RUN_THREAD_ID) {
  refreshRunControls();
  // 2s, not 5. A leg can finish and the run reach VERIFYING inside one
  // five-second window, so Pause was being offered on a state that had
  // already gone — the click then 409s and reads as a dead button.
  runPollTimer = setInterval(refreshRunControls, 2000);
  try {
    if (localStorage.getItem(RUN_TICKER_KEY) === '1') {
      const box = document.getElementById('run-ticker-toggle');
      if (box) { box.checked = true; runTickerToggled(); }
    }
  } catch (e) {}
}
