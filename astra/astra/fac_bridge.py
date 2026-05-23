import json
import re

import frappe

from astra import safety, schema_cache, tool_validation


DEFAULT_FAC_TOOL_ALLOWLIST = {
    "get_document",
    "list_documents",
    "search_documents",
    "search_doctype",
    "search_link",
    "get_doctype_info",
    "metadata_list_doctypes",
    "get_doctype_info_fields",
    "metadata_permissions",
    "metadata_workflow",
    "report_list",
    "report_requirements",
    "generate_report",
    "workflow_list",
    "workflow_status",
}

CONFIRMATION_REQUIRED_TOOLS = {
    "create_document",
    "update_document",
    "delete_document",
    "submit_document",
    "workflow_action",
    "run_workflow",
}

ALWAYS_BLOCKED_FAC_TOOLS = {
    "run_python_code",
    "run_database_query",
    "analyze_business_data",
    "extract_file_content",
    "create_dashboard",
    "create_dashboard_chart",
}

BLOCKED_FAC_TOOLS = CONFIRMATION_REQUIRED_TOOLS | ALWAYS_BLOCKED_FAC_TOOLS


def is_fac_available():
    return "frappe_assistant_core" in frappe.get_installed_apps()


def parse_allowlist(value, include_confirmation_tools=False):
    if not value:
        tools = set(DEFAULT_FAC_TOOL_ALLOWLIST)
    else:
        tools = {
            item.strip()
            for item in re.split(r"[\n,]", value)
            if item and item.strip() and not item.strip().startswith("#")
        }

    blocked = ALWAYS_BLOCKED_FAC_TOOLS
    if not include_confirmation_tools:
        blocked = blocked | CONFIRMATION_REQUIRED_TOOLS
    return tools - blocked


def get_available_tools(allowlist=None, include_confirmation_tools=False):
    if not is_fac_available():
        return []

    allowlist = DEFAULT_FAC_TOOL_ALLOWLIST if allowlist is None else allowlist
    registry = _get_registry()
    tools = registry.get_all_tools() or {}
    result = []

    for name, tool in tools.items():
        if name not in allowlist or name in ALWAYS_BLOCKED_FAC_TOOLS:
            continue
        if name in CONFIRMATION_REQUIRED_TOOLS and not include_confirmation_tools:
            continue

        result.append(
            {
                "name": name,
                "description": getattr(tool, "description", "") or "",
                "category": getattr(tool, "category", "") or "",
                "input_schema": getattr(tool, "inputSchema", {}) or {},
                "requires_confirmation": name in CONFIRMATION_REQUIRED_TOOLS,
            }
        )

    return sorted(result, key=lambda item: item["name"])


def build_tool_prompt(tools):
    if not tools:
        return ""

    lines = [
        "FAC tools are available for permission-aware ERPNext data access.",
        "Use a tool only when the user's question needs live ERPNext data or reports.",
        "You may call one tool at a time. After each tool result, decide whether another tool is needed or give the final answer.",
        "For tool use, respond with only this JSON object and no markdown:",
        '{"tool_call": {"name": "tool_name", "arguments": {}}}',
        "If no tool is needed, answer normally.",
        "Available tools:",
    ]

    for tool in tools:
        schema = _compact_json(tool.get("input_schema") or {})
        description = _single_line(tool.get("description") or "")
        confirmation = " Requires explicit user confirmation." if tool.get("requires_confirmation") else ""
        lines.append(
            f"- {tool['name']}: {description}{confirmation}"
            + (f" Input schema: {schema}" if schema else "")
        )

    return "\n".join(lines)


def extract_tool_call(text):
    if not text:
        return None

    payload = _extract_json_object(text)
    if not payload:
        return None

    tool_call = payload.get("tool_call")
    if not isinstance(tool_call, dict):
        return None

    name = tool_call.get("name")
    arguments = tool_call.get("arguments") or {}
    if not isinstance(name, str) or not isinstance(arguments, dict):
        return None

    return {"name": name, "arguments": arguments}


