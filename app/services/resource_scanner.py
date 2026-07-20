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

# Max file size to read via API (prevent huge files)
MAX_FILE_READ_BYTES = 100_000  # 100KB


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
        head_sha = compare_data.get("ahead_by", 0)

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

    summary = await _summarize_with_gemini(
        f"Update this repository summary based on the changes below. "
        f"Focus on what's new or different.\n\n{analysis}"
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
    max_lines: int = 500,
) -> dict:
    """Read a specific file from a GitHub repo via API."""
    import httpx

    headers = {"Accept": "application/vnd.github.v3+json"}
    if github_token:
        headers["Authorization"] = f"token {github_token}"

    url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/contents/{file_path}?ref={branch}"

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=headers)

    if resp.status_code != 200:
        return {"error": f"File not found: {file_path}", "status": resp.status_code}

    data = resp.json()
    if data.get("size", 0) > MAX_FILE_READ_BYTES:
        return {"error": f"File too large: {data['size']} bytes (max {MAX_FILE_READ_BYTES})"}

    import base64
    content = base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")

    # Trim to max_lines
    lines = content.split("\n")
    truncated = len(lines) > max_lines
    if truncated:
        content = "\n".join(lines[:max_lines]) + f"\n\n... truncated ({len(lines)} total lines)"

    return {
        "path": file_path,
        "content": content,
        "size": data.get("size", 0),
        "truncated": truncated,
        "total_lines": len(lines),
    }


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

    summary = await _summarize_with_gemini(
        f"Summarize this {file_type.upper()} document.\n"
        f"Filename: {filename}\n"
        f"Metadata: {json.dumps(doc_metadata)}\n\n"
        f"Content:\n{text}"
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


async def _summarize_with_gemini(prompt: str) -> str:
    """Call Gemini Flash for summarization."""
    settings = get_settings()
    if not settings.gemini_api_key:
        _log.warning("GEMINI_API_KEY not configured — returning raw text")
        return prompt[:2000]

    try:
        from google import genai
        client = genai.Client(api_key=settings.gemini_api_key)
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt[:100000],  # Limit prompt size
        )
        return response.text.strip()
    except Exception as exc:
        _log.warning("Gemini summarization failed: %s", exc)
        return f"Summarization failed: {exc}"


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
        github_token = await _github_token_for_goal(goal_id, resource.get("repo_name", ""))
        previous_sha = resource.get("head_sha", "")
        if previous_sha:
            return await scan_repo_incremental(
                resource_id, goal_id,
                resource["repo_owner"], resource["repo_name"],
                resource.get("default_branch", "main"),
                previous_sha, github_token,
            )
        return await scan_repo_full(
            resource_id, goal_id,
            resource["repo_owner"], resource["repo_name"],
            resource.get("default_branch", "main"),
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
        return await scan_repo_full(
            resource_id, goal_id,
            repo_owner, repo_name,
            resource.get("branch_name", "main"),
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
