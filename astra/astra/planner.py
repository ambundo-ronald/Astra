import frappe


ACTION_PACKS = {
    "sales_invoice_from_delivery": {
        "title": "Create Sales Invoice from Delivery Note",
        "keywords": ("sales invoice", "delivery note", "invoice from delivery"),
        "roles": ("Sales User", "Sales Manager", "Accounts User", "Accounts Manager"),
        "steps": [
            {"label": "Identify the Delivery Note", "tool": "get_document", "requires_confirmation": False},
            {"label": "Check customer, company, items, and billable quantities", "tool": "get_document", "requires_confirmation": False},
            {"label": "Check outstanding/credit context if available", "tool": "generate_report", "requires_confirmation": False},
            {"label": "Prepare Sales Invoice draft", "tool": "create_document", "requires_confirmation": True},
        ],
    },
    "payment_follow_up": {
        "title": "Payment Follow-up",
        "keywords": ("overdue invoice", "payment follow", "receivables", "outstanding invoice"),
        "roles": ("Accounts User", "Accounts Manager", "Sales Manager"),
        "steps": [
            {"label": "Ask for company, customer, and date range if missing", "tool": None, "requires_confirmation": False},
            {"label": "Run Accounts Receivable", "tool": "generate_report", "requires_confirmation": False},
            {"label": "Summarize overdue balances", "tool": None, "requires_confirmation": False},
            {"label": "Draft follow-up actions for user review", "tool": None, "requires_confirmation": False},
        ],
    },
    "purchase_cycle_check": {
        "title": "Purchase Cycle Check",
        "keywords": ("purchase order", "purchase receipt", "purchase invoice", "buying"),
        "roles": ("Purchase User", "Purchase Manager", "Accounts User", "Accounts Manager"),
        "steps": [
            {"label": "Find linked Purchase Order / Receipt / Invoice", "tool": "search_documents", "requires_confirmation": False},
            {"label": "Check received and billed quantities", "tool": "get_document", "requires_confirmation": False},
            {"label": "Warn about missing supplier, taxes, or receipts", "tool": None, "requires_confirmation": False},
        ],
    },
    "stock_reconciliation": {
        "title": "Stock Reconciliation Assistant",
        "keywords": ("stock reconciliation", "stock adjustment", "actual qty", "warehouse qty"),
        "roles": ("Stock User", "Stock Manager", "System Manager"),
        "steps": [
            {"label": "Ask for item, warehouse, posting date, and counted quantity", "tool": None, "requires_confirmation": False},
            {"label": "Check current stock balance", "tool": "generate_report", "requires_confirmation": False},
            {"label": "Prepare reconciliation draft", "tool": "create_document", "requires_confirmation": True},
        ],
    },
    "manufacturing_work_order": {
        "title": "Manufacturing Work Order Guidance",
        "keywords": ("work order", "bom", "manufacturing", "job card"),
        "roles": ("Manufacturing User", "Manufacturing Manager", "Stock Manager"),
        "steps": [
            {"label": "Identify item, BOM, quantity, and planned start date", "tool": "search_documents", "requires_confirmation": False},
            {"label": "Check BOM and stock availability", "tool": "get_document", "requires_confirmation": False},
            {"label": "Prepare Work Order draft", "tool": "create_document", "requires_confirmation": True},
        ],
    },
}


def build_plan(message, intent_result=None, current_context=None):
    pack = match_action_pack(message)
    if not pack and (intent_result or {}).get("intent") in {"write_action", "report_analytics", "live_erp_data"}:
        pack = _generic_pack(intent_result)

    if not pack:
        return {}

    current_context = current_context or {}
    return {
        "title": pack["title"],
        "intent": (intent_result or {}).get("intent"),
        "context": {
            "doctype": current_context.get("doctype"),
            "docname": current_context.get("docname"),
        },
        "steps": pack["steps"],
        "requires_confirmation": any(step.get("requires_confirmation") for step in pack["steps"]),
        "status": "Draft",
    }


def match_action_pack(message):
    lowered = (message or "").lower()
    roles = set(frappe.get_roles())
    matches = []
    for pack in ACTION_PACKS.values():
        if any(keyword in lowered for keyword in pack["keywords"]):
            role_score = 2 if roles.intersection(pack.get("roles") or ()) else 1
            matches.append((role_score, pack))
    if not matches:
        return None
    return sorted(matches, key=lambda item: item[0], reverse=True)[0][1]


def save_plan(session_id, plan):
    if not plan or not frappe.db.exists("DocType", "Astra Agent Plan"):
        return None
    doc = frappe.get_doc(
        {
            "doctype": "Astra Agent Plan",
            "session": session_id,
            "user": frappe.session.user,
            "title": plan.get("title"),
            "status": plan.get("status") or "Draft",
            "intent": plan.get("intent"),
            "current_doctype": (plan.get("context") or {}).get("doctype"),
            "current_document": (plan.get("context") or {}).get("docname"),
            "plan_json": frappe.as_json(plan),
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def action_pack_prompt():
    user_roles = set(frappe.get_roles())
    lines = ["Native ERPNext action packs available to Astra. Prefer packs matching the user's roles:"]
    for pack in ACTION_PACKS.values():
        if pack.get("roles") and not user_roles.intersection(pack.get("roles")):
            continue
        role_note = " Relevant roles: " + ", ".join(pack.get("roles") or ())
        lines.append("- " + pack["title"] + ": " + " -> ".join(step["label"] for step in pack["steps"]) + role_note)
    return "\n".join(lines)


def _generic_pack(intent_result):
    intent_name = (intent_result or {}).get("intent")
    if intent_name == "report_analytics":
        return {
            "title": "Report Runner",
            "steps": [
                {"label": "Identify report and required filters", "tool": "report_requirements", "requires_confirmation": False},
                {"label": "Ask follow-up questions for missing filters", "tool": None, "requires_confirmation": False},
                {"label": "Run report and show preview", "tool": "generate_report", "requires_confirmation": False},
            ],
        }
    if intent_name == "write_action":
        return {
            "title": "Safe Document Action",
            "steps": [
                {"label": "Identify target DocType and document", "tool": None, "requires_confirmation": False},
                {"label": "Collect required fields and validate links", "tool": "get_doctype_info", "requires_confirmation": False},
                {"label": "Prepare write preview", "tool": None, "requires_confirmation": False},
                {"label": "Ask for approval before execution", "tool": "create_document/update_document", "requires_confirmation": True},
            ],
        }
    return {
        "title": "ERP Data Lookup",
        "steps": [
            {"label": "Identify DocType/report and filters", "tool": None, "requires_confirmation": False},
            {"label": "Read only permission-aware data", "tool": "list_documents", "requires_confirmation": False},
            {"label": "Summarize with citations", "tool": None, "requires_confirmation": False},
        ],
    }
