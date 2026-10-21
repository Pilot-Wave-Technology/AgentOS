"""Resource scanner — analyze repos, documents, and files for GO context.

Supports:
- Git repos: file tree, README, key configs, LLM summary
- PDF files: text extraction + summary
- Excel files: sheet/column structure + summary
- Word docs: text extraction + summary
- Markdown files: direct content + summary
- Incremental repo scans: diff-based updates

Summaries stored in resource_summaries table for GO to query via tools.
"""
import hashlib
import json
import logging
import time
from datetime import datetime
from typing import Optional

from app.database import get_pool
from app.config import get_settings
from app.gemini_client import summarize_conversation

_log = logging.getLogger(__name__)

# Key files to fetch from repos for analysis
REPO_KEY_FILES = [
    "README.md", "readme.md", "README.rst",
    "package.json", "requirements.txt", "Pipfile", "pyproject.toml",
    "go.mod", "Cargo.toml", "pom.xml", "build.gradle",
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    ".env.example", "config.yaml", "config.json",
    "ARCHITECTURE.md", "CONTRIBUTING.md",
]

# Raw-endpoint fetch cap — a download safety net so one pathological file can't
# be held in memory in full. Reads are ALSO windowed by line count
# (DEFAULT_MAX_LINES), which is what actually bounds the tokens returned to an
# agent, so this only guards against enormous blobs — not normal source files.
MAX_FILE_READ_BYTES = 5_000_000  # 5MB
# Default line window returned per read call. Files longer than this are paged
# via `offset`; the read result carries `next_offset` so the caller can continue.
DEFAULT_MAX_LINES = 1500


def window_lines(text: str, offset: int, max_lines: int) -> dict:
    """Return a line window [offset : offset+max_lines] over `text`, plus paging
    metadata. When more lines remain, appends a human-readable 'call again with
    offset=N' notice and sets `next_offset` so an agent can page through the rest
    of a large file instead of being silently cut off.
    """
    lines = text.split("\n")
    total = len(lines)
    offset = max(0, offset)
    win = lines[offset:offset + max_lines]
    out = {
        "content": "\n".join(win),
        "offset": offset,
        "lines_returned": len(win),
        "total_lines": total,
        "truncated": (offset + max_lines < total) or offset > 0,
    }
    if offset + max_lines < total:
        shown = offset + max_lines
        out["next_offset"] = shown
        out["content"] += (
            f"\n\n... {total - shown} more line(s) not shown (this file has {total} lines "
            f"total). Read on with offset={shown} for the next window, or pass lines={total} "
            f"to read the whole file in one call."
        )
    return out


# ── Repo scanning ───────────────────────────────────────────────────────────

