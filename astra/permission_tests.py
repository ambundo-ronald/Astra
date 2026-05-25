import frappe

from astra import safety


DEFAULT_DOCTYPES = ("Customer", "Supplier", "Item", "Sales Invoice", "Purchase Invoice", "Employee")


def run_permission_matrix(doctypes=None):
    if "System Manager" not in frappe.get_roles() and "Astra Admin" not in frappe.get_roles():
        frappe.throw("Only Astra Admins can run the permission matrix.")

    doctypes = doctypes or DEFAULT_DOCTYPES
    results = []
    for doctype in doctypes:
        if not frappe.db.exists("DocType", doctype):
            continue
        can_read = frappe.has_permission(doctype, "read")
        sample = None
        leak_guard = "not_checked"
        if can_read:
            rows = frappe.db.get_list(doctype, fields=["name"], limit_page_length=1)
            sample = rows[0].name if rows else None
            leak_guard = _check_sensitive_fields(doctype)
        results.append(
            {
                "doctype": doctype,
                "can_read": bool(can_read),
                "sample_readable_name": sample,
                "sensitive_field_guard": leak_guard,
            }
        )
    return {"results": results}


def _check_sensitive_fields(doctype):
    meta = frappe.get_meta(doctype)
    sensitive = [field.fieldname for field in meta.fields if safety.is_sensitive_field(field.fieldname)]
    return "sensitive_fields_detected" if sensitive else "no_sensitive_fields_detected"