def looks_like_tool_call(text):
    if not text:
        return False

    lowered = text.lower()
    return "tool_call" in lowered or '"name"' in lowered and '"arguments"' in lowered


def execute_tool(tool_call, allowlist=None):
    allowlist = DEFAULT_FAC_TOOL_ALLOWLIST if allowlist is None else allowlist
    name = tool_call.get("name")

    if name in CONFIRMATION_REQUIRED_TOOLS:
        return {
            "success": False,
            "status": "confirmation_required",
            "error": f"Tool '{name}' requires explicit confirmation.",
        }

    if name not in allowlist or name in ALWAYS_BLOCKED_FAC_TOOLS:
        return {
            "success": False,
            "status": "error",
            "error": f"Tool '{name}' is not enabled for Astra.",
        }

    registry = _get_registry()
    tool = registry.get_tool(name)
    if not tool:
        return {"success": False, "status": "error", "error": f"Tool '{name}' was not found."}

    validation_errors = validate_tool_call(tool, tool_call)
    if validation_errors:
        return {
            "success": False,
            "status": "validation_error",
            "errors": validation_errors,
            "error": "; ".join(validation_errors),
        }

    required_doctype = getattr(tool, "requires_permission", None)
    if required_doctype and not frappe.has_permission(required_doctype, "read"):
        return {
            "success": False,
            "status": "error",
            "error": f"Current user cannot read {required_doctype}.",
        }

    try:
        result = tool.execute(tool_call.get("arguments") or {})
        success = _result_success(result)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Astra FAC Tool Error")
        return {
            "success": False,
            "status": "error",
            "error": "FAC tool execution failed.",
        }

    clipped = safety.redact_mapping(_clip_result(result))
    if isinstance(clipped, dict):
        clipped.setdefault("success", success)
        clipped.setdefault("status", "success" if success else "error")
    return clipped


def execute_confirmed_tool(tool_call, allowlist=None):
    allowlist = DEFAULT_FAC_TOOL_ALLOWLIST if allowlist is None else allowlist
    name = tool_call.get("name")

    if name not in allowlist or name in ALWAYS_BLOCKED_FAC_TOOLS:
        return {
            "success": False,
            "status": "error",
            "error": f"Tool '{name}' is not enabled for Astra.",
        }

    registry = _get_registry()
    tool = registry.get_tool(name)
    if not tool:
        return {"success": False, "status": "error", "error": f"Tool '{name}' was not found."}

    validation_errors = validate_tool_call(tool, tool_call)
    if validation_errors:
        return {
            "success": False,
            "status": "validation_error",
            "errors": validation_errors,
            "error": "; ".join(validation_errors),
        }

    permission_error = _get_confirmed_tool_permission_error(name, tool_call.get("arguments") or {})
    if permission_error:
        return {"success": False, "status": "error", "error": permission_error}

    try:
        result = tool.execute(tool_call.get("arguments") or {})
        success = _result_success(result)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Astra FAC Confirmed Tool Error")
        return {
            "success": False,
            "status": "error",
            "error": "FAC confirmed tool execution failed.",
        }

    clipped = safety.redact_mapping(_clip_result(result))
    if isinstance(clipped, dict):
        clipped.setdefault("success", success)
        clipped.setdefault("status", "success" if success else "error")
    return clipped


def validate_tool_call(tool, tool_call):
    return tool_validation.validate_arguments(
        {
            "input_schema": getattr(tool, "inputSchema", {}) or {},
        },
        tool_call.get("arguments") or {},
    )


def get_tool_schema(tool_name):
    return schema_cache.get_fac_tool_schema(tool_name, _load_tool_schema)


def _load_tool_schema(tool_name):
    registry = _get_registry()
    tool = registry.get_tool(tool_name)
    if not tool:
        return {}
    return getattr(tool, "inputSchema", {}) or {}