async def scan_repo_full(
    resource_id: str,
    goal_id: str,
    repo_owner: str,
    repo_name: str,
    branch: str = "main",
    github_token: str = "",
) -> dict:
    """Full scan of a GitHub repo using gitingest.

    Clones the repo to a temp dir, extracts all file contents in an
    LLM-optimized format, then sends to Gemini Flash for summary.
    """
    start_time = time.monotonic()
    _log.info("Scanning repo %s/%s (branch=%s) via gitingest", repo_owner, repo_name, branch)

    import tempfile
    import shutil
    import subprocess

    tmp_dir = None
    try:
        from gitingest import ingest_async

        # For private repos: clone manually first, then run gitingest on local path
        if github_token:
            tmp_dir = tempfile.mkdtemp(prefix="repo-scan-")
            clone_url = f"https://x-access-token:{github_token}@github.com/{repo_owner}/{repo_name}.git"
            clone_cmd = ["git", "clone", "--depth=1", f"--branch={branch}", clone_url, tmp_dir]
            result = subprocess.run(clone_cmd, capture_output=True, text=True, timeout=60)
            if result.returncode != 0:
                raise RuntimeError(f"git clone failed: {result.stderr[:200]}")
            repo_source = tmp_dir
        else:
            repo_source = f"https://github.com/{repo_owner}/{repo_name}"

        digest_summary, file_tree, content = await ingest_async(
            repo_source,
            branch=branch if not github_token else None,  # already on the right branch if cloned
            max_file_size=100_000,
        )
    except Exception as exc:
        _log.error("gitingest failed for %s/%s:%s — %s", repo_owner, repo_name, branch, exc)
        await _store_summary(
            resource_id=resource_id, goal_id=goal_id, summary_type="full",
            content=f"Scan failed: {exc}", metadata={},
        )
        return {"summary": f"Scan failed: {exc}", "metadata": {}, "duration_ms": 0}
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # Count files from digest
    file_count = digest_summary.count("File") if digest_summary else 0
    content_len = len(content) if content else 0
    _log.info("gitingest: %s/%s:%s — tree=%d chars, content=%d chars",
              repo_owner, repo_name, branch, len(file_tree or ""), content_len)

    # Gemini summarization
    analysis_text = (
        f"Repository: {repo_owner}/{repo_name}\n"
        f"Branch: {branch}\n\n"
        f"Digest stats:\n{digest_summary}\n\n"
        f"File tree:\n{file_tree}\n\n"
        f"File contents:\n{content[:500_000]}"  # Limit to ~125K tokens
    )

    try:
        summary = await _summarize_with_gemini(
            f"Analyze this GitHub repository codebase and provide a comprehensive summary:\n"
            f"- What does it do (purpose and functionality)\n"
            f"- Tech stack, languages, and frameworks used\n"
            f"- Architecture — key modules/directories and what they contain\n"
            f"- Dependencies and infrastructure (Docker, CI/CD, etc.)\n"
            f"- Code quality observations\n"
            f"- Current state and any notable patterns\n\n"
            f"{analysis_text}"
        )
    except SummarizationFailed as exc:
        return _summarization_failure_result(
            resource_id=resource_id, kind="repo/full", exc=exc,
            start_time=start_time,
            metadata={"file_count": file_count, "scan_method": "gitingest"},
        )

    duration_ms = int((time.monotonic() - start_time) * 1000)

    metadata = {
        "file_tree": (file_tree or "")[:10_000],  # Trim for DB storage
        "file_count": file_count,
        "content_chars": content_len,
        "digest_summary": digest_summary or "",
        "scan_method": "gitingest",
    }

    # Store summary
    await _store_summary(
        resource_id=resource_id,
        goal_id=goal_id,
        summary_type="full",
        content=summary,
        metadata=metadata,
        scan_duration_ms=duration_ms,
    )

    # Update resource record
    pool = get_pool()
    await pool.execute("""
        UPDATE goal_resources
        SET last_scanned_at = now(), last_summary_at = now(),
            summary_stale = false,
            metadata = metadata || $1
        WHERE id = $2
    """, json.dumps({"file_count": file_count, "scan_method": "gitingest"}),
        resource_id)

    _log.info("Repo scan complete: %s/%s:%s — %d chars, %dms",
              repo_owner, repo_name, branch, content_len, duration_ms)

    return {"summary": summary, "metadata": metadata, "duration_ms": duration_ms}


