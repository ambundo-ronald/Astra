import frappe


def get_bench_diagnostics():
    if "System Manager" not in frappe.get_roles():
        frappe.throw("Only System Managers can view bench diagnostics.")

    return {
        "installed_apps": _installed_apps(),
        "scheduler": _scheduler_status(),
        "workers": _worker_hint(),
        "failed_jobs": _failed_jobs(),
        "patches": _patch_hint(),
        "site": frappe.local.site,
    }


def _installed_apps():
    try:
        return frappe.get_installed_apps()
    except Exception:
        return []


def _scheduler_status():
    try:
        enabled = frappe.db.get_single_value("System Settings", "enable_scheduler")
        return {"enabled": bool(enabled), "message": "Scheduler flag is enabled." if enabled else "Scheduler flag is disabled."}
    except Exception as exc:
        return {"enabled": None, "message": str(exc)}


def _failed_jobs():
    if not frappe.db.exists("DocType", "RQ Job"):
        return {"count": 0, "message": "RQ Job DocType not available."}
    count = frappe.db.count("RQ Job", {"status": "failed"})
    return {"count": count, "message": f"{count} failed RQ jobs."}


def _patch_hint():
    if not frappe.db.exists("DocType", "Patch Log"):
        return {"message": "Patch Log DocType not available."}
    latest = frappe.db.get_list(
        "Patch Log",
        fields=["patch", "success", "creation"],
        order_by="creation desc",
        limit_page_length=5,
        ignore_permissions=True,
    )
    return {"latest": latest}


def _worker_hint():
    return {
        "message": "Verify bench worker, schedule, and socketio processes from the host process manager.",
        "socketio_required_for_streaming": True,
    }
