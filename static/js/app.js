/**
 * app.js — lightweight helpers used across all pages
 */

// Firebase token refresh lives in base.html's initTokenRefresh() IIFE, not
// here. A duplicate attempt used to sit at the top of this file, but it ran
// before base.html's later inline script had called firebase.initializeApp()
// (Firebase SDK <script> tags load in <head>, well before this file, so
// `typeof firebase !== 'undefined'` was true even though no app had been
// initialized yet) — firebase.auth() threw on every single page load, and
// since this was a top-level statement (not inside a function), the throw
// silently aborted the rest of this script, including the data-toast-submit/
// data-confirm submit listener registered further down. Removed rather than
// reordered: base.html's version already does this correctly (guards with
// `if (!firebase.apps.length) firebase.initializeApp(...)` before calling
// .auth()) and posts to the right endpoint (/auth/refresh) — this copy
// posted to /auth/session, the login-flow endpoint, not a refresh endpoint.

// ── Flash / toast ──────────────────────────────────────────────────────────
// showToast() itself is defined in base.html (shared confirm-modal + toast
// component, loaded on every page after this script) — NotificationManager
// just calls the global.

// Extract a human-readable message from a failed JSON-API response. Shared
// because app/templates/custom_tools/*.html had ~17 call sites each
// re-implementing `const d = await r.json(); ... d.detail || fallback`,
// none of which handled FastAPI/Pydantic's 422 validation-error shape
// (detail: [{loc, msg, type}, ...] — an array, not a string). Coercing that
// array with `||` renders "[object Object]" instead of the actual field
// errors. submitFormWithToast() above has the form-submit equivalent of
// this same fix.
async function extractErrorDetail(resp, fallback) {
  try {
    const d = await resp.json();
    if (Array.isArray(d.detail)) {
      return d.detail.map(item => item.msg || JSON.stringify(item)).join('; ');
    }
    if (d.detail) return d.detail;
  } catch {}
  return fallback;
}

// ── NotificationManager ────────────────────────────────────────────────────
// Handles bursts of simultaneous agent notifications by debouncing the batch.
// All notifications that arrive within DEBOUNCE_MS of each other are grouped
// into a single toast and a single badge re-poll — preventing toast spam and
// redundant badge updates when multiple agents fire at the same moment.
const NotificationManager = (() => {
  const DEBOUNCE_MS = 300;
  let pending = [];
  let timer = null;

  function flush() {
    if (!pending.length) return;
    const batch = pending.splice(0);   // drain atomically

    // Single toast — summarise if more than one arrived in the burst
    if (batch.length === 1) {
      showToast(batch[0].title, 'info');
    } else {
      const agents = [...new Set(batch.map(n => n.agent_id).filter(Boolean))];
      const label = agents.length ? agents.join(', ') : `${batch.length} agents`;
      showToast(`${batch.length} notifications from ${label}`, 'info');
    }

    // Refresh the badge once for the whole burst rather than once per doc.
    // htmx.trigger re-runs the hx-get on the badge span so the count comes
    // from the server's atomic counter — never a stale client-side guess.
    const badgeEl = document.getElementById('notif-badge');
    if (badgeEl) htmx.trigger(badgeEl, 'load');
  }

  return {
    /**
     * Call this when a new notification arrives (Firestore onSnapshot,
     * server-sent event, etc.).
     *
     * @param {{ title: string, agent_id?: string, type?: string }} notif
     */
    push(notif) {
      pending.push(notif);
      clearTimeout(timer);
      timer = setTimeout(flush, DEBOUNCE_MS);
    },
  };
})();

// ── Confirm destructive actions ────────────────────────────────────────────
// window.confirmAction() (the modal itself) is defined in base.html, later
// in page load order than this script. Safe to forward-reference here: this
// file only registers the listener below at load time, and the listener
// body only calls confirmAction()/showToast() later, at actual submit time —
// by which point base.html's script has already run and defined both.

