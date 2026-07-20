/**
 * safe_markdown.js — XSS-safe markdown rendering for chat/goal-thread/summary.
 *
 * Stored messages (chat, goal thread) and generated summaries are rendered to
 * HTML with marked and assigned via innerHTML. Without sanitization a message
 * containing e.g. <img src=x onerror=...> or <script> executes in another
 * user's browser (stored XSS → session/cookie theft).
 *
 * window.safeMarkdown(text) renders markdown then strips anything dangerous
 * with DOMPurify. It FAILS CLOSED: if marked or DOMPurify failed to load, it
 * returns HTML-escaped text (never raw HTML), so a stored message can never
 * inject markup even when the sanitizer is unavailable.
 *
 * Callers still assign the result to innerHTML — that's safe because the output
 * is already sanitized (or escaped).
 */
(function (w) {
  function escapeHtml(s) {
    var d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

  w.safeMarkdown = function (text) {
    var s = text == null ? "" : String(text);
    if (!s) return "";
    if (typeof w.marked === "undefined" || !w.marked.parse) return escapeHtml(s);
    var html;
    try {
      html = w.marked.parse(s);
    } catch (e) {
      return escapeHtml(s);
    }
    if (typeof html !== "string") return escapeHtml(s);
    // Fail closed: no sanitizer → escape rather than inject raw HTML.
    if (typeof w.DOMPurify === "undefined" || !w.DOMPurify.sanitize) {
      return escapeHtml(s);
    }
    // DOMPurify's default ALLOWED_ATTR omits "target" (it does include "rel"),
    // so without ADD_ATTR it silently strips the target="_blank" the marked
    // renderer.link() override sets on every link — the link still renders,
    // it just opens in the current tab instead of a new one.
    return w.DOMPurify.sanitize(html, { ADD_ATTR: ["target"] });
  };
})(window);
