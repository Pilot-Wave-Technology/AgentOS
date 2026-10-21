/**
 * Browse the files a story's executor is working in.
 *
 * WHY THIS IS A FILE. It lived inside chat/index.html, so the workspace
 * was visible from the assistant's thread and nowhere else. Moving an
 * approved errand's conversation to the GO's thread took the explorer
 * away with it -- the data was untouched, and nothing rendered it.
 *
 * WHAT A HOST PAGE MUST PROVIDE:
 *
 *     PID                the goal id
 *     CURRENT_STORY_ID   the story whose workspace this is ("" = none)
 *
 * plus the markup in _shared/workspace_panel.html. Both consts carry
 * Jinja, which is why they stay in the page.
 *
 * READ ONLY. Every endpoint it calls is a GET; nothing here writes to
 * the workspace, and nothing it does reaches the agent.
 */

// ── Workspace file viewer (Stage 1) ──────────────────────────────────────
// Lives in the right-rail #workspace-panel. Only rendered when a story is
// active for this thread (current_story is set + workspace_state='ready').
(function setupWorkspacePanel() {
  if (!CURRENT_STORY_ID) return;
  const panel = document.getElementById('workspace-panel');
  if (!panel) return;
  const treeEl = document.getElementById('ws-tree');
  const refreshBtn = document.getElementById('ws-refresh-btn');
  const contentPane = document.getElementById('ws-content-pane');
  const contentPath = document.getElementById('ws-content-path');
  const contentMeta = document.getElementById('ws-content-meta');
  const contentBody = document.getElementById('ws-content-body');

  const TREE_URL = `/goals/${PID}/stories/${CURRENT_STORY_ID}/workspace/tree`;
  const FILE_URL = `/goals/${PID}/stories/${CURRENT_STORY_ID}/workspace/file`;

  // Map mime → highlight.js language hint
  function langForMime(mime, path) {
    const ext = (path.match(/\.([a-z0-9]+)$/i) || [])[1]?.toLowerCase() || '';
    if (mime === 'text/markdown') return 'markdown';
    if (mime === 'text/x-python') return 'python';
    if (mime === 'text/javascript' || mime === 'application/json') return ext === 'json' ? 'json' : 'javascript';
    if (mime === 'text/x-typescript') return ext.startsWith('ts') ? 'typescript' : 'javascript';
    if (mime === 'text/x-yaml') return 'yaml';
    if (mime === 'text/x-toml') return 'toml';
    if (mime === 'text/x-shellscript') return 'bash';
    if (mime === 'text/html') return 'html';
    if (mime === 'text/css') return 'css';
    if (mime === 'text/x-sql') return 'sql';
    if (mime === 'text/x-go') return 'go';
    if (mime === 'text/x-rust') return 'rust';
    if (mime === 'text/x-c' || mime === 'text/x-c++') return mime.endsWith('++') ? 'cpp' : 'c';
    if (mime === 'text/x-diff') return 'diff';
    return '';
  }

  function fmtSize(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  }

  // Build a tree node structure from a flat entry list.
  function buildTree(entries) {
    const root = { name: '', children: new Map() };
    for (const e of entries) {
      const parts = e.path.split('/');
      let cur = root;
      for (let i = 0; i < parts.length; i++) {
        const name = parts[i];
        const isLeaf = i === parts.length - 1;
        if (!cur.children.has(name)) {
          cur.children.set(name, { name, children: new Map(), isFile: false });
        }
        const node = cur.children.get(name);
        if (isLeaf) {
          node.isFile = (e.type === 'file');
          node.size = e.size;
          node.path = e.path;
        }
        cur = node;
      }
    }
    return root;
  }

  // `detail` is the backend saying WHY the list is empty -- almost always
  // "the pod holding this workspace did not answer", because the pod is
  // still composing or has already been reclaimed. kernel_workspace_pod.tree
  // writes that string precisely so an unreachable pod stops reading as an
  // empty workspace, and this panel used to drop it on the floor and print
  // "No files in workspace yet." over a workspace that was complete.
  function renderTree(root, detail) {
    treeEl.innerHTML = '';
    if (root.children.size === 0) {
      const why = detail
        ? String(detail)
        : 'No files in workspace yet.';
      const box = document.createElement('div');
      box.className = 'text-gray-500 text-2xs italic px-2 py-1';
      box.textContent = why;
      treeEl.appendChild(box);
      return;
    }
    const list = document.createElement('ul');
    list.className = 'space-y-0.5';
    renderNode(root, list, 0);
    treeEl.appendChild(list);
  }

  function renderNode(node, parentList, depth) {
    // Sort: dirs first then files, both alphabetically.
    const items = [...node.children.values()].sort((a, b) => {
      if (a.isFile !== b.isFile) return a.isFile ? 1 : -1;
      return a.name.localeCompare(b.name);
    });
    for (const child of items) {
      const li = document.createElement('li');
      li.style.paddingLeft = `${depth * 0.75}rem`;
      if (child.isFile) {
        const btn = document.createElement('button');
        btn.className = 'block w-full text-left px-1 py-0.5 rounded hover:bg-surface text-gray-300 hover:text-gray-100 truncate';
        btn.title = `${child.path} · ${fmtSize(child.size)}`;
        btn.innerHTML = `<span class="text-gray-500">📄</span> ${child.name}`;
        btn.dataset.path = child.path;
        btn.dataset.size = child.size;
        btn.onclick = () => openFile(child.path);
        li.appendChild(btn);
      } else {
        const toggle = document.createElement('button');
        toggle.className = 'block w-full text-left px-1 py-0.5 rounded hover:bg-surface text-gray-200 truncate';
        toggle.innerHTML = `<span class="ws-caret">▾</span> <span class="text-indigo-400">📁</span> ${child.name}`;
        li.appendChild(toggle);
        const sublist = document.createElement('ul');
        sublist.className = 'space-y-0.5';
        renderNode(child, sublist, depth + 1);
        li.appendChild(sublist);
        toggle.onclick = () => {
          const collapsed = sublist.classList.toggle('hidden');
          toggle.querySelector('.ws-caret').textContent = collapsed ? '▸' : '▾';
        };
      }
      parentList.appendChild(li);
    }
  }

  async function refreshTree() {
    treeEl.innerHTML = '<div class="text-gray-500 text-2xs italic px-2 py-1">Loading…</div>';
    try {
      const resp = await fetch(TREE_URL, { credentials: 'include' });
      if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
      const data = await resp.json();
      const root = buildTree(data.entries || []);
      renderTree(root, data.detail);
      if (data.truncated) {
        const warn = document.createElement('div');
        warn.className = 'text-2xs text-amber-400 italic px-2 py-1';
        warn.textContent = 'Tree truncated — too many files to list.';
        treeEl.appendChild(warn);
      }
    } catch (e) {
      treeEl.innerHTML = `<div class="text-2xs text-red-400 px-2 py-1">Failed to load: ${e.message}</div>`;
    }
  }

  async function openFile(relPath) {
    contentPane.classList.remove('hidden');
    contentPane.classList.add('flex');
    document.getElementById('ws-resize-handle-y').classList.remove('hidden');
    contentPath.textContent = relPath;
    contentMeta.textContent = '';
    contentBody.innerHTML = '<div class="text-gray-500 italic">Loading…</div>';
    try {
      const url = `${FILE_URL}?path=${encodeURIComponent(relPath)}`;
      const resp = await fetch(url, { credentials: 'include' });
      if (!resp.ok) {
        const txt = await resp.text();
        throw new Error(`${resp.status}: ${txt.slice(0, 200)}`);
      }
      const data = await resp.json();
      contentMeta.textContent = `${fmtSize(data.size)}${data.truncated ? ' · truncated' : ''}`;
      renderFileContent(data);
    } catch (e) {
      contentBody.innerHTML = `<div class="text-red-400">Failed to load: ${e.message}</div>`;
    }
  }

  function renderFileContent(data) {
    contentBody.innerHTML = '';
    if (data.mime === 'application/octet-stream' || !data.content) {
      const note = document.createElement('div');
      note.className = 'text-gray-500 italic';
      note.textContent = data.size === 0
        ? '(empty file)'
        : '(binary preview not available)';
      contentBody.appendChild(note);
      return;
    }
    if (data.mime === 'text/markdown') {
      const wrap = document.createElement('div');
      wrap.className = 'agent-prose';
      wrap.innerHTML = renderMd(data.content);
      contentBody.appendChild(wrap);
      // Highlight any code blocks within the rendered markdown
      wrap.querySelectorAll('pre code').forEach(block => {
        try { hljs.highlightElement(block); } catch {}
      });
      return;
    }
    // Source code or text
    const pre = document.createElement('pre');
    pre.className = 'rounded-md text-xs overflow-x-auto m-0';
    pre.style.background = '#0d1117';
    pre.style.padding = '0.8em 1em';
    const code = document.createElement('code');
    const lang = langForMime(data.mime, data.path);
    if (lang) code.className = `language-${lang}`;
    code.textContent = data.content;
    pre.appendChild(code);
    contentBody.appendChild(pre);
    try { hljs.highlightElement(code); } catch {}
  }

  function closeWsContent() {
    contentPane.classList.add('hidden');
    contentPane.classList.remove('flex');
    document.getElementById('ws-resize-handle-y').classList.add('hidden');
    contentBody.innerHTML = '';
  }
  window.closeWsContent = closeWsContent;

  refreshBtn.onclick = refreshTree;
  refreshTree();

  // ── Resizers ──
  // Persist sizes per (goal, story) in localStorage so they stick across reloads.
  const SIZE_KEY = `ws-panel-sizes:${PID}:${CURRENT_STORY_ID}`;
  function loadSizes() {
    try { return JSON.parse(localStorage.getItem(SIZE_KEY) || '{}'); } catch { return {}; }
  }
  function saveSizes(s) {
    try { localStorage.setItem(SIZE_KEY, JSON.stringify(s)); } catch {}
  }
  const sizes = loadSizes();
  if (sizes.panelWidth) panel.style.width = sizes.panelWidth + 'px';
  if (sizes.treeHeight) {
    document.getElementById('ws-tree').style.flex = `0 0 ${sizes.treeHeight}px`;
  }

  // Horizontal resizer (panel width)
  const handleX = document.getElementById('ws-resize-handle-x');
  if (handleX) {
    let startX = 0, startW = 0, dragging = false;
    handleX.addEventListener('mousedown', (e) => {
      dragging = true;
      startX = e.clientX;
      startW = panel.getBoundingClientRect().width;
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
      e.preventDefault();
    });
    window.addEventListener('mousemove', (e) => {
      if (!dragging) return;
      // Drag left = grow panel (handle is on panel's left edge)
      const next = Math.min(Math.max(startW + (startX - e.clientX), 240), Math.min(window.innerWidth - 320, 1200));
      panel.style.width = next + 'px';
    });
    window.addEventListener('mouseup', () => {
      if (!dragging) return;
      dragging = false;
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      const cur = loadSizes();
      cur.panelWidth = Math.round(panel.getBoundingClientRect().width);
      saveSizes(cur);
    });
  }

  // Vertical resizer (tree height vs content pane height)
  const handleY = document.getElementById('ws-resize-handle-y');
  if (handleY) {
    let startY = 0, startH = 0, dragging = false;
    handleY.addEventListener('mousedown', (e) => {
      dragging = true;
      startY = e.clientY;
      startH = treeEl.getBoundingClientRect().height;
      document.body.style.cursor = 'row-resize';
      document.body.style.userSelect = 'none';
      e.preventDefault();
    });
    window.addEventListener('mousemove', (e) => {
      if (!dragging) return;
      const panelBody = document.getElementById('ws-panel-body');
      const panelH = panelBody.getBoundingClientRect().height;
      // Keep at least 64px of tree, at least 96px of content
      const max = panelH - 96 - handleY.getBoundingClientRect().height - 28; // 28px header of content
      const next = Math.min(Math.max(startH + (e.clientY - startY), 64), max);
      treeEl.style.flex = `0 0 ${next}px`;
    });
    window.addEventListener('mouseup', () => {
      if (!dragging) return;
      dragging = false;
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      const cur = loadSizes();
      cur.treeHeight = Math.round(treeEl.getBoundingClientRect().height);
      saveSizes(cur);
    });
  }
})();