// ── Toast-driven form submission (data-toast-submit / data-confirm) ────────
// Single source of truth for "a plain form's 4xx/5xx must not navigate to
// error.html or dump raw JSON" — every form gets this by adding one
// attribute, present and future, instead of each page hand-rolling its own
// fetch+toast interceptor. See the attribute reference in base.html (kept
// there since that's what a template author is reading when adding a form).
//
// form.getAttribute('action') (NOT form.action) is required: a hidden input
// named "action" (e.g. <input name="action" value="approve">) shadows the
// form's own .action property per the HTMLFormElement spec — form.action
// would silently return that input element instead of the URL string.
async function submitFormWithToast(form, submitter) {
  // e.submitter (the actual button the user clicked, passed in from the
  // listener below) is what to disable — NOT a querySelector guess. A guess
  // picks the first `<button>` in DOM order, which in most modals is Cancel
  // (plain `type=button`, so it matches too) or, in resources/browser.html's
  // multi-step wizard, a same-step helper button — never the button that's
  // actually mid-request. e.submitter is undefined only for a script-invoked
  // form.submit(), which nothing on this codebase's toast-submit path does.
  const btn = submitter || form.querySelector('[type=submit], button');
  if (btn) btn.disabled = true;
  try {
    const resp = await fetch(form.getAttribute('action'), { method: 'POST', body: new FormData(form) });
    if (resp.ok) {
      const hash = form.dataset.successHash ? ('#' + form.dataset.successHash) : '';
      let url = resp.url + hash;
      if (form.dataset.success) {
        // showToast() renders into the current document, which this
        // navigation is about to unload — a toast fired here would never be
        // seen. Thread the message through the destination URL instead (the
        // same ?_flash= idiom base.html's shared reader pops back off on the
        // page that loads).
        const sep = url.includes('?') ? '&' : '?';
        url = `${url}${sep}_flash=${encodeURIComponent(form.dataset.success)}`;
      }
      window.location.href = url;
    } else {
      let failMsg = form.dataset.fail || 'Action failed.';
      try {
        const d = await resp.json();
        if (Array.isArray(d.detail)) {
          // FastAPI/Pydantic 422 shape: [{loc, msg, type}, ...], not a string.
          failMsg = d.detail.map(item => item.msg || JSON.stringify(item)).join('; ');
        } else if (d.detail) {
          failMsg = d.detail;
        }
      } catch {
        // Non-JSON body (e.g. a 403/404 that always renders error.html
        // regardless of Accept) — nothing to log, this is an expected path,
        // not a failure of this code.
      }
      showToast(failMsg, 'error');
      if (btn) btn.disabled = false;
    }
  } catch (err) {
    console.warn('[submitFormWithToast] network error:', err);
    showToast('Network error. Please try again.', 'error');
    if (btn) btn.disabled = false;
  }
}

document.addEventListener('submit', (e) => {
  const form = e.target;
  const msg = form.getAttribute('data-confirm');
  if (!msg && !form.hasAttribute('data-toast-submit')) return;
  e.preventDefault();
  const submitter = e.submitter;
  if (msg) {
    const danger = form.getAttribute('data-confirm-danger') === 'true';
    const phrase = danger
      ? (form.getAttribute('data-confirm-phrase') || form.querySelector('button')?.textContent.trim())
      : undefined;
    window.confirmAction({title: 'Confirm', body: msg, phrase, danger}).then((result) => {
      if (result) submitFormWithToast(form, submitter);
    });
    return;
  }
  submitFormWithToast(form, submitter);
});

