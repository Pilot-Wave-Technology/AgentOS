/**
 * markdown_renderer.js — shared marked.js renderer config for every
 * chat-like thread (Assistant chat, GO thread, orchestrator, Goal Review
 * Council, Security Council).
 *
 * Two overrides on top of marked's defaults:
 *  - code(): syntax-highlights fenced code blocks via highlight.js.
 *  - link(): forces target="_blank" + rel="noopener noreferrer" on every
 *    rendered link, so cards like the Git Engine "Merge PR" notice open
 *    their destination in a new tab instead of navigating the thread away.
 *    noopener+noreferrer blocks the new tab from reaching back via
 *    window.opener (reverse-tabnabbing).
 *
 * Load AFTER marked.min.js + highlight.min.js, BEFORE any renderMd()/
 * safeMarkdown() call. Threads that sanitize with DOMPurify (see
 * safe_markdown.js) must also pass ADD_ATTR:['target'] to DOMPurify.sanitize
 * — its default ALLOWED_ATTR list omits "target" (it does allow "rel"), so
 * without that the target attribute set here gets silently stripped.
 *
 * marked.use() composes across multiple calls — a page that needs its own
 * code() variant (e.g. chat/index.html's copy-button code blocks) can call
 * marked.use() again afterward to override just that method; the link()
 * override from here stays intact.
 */
try {
  marked.use({
    async: false,
    breaks: true,
    gfm: true,
    renderer: {
      code(token) {
        // marked@9+ passes a token object; earlier versions pass (code, lang).
        const code = (token && typeof token === 'object') ? (token.text ?? token) : token;
        const lang = (token && typeof token === 'object') ? token.lang : arguments[1];
        const highlighted = lang && hljs.getLanguage(lang)
          ? hljs.highlight(code, { language: lang }).value
          : hljs.highlightAuto(code).value;
        return `<pre><code class="hljs">${highlighted}</code></pre>\n`;
      },
      link(token) {
        // marked@9+ passes a token object; earlier versions pass (href, title, text).
        const href = (token && typeof token === 'object') ? token.href : arguments[0];
        const title = (token && typeof token === 'object') ? token.title : arguments[1];
        const text = (token && typeof token === 'object') ? this.parser.parseInline(token.tokens) : arguments[2];
        const titleAttr = title ? ` title="${title}"` : '';
        return `<a href="${href}" target="_blank" rel="noopener noreferrer"${titleAttr}>${text}</a>`;
      }
    }
  });
} catch (e) {
  console.warn('marked.use() failed, falling back to default config:', e);
}
