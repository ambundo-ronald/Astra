import frappe


def get_summary():
    tool_rows = frappe.db.get_list(
        "Astra Chat Message",
        fields=["tool_trace"],
        filters={"role": "assistant"},
        limit_page_length=5000,
        ignore_permissions=True,
    )
    total_tool_calls = 0
    failed_tool_calls = 0
    tool_counts = {}

    for row in tool_rows:
        for item in _loads(row.get("tool_trace")):
            name = item.get("name") or "unknown"
            total_tool_calls += 1
            tool_counts[name] = tool_counts.get(name, 0) + 1
            if item.get("status") == "error":
                failed_tool_calls += 1

    return {
        "chat_sessions": frappe.db.count("Astra Chat Session"),
        "chat_messages": frappe.db.count("Astra Chat Message"),
        "tool_calls": total_tool_calls,
        "failed_tool_calls": failed_tool_calls,
        "pending_confirmations": frappe.db.count("Astra Tool Confirmation", {"status": "Pending"}),
        "knowledge_chunks": frappe.db.count("AI Knowledge Base"),
        "open_alerts": frappe.db.count("Astra Alert", {"status": "Open"}) if frappe.db.exists("DocType", "Astra Alert") else 0,
        "attachments": frappe.db.count("Astra Attachment") if frappe.db.exists("DocType", "Astra Attachment") else 0,
        "executed_actions": frappe.db.count("Astra Action History", {"status": "Executed"}) if frappe.db.exists("DocType", "Astra Action History") else 0,
        "agent_plans": frappe.db.count("Astra Agent Plan") if frappe.db.exists("DocType", "Astra Agent Plan") else 0,
        "entity_memories": frappe.db.count("Astra Entity Memory") if frappe.db.exists("DocType", "Astra Entity Memory") else 0,
        "fac_tool_stats": frappe.db.count("Astra FAC Tool Stat") if frappe.db.exists("DocType", "Astra FAC Tool Stat") else 0,
        "tool_events": frappe.db.count("Astra Tool Event") if frappe.db.exists("DocType", "Astra Tool Event") else 0,
        "fac_contracts_changed": frappe.db.count("Astra FAC Tool Contract", {"status": "Changed"}) if frappe.db.exists("DocType", "Astra FAC Tool Contract") else 0,
        "stale_knowledge_chunks": frappe.db.count("AI Knowledge Base", {"stale": 1}) if frappe.db.exists("DocType", "AI Knowledge Base") else 0,
        "feedback_down": frappe.db.count("Astra Message Feedback", {"rating": "Down"}) if frappe.db.exists("DocType", "Astra Message Feedback") else 0,
        "top_tools": sorted(tool_counts.items(), key=lambda item: item[1], reverse=True)[:10],
    }


def _loads(value):
    if not value:
        return []
    try:
        import json

        data = json.loads(value)
        return data if isinstance(data, list) else []
    except Exception:
        return []
