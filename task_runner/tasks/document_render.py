"""
Document-render task — extracts readable HTML from uploaded resources.

This is one task handler for the generic task runner (see task_runner/main.py).
It exposes a single `run_once()` entry point that the dispatcher calls:

  run_once() -> bool
    Claim ONE pending row from resource_renders (SELECT FOR UPDATE SKIP LOCKED),
    render it, write the result back, and return True. If no pending row exists
    (duplicate trigger / race), do nothing and return False.

Execution detail:
  1. Claim the oldest pending row; join goal_resources for the GCS URI + file type.
  2. Download file bytes from GCS.
  3. Extract text/table HTML by file type:
     - PDF: pdfplumber (text layer) → pytesseract OCR fallback if text is sparse.
     - DOCX/DOC: python-docx paragraphs + tables (antiword for old binary .doc).
     - XLSX/XLS: openpyxl per-sheet HTML tables (first 500 rows/sheet).
     - CSV: stdlib csv, first 500 rows.
     - Images (png/jpg/jpeg/gif/webp/bmp/tiff): pytesseract OCR.
  4. Write render_status='done' + rendered_html back to resource_renders.
     On any unhandled error: render_status='failed' with the error message so the
     web tier shows an error card (permanent — user must manually retry via UI).
"""

import csv
import html as _html_lib
import io
import logging

import psycopg2

from task_runner.config import DATABASE_URL

_log = logging.getLogger("document_render")

# ── Limits ─────────────────────────────────────────────────────────────────────

MAX_PDF_PAGES = 30
MAX_ROWS = 500
SIZE_LIMIT = 5 * 1024 * 1024       # 5 MB  — hard cap for all file types
DOCX_RENDER_LIMIT = 2 * 1024 * 1024  # 2 MB — DOCX above this writes done/empty so UI shows download button


# ── GCS download ─────────────────────────────────────────────────────────────

def download_from_gcs(gcs_uri: str) -> bytes:
    """Download a file from GCS and return its bytes."""
    from google.cloud import storage as gcs_lib

    _log.info("downloading gcs_uri=%s", gcs_uri)
    if not gcs_uri.startswith("gs://"):
        raise ValueError(f"invalid GCS URI: {gcs_uri!r}")

    without_prefix = gcs_uri[5:]
    bucket_name, _, blob_path = without_prefix.partition("/")

    client = gcs_lib.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_path)
    data = blob.download_as_bytes()
    _log.info("downloaded %d bytes from %s", len(data), gcs_uri)
    return data


# ── Extraction helpers ────────────────────────────────────────────────────────

def _extract_pdf(data: bytes) -> str:
    """Extract text from a PDF. Falls back to OCR if the text layer is empty."""
    import pdfplumber

    _log.info("extracting PDF (%d bytes)", len(data))
    parts: list[str] = []
    total_chars = 0

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        page_count = len(pdf.pages)
        _log.info("PDF has %d page(s); rendering first %d", page_count, MAX_PDF_PAGES)

        for i, page in enumerate(pdf.pages[:MAX_PDF_PAGES], start=1):
            text = page.extract_text() or ""
            total_chars += len(text)
            escaped = _html_lib.escape(text)
            parts.append(
                f'<div class="pdf-page">'
                f'<div class="pdf-page-label">Page {i}</div>'
                f'<pre class="pdf-page-text">{escaped}</pre>'
                f"</div>"
            )

    if total_chars < 100:
        # Image-based PDF — no meaningful text layer; run OCR.
        _log.info("text layer sparse (%d chars); falling back to OCR", total_chars)
        return _ocr_pdf(data)

    suffix = ""
    if page_count > MAX_PDF_PAGES:
        suffix = (
            f'<p class="render-limit-note">Showing first {MAX_PDF_PAGES} of '
            f"{page_count} pages.</p>"
        )
    _log.info("PDF text extraction done: %d pages, %d chars", min(page_count, MAX_PDF_PAGES), total_chars)
    return "".join(parts) + suffix


