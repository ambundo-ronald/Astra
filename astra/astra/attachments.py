import csv
import io
import os

import frappe

from astra import safety


MAX_TEXT_CHARS = 6000
MAX_PREVIEW_ROWS = 20


def analyze_attachment(attachment_name, target_doctype=None):
    doc = frappe.get_doc("Astra Attachment", attachment_name)
    if doc.user != frappe.session.user and "System Manager" not in frappe.get_roles():
        frappe.throw("You do not have access to this attachment.")

    file_doc = _get_file_doc(doc.file)
    if not file_doc.has_permission("read"):
        frappe.throw("You do not have permission to read this file.")

    extension = os.path.splitext(file_doc.file_name or file_doc.file_url or "")[1].lower()
    if extension == ".csv":
        result = _analyze_csv(file_doc, target_doctype)
    elif extension == ".pdf":
        result = _analyze_pdf(file_doc)
    elif extension in {".png", ".jpg", ".jpeg", ".webp"}:
        result = _analyze_image(file_doc)
    else:
        result = _analyze_text(file_doc)

    doc.summary = result.get("summary")
    doc.import_plan = frappe.as_json(result.get("import_plan") or {})
    doc.status = "Planned" if result.get("import_plan") else "Summarized"
    doc.save(ignore_permissions=True)
    return result


def _get_file_doc(file_value):
    file_name = frappe.db.get_value("File", {"file_url": file_value}, "name") or file_value
    return frappe.get_doc("File", file_name)


def _get_file_bytes(file_doc):
    path = file_doc.get_full_path()
    with open(path, "rb") as handle:
        return handle.read()


def _analyze_csv(file_doc, target_doctype=None):
    raw = _get_file_bytes(file_doc)
    text = raw.decode("utf-8-sig", errors="replace")
    sample = text[:MAX_TEXT_CHARS]
    reader = csv.DictReader(io.StringIO(sample))
    rows = [safety.redact_mapping(row) for row in list(reader)[:MAX_PREVIEW_ROWS]]
    columns = reader.fieldnames or []
    import_plan = {}

    if target_doctype:
        import_plan = _build_import_plan(target_doctype, columns, rows)

    summary = (
        f"CSV file with {len(columns)} columns. "
        f"Previewed {len(rows)} rows. "
        "Import requires explicit user confirmation before any ERPNext records are created."
    )
    return {
        "type": "csv",
        "summary": summary,
        "preview": {"columns": columns, "rows": rows},
        "import_plan": import_plan,
    }


def _build_import_plan(target_doctype, columns, rows):
    if not frappe.has_permission(target_doctype, "create"):
        return {
            "target_doctype": target_doctype,
            "allowed": False,
            "reason": f"Current user cannot create {target_doctype}.",
        }

    meta = frappe.get_meta(target_doctype)
    fields = {
        (field.label or field.fieldname or "").strip().lower(): field.fieldname
        for field in meta.fields
        if field.fieldname
    }
    fields.update(
        {
            (field.fieldname or "").strip().lower(): field.fieldname
            for field in meta.fields
            if field.fieldname
        }
    )
    mapping = {}
    unmapped = []
    for column in columns:
        fieldname = fields.get((column or "").strip().lower())
        if fieldname and not safety.is_sensitive_field(fieldname):
            mapping[column] = fieldname
        else:
            unmapped.append(column)

    return {
        "target_doctype": target_doctype,
        "allowed": True,
        "row_count_previewed": len(rows),
        "field_mapping": mapping,
        "unmapped_columns": unmapped,
        "requires_confirmation": True,
    }


def _analyze_pdf(file_doc):
    try:
        from pypdf import PdfReader
    except Exception:
        return {
            "type": "pdf",
            "summary": "PDF uploaded. Install pypdf in the bench environment to enable local PDF text extraction.",
            "preview": {},
            "import_plan": {},
        }

    reader = PdfReader(io.BytesIO(_get_file_bytes(file_doc)))
    pages = []
    for page in reader.pages[:5]:
        pages.append(page.extract_text() or "")
    text = safety.sanitize_context_text("\n".join(pages))
    return {
        "type": "pdf",
        "summary": _summarize_text(text, "PDF"),
        "preview": {"text": text[:MAX_TEXT_CHARS]},
        "import_plan": {},
    }


def _analyze_image(file_doc):
    return {
        "type": "image",
        "summary": "Image uploaded. OCR/vision extraction is not enabled yet; Astra will not import image data automatically.",
        "preview": {"file": file_doc.file_url},
        "import_plan": {},
    }


def _analyze_text(file_doc):
    text = _get_file_bytes(file_doc).decode("utf-8", errors="replace")
    text = safety.sanitize_context_text(text[:MAX_TEXT_CHARS])
    return {
        "type": "text",
        "summary": _summarize_text(text, "Text file"),
        "preview": {"text": text},
        "import_plan": {},
    }


def _summarize_text(text, label):
    words = [word for word in text.split() if word]
    return f"{label} preview extracted locally with about {len(words)} words available for chat context."