async def scan_repo_incremental(
    resource_id: str,
    goal_id: str,
    repo_owner: str,
    repo_name: str,
    branch: str = "main",
    previous_sha: str = "",
    github_token: str = "",
) -> dict:
    """Incremental scan — only analyze what changed since last scan."""
    if not previous_sha:
        return await scan_repo_full(resource_id, goal_id, repo_owner, repo_name, branch, github_token)

    start_time = time.monotonic()
    _log.info("Incremental scan %s/%s since %s", repo_owner, repo_name, previous_sha)

    import httpx

    headers = {"Accept": "application/vnd.github.v3+json"}
    if github_token:
        headers["Authorization"] = f"token {github_token}"

    base_url = f"https://api.github.com/repos/{repo_owner}/{repo_name}"

    async with httpx.AsyncClient(timeout=30) as client:
        # Compare: what changed since previous_sha
        compare_resp = await client.get(
            f"{base_url}/compare/{previous_sha}...{branch}", headers=headers
        )

        if compare_resp.status_code != 200:
            _log.warning("Compare API failed, falling back to full scan")
            return await scan_repo_full(resource_id, goal_id, repo_owner, repo_name, branch, github_token)

        compare_data = compare_resp.json()
        changed_files = compare_data.get("files", [])
        new_commits = compare_data.get("commits", [])
        # BUG FIXED (G4): this assigned `ahead_by` -- a COUNT -- to a
        # variable named head_sha. The real head is fetched below; this
        # line now records what it always meant to.
        ahead_by = compare_data.get("ahead_by", 0)

        # Get current head SHA
        head_resp = await client.get(f"{base_url}/commits/{branch}", headers=headers)
        current_sha = ""
        if head_resp.status_code == 200:
            current_sha = head_resp.json().get("sha", "")[:7]

    if not changed_files:
        return {"summary": "No changes since last scan", "metadata": {}, "duration_ms": 0}

    # Build change description
    changes = []
    for f in changed_files[:50]:  # Limit to 50 files
        status = f.get("status", "modified")
        filename = f.get("filename", "")
        additions = f.get("additions", 0)
        deletions = f.get("deletions", 0)
        changes.append(f"  {status}: {filename} (+{additions}/-{deletions})")

    commits_text = []
    for c in new_commits[:10]:
        msg = c.get("commit", {}).get("message", "").split("\n")[0][:100]
        author = c.get("commit", {}).get("author", {}).get("name", "")
        commits_text.append(f"  {msg} ({author})")

    # Get previous summary for context
    pool = get_pool()
    prev_summary = await pool.fetchval("""
        SELECT content FROM resource_summaries
        WHERE resource_id = $1 AND summary_type IN ('full', 'incremental')
        ORDER BY created_at DESC LIMIT 1
    """, resource_id)

    analysis = (
        f"Previous summary:\n{prev_summary or 'None'}\n\n"
        f"Changes since last scan ({len(changed_files)} files, {len(new_commits)} commits):\n"
        f"\nChanged files:\n" + "\n".join(changes) +
        f"\n\nCommits:\n" + "\n".join(commits_text)
    )

    try:
        summary = await _summarize_with_gemini(
            f"Update this repository summary based on the changes below. "
            f"Focus on what's new or different.\n\n{analysis}"
        )
    except SummarizationFailed as exc:
        # Note this also leaves head_sha unadvanced (it is set in the same
        # UPDATE below), so the next incremental scan re-examines the same
        # commits rather than skipping them as already-summarized.
        return _summarization_failure_result(
            resource_id=resource_id, kind="repo/incremental", exc=exc,
            start_time=start_time,
        )

    duration_ms = int((time.monotonic() - start_time) * 1000)

    metadata = {
        "changed_files": len(changed_files),
        "new_commits": len(new_commits),
        "head_sha": current_sha,
        "files_changed": [f.get("filename", "") for f in changed_files[:20]],
    }

    await _store_summary(
        resource_id=resource_id,
        goal_id=goal_id,
        summary_type="incremental",
        content=summary,
        metadata=metadata,
        previous_scan_sha=current_sha,
        scan_duration_ms=duration_ms,
    )

    await pool.execute("""
        UPDATE goal_resources
        SET last_scanned_at = now(), last_summary_at = now(),
            summary_stale = false, head_sha = $1
        WHERE id = $2
    """, current_sha, resource_id)

    return {"summary": summary, "metadata": metadata, "duration_ms": duration_ms}


async def read_repo_file(
    repo_owner: str,
    repo_name: str,
    file_path: str,
    branch: str = "main",
    github_token: str = "",
    max_lines: int = DEFAULT_MAX_LINES,
    offset: int = 0,
) -> dict:
    """Read a file from a GitHub repo via the RAW endpoint.

    Uses raw.githubusercontent.com (NOT the Contents API), so there is no 1 MB
    base64 content ceiling and no per-file byte error for ordinary source files.
    The returned content is a line window [offset : offset+max_lines]; a long
    file is paged via `offset` (see `next_offset` in the result) instead of
    failing with "File too large" or being silently cut at a fixed line.
    """
    import httpx

    headers = {"Accept": "application/vnd.github.raw+json"}
    if github_token:
        headers["Authorization"] = f"token {github_token}"

    url = f"https://raw.githubusercontent.com/{repo_owner}/{repo_name}/{branch}/{file_path}"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, headers=headers)
    except Exception as exc:
        return {"error": f"Failed to fetch {file_path}: {exc}"}

    if resp.status_code == 404:
        return {"error": f"File not found: {file_path}", "status": 404}
    if resp.status_code != 200:
        return {
            "error": f"GitHub returned {resp.status_code} for {file_path}",
            "status": resp.status_code,
        }

    text = resp.text
    # Download safety net only — the returned content is bounded by the line
    # window, so a normal file is never rejected; this just caps a huge blob.
    if len(text) > MAX_FILE_READ_BYTES:
        text = text[:MAX_FILE_READ_BYTES]

    result = window_lines(text, offset, max_lines)
    result["path"] = file_path
    result["size"] = len(text)
    return result