def _ocr_pdf(data: bytes) -> str:
    """Rasterize PDF pages and OCR each one with pytesseract."""
    from pdf2image import convert_from_bytes
    import pytesseract

    _log.info("OCR-rendering PDF (%d bytes)", len(data))
    images = convert_from_bytes(data, first_page=1, last_page=MAX_PDF_PAGES)
    _log.info("rasterized %d page(s) for OCR", len(images))

    parts: list[str] = []
    for i, img in enumerate(images, start=1):
        text = pytesseract.image_to_string(img)
        escaped = _html_lib.escape(text)
        parts.append(
            f'<div class="pdf-page">'
            f'<div class="pdf-page-label">Page {i} (OCR)</div>'
            f'<pre class="pdf-page-text">{escaped}</pre>'
            f"</div>"
        )
        _log.info("OCR page %d: %d chars", i, len(text))

    return "".join(parts)


def _extract_doc_binary(data: bytes) -> str:
    """Extract text from old binary .doc files using antiword."""
    import subprocess
    import tempfile
    import os as _os

    _log.info("_extract_doc_binary: old binary .doc (%d bytes) — using antiword", len(data))
    with tempfile.NamedTemporaryFile(suffix=".doc", delete=False) as f:
        f.write(data)
        tmp_path = f.name
    try:
        result = subprocess.run(
            ["antiword", tmp_path],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            raise ValueError(f"antiword exited {result.returncode}: {result.stderr.strip()[:300]}")
        text = result.stdout
        _log.info("_extract_doc_binary: antiword extracted %d chars", len(text))
        escaped = _html_lib.escape(text)
        return f'<pre class="doc-text">{escaped}</pre>'
    finally:
        _os.unlink(tmp_path)


def _extract_docx(data: bytes) -> str:
    """Extract paragraphs and tables from a DOCX file.

    Falls back to antiword for old binary .doc files (pre-Office 2007)
    which are not ZIP archives and cannot be opened by python-docx.
    """
    import zipfile
    from docx import Document

    _log.info("extracting DOCX/DOC (%d bytes)", len(data))
    if not zipfile.is_zipfile(io.BytesIO(data)):
        _log.info("file is not a ZIP archive — falling back to antiword for binary .doc")
        return _extract_doc_binary(data)

    try:
        doc = Document(io.BytesIO(data))
    except Exception as exc:
        # ZIP-based but not valid OOXML — missing [Content_Types].xml or other
        # required parts. Antiword won't help (it only handles binary .doc).
        raise ValueError(
            f"python-docx cannot open this file: {exc}. "
            "It is a ZIP archive but not a valid OOXML document — "
            "possibly corrupted or created by a non-Microsoft tool."
        ) from exc
    parts: list[str] = []

    HEADING_TAGS = {
        "Heading 1": "h2",
        "Heading 2": "h3",
        "Heading 3": "h4",
    }

    for block in doc.element.body:
        tag = block.tag.split("}")[-1] if "}" in block.tag else block.tag
        if tag == "p":
            from docx.text.paragraph import Paragraph
            para = Paragraph(block, doc)
            style_name = para.style.name if para.style else ""
            text = para.text
            if not text.strip():
                continue
            escaped = _html_lib.escape(text)
            html_tag = HEADING_TAGS.get(style_name, "p")
            parts.append(f"<{html_tag}>{escaped}</{html_tag}>")
        elif tag == "tbl":
            from docx.table import Table
            table = Table(block, doc)
            rows_html = ["<table>"]
            for row in table.rows:
                cells = "".join(
                    f"<td>{_html_lib.escape(cell.text)}</td>" for cell in row.cells
                )
                rows_html.append(f"<tr>{cells}</tr>")
            rows_html.append("</table>")
            parts.append("\n".join(rows_html))

    _log.info("DOCX extraction done: %d content blocks", len(parts))
    return "\n".join(parts)


def _extract_xlsx(data: bytes) -> str:
    """Extract per-sheet HTML tables from an XLSX file (first 500 rows per sheet)."""
    import openpyxl

    _log.info("extracting XLSX (%d bytes)", len(data))
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    parts: list[str] = []

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        rows_html = [
            f'<h3 class="sheet-name">{_html_lib.escape(sheet_name)}</h3>',
            "<table>",
        ]
        row_count = 0
        truncated = False

        for row in ws.iter_rows():
            if row_count >= MAX_ROWS:
                truncated = True
                break
            cells = "".join(
                f"<td>{_html_lib.escape(str(cell.value) if cell.value is not None else '')}</td>"
                for cell in row
            )
            rows_html.append(f"<tr>{cells}</tr>")
            row_count += 1

        rows_html.append("</table>")
        if truncated:
            rows_html.append(
                f'<p class="render-limit-note">Showing first {MAX_ROWS} rows of this sheet.</p>'
            )
        parts.append("\n".join(rows_html))
        _log.info("sheet '%s': %d rows rendered", sheet_name, row_count)

    wb.close()
    return "\n".join(parts)


def _extract_csv(data: bytes) -> str:
    """Parse CSV bytes into an HTML table (first 500 rows)."""
    _log.info("extracting CSV (%d bytes)", len(data))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")

    reader = csv.reader(io.StringIO(text))
    rows_html = ["<table>"]
    row_count = 0
    truncated = False

    for row in reader:
        if row_count >= MAX_ROWS:
            truncated = True
            break
        if row_count == 0:
            # Treat first row as header
            cells = "".join(f"<th>{_html_lib.escape(c)}</th>" for c in row)
            rows_html.append(f"<tr>{cells}</tr>")
        else:
            cells = "".join(f"<td>{_html_lib.escape(c)}</td>" for c in row)
            rows_html.append(f"<tr>{cells}</tr>")
        row_count += 1

    rows_html.append("</table>")
    if truncated:
        rows_html.append(
            f'<p class="render-limit-note">Showing first {MAX_ROWS} rows.</p>'
        )
    _log.info("CSV extraction done: %d rows", row_count)
    return "\n".join(rows_html)


def _extract_image(data: bytes) -> str:
    """OCR an image file and return the extracted text as preformatted HTML."""
    import pytesseract
    from PIL import Image

    _log.info("OCR-extracting image (%d bytes)", len(data))
    img = Image.open(io.BytesIO(data))
    text = pytesseract.image_to_string(img)
    escaped = _html_lib.escape(text)
    _log.info("image OCR done: %d chars", len(text))
    return f'<pre class="ocr-text">{escaped}</pre>'


def extract(file_type: str, data: bytes) -> str:
    """Dispatch to the appropriate extraction function by file type."""
    if file_type == "pdf":
        return _extract_pdf(data)
    if file_type in ("docx", "doc"):
        return _extract_docx(data)
    if file_type in ("xlsx", "xls"):
        return _extract_xlsx(data)
    if file_type == "csv":
        return _extract_csv(data)
    if file_type in ("png", "jpg", "jpeg", "gif", "webp", "bmp", "tiff", "tif"):
        return _extract_image(data)
    raise ValueError(f"unsupported file type: {file_type!r}")


# ── DB write ──────────────────────────────────────────────────────────────────

def upsert_render_done(resource_id: str, rendered_html: str) -> None:
    """Write a successful render result — resets retry counters."""
    _log.info("writing render result: resource_id=%s status=done", resource_id)
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO resource_renders
                        (resource_id, rendered_html, render_status, error_message, updated_at)
                    VALUES (%s, %s, 'done', NULL, now())
                    ON CONFLICT (resource_id) DO UPDATE
                        SET rendered_html  = EXCLUDED.rendered_html,
                            render_status  = 'done',
                            error_message  = NULL,
                            retry_count    = 0,
                            retry_after    = NULL,
                            updated_at     = now()
                    """,
                    (resource_id, rendered_html),
                )
    finally:
        conn.close()
    _log.info("render result written: resource_id=%s status=done", resource_id)


def upsert_render_failed(resource_id: str, error: str) -> None:
    """Write a permanent render failure — sets retry_count=999 so the web tier
    shows the error card immediately without auto-retrying.

    Render failures are code errors (bad file format, corrupt OOXML, etc.) that
    won't be fixed by re-running. The user must manually retry via the UI button.
    """
    _log.info(
        "writing render result: resource_id=%s status=failed (permanent) error=%r",
        resource_id, error,
    )
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO resource_renders
                        (resource_id, rendered_html, render_status, error_message,
                         retry_count, retry_after, updated_at)
                    VALUES (%s, '', 'failed', %s, 999, NULL, now())
                    ON CONFLICT (resource_id) DO UPDATE
                        SET rendered_html  = '',
                            render_status  = 'failed',
                            error_message  = EXCLUDED.error_message,
                            retry_count    = 999,
                            retry_after    = NULL,
                            updated_at     = now()
                    """,
                    (resource_id, error),
                )
    finally:
        conn.close()
    _log.info("render result written: resource_id=%s status=failed (permanent)", resource_id)


