import hashlib
import json

import frappe


DOCTYPE_TTL = 60 * 60
FAC_TTL = 60 * 30


def get_doctype_schema(doctype):
    key = f"astra:doctype-schema:{doctype}"
    cached = frappe.cache().get_value(key)
    if cached:
        return cached

    meta = frappe.get_meta(doctype)
    schema = {
        "doctype": doctype,
        "modified": str(getattr(meta, "modified", "")),
        "fields": [
            {
                "fieldname": field.fieldname,
                "label": field.label or field.fieldname,
                "fieldtype": field.fieldtype,
                "options": field.options,
                "reqd": bool(field.reqd),
            }
            for field in meta.fields
            if field.fieldname and field.fieldtype not in {"Section Break", "Column Break", "Tab Break"}
        ],
    }
    frappe.cache().set_value(key, schema, expires_in_sec=DOCTYPE_TTL)
    return schema


def get_fac_tool_schema(tool_name, loader):
    key = f"astra:fac-schema:{tool_name}"
    cached = frappe.cache().get_value(key)
    if cached:
        return cached
    schema = loader(tool_name) or {}
    frappe.cache().set_value(key, schema, expires_in_sec=FAC_TTL)
    return schema


def hash_schema(schema):
    text = json.dumps(schema or {}, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def clear():
    cache = frappe.cache()
    for pattern in ("astra:doctype-schema:*", "astra:fac-schema:*"):
        if hasattr(cache, "delete_keys"):
            cache.delete_keys(pattern)
        elif hasattr(cache, "delete_value"):
            cache.delete_value(pattern)
    return {"cleared": True}
