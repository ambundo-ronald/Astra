import json

import frappe
from frappe.utils import add_days, now_datetime

from astra import diagnostics, rag


def daily_maintenance():
    clean_old_sessions()
    embed_stale_knowledge(limit=25)
    create_basic_alerts()
    diagnostics.run_smoke_test()


def clean_old_sessions(days=90):
    cutoff = add_days(now_datetime(), -days)
    rows = frappe.db.get_list(
        "Astra Chat Session",
        fields=["name"],
        filters={"status": "Open", "last_message_at": ["<", cutoff]},
        limit_page_length=200,
        ignore_permissions=True,
    )
    for row in rows:
        frappe.db.set_value("Astra Chat Session", row.name, "status", "Closed", update_modified=False)


def embed_stale_knowledge(limit=25):
    rows = frappe.db.get_list(
        "AI Knowledge Base",
        fields=["name", "embedding_vector"],
        limit_page_length=limit * 4,
        ignore_permissions=True,
    )
    missing = [row for row in rows if not row.get("embedding_vector")]
    if missing:
        rag.rebuild_knowledge_base_embeddings(limit=limit)


def create_basic_alerts():
    if not frappe.db.exists("DocType", "Astra Alert"):
        return
    if not frappe.db.exists("Astra Alert", {"title": "Astra daily health check"}):
        frappe.get_doc(
            {
                "doctype": "Astra Alert",
                "title": "Astra daily health check",
                "category": "Background Job",
                "severity": "Low",
                "status": "Open",
                "message": "Astra maintenance job is active.",
            }
        ).insert(ignore_permissions=True)
    create_overdue_invoice_alert()
    create_low_stock_alert()
    create_configured_alerts()


def create_configured_alerts():
    if not frappe.db.exists("DocType", "Astra Alert Rule"):
        return
    rules = frappe.db.get_list(
        "Astra Alert Rule",
        fields=["title", "target_doctype", "filters_json", "threshold", "category", "severity", "message_template"],
        filters={"enabled": 1},
        ignore_permissions=True,
    )
    for rule in rules:
        if not frappe.db.exists("DocType", rule.target_doctype):
            continue
        try:
            filters = json.loads(rule.filters_json or "{}")
        except ValueError:
            filters = {}
        count = frappe.db.count(rule.target_doctype, filters)
        threshold = int(rule.threshold or 1)
        if count < threshold or frappe.db.exists("Astra Alert", {"title": rule.title}):
            continue
        message = (rule.message_template or "{count} matching {doctype} records.").format(
            count=count,
            doctype=rule.target_doctype,
        )
        frappe.get_doc(
            {
                "doctype": "Astra Alert",
                "title": rule.title,
                "category": rule.category or "Other",
                "severity": rule.severity or "Medium",
                "status": "Open",
                "message": message,
                "reference_doctype": rule.target_doctype,
            }
        ).insert(ignore_permissions=True)


def create_overdue_invoice_alert():
    if not frappe.db.exists("DocType", "Sales Invoice"):
        return
    count = frappe.db.count(
        "Sales Invoice",
        {
            "docstatus": 1,
            "outstanding_amount": [">", 0],
            "due_date": ["<", now_datetime().date()],
        },
    )
    if not count or frappe.db.exists("Astra Alert", {"title": "Overdue Sales Invoices"}):
        return
    frappe.get_doc(
        {
            "doctype": "Astra Alert",
            "title": "Overdue Sales Invoices",
            "category": "Overdue Invoice",
            "severity": "Medium",
            "status": "Open",
            "message": f"{count} submitted Sales Invoices appear overdue with outstanding amounts.",
            "reference_doctype": "Sales Invoice",
        }
    ).insert(ignore_permissions=True)


def create_low_stock_alert():
    if not frappe.db.exists("DocType", "Bin"):
        return
    rows = frappe.db.get_list(
        "Bin",
        fields=["item_code", "warehouse"],
        filters={"actual_qty": ["<=", 0]},
        limit_page_length=1,
        ignore_permissions=True,
    )
    if not rows or frappe.db.exists("Astra Alert", {"title": "Possible Low Stock"}):
        return
    frappe.get_doc(
        {
            "doctype": "Astra Alert",
            "title": "Possible Low Stock",
            "category": "Low Stock",
            "severity": "Medium",
            "status": "Open",
            "message": "One or more Item/Warehouse bins have zero or negative actual quantity.",
            "reference_doctype": "Bin",
        }
    ).insert(ignore_permissions=True)
