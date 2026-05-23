import json

import frappe

from astra import safety


def log_event(
    event_type,
    session_id=None,
    tool_name=None,
    status="Success",
    payload=None,
    intent=None,
    latency_ms=None,
    error=None,
):
    try:
        if not frappe.db.exists("DocType", "Astra Tool Event"):
            return None

        payload = safety.redact_mapping(payload or {})
        arguments = payload.get("arguments") if isinstance(payload, dict) else {}
        doctype = None
        document_name = None
        if isinstance(arguments, dict):
            doctype = arguments.get("doctype") or arguments.get("document_type")
            document_name = arguments.get("name") or arguments.get("document_name") or arguments.get("docname")

        doc = frappe.get_doc(
            {
                "doctype": "Astra Tool Event",
                "session": session_id,
                "user": frappe.session.user,
                "event_type": event_type,
                "intent": intent,
                "tool_name": tool_name,
                "status": status,
                "latency_ms": latency_ms,
                "doctype_name": doctype,
                "document_name": document_name,
                "payload": json.dumps(payload, default=str, ensure_ascii=False, separators=(",", ":")),
                "error": error,
            }
        )
        doc.insert(ignore_permissions=True)
        return doc.name
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Astra Tool Event Audit Error")
        return None