// ── Info-icon popovers ──────────────────────────────────────────────────────
// Click-to-open, anchored explanation panel. Markup lives in
// partials/_info_popover.html, visual styling in app.css under the same
// heading. Wired via a plain inline onclick="agentosTogglePopover(this)" on the
// trigger button — same pattern as every other interactive element in this
// app (modals, dropdowns) — rather than a document-level delegated
// addEventListener. That's a deliberate simplification: it only depends on
// this function existing in global scope by the time the user clicks, not
// on a separate page-load-time listener registration having taken effect.
//
// Deliberately `var`, not `let`/`const`. Live testing hit
// "Cannot access '_agentosPopoverCleanup' before initialization" on click —
// the standard TDZ symptom for `let`/`const` re-executing in the same
// scope. Root cause NOT confirmed: a duplicate <script src="app.js"> tag
// and a duplicated file body were both directly checked and ruled out on
// the affected page, so whatever is re-running this code (if that's even
// what's happening) is still unexplained. `var` sidesteps the failure
// mode regardless — it's hoisted with an immediate value and safely
// tolerates redeclaration, unlike `let`/`const`.
var _agentosPopoverCleanup = null;

function agentosCloseAllPopovers() {
  document.querySelectorAll('.info-popover-panel.is-open').forEach((panel) => {
    panel.classList.remove('is-open');
  });
  if (_agentosPopoverCleanup) {
    _agentosPopoverCleanup();
    _agentosPopoverCleanup = null;
  }
}

// Popovers open upward by default, so anything that clips its own overflow
// (a scrollable modal card, a table's overflow-x-auto wrapper — the latter
// forces overflow-y to auto too per spec, even though only overflow-x was
// set) can clip the panel before it ever reaches the viewport edge. Walk up
// to the nearest such ancestor and treat its top edge as the clip boundary
// instead of just window's — this is what actually clips it in practice.
function agentosFindClipAncestor(el) {
  let node = el.parentElement;
  while (node && node !== document.body) {
    const style = getComputedStyle(node);
    if (/auto|scroll|hidden/.test(style.overflowY) || /auto|scroll|hidden/.test(style.overflowX)) {
      return node;
    }
    node = node.parentElement;
  }
  return null;
}