# ── Document scanning ───────────────────────────────────────────────────────

async def scan_document(
    resource_id: str,
    goal_id: str,
    gcs_uri: str,
    file_type: str,
    filename: str,
) -> dict:
    """Scan a document (PDF, Excel, Word, Markdown) and generate summary."""
    start_time = time.monotonic()
    _log.info("Scanning document: %s (type=%s)", filename, file_type)

    # Download from GCS
    content_bytes = await _download_from_gcs(gcs_uri)
    if not content_bytes:
        return {"error": f"Failed to download {gcs_uri}"}

    # Extract text based on file type
    if file_type in ("pdf",):
        extracted = await _extract_pdf(content_bytes, filename)
    elif file_type in ("xlsx", "xls"):
        extracted = await _extract_excel(content_bytes, filename)
    elif file_type in ("docx", "doc"):
        extracted = await _extract_word(content_bytes, filename)
    elif file_type in ("md", "txt", "markdown"):
        extracted = {
            "text": content_bytes.decode("utf-8", errors="replace"),
            "metadata": {"type": "text", "size": len(content_bytes)},
        }
    else:
        return {"error": f"Unsupported file type: {file_type}"}

    if "error" in extracted:
        return extracted

    # Summarize with Gemini
    text = extracted.get("text", "")[:50000]  # Limit input
    doc_metadata = extracted.get("metadata", {})

    try:
        summary = await _summarize_with_gemini(
            f"Summarize this {file_type.upper()} document.\n"
            f"Filename: {filename}\n"
            f"Metadata: {json.dumps(doc_metadata)}\n\n"
            f"Content:\n{text}"
        )
    except SummarizationFailed as exc:
        return _summarization_failure_result(
            resource_id=resource_id, kind="document", exc=exc,
            start_time=start_time, metadata=doc_metadata,
        )

    duration_ms = int((time.monotonic() - start_time) * 1000)

    await _store_summary(
        resource_id=resource_id,
        goal_id=goal_id,
        summary_type="full",
        content=summary,
        metadata=doc_metadata,
        scan_duration_ms=duration_ms,
    )

    pool = get_pool()
    await pool.execute("""
        UPDATE goal_resources
        SET last_scanned_at = now(), last_summary_at = now(), summary_stale = false
        WHERE id = $1
    """, resource_id)

    return {"summary": summary, "metadata": doc_metadata, "duration_ms": duration_ms}


# ── Document extractors ─────────────────────────────────────────────────────

async def _extract_pdf(data: bytes, filename: str) -> dict:
    """Extract text and structure from PDF."""
    try:
        import io
        import pdfplumber

        text_parts = []
        page_count = 0

        with pdfplumber.open(io.BytesIO(data)) as pdf:
            page_count = len(pdf.pages)
            for i, page in enumerate(pdf.pages[:50]):  # Max 50 pages
                page_text = page.extract_text() or ""
                if page_text.strip():
                    text_parts.append(f"--- Page {i + 1} ---\n{page_text}")

        return {
            "text": "\n\n".join(text_parts),
            "metadata": {"type": "pdf", "pages": page_count, "filename": filename},
        }
    except Exception as exc:
        _log.warning("PDF extraction failed for %s: %s", filename, exc)
        return {"error": f"PDF extraction failed: {exc}"}


