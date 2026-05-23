import frappe

from astra import diagnostics, fac_bridge, rag


@frappe.whitelist()
def run():
    if frappe.session.user == "Guest":
        frappe.throw("Please log in to run Astra install doctor.")
    if "Astra Admin" not in frappe.get_roles() and "System Manager" not in frappe.get_roles():
        frappe.throw("Only Astra Admins can run install doctor.")
    checks = []
    _check(checks, "roles", all(frappe.db.exists("Role", role) for role in ("Astra User", "Astra Admin")))
    _check(checks, "ollama_settings", frappe.db.exists("DocType", "Ollama Settings"))
    _check(checks, "desk_assets", _asset_hooks_present())
    _check(checks, "scheduler", _scheduler_enabled())
    _check(checks, "socketio", True, "Requires standard Frappe Socket.IO service for streaming.")
    _check(checks, "fac_installed", fac_bridge.is_fac_available(), "Optional. Needed for live ERP tool actions.")
    _check(checks, "rag_chunks", _knowledge_count() > 0, f"{_knowledge_count()} knowledge chunks found.")
    smoke = diagnostics.run_smoke_test()
    return {"ok": all(item["ok"] for item in checks if item["required"]), "checks": checks, "smoke_test": smoke}


def _check(checks, name, ok, message="", required=True):
    checks.append({"name": name, "ok": bool(ok), "message": message, "required": required})


def _asset_hooks_present():
    try:
        from astra import hooks

        return bool(getattr(hooks, "app_include_js", None)) and bool(getattr(hooks, "app_include_css", None))
    except Exception:
        return False


def _scheduler_enabled():
    try:
        return not bool(frappe.conf.get("pause_scheduler"))
    except Exception:
        return True


def _knowledge_count():
    try:
        return frappe.db.count("AI Knowledge Base")
    except Exception:
        return 0
