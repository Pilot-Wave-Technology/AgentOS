/* Client-side message rendering for the read-only agent channels
 * (council_thread, security_thread, orchestrator).
 *
 * These three pages previously carried three byte-identical copies of
 * renderMessageBody() differing only in their default accent colour. It now
 * lives here once; each page passes its own accent.
 *
 * NOTE the deliberate split from the GO thread: that page uses safeMarkdown()
 * over `.rendered-md` and has no card_data or bracket-header convention. The
 * two renderers are NOT interchangeable — see app/templates/goal_thread/README.md.
 *
 * Depends on renderMd() / escapeHtml() / marked from markdown_renderer.js.
 */

/* Many plain (non-card) messages in these channels start with a bracketed
 * header line — e.g. "[Prateek Upadhyay's Assistant · Story: … · request]\nbody"
 * or "[GO → Story: …]\nbody" (see app/routers/orchestrator.py's
 * internal_orchestrator_message/internal_orchestrator_reply formatting).
 * Render that header as compact segments instead of dumping the brackets into
 * the body. Falls back to plain renderMd() untouched — including on any parse
 * error — if the content doesn't match this shape. */
function renderMessageBody(raw, accent) {
  accent = accent || 'amber';
  const plainFallback = () => {
    const rendered = renderMd(raw);
    return rendered && rendered.trim()
      ? `<div class="agent-prose text-sm text-gray-300">${rendered}</div>`
      : `<div class="text-sm text-gray-300 whitespace-pre-wrap">${escapeHtml(raw)}</div>`;
  };
  if (!raw) return '';
  try {
    const m = /^\[([^\]]+)\]\s*([\s\S]*)$/.exec(raw.trim());
    if (!m) return plainFallback();
    const header = m[1];
    const body = m[2].trim();
    const headerHtml = header.split('·').map((part, i) => {
      const trimmed = part.trim();
      if (!trimmed) return '';
      // First segment is the "who" attribution — give it more visual weight
      // than the metadata segments that follow (Story:, request, etc.).
      const weight = i === 0 ? 'font-semibold' : 'font-medium';
      return `<span class="header-pill header-pill-${accent} ${weight}">${marked.parseInline(trimmed)}</span>`;
    }).join('');
    if (!headerHtml.trim()) return plainFallback();
    const renderedBody = body ? renderMd(body) : '';
    const bodyHtml = renderedBody && renderedBody.trim()
      ? `<div class="agent-prose text-sm text-gray-300">${renderedBody}</div>`
      : (body ? `<div class="text-sm text-gray-300 whitespace-pre-wrap">${escapeHtml(body)}</div>` : '');
    return `<div class="flex flex-wrap items-center gap-1.5 mb-2 text-2xs">${headerHtml}</div>${bodyHtml}`;
  } catch (e) {
    console.warn('renderMessageBody: bracket-header parse failed, falling back to plain text:', e);
    return plainFallback();
  }
}

/* Hydrate server-rendered rows within `root` (defaults to the document).
 *
 * Jinja emits the RAW markdown in data-md and an escaped plain-text fallback as
 * the element's own content; this converts it. The data-md-done marker makes
 * the pass idempotent, which matters because "Load earlier" runs it again over
 * a container that already holds hydrated rows — re-rendering rendered HTML
 * mangles it.
 *
 * `defaultAccent` applies to bracket-header pills when a row does not carry its
 * own data-accent (the orchestrator sets one per message role).
 */
function hydrateChannelMessages(root, defaultAccent) {
  const scope = root || document;

  // Review cards: plain markdown, no bracket-header convention.
  scope.querySelectorAll('.agent-prose[data-md]:not([data-md-done])').forEach(el => {
    const rendered = renderMd(el.dataset.md);
    if (rendered && rendered.trim()) {
      el.innerHTML = rendered;
    } else if (el.dataset.md) {
      // Keep the server-rendered text rather than blanking the bubble if
      // marked.parse fails.
      el.style.whiteSpace = 'pre-wrap';
      el.textContent = el.dataset.md;
    }
    el.setAttribute('data-md-done', '1');
  });

  // Plain message bodies — bracket-header aware.
  scope.querySelectorAll('.message-body[data-md]:not([data-md-done])').forEach(el => {
    el.innerHTML = renderMessageBody(el.dataset.md, el.dataset.accent || defaultAccent);
    el.setAttribute('data-md-done', '1');
  });
}