async def _extract_excel(data: bytes, filename: str) -> dict:
    """Extract structure and sample data from Excel."""
    try:
        import io
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        sheets_info = []
        text_parts = []

        for sheet_name in wb.sheetnames[:10]:  # Max 10 sheets
            ws = wb[sheet_name]
            rows = list(ws.iter_rows(max_row=20, values_only=True))  # First 20 rows

            if not rows:
                sheets_info.append({"name": sheet_name, "rows": 0, "columns": 0})
                continue

            headers = [str(c) if c else "" for c in rows[0]]
            row_count = ws.max_row or 0
            col_count = ws.max_column or 0

            sheets_info.append({
                "name": sheet_name,
                "rows": row_count,
                "columns": col_count,
                "headers": headers[:20],
            })

            text_parts.append(f"--- Sheet: {sheet_name} ({row_count} rows, {col_count} cols) ---")
            text_parts.append(f"Headers: {', '.join(headers[:15])}")

            # Sample data (first 5 data rows)
            for row in rows[1:6]:
                vals = [str(c)[:50] if c else "" for c in row[:15]]
                text_parts.append("  " + " | ".join(vals))

        wb.close()

        return {
            "text": "\n".join(text_parts),
            "metadata": {"type": "excel", "sheets": sheets_info, "filename": filename},
        }
    except Exception as exc:
        _log.warning("Excel extraction failed for %s: %s", filename, exc)
        return {"error": f"Excel extraction failed: {exc}"}


async def _extract_word(data: bytes, filename: str) -> dict:
    """Extract text and structure from Word document."""
    try:
        import io
        import docx

        doc = docx.Document(io.BytesIO(data))
        text_parts = []
        headings = []

        for para in doc.paragraphs[:500]:  # Max 500 paragraphs
            if para.style.name.startswith("Heading"):
                headings.append({"level": para.style.name, "text": para.text})
                text_parts.append(f"\n## {para.text}")
            elif para.text.strip():
                text_parts.append(para.text)

        # Tables
        table_info = []
        for i, table in enumerate(doc.tables[:10]):
            rows = len(table.rows)
            cols = len(table.columns)
            table_info.append({"index": i, "rows": rows, "columns": cols})
            if rows > 0:
                header = [cell.text[:30] for cell in table.rows[0].cells[:10]]
                text_parts.append(f"\n[Table {i + 1}: {rows}x{cols}] Headers: {', '.join(header)}")

        return {
            "text": "\n".join(text_parts),
            "metadata": {
                "type": "docx",
                "headings": headings[:20],
                "tables": table_info,
                "paragraph_count": len(doc.paragraphs),
                "filename": filename,
            },
        }
    except Exception as exc:
        _log.warning("Word extraction failed for %s: %s", filename, exc)
        return {"error": f"Word extraction failed: {exc}"}


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _download_from_gcs(gcs_uri: str) -> Optional[bytes]:
    """Download file from GCS."""
    try:
        from app.storage import get_gcs_client
        # Parse gs://bucket/path
        if not gcs_uri.startswith("gs://"):
            return None
        parts = gcs_uri[5:].split("/", 1)
        if len(parts) != 2:
            return None
        bucket_name, blob_path = parts
        client = get_gcs_client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_path)
        return blob.download_as_bytes()
    except Exception as exc:
        _log.warning("GCS download failed for %s: %s", gcs_uri, exc)
        return None


async def _fetch_file_content(client, base_url: str, filename: str, branch: str, headers: dict) -> Optional[str]:
    """Fetch a single file from GitHub API."""
    import base64
    url = f"{base_url}/contents/{filename}?ref={branch}"
    try:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if data.get("size", 0) > MAX_FILE_READ_BYTES:
            return None
        return base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")
    except Exception:
        return None


def _build_file_tree(files: list, max_depth: int = 3) -> str:
    """Build a readable file tree from GitHub tree API response."""
    tree: dict = {}
    for f in files:
        if f.get("type") != "blob" and f.get("type") != "tree":
            continue
        path = f.get("path", "")
        parts = path.split("/")
        if len(parts) > max_depth + 1:
            continue
        current = tree
        for part in parts[:-1]:
            current = current.setdefault(part + "/", {})
        if f.get("type") == "blob":
            current[parts[-1]] = None  # leaf

    lines = []
    _render_tree(tree, lines, "")
    return "\n".join(lines[:200])  # Limit output


