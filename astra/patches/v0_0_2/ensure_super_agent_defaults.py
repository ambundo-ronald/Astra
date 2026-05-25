import frappe

from astra import install


def execute():
    install.create_roles()
    install.create_default_model_profiles()
    install.create_default_policy()
    install.create_prompt_injection_tests()
    _add_workspace_shortcuts()


def _add_workspace_shortcuts():
    if not frappe.db.exists("DocType", "Workspace") or not frappe.db.exists("Workspace", "Astra"):
        return
    workspace = frappe.get_doc("Workspace", "Astra")
    existing = {row.link_to for row in workspace.get("shortcuts") if row.link_to}
    for doctype in (
        "Astra Agent Plan",
        "Astra Entity Memory",
        "Astra FAC Tool Stat",
        "Astra Alert Rule",
        "Astra Tool Event",
        "Astra FAC Tool Contract",
    ):
        if doctype in existing or not frappe.db.exists("DocType", doctype):
            continue
        workspace.append("shortcuts", {"type": "DocType", "label": doctype, "link_to": doctype})
    workspace.save(ignore_permissions=True)