# ── Claim pending work from DB ────────────────────────────────────────────────

def claim_pending_resource() -> tuple[str, str, str] | None:
    """Claim the oldest pending render row and return (resource_id, gcs_uri, file_type).

    Uses SELECT FOR UPDATE SKIP LOCKED so concurrent executions don't double-process
    the same row. Returns None if no pending work exists (trigger was a duplicate).
    The claimed row is left in 'pending' status — the caller writes 'done' or 'failed'.
    """
    _log.info("claim_pending_resource: querying for oldest pending render row")
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT rr.resource_id, gr.gcs_uri, gr.file_type
                    FROM resource_renders rr
                    JOIN goal_resources gr ON gr.id = rr.resource_id
                    WHERE rr.render_status = 'pending'
                    ORDER BY rr.updated_at ASC
                    FOR UPDATE OF rr SKIP LOCKED
                    LIMIT 1
                    """,
                )
                row = cur.fetchone()
    finally:
        conn.close()

    if row is None:
        _log.info("claim_pending_resource: no pending rows — nothing to do")
        return None

    resource_id, gcs_uri, file_type = row
    gcs_uri = (gcs_uri or "").strip()
    file_type = (file_type or "").lower().lstrip(".")
    _log.info(
        "claim_pending_resource: claimed resource_id=%s file_type=%s gcs_uri=%s",
        resource_id, file_type, gcs_uri,
    )
    return resource_id, gcs_uri, file_type


# ── Task entry point ───────────────────────────────────────────────────────────

def run_once() -> bool:
    """Process one pending document render, if any.

    Returns True if a row was claimed and processed (done or failed), False if
    there was no pending work. The dispatcher uses the return value to decide
    whether this task had anything to do this execution.
    """
    work = claim_pending_resource()
    if work is None:
        return False

    resource_id, gcs_uri, file_type = work
    _log.info(
        "document_render: processing resource_id=%s file_type=%s gcs_uri=%s",
        resource_id, file_type, gcs_uri,
    )

    try:
        if not gcs_uri:
            raise ValueError(
                f"resource_id={resource_id} has no GCS URI (uri={gcs_uri!r}) — cannot render"
            )

        data = download_from_gcs(gcs_uri)

        if len(data) > SIZE_LIMIT:
            raise ValueError(
                f"file too large: {len(data)} bytes exceeds {SIZE_LIMIT} byte limit"
            )

        # DOCX/DOC above the render limit would produce too much HTML content for the
        # browser to handle comfortably. Write a done row with empty HTML so the web
        # tier falls back to the download button without showing an error card.
        if file_type in ("docx", "doc") and len(data) > DOCX_RENDER_LIMIT:
            _log.info(
                "document_render: DOCX too large to render inline "
                "(%d bytes > %d limit) — writing done/empty so UI shows download",
                len(data), DOCX_RENDER_LIMIT,
            )
            upsert_render_done(resource_id, "")
            _log.info("document_render: complete (no-render): resource_id=%s", resource_id)
            return True

        rendered_html = extract(file_type, data)
        upsert_render_done(resource_id, rendered_html)
        _log.info("document_render: complete: resource_id=%s", resource_id)
        return True

    except Exception as exc:
        _log.exception("document_render: failed resource_id=%s error=%s", resource_id, exc)
        try:
            upsert_render_failed(resource_id, str(exc))
        except Exception as db_exc:
            _log.exception("document_render: failed to write error to DB resource_id=%s %s", resource_id, db_exc)
        # A processing failure is still "work done" from the dispatcher's view — the
        # row moved out of pending. Re-raise so the execution exits non-zero and the
        # failure is visible in Cloud Run execution history.
        raise