function agentosTogglePopover(trigger) {
  if (window.userPrefs && window.userPrefs.tooltips_enabled === false) return;
  const VIEWPORT_MARGIN = 8;
  const wrap = trigger.closest('.info-popover-wrap');
  const panel = wrap && wrap.querySelector('.info-popover-panel');
  if (!panel) return;

  const wasOpen = panel.classList.contains('is-open');
  agentosCloseAllPopovers();
  if (wasOpen) return; // re-click just closes it

  // Reset first, so a shift/flip from a PRIOR open on this same panel can't
  // bias anything measured below.
  panel.style.setProperty('--info-popover-shift-x', '0px');
  panel.classList.remove('info-popover-below');

  // Deliberately NOT panel.getBoundingClientRect(). That reads through the
  // panel's own CSS transform (translateX/Y driven by the shift-x custom
  // property above), and on a visibility:hidden element, a prior open's
  // shift can still taint that reading on the NEXT open even after the
  // reset just above (confirmed live: reopening the same popover a second
  // time measured a position offset by exactly the previous shift amount,
  // every time, in Chromium) — a forced reflow (reading offsetHeight) before
  // measuring did not fix it either. Computing the natural (unshifted,
  // opens-upward) position from the WRAP's rect - which is never
  // transformed - plus the panel's own intrinsic, transform-independent
  // offsetWidth/offsetHeight sidesteps that class of bug entirely rather
  // than fighting the browser to "catch up" on a hidden element.
  const wrapRect = wrap.getBoundingClientRect();
  const panelWidth = panel.offsetWidth;
  const panelHeight = panel.offsetHeight;
  const gapPx = parseFloat(getComputedStyle(panel).getPropertyValue('--info-popover-gap')) || 6;
  const naturalLeft = wrapRect.left + (wrapRect.width / 2) - (panelWidth / 2);
  const naturalRight = naturalLeft + panelWidth;
  const naturalTop = wrapRect.top - gapPx - panelHeight;

  // Same clip ancestor bounds the panel on all four sides: a scroll-clipping
  // container (e.g. the New Goal modal's .card, which has overflow-y-auto —
  // and per spec that forces overflow-x to clip too) can slice off the
  // panel's left/right edge even when it fits fine within window.innerWidth.
  // Previously only clipTop used the ancestor; the horizontal clamp used
  // window bounds alone, so a popover anchored near a narrow modal's edge
  // rendered on-screen but with its text cut off by the modal's own overflow.
  const clipAncestor = agentosFindClipAncestor(wrap);
  const clipRect = clipAncestor ? clipAncestor.getBoundingClientRect() : null;
  const clipTop = clipRect ? Math.max(clipRect.top, 0) : 0;
  const clipLeft = clipRect ? Math.max(clipRect.left, 0) : 0;
  const clipRight = clipRect ? Math.min(clipRect.right, window.innerWidth) : window.innerWidth;

  // Flip below the trigger if opening upward would clip past the top edge —
  // either the viewport's, or the nearest scroll-clipping ancestor's,
  // whichever is lower on screen.
  if (naturalTop < clipTop + VIEWPORT_MARGIN) {
    panel.classList.add('info-popover-below');
  }

  // Nudge horizontally if the centered panel would clip past either edge.
  let shift = 0;
  if (naturalLeft < clipLeft + VIEWPORT_MARGIN) {
    shift = (clipLeft + VIEWPORT_MARGIN) - naturalLeft;
  } else if (naturalRight > clipRight - VIEWPORT_MARGIN) {
    shift = (clipRight - VIEWPORT_MARGIN) - naturalRight;
  }
  if (shift !== 0) {
    panel.style.setProperty('--info-popover-shift-x', `${shift}px`);
  }

  panel.classList.add('is-open');

  // Close on outside click / Escape — attached fresh on open, torn down on
  // close. Deferred by a tick so the click that just opened this popover
  // (still bubbling) doesn't immediately close it again.
  setTimeout(() => {
    const onDocClick = (e) => {
      if (!wrap.contains(e.target)) agentosCloseAllPopovers();
    };
    const onDocKeydown = (e) => {
      if (e.key === 'Escape') agentosCloseAllPopovers();
    };
    document.addEventListener('click', onDocClick);
    document.addEventListener('keydown', onDocKeydown);
    _agentosPopoverCleanup = () => {
      document.removeEventListener('click', onDocClick);
      document.removeEventListener('keydown', onDocKeydown);
    };
  }, 0);
}

// ── Explainability preferences (Settings > Help & Explainability) ─────────
// window.userPrefs is bootstrapped in base.html from the user's saved
// preferences (app/services/user_preferences.py — see that file's docstring
// for why the DB column names below don't match their Settings labels).
// Three toggles, three different enforcement points:
//   - tooltips_enabled (Settings label "Tooltip") gates the ⓘ info-popover:
//     agentosTogglePopover() still no-ops as a defense-in-depth guard, but
//     applyInfoPopoverPreference below is what actually removes the ⓘ
//     trigger from layout (via CSS .is-disabled { display: none }) —
//     otherwise the icon looks clickable but silently does nothing, which
//     reads as broken rather than as an intentional setting.
//   - hover_help_enabled (Settings label "Hover cards") gates every
//     hover-triggered card — the sidebar preview card AND every plain
//     title="" hint in the app (converted to a hover-card instead of the
//     browser's native tooltip — see initHoverCards() in base.html). Both
//     are checked live at hover time since neither has persistent visual
//     state to toggle here.
//   - startup_wizard_enabled (Settings label "Guided tours") is enforced in
//     tour.js.
function applyInfoPopoverPreference(enabled) {
  document.querySelectorAll('.info-popover-trigger').forEach((el) => {
    el.classList.toggle('is-disabled', !enabled);
  });
}
document.addEventListener('DOMContentLoaded', () => {
  if (window.userPrefs && window.userPrefs.tooltips_enabled === false) applyInfoPopoverPreference(false);
});
// HTMX swaps can introduce fresh info-popover triggers (newly rendered rows,
// badges, etc.) after the initial pass above already ran.
document.addEventListener('htmx:afterSwap', () => {
  if (window.userPrefs && window.userPrefs.tooltips_enabled === false) applyInfoPopoverPreference(false);
});