def _render_tree(node: dict, lines: list, prefix: str):
    """Recursively render tree structure."""
    items = sorted(node.keys())
    for i, name in enumerate(items):
        is_last = i == len(items) - 1
        connector = "└── " if is_last else "├── "
        lines.append(f"{prefix}{connector}{name}")
        if node[name] is not None:  # directory
            extension = "    " if is_last else "│   "
            _render_tree(node[name], lines, prefix + extension)


class SummarizationFailed(Exception):
    """The utility model could not summarize this resource.

    Raised rather than returned so a failure cannot be mistaken for a
    summary. It previously returned the string ``f"Summarization failed:
    {exc}"``, which callers stored verbatim as the resource's summary AND
    stamped ``last_scanned_at = now(), summary_stale = false`` alongside —
    two separate problems:

      * the resource was marked freshly scanned, so it was never retried;
      * ``get_resource_summary`` served that error text to GO as the
        repository's description.

    A scan that failed should look like it hasn't happened, which is what
    the sibling gitingest failure path already does by returning before
    the stamping UPDATE.
    """


async def _summarize_with_gemini(prompt: str) -> str:
    """Call the configured utility model (resource_scan_summary purpose) for
    summarization. Raises `SummarizationFailed` — never returns error text."""
    from app.services.utility_llm import UtilityLLMError, generate_utility_text

    try:
        return await generate_utility_text("resource_scan_summary", prompt[:100000])
    except UtilityLLMError as exc:
        raise SummarizationFailed(str(exc)) from exc


def _summarization_failure_result(
    *, resource_id: str, kind: str, exc: Exception, start_time: float,
    metadata: dict | None = None,
) -> dict:
    """Uniform result for a scan whose summarization failed.

    Deliberately does NOT call `_store_summary` and the caller must return
    this BEFORE its `UPDATE goal_resources ... summary_stale = false`, so the
    resource stays stale and is picked up by the next scan. Writing nothing is
    the point: a stored failure is both a lie to `get_resource_summary` and a
    reason never to retry.
    """
    duration_ms = int((time.monotonic() - start_time) * 1000)
    _log.warning(
        "Resource scan (%s) for resource_id=%s produced no summary — %s. "
        "Leaving the resource stale so the next scan retries; nothing stored.",
        kind, resource_id, exc,
    )
    return {
        "summary": "",
        "error": f"Summarization failed: {exc}",
        "metadata": metadata or {},
        "duration_ms": duration_ms,
    }


