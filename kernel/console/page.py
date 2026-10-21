"""H0v — the page, as one string.

Inlined rather than served from disk so the console is two Python files and
no static-asset plumbing. It is a debugging window; the cost of a build
step here would exceed its value.
"""

PAGE = r"""<!doctype html>
<meta charset="utf-8">
<title>AgentOS console</title>
<style>
  :root {
    --bg:#0f1115; --panel:#161a21; --line:#242a34; --ink:#d8dee9;
    --dim:#7d8799; --acc:#7aa2f7; --ok:#9ece6a; --warn:#e0af68;
    --bad:#f7768e; --park:#bb9af7;
  }
  * { box-sizing:border-box }
  body { margin:0; background:var(--bg); color:var(--ink);
         font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace }
  header { padding:10px 14px; border-bottom:1px solid var(--line);
           display:flex; gap:8px; align-items:center; flex-wrap:wrap }
  header textarea { resize:vertical; min-height:26px;
                    vertical-align:middle }
  input,select,button,textarea {
    background:#0c0e12; color:var(--ink); border:1px solid var(--line);
    border-radius:4px; padding:5px 8px; font:inherit }
  button { cursor:pointer; border-color:var(--acc); color:var(--acc) }
  button:hover { background:#1b2130 }
  .wrap { display:grid; grid-template-columns:300px 1fr; height:calc(100vh - 51px) }
  .list, .main { overflow:auto; padding:10px }
  .list { border-right:1px solid var(--line) }
  .row { padding:7px 8px; border:1px solid transparent; border-radius:4px;
         cursor:pointer }
  .row:hover { background:#1b2130 }
  .row.sel { border-color:var(--acc); background:#141b2b }
  .st { font-weight:700 }
  .RUNNING{color:var(--acc)} .DONE{color:var(--ok)} .FAILED{color:var(--bad)}
  .PARKED_HOT,.PARKED_COLD{color:var(--park)} .VERIFYING{color:var(--warn)}
  .PENDING,.ADMITTED,.DISPATCHING{color:var(--dim)} .CANCELLED{color:var(--dim)}
  .dim { color:var(--dim) }
  section { border:1px solid var(--line); border-radius:6px; padding:10px;
            margin-bottom:12px; background:var(--panel) }
  h3 { margin:0 0 8px; font-size:12px; text-transform:uppercase;
       letter-spacing:.08em; color:var(--dim) }
  .step { display:grid; grid-template-columns:44px 90px 1fr 70px; gap:8px;
          padding:3px 0; border-bottom:1px solid #1b2028 }
  .tool { color:var(--acc) }
  .d1 { padding-left:22px; border-left:2px solid var(--park) }
  .say { color:var(--dim); font-style:italic; padding:2px 0 6px 52px }
  pre { white-space:pre-wrap; margin:0; color:var(--ink) }
  .hop { display:inline-block; padding:2px 6px; margin:2px 4px 2px 0;
         border:1px solid var(--line); border-radius:3px }
  .undeclared { border-color:var(--bad); color:var(--bad) }
  .next { color:var(--ok) }
  a { color:var(--acc) }
  .park { border-left:3px solid var(--park); padding-left:10px; margin:8px 0 }
  .askq { margin:4px 0 6px; font-weight:600 }
  .tried { margin:2px 0 6px 18px; padding:0; color:var(--dim) }
  .plan { margin:4px 0 6px 20px; padding:0 }
  .plan li { margin:2px 0 }
  .why { margin:4px 0 6px; font-style:italic; color:var(--dim) }
  .verdict { border-left:3px solid var(--line); padding-left:10px; margin:8px 0 }
  .verdict.ok { border-color:var(--ok) } .verdict.no { border-color:var(--bad) }
  .concern { color:var(--warn); margin:3px 0 }
  .crit { margin:5px 0 }
  .cv { font-size:11px; padding:1px 6px; border-radius:3px; margin-right:6px;
        border:1px solid var(--line) }
  .cv.pass { color:var(--ok); border-color:var(--ok) }
  .cv.fail { color:var(--bad); border-color:var(--bad) }
  .cv.flake { color:var(--warn); border-color:var(--warn) }
  .cv.error { color:var(--dim) }
  .warnx { color:var(--warn) }
  /* An acceptance must never look like a pass. Warn-coloured, not ok. */
  .accepted { border:1px solid var(--warn); border-left:3px solid var(--warn);
        border-radius:4px; padding:8px; margin:8px 0 }
  .accepted b { color:var(--warn) }
  /* F11 — a check, and what it did BEFORE the work. */
  .chk { border-left:3px solid var(--line); padding:4px 0 4px 8px;
         margin:5px 0 }
  .chk.ok { border-color:var(--ok) } .chk.blocks { border-color:var(--bad) }
  .chk.promote { border-color:var(--warn) }
  .chk.inert { border-color:var(--warn) }
  .lbl { font-size:11px; text-transform:uppercase; letter-spacing:.5px;
         padding:1px 5px; border:1px solid var(--line); border-radius:3px }
  .lbl.goal { color:var(--acc); border-color:var(--acc) }
  .lbl.guard { color:var(--ok); border-color:var(--ok) }
  .lbl.unlabelled { color:var(--warn); border-color:var(--warn) }
  .nocontract { color:var(--bad); border:1px solid var(--bad);
                border-radius:4px; padding:8px }
  .done { border:1px solid var(--ok); border-radius:4px; padding:8px;
          margin-top:8px }
  .amend { border:1px solid var(--line); border-radius:4px; padding:8px }
  .stepnote { margin:5px 0 }
  section.needsyou { border-color:var(--park); box-shadow:0 0 0 1px var(--park) }
  section.needsyou h3 { color:var(--park) }
  .intent { border-left:3px solid var(--line); padding-left:10px; margin:8px 0 }
  .ps { font-size:11px; padding:1px 6px; border-radius:3px; margin-right:6px;
        border:1px solid var(--line) }
  .ps.accepted { color:var(--ok); border-color:var(--ok) }
  .ps.pending { color:var(--warn); border-color:var(--warn) }
  .ps.superseded, .ps.rejected { color:var(--bad); border-color:var(--bad) }
</style>

<header>
  <strong>AgentOS</strong>
  <select id=profile></select>
  <input id=task size=44 placeholder="task — what should it do?">
  <input id=cmd size=22 placeholder="terminal command (errands)">
  <textarea id=checks rows=1 cols=40 spellcheck=false
    placeholder="checks — one command per LINE"></textarea>
  <input id=surface size=26 placeholder="check surface globs (optional)">
  <select id=adapter><option>gemini</option><option>anthropic</option></select>
  <button onclick=start()>start</button>
  <span id=msg class=dim></span>
</header>

<div class=wrap>
  <div class=list id=list></div>
  <div class=main id=main><span class=dim>pick a run</span></div>
</div>

<script>
let sel = null;
// Built as a constant with explicit escapes. Written inline as a
// multi-line string it kept arriving with REAL newlines in it,
// which is a SyntaxError that kills the whole script.
const ACCEPT_WARNING =
  'This finishes the story WITHOUT the Verifier passing it.' + '\n\n' +
  'The refusal is not erased and this is not recorded as a pass - the ' +
  'story will show as accepted by you, with your reason, from now on.'
  + '\n\n' +
  'Continue?';

const NO_CHECKS_WARNING =
  'No checks.' + '\n\n' +
  'This story can work and commit, and can NEVER be judged - it ' +
  'will claim completion and sit in VERIFYING until you amend it ' +
  'or cancel it.' + '\n\n' +
  'Start it anyway?';
let d0 = null;   // the detail last rendered, for amend()

async function j(u, o) {
  const r = await fetch(u, o);
  return r.json();
}

async function boot() {
  const ps = await j('/api/profiles');
  document.getElementById('profile').innerHTML = ps.map(p =>
    `<option>${p.kind}/${p.domain}</option>`).join('');
  tick();
  setInterval(tick, 1500);
}

async function start() {
  const body = {
    profile: document.getElementById('profile').value,
    task: document.getElementById('task').value,
    terminal_command: document.getElementById('cmd').value,
    adapter: document.getElementById('adapter').value,
    // One check per LINE. It was `;` separated, and a shell command
    // routinely contains a semicolon — so two checks silently became
    // one, visible only as "1 check(s)" in a note somebody had to
    // notice. The field is a textarea for exactly this reason.
    checks: document.getElementById('checks').value,
    surface: document.getElementById('surface').value,
  };
  // A story with no checks can work, commit, claim completion and then
  // sit in VERIFYING for ever, because nothing defines done for it. The
  // console USED to mention this in small grey text after the fact,
  // which is where a warning goes to be ignored — a real run was
  // started, finished its work and stuck, and the note had been on
  // screen the whole time.
  if (!body.checks.trim() &&
      !confirm(NO_CHECKS_WARNING)) {
    document.getElementById('msg').textContent =
      'not started — give it at least one check';
    return;
  }
  document.getElementById('msg').textContent = 'starting…';
  const r = await j('/api/start', {method:'POST', body:JSON.stringify(body)});
  document.getElementById('msg').textContent =
    r.error ? r.error
            : `#${r.execution_id} on port ${r.bridge_port}` +
              (r.contract ? ` · ${r.contract}` : '');
  if (r.execution_id) sel = r.execution_id;
  tick();
}

// A plan verdict, with the person's notes attached to the steps they
// are about. Three verdicts, not two: `revise` keeps the approach and
// asks for a different plan, and conflating it with `decline` told the
// model to abandon something a person actually wanted.
async function answerPlan(id, verdict) {
  const notes = [];
  document.querySelectorAll(`[id^="sn${id}_"]`).forEach(el => {
    const v = (el.value||'').trim();
    if (v) notes.push({step: parseInt(el.id.split('_')[1], 10), note: v});
  });
  const el = document.getElementById('ans' + id);
  const r = await j('/api/answer', {method:'POST', body:JSON.stringify(
    {request_id:id, answer:(el ? el.value : ''), verdict, step_notes:notes,
     who:'console'})});
  // An approval can still be REFUSED -- H8a wants a human uid, and a
  // plan can race another answer. Swallowing that would leave the person
  // looking at an unchanged page with no idea why nothing happened,
  // which is how the earlier version of this button behaved when the
  // server errored. (F11's own refusal retired at E9; this is not it.)
  if (r && r.ok === false) { alert('Not approved: ' + r.error); return; }
  if (el) el.value = '';
  tick();
}

async function answer(id, declined) {
  const el = document.getElementById('ans' + id);
  await j('/api/answer', {method:'POST', body:JSON.stringify(
    {request_id:id, answer:el.value, declined:!!declined})});
  el.value = '';
  tick();
}

// Speak as the assistant. Goes through the mailbox like a real one, so
// what you see here is the mechanism, not a console-only shortcut.
async function propose(kind) {
  if (!sel) return;
  const el = document.getElementById('say');
  const text = (el.value || '').trim();
  if (!text && kind !== 'cancel') return;
  const out = document.getElementById('saymsg');
  out.textContent = 'queued…';
  // A fresh key each time: two identical steers a minute apart are two
  // intents. The key is for retries of ONE send, which is the browser's
  // job, not the person's.
  const r = await j('/api/propose', {method:'POST', body:JSON.stringify(
    {execution_id: sel, kind, text, key: kind + '-' + sel + '-' + Date.now(),
     who: 'console-assistant'})});
  out.textContent = r.ok ? r.note : `${r.code||'error'}: ${r.error||''}`;
  if (r.ok) el.value = '';
  tick();
}

// Re-scope: MORE WORK IN THE SAME STORY. The contract was satisfied,
// not wrong, so this writes a NEW one and puts the execution back to
// PENDING — through admission, which re-claims the slot the story
// released when it finished. The journal, the briefs and the workspace
// all survive, so the next leg does not re-orient from nothing.
async function accept() {
  // Never phrased as 'mark it passed'. The person is overriding a
  // judgement, and the control should say so — including in the
  // confirm, because this is the one act that finishes a story
  // without evidence.
  const reason = (document.getElementById('acreason')||{}).value || '';
  const out = document.getElementById('acmsg');
  if (reason.trim().length < 10) {
    out.textContent = 'say why — this is the only record there will be';
    return;
  }
  if (!confirm(ACCEPT_WARNING)) return;
  out.textContent = 'accepting…';
  const r = await j('/api/accept', {method:'POST', body:JSON.stringify(
    {execution_id: d0.execution.id, reason: reason})});
  out.textContent = r.ok ? r.detail : ('refused: ' + r.error);
  if (r.ok) { const b = document.getElementById('acreason'); if (b) b.value=''; }
  refresh();
}

async function amend() {
  if (!sel) return;
  const scope = (document.getElementById('amscope').value||'').trim();
  const raw = document.getElementById('amchecks').value;
  const out = document.getElementById('ammsg');
  const current = ((d0 && d0.contract && d0.contract.checks) || []).join('\n');
  // Checks are sent only when they DIFFER from what is in force. Sending
  // an unchanged set would issue a contract version that changes
  // nothing, and a history full of no-op versions is a history nobody
  // reads.
  const checks = (raw.trim() && raw.trim() !== current.trim()) ? raw : null;
  if (!scope && checks === null) {
    out.textContent = 'change something: new scope, new checks, or both';
    return;
  }
  out.textContent = 'amending…';
  const body = {execution_id: sel, scope, who: 'console'};
  if (checks !== null) body.checks = checks;
  const r = await j('/api/amend', {method:'POST', body:JSON.stringify(body)});
  out.textContent = r.ok ? r.detail : (r.error || 'failed');
  if (r.ok) {
    // Cleared so the next render re-fills them from the NEW contract.
    // With the restore now unconditional, a box left holding the old
    // text would keep it for ever and the next amend would send stale
    // checks.
    document.getElementById('amscope').value = '';
    document.getElementById('amchecks').value = '';
  }
  tick();
}

// "What if I want more." A finished story is not the end of the work:
// the workspace and its commits survive, so a follow-on starts from
// where this one stopped rather than from nothing. A NEW story rather
// than reopening this one — this one's contract was satisfied, and
// reopening it would mean judging new work against an old definition of
// done.
async function followOn() {
  const el = document.getElementById('amscope');
  const task = ((el && el.value) || '').trim();
  if (!task) return;
  document.getElementById('msg').textContent = 'starting a follow-on…';
  const r = await j('/api/start', {method:'POST', body:JSON.stringify({
    profile: document.getElementById('profile').value,
    task, adapter: document.getElementById('adapter').value,
    checks: (document.getElementById('amchecks')||{}).value ||
            document.getElementById('checks').value,
    surface: document.getElementById('surface').value})});
  document.getElementById('msg').textContent =
    r.error ? r.error
            : `#${r.execution_id} — a separate story, same workspace`;
  if (r.execution_id) sel = r.execution_id;
  tick();
}

async function tick() {
  const xs = await j('/api/executions');
  document.getElementById('list').innerHTML = xs.map(x => `
    <div class="row ${x.id===sel?'sel':''}" onclick="sel=${x.id};tick()">
      <div><span class="st ${x.state}">${x.state}</span>
           <span class=dim>#${x.id}</span></div>
      <div class=dim>${x.kind}/${x.domain} · leg ${x.generation+1} ·
           ${x.steps} steps${x.waiting?' · <b>waiting</b>':''}</div>
      <div class=dim>${(x.task||'').slice(0,52)}</div>
    </div>`).join('') || '<span class=dim>nothing yet</span>';
  if (sel) render(await j('/api/execution/' + sel));
}

function render(d) {
  if (!d.execution) return;
  const e = d.execution, m = d.machine || {};
  const acc = d.acceptance;
  const hops = (d.transitions||[]).map(t =>
    `<span class="hop ${t.declared?'':'undeclared'}"
       title="${t.declared?'declared by machine v'+t.machine_version
              :'NOT in machine v'+t.machine_version}">
       ${t.from ? t.from+' → ' : ''}${t.to}</span>`).join('');
  const next = (d.next||[]).map(t =>
    `<span class="hop next">${t.to} <span class=dim>(${t.predicate})</span></span>`
    ).join('') || '<span class=dim>none — terminal</span>';

  const steps = (d.journal||[]).map(s => `
    <div class="step ${s.depth?'d1':''}">
      <span class=dim>${s.seq}</span>
      <span class=tool>${s.tool}</span>
      <span>${esc(summarise(s.args))}${s.artifact
        ? ` <a href="/api/artifact/${s.artifact}" target=_blank>artifact</a>`:''}</span>
      <span class=dim>${s.tokens||''}</span>
    </div>` + (s.text ? `<div class=say>${esc(s.text)}</div>` : '')).join('');

  // Two shapes of waiting, two things a person needs in front of them.
  //
  // An AUTHORIZATION must show the plan. An approval screen that does not
  // render what is being approved is worse than no screen: it collects a
  // yes about something the person never saw. This panel shipped without
  // it for exactly one run.
  //
  // A QUESTION must show the evidence — §1.6, the ask is evidence, not a
  // claim: what was tried, what it blocks, and what the executor will
  // assume if nobody replies.
  const evidence = p => {
    const pay = p.payload || {};
    if (p.kind === 'plan_approval' || (pay.steps || []).length) {
      const steps = pay.steps || [];
      return (pay.rationale ? `<div class=why>${esc(pay.rationale)}</div>` : '')
        + (steps.length
            ? `<ol class=plan>` + steps.map(x => `<li>${esc(String(x))}</li>`).join('')
              + `</ol>`
            : `<div class=dim>no steps submitted — nothing to approve</div>`);
    }
    const a = pay.attempts || [];
    return (a.length ? `<div class=dim>tried first:</div><ul class=tried>`
              + a.map(x => `<li>${esc(String(x))}</li>`).join('') + `</ul>` : '')
      + (p.blocking_on ? `<div class=dim>blocked on: ${esc(p.blocking_on)}</div>` : '')
      + (p.proposed ? `<div class=dim>will assume if you say nothing:</div>`
                    + `<pre>${esc(p.proposed)}</pre>` : '');
  };
  // F11 — the checks this plan carries, which approving it also
  // approves. WHAT THEY DO against the pre-work tree is no longer shown,
  // because nothing measures it any more: E9 retired the dry run (plan
  // 5.6) after one cost a full `verify_claim` per approval. The labels
  // are the model's own claim about each check and are displayed as
  // such -- a person reading them is the whole of the gate now.
  const checksOf = p => {
    const checks = p.checks || [];
    if (!checks.length) return '';
    return `<div style="margin-top:8px">
      <div class=dim><b>how this will be proven:</b> ${checks.length}
        check(s), as written by the executor and unverified</div>`
      + checks.map(c =>
          `<div class=chk><span class="lbl ${c.label||'unlabelled'}"
            >${c.label||'unlabelled'}</span>
            <code>${esc(c.run)}</code></div>`).join('') + '</div>';
  };

  const parks = (d.parks||[]).map(p => `
    <div class=park>
      <div><b>${p.kind}</b> <span class=dim>${p.status} · ${p.at}</span></div>
      ${p.kind==='plan_approval' ? '' :
        `<div class=askq>${esc(p.question||'')}</div>`}
      ${evidence(p)}
      ${checksOf(p)}
      ${p.status==='open' ? (p.kind==='plan_approval' ? `
        <div class=dim style="margin:6px 0 2px">
          A note against a step goes to the executor attached to that
          step. <b>Revise</b> is not <b>decline</b>: revise keeps the
          approach and asks for a different plan.</div>
        ${((p.payload||{}).steps||[]).map((st,i) => `
          <div class=stepnote>
            <span class=dim>${i+1}.</span> ${esc(st)}
            <input id="sn${p.id}_${i+1}" placeholder="note on this step"
              style="width:100%;margin-top:2px">
          </div>`).join('')}
        <textarea id="ans${p.id}" rows=2 style="width:100%;margin-top:6px"
          placeholder="anything about the plan as a whole"></textarea>
        <button onclick="answerPlan(${p.id},'approve')">approve</button>
        <button onclick="answerPlan(${p.id},'revise')">revise</button>
        <button onclick="answerPlan(${p.id},'declined')">decline</button>` : `
        <textarea id="ans${p.id}" rows=2 style="width:100%"
          placeholder="your answer — this un-parks the run"></textarea>
        <button onclick="answer(${p.id},false)">answer</button>
        <button onclick="answer(${p.id},true)">decline</button>`)
        : `<div class=dim>${p.status==='declined'?'declined':'answered'} by `
          + `${p.answered_by||'—'}:</div>
           <pre>${esc(p.answer||'')}</pre>`}
    </div>`).join('') || '<span class=dim>none</span>';

  // What the assistant said, and what became of it. `pending` on a
  // running leg means it has not reached a step boundary YET; on a parked
  // one it means there is no leg to reach.
  // A status word is not an explanation. `pending` is correct while a
  // run is parked and reads as broken — somebody said something, the
  // console said "pending", and nothing appeared to happen. Say WHEN it
  // will land.
  const willLand = st => ({
    RUNNING:     'lands at the next step boundary, within a step or two',
    PARKED_HOT:  'waits — this run is parked on a human. It lands the ' +
                 'moment you answer, at the first step of the next leg',
    PARKED_COLD: 'waits until this run is resumed',
    VERIFYING:   'waits for the verdict. If the claim passes, the story ' +
                 'will NOT finish while this is undelivered — it goes ' +
                 'back to work so this lands',
    PENDING:     'waits for admission, then lands on the first step',
    ADMITTED:    'waits for a sandbox, then lands on the first step',
    DISPATCHING: 'waits for a sandbox, then lands on the first step',
  }[st] || 'this run will not take another leg, so this will not be ' +
           'delivered — amend the story instead');

  const said = (d.proposals||[]).map(p => `
    <div class=intent>
      <span class="ps ${p.status}">${p.status}</span>
      <b>${p.kind}</b>
      <span class=dim>${p.actor} · ${p.at}</span>
      <div>${esc(p.text||'')}</div>
      ${p.status==='pending' && p.kind!=='completion'
        ? `<div class=dim>↳ ${willLand(e.state)}</div>` : ''}
    </div>`).join('') || '<span class=dim>nothing said yet</span>';

  // What the Verifier decided, and what it could see while deciding.
  // A verdict alone is not actionable: a refusal needs the criterion
  // that failed and its output, and a PASS needs to say whether
  // anything could not be checked.
  const verdicts = (d.verdicts||[]).map(v => `
    <div class="verdict ${v.passed?'ok':'no'}">
      <div><b>${v.passed?'PASSED':'REFUSED'}</b>
        <span class=dim>claim ${v.claim_seq} · leg ${v.generation+1}
        · ${v.by} · ${v.at}</span></div>
      ${(v.concerns||[]).map(c =>
        `<div class=concern>${esc(c)}</div>`).join('')}
      ${(v.criteria||[]).map(c => `
        <div class=crit>
          <span class="cv ${c.passes}">${c.passes}</span>
          <code>${esc(c.run)}</code>
          ${c.run_index ? `<span class=dim>run ${c.run_index+1}</span>`:''}
          ${c.output && c.passes!=='pass'
            ? `<pre>${esc(c.output.slice(-900))}</pre>` : ''}
        </div>`).join('')}
    </div>`).join('') || '<span class=dim>not adjudicated yet</span>';

  const contract = d.contract ? `
    <div class=dim>image <code>${esc(d.contract.image)}</code> ·
      surface ${d.contract.surface_hashed
        ? `hashed over ${d.contract.surface.length} glob(s)`
        : '<b class=warnx>NOT hashed — an agent that edits a test is not caught</b>'}
      · approved by ${esc(d.contract.approved_by||'—')}</div>
    <ol class=plan>${(d.contract.checks||[]).map(c =>
      `<li><code>${esc(c)}</code></li>`).join('')}</ol>`
    : `<div class=nocontract><b>No contract — this story can never
        finish.</b> Nothing defines done for it, so the adjudicator
        reports it as unjudgeable rather than passing it, every tick,
        for ever. Put checks in the box below and amend: it gets its
        first contract and can be judged.</div>`;

  const briefs = (d.briefs||[]).map(b => `
    <div><span class=dim>after leg ${b.generation+1}</span>
    <pre>${esc(b.rendered)}</pre></div>`).join('')
    || '<span class=dim>no boundary yet</span>';

  // The panel re-renders on a timer. Every textarea in it — the steer box
  // and every park answer — would empty itself mid-sentence, so their
  // contents and the caret are carried across the re-render.
  d0 = d;
  const kept = {}, active = document.activeElement;
  let refocus = null, caret = 0;
  for (const t of document.querySelectorAll('#main textarea')) {
    kept[t.id] = t.value;
    if (t === active) { refocus = t.id; caret = t.selectionStart; }
  }
  const draft = kept['say'] || '';
  document.getElementById('main').innerHTML = `
    <section>
      <h3>execution #${e.id} · ${e.kind}/${e.domain}</h3>
      <div><span class="st ${e.state}">${e.state}</span>
        ${acc ? `<span class="st warnx" title="accepted by a person, not passed by the Verifier">&nbsp;· ACCEPTED, NOT VERIFIED</span>` : ''}
        <span class=dim>· leg ${e.generation+1} · machine v${e.machine_version}
        ${d.parked?'· parked':''} ${d.terminal?'· terminal':''}</span></div>
      <div class=dim style="margin-top:6px">${esc(e.task||'')}</div>
      ${e.terminal_command ? `<div class=dim>terminal: <code>${esc(e.terminal_command)}</code>
        ${e.terminal_exit_code!==null?`→ exit ${e.terminal_exit_code}`:''}</div>`:''}
      ${e.tokens_to_first_write?`<div class=dim>orientation floor:
        ${e.tokens_to_first_write} tokens to first write</div>`:''}
    </section>
    <section>
      <h3>state machine — path taken</h3>
      <div>${hops || '<span class=dim>no transitions</span>'}</div>
      <h3 style="margin-top:10px">legal from ${e.state}</h3>
      <div>${next}</div>
      <div class=dim style="margin-top:6px">
        A red hop is a transition the declared machine does not contain.</div>
    </section>
    <section>
      <h3>say something as the assistant</h3>
      <textarea id=say rows=2 style="width:100%"
        placeholder="a steer — it lands at the executor's next step boundary"
      >${esc(draft)}</textarea>
      <div style="display:flex;gap:6px;margin-top:6px;flex-wrap:wrap">
        <button onclick="propose('steer')">steer</button>
        <button onclick="propose('replan')">withdraw the plan</button>
        <button onclick="propose('cancel')">cancel the run</button>
        <span id=saymsg class=dim></span>
      </div>
    </section>
    <section><h3>what "done" means here</h3>${contract}</section>
    <section><h3>the verdict</h3>${verdicts}
      ${acc ? `<div class=accepted>
        <b>Accepted by ${esc(acc.by)}</b>
        <span class=dim>· out of ${acc.from_state} · ${acc.at}</span>
        <div style="margin-top:4px">${esc(acc.reason)}</div>
        <div class=dim style="margin-top:4px">${acc.verdict_id
          ? 'This overrode the refusal of claim ' + acc.claim_seq +
            '. That verdict is unchanged — it still says refused.'
          : 'Nothing was ever verified for this story.'}</div>
      </div>` : ''}
      ${e.terminal && e.state !== 'DONE' ? '' : `
        <div class="${e.state==='DONE'?'done':'amend'}">
          ${e.state==='DONE' ? (acc ? `<b>Accepted, not verified.</b>
            The Verifier did not pass this story — a person finished it
            anyway. The refusal above still stands and was not
            rewritten.<br>` : `<b>The criteria pass.</b> That is not a
            merge — a human still approves that, and a Reviewer would
            look at the diff first.<br>`) : ''}
          <b>Want more, or something different?</b>
          <div class=dim>Say it at any point. Mid-leg it lands at the next
            step boundary; parked, when the run resumes; finished, it
            reopens the story. Changing the checks issues a new contract
            version — the old verdicts stay true. Either way the plan
            approval is withdrawn and a person approves the next one.</div>
          <textarea id=amscope rows=2 style="width:100%;margin-top:6px"
            placeholder="what else should happen? (optional if you are only changing checks)"
          ></textarea>
          <textarea id=amchecks rows=3 style="width:100%;margin-top:4px"
            placeholder="checks — the COMPLETE new set, one per line"
          >${esc((d.contract ? (d.contract.checks||[]) : []).join('\n'))}</textarea>
          <div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">
            <button onclick="amend()">amend this story</button>
            <button onclick="followOn()">start a separate story</button>
            <span id=ammsg class=dim></span>
          </div>
        </div>`}
      ${d.terminal ? '' : `
        <div class=accepted>
          <b>Or finish it anyway.</b>
          <div class=dim>Use this when the CHECK is wrong, not the work —
            a criterion that needs something the sandbox cannot have, or
            a bar that turned out not to matter. It does not erase the
            refusal and it is not recorded as a pass: the story shows as
            accepted by you, with this reason, from now on. If the check
            is merely out of date, amend it instead — that keeps the
            story judgeable.</div>
          <textarea id=acreason rows=2 style="width:100%;margin-top:6px"
            placeholder="why is this finished without the Verifier? (required)"
          ></textarea>
          <div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">
            <button onclick="accept()">accept over the Verifier</button>
            <span id=acmsg class=dim></span>
          </div>
        </div>`}
    </section>
    <section><h3>what the assistant said</h3>${said}</section>
    <section${(d.parks||[]).some(p=>p.status==='open')?' class=needsyou':''}>
      <h3>waiting on a human${(d.parks||[]).some(p=>p.status==='open')
        ? ' — IT IS WAITING FOR YOU' : ''}</h3>${parks}</section>
    <section><h3>journal</h3>${steps || '<span class=dim>no steps</span>'}</section>
    <section><h3>what each leg handed on</h3>${briefs}</section>`;
  // What was in the box wins over what the template pre-fills.
  //
  // This restored a draft only into an EMPTY box, which worked for boxes
  // that start empty and silently defeated the one that does not: the
  // amend panel pre-fills the current checks so they can be EDITED, so
  // every 1.5s tick found the box non-empty, skipped the restore, and
  // replaced the person's typing with the contract's version. The field
  // existed to be edited and could not be.
  //
  // A box the template pre-fills gets that value on its FIRST render —
  // there is no kept entry for an element that did not exist yet — and
  // keeps whatever it holds from then on.
  for (const [id, v] of Object.entries(kept)) {
    const el = document.getElementById(id);
    if (el) el.value = v;
  }
  if (refocus) {
    const el = document.getElementById(refocus);
    if (el) { el.focus(); el.setSelectionRange(caret, caret); }
  }
}

function summarise(a) {
  if (!a) return '';
  for (const k of ['path','command','pattern','message','name','url',
                   'question','task','artifact_id','job_id'])
    if (a[k]) return String(a[k]).slice(0, 120);
  return Object.keys(a).length ? JSON.stringify(a).slice(0, 120) : '';
}
function esc(s) {
  return String(s??'').replace(/[&<>]/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
}
boot();
</script>
"""
