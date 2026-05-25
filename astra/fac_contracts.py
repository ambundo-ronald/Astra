import json

import frappe
from frappe.utils import now_datetime

from astra import schema_cache


def sync_tool_contracts(tools, allowlist=None):
    if not frappe.db.exists("DocType", "Astra FAC Tool Contract"):
        return {"synced": 0, "changed": 0}

    allowlist = set(allowlist or [])
    seen = set()
    changed = 0
    synced = 0
    for tool in tools or []:
        name = tool.get("name")
        if not name:
            continue
        seen.add(name)
        schema = tool.get("input_schema") or {}
        schema_hash = schema_cache.hash_schema(schema)
        existing = frappe.db.exists("Astra FAC Tool Contract", name)
        if existing:
            doc = frappe.get_doc("Astra FAC Tool Contract", existing)
            if doc.schema_hash and doc.schema_hash != schema_hash:
                doc.status = "Changed"
                changed += 1
            else:
                doc.status = "Current"
        else:
            doc = frappe.get_doc({"doctype": "Astra FAC Tool Contract", "tool_name": name})
        doc.category = tool.get("category")
        doc.enabled_in_allowlist = 1 if name in allowlist else 0
        doc.requires_confirmation = 1 if tool.get("requires_confirmation") else 0
        doc.schema_hash = schema_hash
        doc.last_seen = now_datetime()
        doc.description = tool.get("description")
        doc.input_schema = json.dumps(schema, default=str, ensure_ascii=False, separators=(",", ":"))
        doc.save(ignore_permissions=True)
        synced += 1

    for row in frappe.db.get_list(
        "Astra FAC Tool Contract",
        fields=["name", "tool_name"],
        ignore_permissions=True,
        limit_page_length=500,
    ):
        if row.tool_name not in seen:
            frappe.db.set_value("Astra FAC Tool Contract", row.name, "status", "Missing", update_modified=False)
    return {"synced": synced, "changed": changed}