async def _store_summary(
    resource_id: str,
    goal_id: str,
    summary_type: str,
    content: str,
    metadata: dict,
    previous_scan_sha: str = "",
    scan_duration_ms: int = 0,
):
    """Store a resource summary in the database."""
    pool = get_pool()
    await pool.execute("""
        INSERT INTO resource_summaries
            (resource_id, goal_id, summary_type, content, metadata,
             previous_scan_sha, scan_duration_ms, token_count)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
    """, resource_id, goal_id, summary_type, content,
        json.dumps(metadata), previous_scan_sha, scan_duration_ms,
        len(content) // 4)  # rough token estimate


# ── Get summary for GO tools ────────────────────────────────────────────────

async def declared_trunks(goal_id: str, repo_name: str,
                          repo_owner: str = "") -> list:
    """The branches this goal has designated as trunks for this repo.

    A goal declares them by ADDING them -- `kind='branch'` with
    `role='trunk'` -- and there can be several: one repository can carry
    a backend lane and a frontend lane in the same goal, each its own
    merge target. `repo_identity.resolve_ws_trunk` already works this
    way; the lane a story lands on is the trunk resource it was
    GRANTED, not a property of the repository.
    """
    if not (goal_id and repo_name):
        return []
    rows = await get_pool().fetch(
        "SELECT branch_name FROM goal_resources "
        "WHERE goal_id = $1 AND kind = 'branch' AND role = 'trunk' "
        "  AND repo_name = $2 "
        # OWNER TOO, or two same-named repositories from different
        # owners in one goal suppress each other's summaries -- and the
        # symptom is a card that is simply never built.
        "  AND lower(coalesce(repo_owner, '')) = lower(coalesce($3, '')) "
        "  AND coalesce(branch_name, '') <> '' "
        "  AND coalesce(status, 'active') = 'active'",
        goal_id, repo_name, repo_owner)
    return [r["branch_name"] for r in rows]


async def trunk_branch_for(resource: dict) -> str:
    """The branch a REPO row is summarised from, when one is at all.

    ONLY REACHED WHEN THE GOAL HAS DECLARED NO TRUNKS. If it has, the
    repo row is not summarised -- see `scan_resource` -- because work
    happens at BRANCH level and each declared trunk is its own resource
    with its own summary. A repo-level card that picks one of them is an
    overview that can contradict the cards beneath it.

    NO SECOND RESOLVER. An earlier version of this resolved the trunk
    here, with its own precedence and its own ordering, and that was the
    real defect: `resolve_repo_trunk` already answers it for the engine,
    and mine took the NEWEST declared trunk where it takes the OLDEST,
    so on a two-trunk goal the engine would have cut from one branch
    while the summary described the other.

    With no declared trunk there is nothing to disagree about: the
    repository's own default branch is what the engine falls back to,
    and it is what this uses.
    """
    return resource.get("default_branch") or "main"


async def scan_resource(goal_id: str, resource_id: str) -> dict:
    """Trigger a fresh scan for any supported resource kind.

    Dispatches to the appropriate scanner (scan_repo_full / scan_repo_incremental
    / scan_document) based on the resource's kind.  GitHub tokens are fetched
    internally so callers don't need to know what kind of resource they're scanning.

    Returns the scanner result dict, or raises ValueError for unsupported kinds.
    This is the canonical entry point used by both the GO internal API and the
    Slack /resources scan command.
    """
    pool = get_pool()
    resource = await pool.fetchrow(
        "SELECT * FROM goal_resources WHERE id = $1 AND goal_id = $2",
        resource_id, goal_id,
    )
    if not resource:
        raise ValueError(f"Resource {resource_id} not found in goal {goal_id}")

    kind = resource["kind"]
    _log.info("scan_resource: goal=%s resource=%s kind=%s", goal_id, resource_id, kind)

    if kind == "repo":
        # THE BRANCH CARDS ARE THE ANSWER, so the repo card does not
        # compete with them.
        #
        # Work happens at BRANCH level: a goal declares the branches it
        # works on as resources, one repository can carry several trunk
        # lanes in one goal, and which lane a story lands on is decided
        # by the trunk resource it was granted
        # (`repo_identity.resolve_ws_trunk`). Nothing in the engine asks
        # "what is THE repo's branch".
        #
        # A summary is a scan of ONE branch's tree, so a repo card can
        # only ever show one of those lanes -- an overview that silently
        # equals one of the cards beneath it, and contradicts the rest.
        # Observed on NCAL2: two declared trunks, `cal_backend` and
        # `cal_frontend`, each correctly summarised, and a repo card
        # describing a third branch entirely.
        #
        # A goal that has declared NOTHING still gets a repo summary --
        # it is the only thing there is to describe, and with no lanes
        # there is nothing for it to contradict.
        trunks = await declared_trunks(goal_id,
                                       resource.get("repo_name", ""),
                                       resource.get("repo_owner", ""))
        if trunks:
            _log.info(
                "resource %s: repo-level summary skipped; this goal works "
                "on %d declared trunk branch(es) (%s), each summarised "
                "from its own branch",
                resource_id, len(trunks), ", ".join(sorted(trunks)))
            return {"skipped": "repo-level summary not built",
                    "reason": "this goal declares trunk branches; each is "
                              "summarised from its own branch",
                    "trunks": sorted(trunks)}

        github_token = await _github_token_for_goal(goal_id, resource.get("repo_name", ""))
        previous_sha = resource.get("head_sha", "")
        if previous_sha:
            return await scan_repo_incremental(
                resource_id, goal_id,
                resource["repo_owner"], resource["repo_name"],
                await trunk_branch_for(dict(resource)),
                previous_sha, github_token,
            )
        return await scan_repo_full(
            resource_id, goal_id,
            resource["repo_owner"], resource["repo_name"],
            await trunk_branch_for(dict(resource)),
            github_token,
        )

    if kind == "branch":
        repo_owner = resource.get("repo_owner", "")
        repo_name = resource.get("repo_name", "")
        if not repo_owner and resource.get("parent_id"):
            parent = await pool.fetchrow(
                "SELECT * FROM goal_resources WHERE id = $1", resource["parent_id"],
            )
            if parent:
                repo_owner = parent.get("repo_owner", "")
                repo_name = parent.get("repo_name", "")
        if not repo_owner or not repo_name:
            raise ValueError("Cannot determine repo for this branch resource")
        github_token = await _github_token_for_goal(goal_id, repo_name)
        # A BRANCH RESOURCE IS ABOUT ITS OWN BRANCH, so `branch_name`
        # stands. Only the FALLBACK changes: a branch row with no name
        # is a bug, and scanning the trunk says something true about the
        # repository rather than something false about `main`.
        return await scan_repo_full(
            resource_id, goal_id,
            repo_owner, repo_name,
            resource.get("branch_name")
            or await trunk_branch_for(dict(resource)),
            github_token,
        )

    if kind in ("file", "document") and resource.get("gcs_uri"):
        return await scan_document(
            resource_id, goal_id,
            resource["gcs_uri"],
            resource.get("file_type", ""),
            resource["name"],
        )

    raise ValueError(f"Scanning not supported for resource kind: {kind}")


async def _github_token_for_goal(goal_id: str, repo_name: str = "") -> str:
    """Fetch a GitHub installation token scoped to the goal's repos.

    Moved here from go_internal._get_github_token so any caller (GO tools,
    Slack commands, future integrations) can trigger scans without duplicating
    the token-fetch logic.

    Scanning only ever READS repo content (gitingest clone, file fetch), so we
    request contents:read + metadata:read only — least privilege.  This is
    deliberately narrower than go_internal._get_github_token (which defaults to
    write for branch creation / commits); do NOT widen it here just to share
    one helper, or every scan would mint a write-capable token unnecessarily.
    """
    try:
        from app.github_tokens import generate_installation_token
        pool = get_pool()
        if not repo_name:
            repos = await pool.fetch("""
                SELECT DISTINCT repo_name FROM goal_resources
                 WHERE goal_id = $1 AND repo_name != '' AND status = 'active'
            """, goal_id)
            repo_names = [r["repo_name"] for r in repos if r["repo_name"]]
        else:
            repo_names = [repo_name]

        if not repo_names:
            _log.warning("_github_token_for_goal: no repos found for goal %s — token unscoped", goal_id)

        result = await generate_installation_token(
            repo_names=repo_names,
            permissions={"contents": "read", "metadata": "read"},
        )
        return result.get("token", "") if isinstance(result, dict) else (result or "")
    except Exception as exc:
        _log.warning("_github_token_for_goal: token fetch failed goal=%s: %s", goal_id, exc)
        return ""


async def get_resource_summary(resource_id: str) -> Optional[dict]:
    """Get the latest summary for a resource (used by GO tools)."""
    pool = get_pool()
    row = await pool.fetchrow("""
        SELECT rs.content, rs.summary_type, rs.metadata, rs.scanned_at,
               rs.summary_updated_at, rs.previous_scan_sha,
               gr.name, gr.kind, gr.file_type, gr.last_scanned_at, gr.last_summary_at,
               gr.summary_stale
        FROM resource_summaries rs
        JOIN goal_resources gr ON gr.id = rs.resource_id
        WHERE rs.resource_id = $1
        ORDER BY rs.created_at DESC LIMIT 1
    """, resource_id)

    if not row:
        return None

    return {
        "resource_name": row["name"],
        "resource_kind": row["kind"],
        "summary": row["content"],
        "summary_type": row["summary_type"],
        "metadata": json.loads(row["metadata"]) if isinstance(row["metadata"], str) else row["metadata"],
        "scanned_at": row["scanned_at"].isoformat() if row["scanned_at"] else None,
        "summary_updated_at": row["summary_updated_at"].isoformat() if row["summary_updated_at"] else None,
        "is_stale": row["summary_stale"],
    }