// Tear down an open popover BEFORE a swap destroys the DOM it lives in.
//
// A polled region (e.g. the monitor page's #agents-status-body, which does
// hx-swap="innerHTML" on hx-trigger="every 5s") can replace its whole subtree
// while a popover inside it is open. pwtTogglePopover registers the panel's
// outside-click / Escape dismissal as listeners on `document`, with a closure
// over that panel's `wrap`. The swap destroys the panel, its wrap, and its
// trigger button -- but NOT those document listeners. Orphaned, they keep
// firing on every subsequent click (their detached wrap can never `contains`
// the click target), calling pwtCloseAllPopovers() and slamming shut any new
// popover on the same click that opened it. Symptom: the panel vanishes on the
// 5s poll, then clicking the ⓘ "does nothing" until a full page refresh drops
// the leaked listeners. Popovers OUTSIDE any polled region (e.g. the monitor
// header's "From finished agents" ⓘ) are unaffected -- their DOM is never
// swapped -- which is why only Agent Status exhibited this.
//
// Fix: close the popover here, during beforeSwap, so its listeners are removed
// while _pwtPopoverCleanup still points at them -- BEFORE the swap detaches the
// wrap (and before anything the swap triggers can reset that cleanup ref).
// Scoped to evt.detail.target: only a popover inside the subtree actually being
// replaced is closed, so an open popover elsewhere on the page is left alone.
// A 304 is skipped entirely -- base.html cancels its swap, so no DOM changes
// and there is nothing to tear down.
document.body.addEventListener('htmx:beforeSwap', (evt) => {
  if (evt.detail.xhr && evt.detail.xhr.status === 304) return;
  const target = evt.detail.target;
  const openPanel = document.querySelector('.info-popover-panel.is-open');
  if (openPanel && target && target.contains(openPanel)) {
    console.debug('[info-popover] closing open popover before htmx swap of',
                  target.id || target.tagName);
    pwtCloseAllPopovers();
  }
});

// Settings > Help & Explainability toggle switches (settings/preferences.html).
// Saves immediately on click — no separate Save button.
async function agentosTogglePreference(btn) {
  const key = btn.dataset.prefKey;
  const next = !btn.classList.contains('pref-switch-on');
  btn.classList.toggle('pref-switch-on', next);
  btn.setAttribute('aria-checked', String(next));
  try {
    const fd = new FormData();
    fd.append('value', String(next));
    const resp = await fetch(`/settings/preferences/${key}`, { method: 'POST', body: fd });
    if (!resp.ok) throw new Error('Save failed');
    window.userPrefs = window.userPrefs || {};
    window.userPrefs[key] = next;
    // hover_help_enabled needs no live DOM update: both things it gates
    // (the sidebar hover-card and every converted title="" hint) check the
    // preference live at hover time instead of holding persistent state.
    if (key === 'tooltips_enabled') applyInfoPopoverPreference(next);
    showToast('Preference saved.', 'success');
  } catch (e) {
    btn.classList.toggle('pref-switch-on', !next);
    btn.setAttribute('aria-checked', String(!next));
    showToast('Could not save preference.', 'error');
  }
}

// ── HTMX progress bar ──────────────────────────────────────────────────────
document.addEventListener('htmx:beforeRequest', () => {
  document.body.style.cursor = 'progress';
});
document.addEventListener('htmx:afterRequest', () => {
  document.body.style.cursor = '';
});
