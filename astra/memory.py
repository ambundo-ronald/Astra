import frappe


def get_memory_context(limit=12):
    if frappe.session.user == "Guest" or not frappe.db.exists("DocType", "Astra Entity Memory"):
        return ""
    rows = frappe.db.get_list(
        "Astra Entity Memory",
        fields=["memory_key", "memory_value", "company", "priority"],
        filters={"user": frappe.session.user, "enabled": 1},
        order_by="priority desc, modified desc",
        limit_page_length=limit,
        ignore_permissions=True,
    )
    if not rows:
        return ""
    lines = ["Controlled user/company memory:"]
    for row in rows:
        company = f" [{row.company}]" if row.company else ""
        lines.append(f"- {row.memory_key}{company}: {row.memory_value}")
    return "\n".join(lines)


def upsert_memory(key, value, company=None, priority=5):
    if not frappe.db.exists("DocType", "Astra Entity Memory"):
        return None
    if not key:
        frappe.throw("Memory key is required.")
    existing = frappe.db.get_value(
        "Astra Entity Memory",
        {"user": frappe.session.user, "memory_key": key, "company": company},
        "name",
    )
    if existing:
        doc = frappe.get_doc("Astra Entity Memory", existing)
    else:
        doc = frappe.get_doc(
            {
                "doctype": "Astra Entity Memory",
                "user": frappe.session.user,
                "memory_key": key,
                "company": company,
            }
        )
    doc.memory_value = value
    doc.priority = priority
    doc.enabled = 1
    doc.save(ignore_permissions=True)
    return doc.name