def build_write_preview(tool_name, arguments):
    doctype = arguments.get("doctype") or arguments.get("document_type")
    document_name = arguments.get("name") or arguments.get("document_name")
    changes = arguments.get("fields") or arguments.get("data") or {}

    if isinstance(arguments.get("doc"), dict):
        doctype = doctype or arguments["doc"].get("doctype")
        document_name = document_name or arguments["doc"].get("name")
        changes = changes or arguments["doc"]

    preview = {
        "tool": tool_name,
        "doctype": doctype,
        "document_name": document_name,
        "changes": [],
    }

    if not isinstance(changes, dict):
        return preview

    existing = {}
    if doctype and document_name and frappe.db.exists(doctype, document_name):
        doc = frappe.get_doc(doctype, document_name)
        existing = doc.as_dict()

    for fieldname, new_value in changes.items():
        if fieldname in {"doctype", "name"}:
            continue
        before = existing.get(fieldname)
        after = new_value
        if safety.is_sensitive_field(fieldname):
            before = "***" if before else before
            after = "***" if after else after
        preview["changes"].append(
            {
                "fieldname": fieldname,
                "before": before,
                "after": after,
            }
        )

    return preview


def format_tool_result(tool_name, result):
    return (
        f"FAC tool '{tool_name}' returned this JSON result. "
        "Use it to answer the user. Do not reveal data beyond this result.\n"
        f"{_compact_json(safety.redact_mapping(result), max_chars=10000)}"
    )


def summarize_tool_result(result, max_chars=1200):
    return _compact_json(safety.redact_mapping(result), max_chars=max_chars)


def _get_registry():
    from frappe_assistant_core.core.tool_registry import get_tool_registry

    return get_tool_registry()


def _extract_json_object(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"```$", "", text).strip()

    try:
        return json.loads(text)
    except ValueError:
        pass

    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None

    try:
        return json.loads(match.group(0))
    except ValueError:
        return None


def _compact_json(value, max_chars=2500):
    try:
        text = json.dumps(value, default=str, ensure_ascii=False, separators=(",", ":"))
    except TypeError:
        text = str(value)

    if len(text) > max_chars:
        return text[: max_chars - 20] + "...[truncated]"
    return text


def _single_line(value):
    return re.sub(r"\s+", " ", value).strip()[:500]


def _clip_result(result, max_chars=12000):
    try:
        text = json.dumps(result, default=str, ensure_ascii=False, separators=(",", ":"))
    except TypeError:
        text = str(result)

    if len(text) <= max_chars:
        return result
    return {"success": True, "result": text[: max_chars - 20] + "...[truncated]"}


def _result_success(result):
    if not isinstance(result, dict):
        return True

    if "success" in result:
        return bool(result.get("success"))

    if "error" in result:
        return False

    return True


def _get_confirmed_tool_permission_error(tool_name, arguments):
    doctype = arguments.get("doctype") or arguments.get("document_type")
    document_name = arguments.get("name") or arguments.get("document_name")

    if isinstance(arguments.get("doc"), dict):
        doctype = doctype or arguments["doc"].get("doctype")
        document_name = document_name or arguments["doc"].get("name")

    if not doctype:
        return None

    permission_type = "write"
    if tool_name == "create_document":
        permission_type = "create"
    elif tool_name == "delete_document":
        permission_type = "delete"
    elif tool_name in {"submit_document", "workflow_action", "run_workflow"}:
        permission_type = "submit" if tool_name == "submit_document" else "write"

    if document_name and permission_type != "create":
        doc = frappe.get_doc(doctype, document_name)
        if not doc.has_permission(permission_type):
            return f"Current user cannot {permission_type} {doctype} {document_name}."
        return None

    if not frappe.has_permission(doctype, permission_type):
        return f"Current user cannot {permission_type} {doctype}."

    return None
