import hashlib
import json
import re
import time
import uuid
from urllib.parse import urlparse

import frappe
from frappe import _
from frappe.utils import cint
from frappe.utils import now_datetime

from astra import (
    admin_diagnostics,
    audit,
    attachments,
    fac_contracts,
    fac_bridge,
    intent,
    memory,
    observability,
    permission_tests,
    planner,
    rag,
    safety,
    schema_cache,
    semantic,
    tool_validation,
    workflow_packs,
)


DEFAULT_API_URL = "http://localhost:11434"
DEFAULT_MODEL = "llama3"
MAX_CONTEXT_CHARS = 12000
MAX_HISTORY_MESSAGES = 12
DEFAULT_MAX_FAC_TOOL_CALLS = 4
MAX_FAC_TOOL_CALLS = 8

STOP_WORDS = {
    "about",
    "after",
    "before",
    "can",
    "could",
    "does",
    "erpnext",
    "field",
    "fields",
    "frappe",
    "from",
    "have",
    "help",
    "how",
    "into",
    "make",
    "show",
    "the",
    "their",
    "there",
    "this",
    "what",
    "when",
    "where",
    "which",
    "with",
    "workflow",
}


@frappe.whitelist()
def get_chat_response(user_message, history=None, session_id=None, current_context=None):
    """Return an Ollama chat response using local, permission-aware context."""
    _require_logged_in_user()
    user_message = (user_message or "").strip()
    if not user_message:
        frappe.throw(_("Please enter a message."))

    session_id = _get_or_create_session(session_id, user_message)
    if not history:
        history = _get_session_history(session_id)
    frappe.flags.astra_session_id = session_id
    current_context = _normalize_current_context(current_context)

    settings = _get_settings()
    api_url = _prepare_ollama_connection(settings)
    preferences = _get_user_preferences()
    model_name = _resolve_model_from_preferences(
        preferences,
        settings.get("model_name") or DEFAULT_MODEL,
    ).strip()
    fac_tools = _get_fac_tools(settings)
    intent_result = intent.classify(user_message, current_context)
    audit.log_event("Intent", session_id=session_id, intent=intent_result.get("intent"), payload=intent_result)
    agent_plan = planner.build_plan(user_message, intent_result, current_context)
    plan_id = planner.save_plan(session_id, agent_plan)
    if plan_id:
        agent_plan["name"] = plan_id
    if agent_plan:
        audit.log_event("Plan", session_id=session_id, intent=intent_result.get("intent"), payload=agent_plan)
    system_prompt = _build_system_prompt(
        settings,
        user_message,
        fac_tools,
        intent_result=intent_result,
        preferences=preferences,
        current_context=current_context,
        agent_plan=agent_plan,
    )
    messages = _build_messages(system_prompt, history, user_message)

    response = _send_ollama_chat(api_url, model_name, messages)
    answer = _extract_ollama_message(response)
    answer, tool_trace = _run_fac_tool_loop(
        api_url=api_url,
        model_name=model_name,
        messages=messages,
        initial_answer=answer,
        settings=settings,
        fac_tools=fac_tools,
    )
    visible_tools = tool_trace if cint(settings.get("show_fac_tool_trace")) else []
    sources = getattr(frappe.flags, "local_ai_sources", [])
    _persist_chat_turn(
        session_id=session_id,
        user_message=user_message,
        assistant_message=answer,
        model_name=model_name,
        sources=sources,
        tool_trace=tool_trace,
    )
    audit.log_event("Final Answer", session_id=session_id, payload={"message": answer, "sources": sources})

    return {
        "session_id": session_id,
        "message": answer,
        "model": model_name,
        "sources": sources,
        "source_cards": _build_source_cards(sources),
        "visualization": _build_report_visualization_hint(tool_trace),
        "guidance": _build_guided_actions(intent_result, current_context),
        "plan": agent_plan,
        "tools": visible_tools,
        "confirmations": [
            item for item in tool_trace if item.get("status") == "confirmation_required"
        ],
        "intent": intent_result,
    }


@frappe.whitelist()
def get_assistant_status():
    _require_logged_in_user()
    settings = _get_settings()
    status = {
        "ollama": {"ok": False, "message": "Ollama has not been checked yet."},
        "fac": {"installed": False, "enabled": bool(cint(settings.get("enable_fac_tools"))), "message": ""},
        "rag": {
            "enabled": bool(cint(settings.get("enable_rag"))),
            "vector_search": bool(cint(settings.get("enable_vector_search"))),
            "message": "",
        },
    }

    try:
        api_url = _prepare_ollama_connection(settings)
        response = _get_from_ollama(f"{api_url}/api/tags")
        model_rows = response.get("models", [])
        models = [item.get("name", "") for item in model_rows]
        model_name = settings.get("model_name") or DEFAULT_MODEL
        model_found = any(name == model_name or name.startswith(model_name + ":") for name in models)
        model_health = _assess_model_health(model_name, model_rows, model_found)
        status["ollama"] = {
            "ok": True,
            "provider": settings.get("provider") or "Local Ollama",
            "model_found": model_found,
            "model_health": model_health,
            "message": "Ollama is reachable."
            if model_found
            else f"Ollama is reachable, but model '{model_name}' was not listed.",
        }
    except Exception as exc:
        status["ollama"] = {"ok": False, "message": _friendly_exception_message(exc)}

    fac_installed = fac_bridge.is_fac_available()
    status["fac"]["installed"] = fac_installed
    if status["fac"]["enabled"] and not fac_installed:
        status["fac"]["message"] = "FAC tools are enabled, but Frappe Assistant Core is not installed."
    elif status["fac"]["enabled"]:
        fac_status = get_fac_status()
        if fac_status.get("tool_count"):
            status["fac"]["message"] = f"{fac_status.get('tool_count')} FAC tools discovered."
        else:
            status["fac"]["message"] = "FAC is enabled, but no allowed tools were discovered."
    else:
        status["fac"]["message"] = "FAC tools are disabled."

    kb_count = frappe.db.count("AI Knowledge Base") if frappe.has_permission("AI Knowledge Base", "read") else 0
    status["rag"]["message"] = (
        f"{kb_count} knowledge chunks available."
        if kb_count
        else "No readable knowledge chunks found yet."
    )
    return status


@frappe.whitelist()
def get_bench_diagnostics():
    _require_logged_in_user()
    return admin_diagnostics.get_bench_diagnostics()


@frappe.whitelist()
def clear_schema_cache():
    _require_logged_in_user()
    if "System Manager" not in frappe.get_roles() and "Astra Admin" not in frappe.get_roles():
        frappe.throw(_("Only Astra Admins can clear schema cache."))
    return schema_cache.clear()


@frappe.whitelist()
def start_chat_response_stream(user_message, history=None, session_id=None, current_context=None):
    _require_logged_in_user()
    user_message = (user_message or "").strip()
    if not user_message:
        frappe.throw(_("Please enter a message."))

    session_id = _get_or_create_session(session_id, user_message)
    if not history:
        history = _get_session_history(session_id)

    request_id = str(uuid.uuid4())
    frappe.enqueue(
        "astra.api.run_streaming_chat_response",
        queue="short",
        timeout=180,
        user=frappe.session.user,
        request_id=request_id,
        user_message=user_message,
        history=history,
        session_id=session_id,
        current_context=_normalize_current_context(current_context),
    )
    return {"streaming": True, "request_id": request_id, "session_id": session_id}


def run_streaming_chat_response(user, request_id, user_message, history, session_id, current_context=None):
    frappe.set_user(user)
    event = f"astra_stream_{request_id}"

    try:
        settings = _get_settings()
        api_url = _prepare_ollama_connection(settings)
        preferences = _get_user_preferences()
        model_name = _resolve_model_from_preferences(
            preferences,
            settings.get("model_name") or DEFAULT_MODEL,
        ).strip()
        fac_tools = _get_fac_tools(settings)
        current_context = _normalize_current_context(current_context)
        intent_result = intent.classify(user_message, current_context)
        audit.log_event("Intent", session_id=session_id, intent=intent_result.get("intent"), payload=intent_result)
        agent_plan = planner.build_plan(user_message, intent_result, current_context)
        plan_id = planner.save_plan(session_id, agent_plan)
        if plan_id:
            agent_plan["name"] = plan_id
        if agent_plan:
            audit.log_event("Plan", session_id=session_id, intent=intent_result.get("intent"), payload=agent_plan)
        system_prompt = _build_system_prompt(
            settings,
            user_message,
            fac_tools=fac_tools,
            intent_result=intent_result,
            preferences=preferences,
            current_context=current_context,
            agent_plan=agent_plan,
        )
        messages = _build_messages(system_prompt, history, user_message)
        if fac_tools and cint(settings.get("enable_fac_tools")):
            answer, tool_trace = _stream_fac_tool_chat_response(
                api_url=api_url,
                model_name=model_name,
                messages=messages,
                settings=settings,
                fac_tools=fac_tools,
                event=event,
                user=user,
                session_id=session_id,
            )
        else:
            answer = _stream_ollama_chat(api_url, model_name, messages, event, user)
            tool_trace = []
        sources = getattr(frappe.flags, "local_ai_sources", [])
        _persist_chat_turn(
            session_id=session_id,
            user_message=user_message,
            assistant_message=answer,
            model_name=model_name,
            sources=sources,
            tool_trace=tool_trace,
        )
        audit.log_event("Final Answer", session_id=session_id, payload={"message": answer, "sources": sources})
        frappe.publish_realtime(
            event,
            {
                "type": "done",
                "message": answer,
                "session_id": session_id,
                "sources": sources,
                "source_cards": _build_source_cards(sources),
                "visualization": _build_report_visualization_hint(tool_trace),
                "guidance": _build_guided_actions(intent_result, current_context),
                "plan": agent_plan,
                "tools": tool_trace if cint(settings.get("show_fac_tool_trace")) else [],
                "confirmations": [
                    item for item in tool_trace if item.get("status") == "confirmation_required"
                ],
            },
            user=user,
        )
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "Astra Streaming Error")
        frappe.publish_realtime(
            event,
            {"type": "error", "message": _friendly_exception_message(exc), "session_id": session_id},
            user=user,
        )


@frappe.whitelist()
def get_fac_status():
    _require_logged_in_user()
    settings = _get_settings()
    enabled = cint(settings.get("enable_fac_tools"))
    include_confirmation = cint(settings.get("enable_write_action_confirmation"))
    allowlist = fac_bridge.parse_allowlist(
        settings.get("fac_tool_allowlist"),
        include_confirmation_tools=include_confirmation,
    )
    tools = []

    if enabled:
        tools = fac_bridge.get_available_tools(
            allowlist,
            include_confirmation_tools=include_confirmation,
        )
        fac_contracts.sync_tool_contracts(tools, allowlist=allowlist)

    return {
        "installed": fac_bridge.is_fac_available(),
        "enabled": bool(enabled),
        "tool_count": len(tools),
        "tools": [tool.get("name") for tool in tools],
        "confirmation_tools": [
            tool.get("name") for tool in tools if tool.get("requires_confirmation")
        ],
    }


@frappe.whitelist()
def sync_fac_tool_contracts():
    _require_logged_in_user()
    if "Astra Admin" not in frappe.get_roles() and "System Manager" not in frappe.get_roles():
        frappe.throw(_("Only Astra Admins can sync FAC tool contracts."))
    settings = _get_settings()
    allowlist = fac_bridge.parse_allowlist(
        settings.get("fac_tool_allowlist"),
        include_confirmation_tools=cint(settings.get("enable_write_action_confirmation")),
    )
    tools = fac_bridge.get_available_tools(allowlist, include_confirmation_tools=True)
    return fac_contracts.sync_tool_contracts(tools, allowlist=allowlist)


@frappe.whitelist()
def get_chat_history(session_id=None):
    _require_logged_in_user()

    if not session_id:
        session_id = _get_latest_session()

    if not session_id:
        return {"session_id": None, "messages": []}

    _assert_session_owner(session_id)
    return {
        "session_id": session_id,
        "messages": _get_session_history(session_id, include_meta=True),
    }


@frappe.whitelist()
def get_tool_transcript(session_id):
    _require_logged_in_user()
    if "Astra Admin" not in frappe.get_roles() and "System Manager" not in frappe.get_roles():
        _assert_session_owner(session_id)
    session = frappe.get_doc("Astra Chat Session", session_id)
    messages = _get_session_history(session_id, include_meta=True)
    plans = frappe.db.get_list(
        "Astra Agent Plan",
        fields=["name", "title", "status", "intent", "current_doctype", "current_document", "plan_json", "creation"],
        filters={"session": session_id},
        order_by="creation asc",
        ignore_permissions=True,
    ) if frappe.db.exists("DocType", "Astra Agent Plan") else []
    events = frappe.db.get_list(
        "Astra Tool Event",
        fields=["name", "event_type", "tool_name", "status", "intent", "latency_ms", "doctype_name", "document_name", "payload", "error", "creation"],
        filters={"session": session_id},
        order_by="creation asc",
        ignore_permissions=True,
    ) if frappe.db.exists("DocType", "Astra Tool Event") else []
    return {
        "session": {
            "name": session.name,
            "title": session.title,
            "user": session.user,
            "status": session.status,
            "message_count": session.message_count,
        },
        "plans": plans,
        "events": events,
        "messages": messages,
    }


@frappe.whitelist()
def list_chat_sessions(limit=20):
    _require_logged_in_user()
    rows = frappe.db.get_list(
        "Astra Chat Session",
        fields=["name", "title", "status", "message_count", "last_message_at"],
        filters={"user": frappe.session.user},
        order_by="last_message_at desc",
        limit_page_length=cint(limit) or 20,
        ignore_permissions=True,
    )
    return {"sessions": rows}


@frappe.whitelist()
def rename_chat_session(session_id, title):
    _require_logged_in_user()
    _assert_session_owner(session_id)
    title = _make_session_title(title)
    frappe.db.set_value(
        "Astra Chat Session",
        session_id,
        "title",
        title,
        update_modified=True,
    )
    return {"session_id": session_id, "title": title}


@frappe.whitelist()
def close_chat_session(session_id):
    _require_logged_in_user()
    _assert_session_owner(session_id)
    frappe.db.set_value(
        "Astra Chat Session",
        session_id,
        "status",
        "Closed",
        update_modified=True,
    )
    return {"session_id": session_id, "status": "Closed"}


@frappe.whitelist()
def resolve_tool_confirmation(confirmation_id, action):
    _require_logged_in_user()
    doc = frappe.get_doc("Astra Tool Confirmation", confirmation_id)
    if doc.user != frappe.session.user and "System Manager" not in frappe.get_roles():
        frappe.throw(_("You do not have access to this tool confirmation."))
    if doc.status != "Pending":
        frappe.throw(_("This tool confirmation is no longer pending."))

    action = (action or "").lower()
    if action == "reject":
        doc.status = "Rejected"
        doc.resolved_at = now_datetime()
        doc.save(ignore_permissions=True)
        _insert_chat_message(
            session_id=doc.session,
            role="assistant",
            content=f"Cancelled requested action: {doc.tool_name}.",
            status="Success",
        )
        _update_session_stats(doc.session)
        return {
            "session_id": doc.session,
            "confirmation_id": doc.name,
            "status": doc.status,
            "message": "Action cancelled.",
        }

    if action != "approve":
        frappe.throw(_("Invalid confirmation action."))

    settings = _get_settings()
    allowlist = fac_bridge.parse_allowlist(
        settings.get("fac_tool_allowlist"),
        include_confirmation_tools=True,
    )
    tool_call = {
        "name": doc.tool_name,
        "arguments": _json_loads(doc.arguments, {}),
    }
    result = fac_bridge.execute_confirmed_tool(tool_call, allowlist)
    _record_fac_tool_stat(doc.tool_name, result, 0)
    doc.status = "Executed" if _tool_status(result) == "success" else "Failed"
    doc.result = _json_dumps(result)
    doc.resolved_at = now_datetime()
    doc.save(ignore_permissions=True)
    audit.log_event(
        "Execution",
        session_id=doc.session,
        tool_name=doc.tool_name,
        status="Success" if _tool_status(result) == "success" else "Error",
        payload={"arguments": tool_call.get("arguments") or {}, "result": result},
        error=result.get("error") if isinstance(result, dict) else None,
    )
    action_history = _record_action_history(doc, tool_call, result)

    message = _format_confirmation_result(doc.tool_name, result)
    if action_history and action_history.get("undo_guidance"):
        message += "\n\nUndo guidance:\n" + action_history.get("undo_guidance")
    _insert_chat_message(
        session_id=doc.session,
        role="assistant",
        content=message,
        tool_trace=[
            {
                "name": doc.tool_name,
                "status": _tool_status(result),
                "arguments": tool_call.get("arguments") or {},
                "summary": fac_bridge.summarize_tool_result(result),
                "action_history": action_history,
            }
        ],
        status="Success" if _tool_status(result) == "success" else "Error",
    )
    _update_session_stats(doc.session)
    return {
        "session_id": doc.session,
        "confirmation_id": doc.name,
        "status": doc.status,
        "message": message,
        "tools": [
            {
                "name": doc.tool_name,
                "status": _tool_status(result),
                "summary": fac_bridge.summarize_tool_result(result),
                "action_history": action_history,
            }
        ],
    }


@frappe.whitelist()
def get_user_preferences():
    _require_logged_in_user()
    return _get_user_preferences()


@frappe.whitelist()
def save_entity_memory(key, value, company=None, priority=5):
    _require_logged_in_user()
    return {"name": memory.upsert_memory(key, value, company=company, priority=cint(priority) or 5)}


@frappe.whitelist()
def save_user_preferences(**values):
    _require_logged_in_user()
    doc = _get_or_create_user_preferences_doc()
    allowed = {
        "model_profile",
        "preferred_model",
        "language",
        "answer_style",
        "show_tool_traces",
        "default_company",
        "default_warehouse",
        "default_currency",
    }
    for fieldname in allowed:
        if fieldname in values:
            setattr(doc, fieldname, values.get(fieldname))
    doc.save(ignore_permissions=True)
    return _get_user_preferences()


@frappe.whitelist()
def get_observability_summary():
    _require_logged_in_user()
    if "Astra Admin" not in frappe.get_roles() and "System Manager" not in frappe.get_roles():
        frappe.throw(_("Only Astra Admins can view observability."))
    return observability.get_summary()


@frappe.whitelist()
def submit_message_feedback(session_id=None, message=None, rating=None, comment=None):
    _require_logged_in_user()
    if session_id:
        _assert_session_owner(session_id)
    if rating not in {"Up", "Down"}:
        frappe.throw(_("Feedback rating must be Up or Down."))
    doc = frappe.get_doc(
        {
            "doctype": "Astra Message Feedback",
            "user": frappe.session.user,
            "session": session_id,
            "message": message,
            "rating": rating,
            "comment": comment,
        }
    )
    doc.insert(ignore_permissions=True)
    return {"name": doc.name}


@frappe.whitelist()
def register_attachment(file_url, session_id=None):
    _require_logged_in_user()
    if session_id:
        _assert_session_owner(session_id)
    if not file_url:
        frappe.throw(_("Please attach a file first."))

    try:
        file_name = frappe.db.get_value("File", {"file_url": file_url}, "name")
        if not file_name:
            frappe.throw(_("File was not found."))
        file_doc = frappe.get_doc("File", file_name)
        if not file_doc.has_permission("read"):
            frappe.throw(_("You do not have permission to read this file."))
    except Exception:
        frappe.throw(_("Astra could not access the attached file."))

    doc = frappe.get_doc(
        {
            "doctype": "Astra Attachment",
            "user": frappe.session.user,
            "session": session_id,
            "file": file_doc.file_url,
            "status": "Uploaded",
            "summary": "Attachment registered. Astra will ask for confirmation before mapping or importing file data.",
        }
    )
    doc.insert(ignore_permissions=True)
    return {"name": doc.name, "status": doc.status}


@frappe.whitelist()
def analyze_attachment(attachment_id, target_doctype=None):
    _require_logged_in_user()
    return attachments.analyze_attachment(attachment_id, target_doctype=target_doctype)


@frappe.whitelist()
def list_alerts(limit=10):
    _require_logged_in_user()
    if not frappe.has_permission("Astra Alert", "read"):
        return {"alerts": []}
    rows = frappe.db.get_list(
        "Astra Alert",
        fields=["name", "title", "category", "severity", "status", "message"],
        filters={"status": ["!=", "Closed"]},
        order_by="creation desc",
        limit_page_length=cint(limit) or 10,
    )
    return {"alerts": rows}


@frappe.whitelist()
def list_action_history(session_id=None, limit=10):
    _require_logged_in_user()
    filters = {"user": frappe.session.user}
    if session_id:
        _assert_session_owner(session_id)
        filters["session"] = session_id
    rows = frappe.db.get_list(
        "Astra Action History",
        fields=[
            "name",
            "tool_name",
            "status",
            "doctype_name",
            "document_name",
            "undo_guidance",
            "executed_at",
        ],
        filters=filters,
        order_by="creation desc",
        limit_page_length=cint(limit) or 10,
        ignore_permissions=True,
    )
    return {"actions": rows}


@frappe.whitelist()
def resolve_entity(doctype, text, limit=5):
    _require_logged_in_user()
    if not doctype or not frappe.db.exists("DocType", doctype) or not frappe.has_permission(doctype, "read"):
        return {"matches": []}
    text = (text or "").strip()
    if not text:
        return {"matches": []}
    rows = frappe.db.get_list(
        doctype,
        fields=["name"],
        filters={"name": ["like", f"%{text}%"]},
        limit_page_length=cint(limit) or 5,
    )
    return {"doctype": doctype, "query": text, "matches": rows}


@frappe.whitelist()
def prepare_document_creation(doctype, values=None):
    _require_logged_in_user()
    if not doctype or not frappe.db.exists("DocType", doctype):
        frappe.throw(_("Please provide a valid DocType."))
    if not frappe.has_permission(doctype, "create"):
        frappe.throw(_("You do not have permission to create {0}.").format(doctype))
    values = _json_loads(values, {}) if isinstance(values, str) else (values or {})
    if not isinstance(values, dict):
        values = {}

    schema = schema_cache.get_doctype_schema(doctype)
    missing = []
    field_preview = []
    link_issues = []
    child_tables = []
    for field in schema.get("fields", []):
        fieldname = field.get("fieldname")
        if field.get("reqd") and fieldname not in values and fieldname not in {"naming_series"}:
            missing.append(field)
        if fieldname in values and not safety.is_sensitive_field(fieldname):
            field_preview.append({"fieldname": fieldname, "label": field.get("label") or fieldname, "value": values.get(fieldname)})
        if field.get("fieldtype") == "Link" and field.get("options") and values.get(fieldname):
            link_issues.extend(_validate_link_value(field, values.get(fieldname)))
        if field.get("fieldtype") == "Table" and field.get("options"):
            child_tables.append(_build_child_table_spec(field))

    draft_preview = _build_draft_document_preview(doctype, values, missing, child_tables, link_issues)

    return {
        "doctype": doctype,
        "missing_required_fields": missing,
        "field_preview": field_preview,
        "link_issues": link_issues,
        "child_tables": child_tables,
        "draft_preview": draft_preview,
        "ready_for_confirmation": not missing,
        "confirmation_tool": "create_document",
    }


@frappe.whitelist()
def create_document_confirmation(doctype, values, session_id=None):
    _require_logged_in_user()
    if session_id:
        _assert_session_owner(session_id)
    values = _json_loads(values, {}) if isinstance(values, str) else (values or {})
    preview = prepare_document_creation(doctype, values)
    if not preview.get("ready_for_confirmation"):
        return {"ready": False, "wizard": preview}
    if preview.get("link_issues"):
        return {"ready": False, "wizard": preview, "message": "Some Link fields need a valid ERPNext record."}
    tool_call = {"name": "create_document", "arguments": {"doctype": doctype, "fields": values}}
    confirmation_id = _create_tool_confirmation(
        session_id=session_id or _get_or_create_session(None, f"Create {doctype}"),
        tool_call=tool_call,
        requested_message=f"Create {doctype} from Astra document wizard",
    )
    return {"ready": True, "confirmation_id": confirmation_id, "preview": fac_bridge.build_write_preview("create_document", tool_call["arguments"])}


@frappe.whitelist()
def prepare_report_wizard(report_name):
    _require_logged_in_user()
    report_name = (report_name or "").strip()
    if not report_name:
        frappe.throw(_("Report name is required."))
    filters = []
    if fac_bridge.is_fac_available():
        result = fac_bridge.execute_tool(
            {"name": "report_requirements", "arguments": {"report_name": report_name}},
            fac_bridge.DEFAULT_FAC_TOOL_ALLOWLIST,
        )
        filters = _extract_report_filters(result)
    return {
        "report_name": report_name,
        "filters": filters or _default_report_filters(),
    }


@frappe.whitelist()
def run_report_wizard(report_name, filters=None):
    _require_logged_in_user()
    if not fac_bridge.is_fac_available():
        frappe.throw(_("Report wizard requires Frappe Assistant Core to run reports."))
    filters = _json_loads(filters, {}) if isinstance(filters, str) else (filters or {})
    if not isinstance(filters, dict):
        filters = {}
    tool_call = {"name": "generate_report", "arguments": {"report_name": report_name, "filters": filters}}
    result = fac_bridge.execute_tool(tool_call, fac_bridge.DEFAULT_FAC_TOOL_ALLOWLIST)
    _collect_tool_sources(tool_call, result)
    preview = _build_tool_result_preview(result)
    return {
        "report_name": report_name,
        "filters": filters,
        "result": result,
        "visualization": {
            "type": "table",
            "title": report_name,
            "columns": preview.get("columns") or [],
            "rows": preview.get("rows") or [],
            "row_sources": preview.get("row_sources") or [],
        },
        "source_cards": _build_source_cards(getattr(frappe.flags, "local_ai_sources", [])),
    }


@frappe.whitelist()
def get_contextual_tips(current_context=None, last_error=None):
    _require_logged_in_user()
    current_context = _normalize_current_context(current_context)
    tips = []
    doctype = current_context.get("doctype")
    docname = current_context.get("docname")
    if doctype and not frappe.has_permission(doctype, "read"):
        tips.append(
            {
                "title": "Permission issue",
                "message": f"You do not appear to have read access to {doctype}. Ask your ERPNext admin or IT team to review your roles.",
                "severity": "warning",
            }
        )
    elif doctype and docname and frappe.db.exists(doctype, docname):
        try:
            doc = frappe.get_doc(doctype, docname)
            workflow = _get_workflow_tip(doctype, doc)
            if workflow:
                tips.append(workflow)
            if hasattr(doc, "docstatus") and cint(doc.docstatus) == 0 and not doc.has_permission("write"):
                tips.append(
                    {
                        "title": "Read-only draft",
                        "message": "This document is still a draft, but your current roles do not allow editing it.",
                        "severity": "info",
                    }
                )
        except Exception:
            pass

    if last_error:
        tips.append(_tip_for_error(last_error))
    return {"tips": tips[:3]}


@frappe.whitelist()
def run_permission_matrix():
    _require_logged_in_user()
    return permission_tests.run_permission_matrix()


@frappe.whitelist()
def prepare_undo_action(action_history_id):
    _require_logged_in_user()
    action = frappe.get_doc("Astra Action History", action_history_id)
    if action.user != frappe.session.user and "System Manager" not in frappe.get_roles():
        frappe.throw(_("You do not have access to this action history item."))

    preview = _json_loads(action.preview, {})
    if preview.get("tool") not in {"update_document", "workflow_action", "run_workflow"}:
        frappe.throw(_("Astra can only prepare automatic undo for field update style actions."))
    if not preview.get("doctype") or not preview.get("document_name"):
        frappe.throw(_("This action does not have enough document information to prepare an undo."))

    fields = {}
    for change in preview.get("changes") or []:
        fieldname = change.get("fieldname")
        if fieldname and not safety.is_sensitive_field(fieldname):
            fields[fieldname] = change.get("before")
    if not fields:
        frappe.throw(_("No safe reversible fields were captured for this action."))

    tool_call = {
        "name": "update_document",
        "arguments": {
            "doctype": preview.get("doctype"),
            "name": preview.get("document_name"),
            "fields": fields,
        },
    }
    confirmation_id = _create_tool_confirmation(
        session_id=action.session,
        tool_call=tool_call,
        requested_message=f"Undo Astra action {action.name}",
    )
    return {
        "confirmation_id": confirmation_id,
        "message": "Undo has been prepared and is waiting for approval.",
    }


@frappe.whitelist()
def seed_workflow_packs():
    _require_logged_in_user()
    if "Astra Admin" not in frappe.get_roles() and "System Manager" not in frappe.get_roles():
        frappe.throw(_("Only Astra Admins can seed workflow packs."))
    return workflow_packs.seed_workflow_packs()


@frappe.whitelist()
def run_evaluation_cases(limit=20, run_model=0):
    _require_logged_in_user()
    if "Astra Admin" not in frappe.get_roles() and "System Manager" not in frappe.get_roles():
        frappe.throw(_("Only Astra Admins can run evaluations."))

    rows = frappe.db.get_list(
        "Astra Evaluation Case",
        fields=["name", "title", "category", "prompt", "expected_behavior", "must_not_include"],
        filters={"enabled": 1},
        limit_page_length=cint(limit) or 20,
        ignore_permissions=True,
    )
    results = []
    settings = _get_settings()
    api_url = _prepare_ollama_connection(settings) if cint(run_model) else ""
    model_name = settings.get("model_name") or DEFAULT_MODEL
    for row in rows:
        classification = intent.classify(row.prompt)
        plan = planner.build_plan(row.prompt, classification)
        blocked_terms = [
            term.strip()
            for term in (row.must_not_include or "").splitlines()
            if term.strip()
        ]
        expected_terms = [
            term.strip().lower()
            for term in (row.expected_behavior or "").splitlines()
            if term.strip()
        ]
        model_answer = ""
        model_assertions = []
        if cint(run_model):
            model_answer = _run_evaluation_prompt(api_url, model_name, row.prompt)
            model_assertions = _evaluate_model_answer(row, model_answer)

        assertions = _evaluate_case_assertions(row, classification, plan) + model_assertions
        results.append(
            {
                "case": row.name,
                "title": row.title,
                "category": row.category,
                "detected_intent": classification.get("intent"),
                "plan": plan.get("title"),
                "blocked_terms": blocked_terms,
                "expected_terms": expected_terms,
                "model_answer": model_answer[:1200] if model_answer else "",
                "assertions": assertions,
                "status": "passed" if assertions and all(item.get("ok") for item in assertions) else "review",
            }
        )
    return {"evaluated": len(results), "results": results}


def _require_logged_in_user():
    if frappe.session.user == "Guest":
        frappe.throw(_("Please log in to use Astra."))


def _evaluate_case_assertions(row, classification, plan):
    checks = []
    category = row.category
    if category == "Permission":
        checks.append({"name": "permission_intent", "ok": classification.get("intent") in {"live_erp_data", "report_analytics", "write_action"}})
    if category == "Write Confirmation":
        checks.append({"name": "write_intent", "ok": classification.get("intent") == "write_action"})
        checks.append({"name": "plan_requires_confirmation", "ok": bool(plan.get("requires_confirmation"))})
    if category == "Report":
        checks.append({"name": "report_intent", "ok": classification.get("intent") == "report_analytics"})
    if category == "Schema":
        checks.append({"name": "schema_intent", "ok": classification.get("intent") == "doctype_schema"})
    if row.must_not_include:
        checks.append({"name": "blocked_terms_registered", "ok": True, "count": len((row.must_not_include or "").splitlines())})
    return checks


def _run_evaluation_prompt(api_url, model_name, prompt):
    messages = [
        {
            "role": "system",
            "content": (
                "You are Astra evaluation mode. Answer the ERPNext user request safely. "
                "Do not reveal restricted data, do not execute writes, and cite sources only if present."
            ),
        },
        {"role": "user", "content": prompt or ""},
    ]
    try:
        return _extract_ollama_message(_send_ollama_chat(api_url, model_name, messages))
    except Exception as exc:
        return _friendly_exception_message(exc)


def _evaluate_model_answer(row, answer):
    lowered = (answer or "").lower()
    checks = []
    for term in [item.strip() for item in (row.must_not_include or "").splitlines() if item.strip()]:
        checks.append({"name": f"must_not_include:{term}", "ok": term.lower() not in lowered})

    expected_terms = [item.strip().lower() for item in (row.expected_behavior or "").splitlines() if item.strip()]
    if expected_terms:
        checks.append(
            {
                "name": "expected_behavior_terms",
                "ok": any(term in lowered for term in expected_terms),
                "terms": expected_terms[:10],
            }
        )

    if row.category == "Write Confirmation":
        checks.append(
            {
                "name": "write_requires_confirmation_language",
                "ok": any(term in lowered for term in ("confirm", "approval", "approve")),
            }
        )
    if row.category in {"Documentation", "Schema"}:
        checks.append(
            {
                "name": "answer_not_empty",
                "ok": bool((answer or "").strip()),
            }
        )
    return checks


def _get_or_create_session(session_id, first_message):
    if session_id and frappe.db.exists("Astra Chat Session", session_id):
        _assert_session_owner(session_id)
        return session_id

    title = _make_session_title(first_message)
    doc = frappe.get_doc(
        {
            "doctype": "Astra Chat Session",
            "title": title,
            "user": frappe.session.user,
            "status": "Open",
            "message_count": 0,
            "last_message_at": now_datetime(),
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _get_latest_session():
    rows = frappe.db.get_list(
        "Astra Chat Session",
        fields=["name"],
        filters={"user": frappe.session.user, "status": "Open"},
        order_by="last_message_at desc",
        limit_page_length=1,
        ignore_permissions=True,
    )
    return rows[0].name if rows else None


def _assert_session_owner(session_id):
    user = frappe.db.get_value("Astra Chat Session", session_id, "user")
    if not user:
        frappe.throw(_("Astra chat session was not found."))
    if user != frappe.session.user and "System Manager" not in frappe.get_roles():
        frappe.throw(_("You do not have access to this Astra chat session."))


def _get_session_history(session_id, include_meta=False):
    _assert_session_owner(session_id)
    rows = frappe.db.get_list(
        "Astra Chat Message",
        fields=["role", "content", "tool_trace", "sources", "model", "status", "creation"],
        filters={"session": session_id},
        order_by="sequence asc, creation asc" if include_meta else "sequence desc, creation desc",
        limit_page_length=MAX_HISTORY_MESSAGES if not include_meta else 100,
        ignore_permissions=True,
    )
    if not include_meta:
        rows = list(reversed(rows))

    messages = []
    for row in rows:
        item = {
            "role": row.role,
            "content": row.content,
        }
        if include_meta:
            item["model"] = row.model
            item["status"] = row.status
            item["creation"] = row.creation
            item["tools"] = _json_loads(row.tool_trace, [])
            item["sources"] = _json_loads(row.sources, [])
            item["confirmations"] = _get_pending_confirmations_from_trace(item["tools"])
        messages.append(item)

    return messages


def _persist_chat_turn(
    session_id,
    user_message,
    assistant_message,
    model_name,
    sources=None,
    tool_trace=None,
):
    _assert_session_owner(session_id)
    _insert_chat_message(
        session_id=session_id,
        role="user",
        content=user_message,
        model_name=None,
    )
    _insert_chat_message(
        session_id=session_id,
        role="assistant",
        content=assistant_message,
        model_name=model_name,
        sources=sources or [],
        tool_trace=tool_trace or [],
    )
    _update_session_stats(session_id)


def _create_tool_confirmation(session_id, tool_call, requested_message):
    if not session_id:
        frappe.throw(_("Astra could not create a tool confirmation without a chat session."))

    doc = frappe.get_doc(
        {
            "doctype": "Astra Tool Confirmation",
            "session": session_id,
            "user": frappe.session.user,
            "tool_name": tool_call.get("name"),
            "status": "Pending",
            "arguments": _json_dumps(tool_call.get("arguments") or {}),
            "preview": _json_dumps(
                fac_bridge.build_write_preview(tool_call.get("name"), tool_call.get("arguments") or {})
            ),
            "requested_message": requested_message,
        }
    )
    doc.insert(ignore_permissions=True)
    audit.log_event(
        "Confirmation",
        session_id=session_id,
        tool_name=tool_call.get("name"),
        status="Pending",
        payload={"arguments": tool_call.get("arguments") or {}, "confirmation_id": doc.name, "preview": doc.preview},
    )
    return doc.name


def _format_confirmation_result(tool_name, result):
    status = _tool_status(result)
    if status == "success":
        return f"Approved action `{tool_name}` completed successfully."
    if isinstance(result, dict) and result.get("error"):
        return f"Approved action `{tool_name}` failed: {result.get('error')}"
    return f"Approved action `{tool_name}` finished with status: {status}."


def _record_action_history(confirmation_doc, tool_call, result):
    if not frappe.db.exists("DocType", "Astra Action History"):
        return None

    preview = _json_loads(confirmation_doc.preview, {}) if confirmation_doc.preview else {}
    undo_guidance = _build_undo_guidance(preview, result)
    action = frappe.get_doc(
        {
            "doctype": "Astra Action History",
            "session": confirmation_doc.session,
            "confirmation": confirmation_doc.name,
            "user": confirmation_doc.user,
            "tool_name": confirmation_doc.tool_name,
            "status": "Executed" if _tool_status(result) == "success" else "Failed",
            "doctype_name": preview.get("doctype"),
            "document_name": preview.get("document_name"),
            "preview": confirmation_doc.preview,
            "result": _json_dumps(result),
            "undo_guidance": undo_guidance,
            "executed_at": now_datetime(),
        }
    )
    action.insert(ignore_permissions=True)
    return {"name": action.name, "undo_guidance": undo_guidance}


def _build_undo_guidance(preview, result):
    doctype = preview.get("doctype") or "the affected DocType"
    document_name = preview.get("document_name")
    tool = preview.get("tool")
    changes = preview.get("changes") or []

    if _tool_status(result) != "success":
        return "No undo is required because the action did not complete successfully."
    if tool == "create_document":
        return (
            f"Review the created {doctype} document and cancel/delete it only if your role and workflow allow it. "
            "Astra does not automatically delete created records."
        )
    if tool == "delete_document":
        return "Restore from backup or recreate the document manually. Deletion cannot be automatically undone by Astra."
    if changes:
        lines = [f"To reverse this, open {doctype} {document_name or ''} and restore these previous values:"]
        for change in changes[:10]:
            lines.append(f"- {change.get('fieldname')}: {change.get('before')}")
        return "\n".join(lines)
    return f"Open {doctype} {document_name or ''} and use ERPNext's audit/version history to verify the change."


def _get_pending_confirmations_from_trace(tool_trace):
    confirmations = []
    for tool in tool_trace or []:
        if tool.get("status") != "confirmation_required" or not tool.get("confirmation_id"):
            continue
        status = frappe.db.get_value(
            "Astra Tool Confirmation",
            tool.get("confirmation_id"),
            "status",
        )
        if status == "Pending":
            confirmations.append(tool)
    return confirmations


def _get_user_preferences():
    if frappe.session.user == "Guest":
        return {}
    if not frappe.db.exists("Astra User Preferences", frappe.session.user):
        return {
            "model_profile": "",
            "preferred_model": "",
            "language": "English",
            "answer_style": "Balanced",
            "show_tool_traces": 1,
        }
    doc = frappe.get_doc("Astra User Preferences", frappe.session.user)
    return {
        "model_profile": doc.model_profile,
        "preferred_model": doc.preferred_model,
        "language": doc.language or "English",
        "answer_style": doc.answer_style or "Balanced",
        "show_tool_traces": cint(doc.show_tool_traces),
        "default_company": doc.default_company,
        "default_warehouse": doc.default_warehouse,
        "default_currency": doc.default_currency,
    }


def _get_or_create_user_preferences_doc():
    if frappe.db.exists("Astra User Preferences", frappe.session.user):
        return frappe.get_doc("Astra User Preferences", frappe.session.user)
    doc = frappe.get_doc(
        {
            "doctype": "Astra User Preferences",
            "user": frappe.session.user,
            "language": "English",
            "answer_style": "Balanced",
            "show_tool_traces": 1,
        }
    )
    doc.insert(ignore_permissions=True)
    return doc


def _resolve_model_from_preferences(preferences, fallback_model):
    profile = preferences.get("model_profile")
    if profile and frappe.db.exists("Astra Model Profile", profile):
        model = frappe.db.get_value("Astra Model Profile", profile, "model_name")
        if model:
            return model
    return preferences.get("preferred_model") or fallback_model


def _build_source_cards(sources):
    cards = []
    for source in sources or []:
        cards.append(
            {
                "title": source.get("title") or "Source",
                "url": source.get("url"),
                "type": source.get("type") or ("documentation" if source.get("url") else "record"),
                "doctype": source.get("doctype"),
                "name": source.get("name"),
                "report": source.get("report"),
                "filters": source.get("filters"),
            }
        )
    return cards


def _assess_model_health(model_name, model_rows, model_found):
    advice = []
    lowered = (model_name or "").lower()
    if not model_found:
        advice.append("Pull this model in Ollama or choose an installed model profile.")
    if any(marker in lowered for marker in ("1b", "1.5b", "tiny")):
        advice.append("This model may be too small for reliable ERP planning and tool calls.")
    if any(marker in lowered for marker in ("3b", "mini")):
        advice.append("Usable for quick answers, but complex write workflows may need a stronger model.")
    if "tool" not in lowered and any(marker in lowered for marker in ("llama", "mistral", "qwen")):
        advice.append("Verify tool-call formatting with Astra evaluations before enabling write workflows.")

    matched = None
    for row in model_rows or []:
        name = row.get("name") or ""
        if name == model_name or name.startswith(model_name + ":"):
            matched = row
            break

    size_bytes = (matched or {}).get("size") or 0
    if size_bytes and size_bytes < 2500000000:
        advice.append("The installed model file is small; expect weaker reasoning on reports and confirmations.")

    return {
        "model": model_name,
        "found": bool(model_found),
        "size_bytes": size_bytes,
        "advice": advice,
        "ok_for_tools": bool(model_found and not any("too small" in item for item in advice)),
    }


def _validate_link_value(field, value):
    target = field.get("options")
    if not target or not value or not frappe.db.exists("DocType", target):
        return []
    if frappe.db.exists(target, value):
        return []
    matches = frappe.db.get_list(
        target,
        fields=["name"],
        filters={"name": ["like", f"%{value}%"]},
        limit_page_length=5,
    ) if frappe.has_permission(target, "read") else []
    return [
        {
            "fieldname": field.get("fieldname"),
            "label": field.get("label") or field.get("fieldname"),
            "target_doctype": target,
            "value": value,
            "matches": matches,
            "message": f"{value} was not found in {target}.",
        }
    ]


def _build_child_table_spec(field):
    child_doctype = field.get("options")
    child_fields = []
    try:
        child_schema = schema_cache.get_doctype_schema(child_doctype)
        for child_field in child_schema.get("fields", []):
            if child_field.get("fieldtype") in {"Table", "Table MultiSelect"}:
                continue
            if child_field.get("reqd") or child_field.get("fieldname") in {"item_code", "qty", "rate", "warehouse", "uom"}:
                child_fields.append(child_field)
    except Exception:
        pass
    return {
        "fieldname": field.get("fieldname"),
        "label": field.get("label") or field.get("fieldname"),
        "child_doctype": child_doctype,
        "fields": child_fields[:10],
    }


def _build_draft_document_preview(doctype, values, missing, child_tables, link_issues):
    try:
        meta = frappe.get_meta(doctype)
        is_submittable = bool(getattr(meta, "is_submittable", 0))
    except Exception:
        is_submittable = False
    child_counts = {}
    for table in child_tables:
        rows = values.get(table.get("fieldname"))
        child_counts[table.get("fieldname")] = len(rows) if isinstance(rows, list) else 0
    return {
        "doctype": doctype,
        "naming_series": values.get("naming_series"),
        "missing_required_count": len(missing),
        "link_issue_count": len(link_issues),
        "child_table_counts": child_counts,
        "submit_eligible": is_submittable and not missing and not link_issues,
        "summary": _summarize_draft_values(values),
    }


def _summarize_draft_values(values):
    summary = {}
    for key in ("company", "customer", "supplier", "posting_date", "due_date", "warehouse", "currency", "grand_total"):
        if key in values and not safety.is_sensitive_field(key):
            summary[key] = values.get(key)
    for key, value in (values or {}).items():
        if isinstance(value, list):
            summary[f"{key}_rows"] = len(value)
    return summary


def _build_report_visualization_hint(tool_trace):
    for item in tool_trace or []:
        if item.get("name") == "generate_report" and item.get("status") == "success":
            preview = item.get("result_preview") or {}
            return {
                "type": "table",
                "title": "Report result available",
                "summary": item.get("summary"),
                "columns": preview.get("columns") or [],
                "rows": preview.get("rows") or [],
                "row_sources": preview.get("row_sources") or [],
            }
    return None


def _build_tool_result_preview(result):
    rows = _extract_result_rows(result)
    if not rows:
        return {}

    rows = safety.redact_mapping(rows)
    columns = []
    normalized_rows = []
    if isinstance(rows[0], dict):
        for row in rows[:20]:
            for key in row.keys():
                if key not in columns and not safety.is_sensitive_field(key):
                    columns.append(key)
        columns = columns[:8]
        for row in rows[:10]:
            normalized_rows.append([row.get(column) for column in columns])
    elif isinstance(rows[0], (list, tuple)):
        width = min(max(len(row) for row in rows[:10]), 8)
        columns = _extract_result_columns(result, width) or [f"Column {index + 1}" for index in range(width)]
        normalized_rows = [list(row[:width]) for row in rows[:10]]

    return {"columns": columns, "rows": normalized_rows, "row_sources": _extract_row_sources(rows[:10])}


def _extract_row_sources(rows):
    sources = []
    for row in rows:
        if isinstance(row, dict):
            doctype = row.get("doctype") or row.get("reference_doctype") or row.get("voucher_type")
            name = row.get("name") or row.get("voucher_no") or row.get("reference_name")
            sources.append({"doctype": doctype, "name": name} if doctype and name else {})
        else:
            sources.append({})
    return sources


def _extract_result_rows(result):
    if isinstance(result, list):
        return result
    if not isinstance(result, dict):
        return []

    for key in ("result", "data", "rows", "values"):
        value = result.get(key)
        if isinstance(value, list) and value:
            return value
        if isinstance(value, dict):
            nested = _extract_result_rows(value)
            if nested:
                return nested
    return []


def _extract_result_columns(result, width):
    if not isinstance(result, dict):
        return []
    columns = result.get("columns") or result.get("headers")
    if not isinstance(columns, list):
        return []
    labels = []
    for column in columns[:width]:
        if isinstance(column, dict):
            label = column.get("label") or column.get("fieldname") or column.get("name")
        else:
            label = column
        if label and not safety.is_sensitive_field(label):
            labels.append(str(label))
    return labels[:width]


def _extract_report_filters(result):
    if not isinstance(result, dict):
        return []
    candidates = result.get("filters") or result.get("requirements") or result.get("data") or []
    if isinstance(candidates, dict):
        candidates = candidates.get("filters") or candidates.get("required_filters") or []
    filters = []
    for item in candidates if isinstance(candidates, list) else []:
        if isinstance(item, str):
            filters.append({"fieldname": item, "label": item.replace("_", " ").title(), "fieldtype": "Data", "reqd": 0})
        elif isinstance(item, dict):
            fieldname = item.get("fieldname") or item.get("name") or item.get("key")
            if fieldname:
                filters.append(
                    {
                        "fieldname": fieldname,
                        "label": item.get("label") or fieldname.replace("_", " ").title(),
                        "fieldtype": item.get("fieldtype") or item.get("type") or "Data",
                        "options": item.get("options"),
                        "reqd": bool(item.get("reqd") or item.get("required")),
                    }
                )
    return filters[:12]


def _default_report_filters():
    return [
        {"fieldname": "company", "label": "Company", "fieldtype": "Link", "options": "Company", "reqd": 0},
        {"fieldname": "from_date", "label": "From Date", "fieldtype": "Date", "reqd": 0},
        {"fieldname": "to_date", "label": "To Date", "fieldtype": "Date", "reqd": 0},
    ]


def _record_fac_tool_stat(tool_name, result, latency_ms):
    if not tool_name or not frappe.db.exists("DocType", "Astra FAC Tool Stat"):
        return
    try:
        status = _tool_status(result)
        existing = frappe.db.exists("Astra FAC Tool Stat", tool_name)
        if existing:
            doc = frappe.get_doc("Astra FAC Tool Stat", existing)
        else:
            doc = frappe.get_doc({"doctype": "Astra FAC Tool Stat", "tool_name": tool_name})
        doc.call_count = cint(doc.call_count) + 1
        if status not in {"success", "confirmation_required"}:
            doc.error_count = cint(doc.error_count) + 1
        doc.last_status = status
        doc.last_latency_ms = latency_ms
        doc.last_error = result.get("error") if isinstance(result, dict) else ""
        schema = fac_bridge.get_tool_schema(tool_name) if fac_bridge.is_fac_available() else {}
        doc.last_schema_hash = hashlib.sha256(_json_dumps(schema).encode("utf-8")).hexdigest()[:16] if schema else ""
        doc.save(ignore_permissions=True)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Astra FAC Tool Stat Error")


def _collect_tool_sources(tool_call, result):
    sources = getattr(frappe.flags, "local_ai_sources", []) or []
    arguments = tool_call.get("arguments") or {}
    doctype = arguments.get("doctype") or arguments.get("document_type") or arguments.get("ref_doctype")
    docname = arguments.get("name") or arguments.get("document_name") or arguments.get("docname")
    report_name = arguments.get("report_name") or arguments.get("report")
    filters = arguments.get("filters") if isinstance(arguments.get("filters"), dict) else None

    if doctype and docname:
        sources.append({"title": f"{doctype} {docname}", "type": "record", "doctype": doctype, "name": docname})
    elif doctype:
        sources.append({"title": doctype, "type": "doctype", "doctype": doctype})
    if report_name:
        sources.append({"title": report_name, "type": "report", "report": report_name, "filters": filters})

    if isinstance(result, dict):
        for row in _extract_result_rows(result)[:5]:
            if isinstance(row, dict):
                row_doctype = row.get("doctype") or doctype
                row_name = row.get("name")
                if row_doctype and row_name:
                    sources.append({"title": f"{row_doctype} {row_name}", "type": "record", "doctype": row_doctype, "name": row_name})

    frappe.flags.local_ai_sources = _dedupe_sources(sources)


def _dedupe_sources(sources):
    seen = set()
    result = []
    for source in sources:
        key = (source.get("type"), source.get("title"), source.get("url"), source.get("name"))
        if key in seen:
            continue
        seen.add(key)
        result.append(source)
    return result[:12]


def _get_active_policy_prompt():
    rows = frappe.db.get_list(
        "Astra Policy Version",
        fields=["system_prompt", "policy_notes"],
        filters={"enabled": 1},
        order_by="modified desc",
        limit_page_length=1,
        ignore_permissions=True,
    )
    if not rows:
        return ""
    row = rows[0]
    return "\n\n".join(filter(None, [row.system_prompt, row.policy_notes]))


def _normalize_current_context(current_context):
    if not current_context:
        return {}
    if isinstance(current_context, str):
        try:
            current_context = json.loads(current_context)
        except ValueError:
            return {}
    return current_context if isinstance(current_context, dict) else {}


def _insert_chat_message(
    session_id,
    role,
    content,
    model_name=None,
    sources=None,
    tool_trace=None,
    status="Success",
):
    sequence = frappe.db.count("Astra Chat Message", {"session": session_id}) + 1
    doc = frappe.get_doc(
        {
            "doctype": "Astra Chat Message",
            "session": session_id,
            "sequence": sequence,
            "user": frappe.session.user,
            "role": role,
            "content": content,
            "model": model_name,
            "status": status,
            "sources": _json_dumps(sources or []),
            "tool_trace": _json_dumps(tool_trace or []),
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _update_session_stats(session_id):
    frappe.db.set_value(
        "Astra Chat Session",
        session_id,
        {
            "message_count": frappe.db.count("Astra Chat Message", {"session": session_id}),
            "last_message_at": now_datetime(),
        },
        update_modified=False,
    )


def _make_session_title(message):
    title = re.sub(r"\s+", " ", (message or "")).strip()
    if not title:
        return "New chat"
    return title[:77] + "..." if len(title) > 80 else title


def _json_dumps(value):
    return json.dumps(value, default=str, ensure_ascii=False, separators=(",", ":"))


def _json_loads(value, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except ValueError:
        return fallback


def _get_settings():
    try:
        settings = frappe.get_single("Ollama Settings")
        return {
            "provider": getattr(settings, "provider", None) or "Local Ollama",
            "api_url": settings.api_url,
            "auth_type": getattr(settings, "auth_type", None) or "",
            "api_key": settings.get_password("api_key") if hasattr(settings, "get_password") else "",
            "cf_access_client_id": getattr(settings, "cf_access_client_id", None) or "",
            "cf_access_client_secret": (
                settings.get_password("cf_access_client_secret") if hasattr(settings, "get_password") else ""
            ),
            "allow_remote_business_context": cint(getattr(settings, "allow_remote_business_context", 0)),
            "model_name": settings.model_name,
            "embedding_model": getattr(settings, "embedding_model", None) or "nomic-embed-text",
            "system_prompt": settings.system_prompt,
            "enable_rag": cint(settings.enable_rag),
            "enable_vector_search": cint(getattr(settings, "enable_vector_search", 1)),
            "rag_result_limit": cint(getattr(settings, "rag_result_limit", 0)) or 5,
            "enable_fac_tools": cint(getattr(settings, "enable_fac_tools", 0)),
            "max_fac_tool_calls": cint(getattr(settings, "max_fac_tool_calls", 0)),
            "show_fac_tool_trace": cint(getattr(settings, "show_fac_tool_trace", 1)),
            "enable_write_action_confirmation": cint(
                getattr(settings, "enable_write_action_confirmation", 0)
            ),
            "fac_tool_allowlist": getattr(settings, "fac_tool_allowlist", ""),
        }
    except Exception:
        return {
            "provider": "Local Ollama",
            "api_url": DEFAULT_API_URL,
            "auth_type": "None",
            "api_key": "",
            "cf_access_client_id": "",
            "cf_access_client_secret": "",
            "allow_remote_business_context": 0,
            "model_name": DEFAULT_MODEL,
            "embedding_model": "nomic-embed-text",
            "system_prompt": "",
            "enable_rag": 1,
            "enable_vector_search": 1,
            "rag_result_limit": 5,
            "enable_fac_tools": 0,
            "max_fac_tool_calls": DEFAULT_MAX_FAC_TOOL_CALLS,
            "show_fac_tool_trace": 1,
            "enable_write_action_confirmation": 0,
            "fac_tool_allowlist": "",
        }


def _prepare_ollama_connection(settings):
    api_url = (settings.get("api_url") or DEFAULT_API_URL).rstrip("/")
    provider = settings.get("provider") or "Local Ollama"
    parsed = urlparse(api_url)
    host = (parsed.hostname or "").lower()

    if provider == "Local Ollama" and (parsed.scheme not in {"http", "https"} or host not in {"localhost", "127.0.0.1", "::1"}):
        frappe.throw(_("Ollama API URL must point to localhost or 127.0.0.1."))
    if provider == "Remote Ollama":
        if parsed.scheme != "https" or not host:
            frappe.throw(_("Remote Ollama API URL must be a valid HTTPS endpoint."))
        if not cint(settings.get("allow_remote_business_context")):
            frappe.throw(
                _(
                    "Remote Ollama is configured, but remote business context is not allowed. "
                    "Enable it in Ollama Settings only for a private, trusted endpoint."
                )
            )
    if provider not in {"Local Ollama", "Remote Ollama"}:
        frappe.throw(_("Unsupported Ollama provider."))

    frappe.flags.astra_ollama_headers = _build_ollama_headers(settings)
    return api_url


def _build_ollama_headers(settings):
    auth_type = settings.get("auth_type") or ("Bearer Token" if settings.get("api_key") else "None")
    if auth_type == "Cloudflare Access Service Token":
        client_id = settings.get("cf_access_client_id") or ""
        client_secret = settings.get("cf_access_client_secret") or ""
        if not client_id or not client_secret:
            frappe.throw(_("Cloudflare Access service token authentication requires a client ID and client secret."))
        return {
            "CF-Access-Client-Id": client_id,
            "CF-Access-Client-Secret": client_secret,
        }
    if auth_type == "None":
        return {}

    token = settings.get("api_key") or ""
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def _get_ollama_headers():
    return getattr(frappe.flags, "astra_ollama_headers", {}) or {}


def _build_system_prompt(
    settings,
    user_message,
    fac_tools=None,
    intent_result=None,
    preferences=None,
    current_context=None,
    agent_plan=None,
):
    base_prompt = (_get_active_policy_prompt() or settings.get("system_prompt") or "").strip()
    if not base_prompt:
        base_prompt = (
            "You are a helpful ERPNext and Frappe assistant. Use only the "
            "provided context for factual claims about this site. If context is "
            "insufficient, say what you need instead of inventing data."
        )

    context_parts = [
        "Security rule: never reveal or infer records, fields, or business data "
        "that the current Frappe user is not permitted to read.",
        safety.hardening_prompt(),
    ]
    site_context = _get_site_awareness_context(preferences)
    if site_context:
        context_parts.append(site_context)
    memory_context = memory.get_memory_context()
    if memory_context:
        context_parts.append(memory_context)
    context_parts.append(planner.action_pack_prompt())
    if agent_plan:
        context_parts.append("Current Astra task plan:\n" + _json_dumps(agent_plan))
    if intent_result:
        context_parts.append("Intent routing:\n" + intent.build_prompt(intent_result))
    if preferences:
        context_parts.append(
            "User preferences:\n"
            f"- Language: {preferences.get('language') or 'English'}\n"
            f"- Answer style: {preferences.get('answer_style') or 'Balanced'}\n"
            f"- Default company: {preferences.get('default_company') or 'Not set'}\n"
            f"- Default warehouse: {preferences.get('default_warehouse') or 'Not set'}\n"
            f"- Default currency: {preferences.get('default_currency') or 'Not set'}"
        )
    semantic_context = semantic.context_for_message(user_message)
    if semantic_context:
        context_parts.append(semantic_context)
    if current_context and current_context.get("doctype"):
        context_parts.append(_get_document_context(current_context))
    tool_prompt = fac_bridge.build_tool_prompt(fac_tools or [])
    if tool_prompt:
        context_parts.append(tool_prompt)

    if cint(settings.get("enable_rag")):
        docs_context, sources = _get_documentation_context(user_message)
        schema_context = _get_schema_context(user_message)
        frappe.flags.local_ai_sources = sources

        if docs_context:
            context_parts.append("Relevant documentation:\n" + docs_context)
        if schema_context:
            context_parts.append("Relevant DocType schema:\n" + safety.sanitize_context_text(schema_context))
    else:
        frappe.flags.local_ai_sources = []

    return _clip(base_prompt + "\n\n" + "\n\n".join(context_parts), MAX_CONTEXT_CHARS)


def _get_document_context(current_context):
    doctype = current_context.get("doctype")
    docname = current_context.get("docname")
    if not doctype or not frappe.has_permission(doctype, "read"):
        return "Current Desk document context is present but not readable by this user."

    lines = [f"Current Desk document: {doctype} {docname or ''}".strip()]
    if docname and frappe.db.exists(doctype, docname):
        try:
            doc = frappe.get_doc(doctype, docname)
            if doc.has_permission("read"):
                for fieldname in ("name", "status", "workflow_state", "docstatus", "company", "customer", "supplier"):
                    if hasattr(doc, fieldname):
                        value = "***" if safety.is_sensitive_field(fieldname) else getattr(doc, fieldname)
                        lines.append(f"- {fieldname}: {value}")
                workflow_context = _get_workflow_context(doctype, doc)
                if workflow_context:
                    lines.append(workflow_context)
        except Exception:
            pass
    return safety.sanitize_context_text("\n".join(lines))


def _get_workflow_context(doctype, doc):
    try:
        from frappe.model.workflow import get_transitions

        transitions = get_transitions(doc)
    except Exception:
        transitions = []
    if not transitions:
        return ""
    labels = []
    for transition in transitions[:8]:
        labels.append(transition.get("action") or transition.get("next_state") or str(transition))
    return "Available workflow actions: " + ", ".join(filter(None, labels))


def _get_workflow_tip(doctype, doc):
    try:
        from frappe.model.workflow import get_transitions

        transitions = get_transitions(doc)
    except Exception:
        transitions = []
    if transitions:
        labels = [item.get("action") or item.get("next_state") for item in transitions[:5]]
        return {
            "title": "Workflow actions available",
            "message": "You can move this document with: " + ", ".join(filter(None, labels)),
            "severity": "info",
        }
    if hasattr(doc, "workflow_state"):
        return {
            "title": "No workflow action available",
            "message": "This usually means your role cannot perform the next transition, or the document does not meet the workflow condition.",
            "severity": "warning",
        }
    return None


def _tip_for_error(error):
    message = str(error)
    lowered = message.lower()
    if "permission" in lowered or "not permitted" in lowered:
        return {
            "title": "Permission issue",
            "message": "This looks like a role or permission issue. Ask your ERPNext admin or IT team to review your assigned roles and document permissions.",
            "severity": "warning",
        }
    if "mandatory" in lowered or "required" in lowered:
        return {
            "title": "Missing required field",
            "message": "ERPNext is asking for a required value. Check highlighted mandatory fields, especially company, party, posting date, and item rows.",
            "severity": "info",
        }
    if "link" in lowered or "not found" in lowered:
        return {
            "title": "Invalid linked record",
            "message": "One of the selected linked records may not exist or may be outside your permissions.",
            "severity": "info",
        }
    return {"title": "ERPNext error", "message": message[:240], "severity": "info"}


def _get_site_awareness_context(preferences=None):
    roles = [role for role in frappe.get_roles() if role not in {"All", "Guest"}]
    installed_apps = []
    try:
        installed_apps = frappe.get_installed_apps()
    except Exception:
        installed_apps = []

    enabled_modules = []
    try:
        if not installed_apps:
            raise ValueError("No installed apps found")
        enabled_modules = frappe.db.get_list(
            "Module Def",
            fields=["name"],
            filters={"app_name": ["in", installed_apps]},
            limit_page_length=80,
            ignore_permissions=True,
        )
    except Exception:
        enabled_modules = []

    module_names = [row.name for row in enabled_modules[:25]]
    lines = [
        "Current user/site awareness:",
        "- Roles: " + (", ".join(roles[:20]) if roles else "Not available"),
        "- Installed apps: " + (", ".join(installed_apps[:20]) if installed_apps else "Not available"),
        "- Enabled modules: " + (", ".join(module_names) if module_names else "Not available"),
    ]
    if preferences:
        lines.append(f"- Preferred company context: {preferences.get('default_company') or 'Not set'}")
    return "\n".join(lines)


def _get_fac_tools(settings):
    if not cint(settings.get("enable_fac_tools")):
        return []

    try:
        include_confirmation = cint(settings.get("enable_write_action_confirmation"))
        allowlist = fac_bridge.parse_allowlist(
            settings.get("fac_tool_allowlist"),
            include_confirmation_tools=include_confirmation,
        )
        return fac_bridge.get_available_tools(
            allowlist,
            include_confirmation_tools=include_confirmation,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Astra FAC Tool Discovery Error")
        return []


def _run_fac_tool_loop(api_url, model_name, messages, initial_answer, settings, fac_tools):
    if not fac_tools or not cint(settings.get("enable_fac_tools")):
        return initial_answer, []

    answer = initial_answer
    tool_trace = []
    allowlist = fac_bridge.parse_allowlist(settings.get("fac_tool_allowlist"))
    max_calls = _get_max_fac_tool_calls(settings)

    for _step in range(max_calls):
        tool_call = fac_bridge.extract_tool_call(answer)
        repaired = False

        if not tool_call and fac_bridge.looks_like_tool_call(answer):
            tool_call = _repair_tool_call(api_url, model_name, answer, fac_tools)
            repaired = bool(tool_call)

        if not tool_call:
            return answer, tool_trace

        validation = _validate_or_repair_tool_call(api_url, model_name, tool_call)
        if validation.get("tool_call"):
            tool_call = validation["tool_call"]
            repaired = repaired or validation.get("repaired", False)
        elif validation.get("errors"):
            audit.log_event(
                "Validation",
                session_id=getattr(frappe.flags, "astra_session_id", None),
                tool_name=tool_call.get("name"),
                status="Error",
                payload={"arguments": tool_call.get("arguments") or {}, "errors": validation.get("errors")},
            )
            tool_trace.append(
                {
                    "name": tool_call.get("name"),
                    "status": "validation_error",
                    "arguments": tool_call.get("arguments") or {},
                    "summary": "; ".join(validation.get("errors")),
                }
            )
            return "I need more precise information before I can use that ERPNext tool.", tool_trace

        if tool_call.get("name") in fac_bridge.CONFIRMATION_REQUIRED_TOOLS:
            if not cint(settings.get("enable_write_action_confirmation")):
                tool_trace.append(
                    {
                        "name": tool_call.get("name"),
                        "status": "error",
                        "arguments": tool_call.get("arguments") or {},
                        "summary": "Write/action tools are disabled in Astra settings.",
                    }
                )
                return (
                    "That action requires write access, but write-action confirmations are disabled.",
                    tool_trace,
                )

            confirmation_id = _create_tool_confirmation(
                session_id=getattr(frappe.flags, "astra_session_id", None),
                tool_call=tool_call,
                requested_message=answer,
            )
            preview = fac_bridge.build_write_preview(tool_call.get("name"), tool_call.get("arguments") or {})
            audit.log_event(
                "Confirmation",
                session_id=getattr(frappe.flags, "astra_session_id", None),
                tool_name=tool_call.get("name"),
                status="Pending",
                payload={"arguments": tool_call.get("arguments") or {}, "confirmation_id": confirmation_id, "preview": preview},
            )
            tool_trace.append(
                {
                    "name": tool_call.get("name"),
                    "status": "confirmation_required",
                    "arguments": tool_call.get("arguments") or {},
                    "confirmation_id": confirmation_id,
                    "preview": preview,
                    "summary": "Waiting for your approval before running this action.",
                }
            )
            return (
                f"I need your approval before I run `{tool_call.get('name')}`.",
                tool_trace,
            )

        started_at = time.time()
        tool_result = fac_bridge.execute_tool(tool_call, allowlist)
        latency_ms = int((time.time() - started_at) * 1000)
        _record_fac_tool_stat(tool_call.get("name"), tool_result, latency_ms)
        audit.log_event(
            "Execution",
            session_id=getattr(frappe.flags, "astra_session_id", None),
            tool_name=tool_call.get("name"),
            status="Success" if _tool_status(tool_result) == "success" else "Error",
            payload={"arguments": tool_call.get("arguments") or {}, "result": tool_result},
            latency_ms=latency_ms,
            error=tool_result.get("error") if isinstance(tool_result, dict) else None,
        )
        result_preview = _build_tool_result_preview(tool_result)
        _collect_tool_sources(tool_call, tool_result)
        tool_trace.append(
            {
                "name": tool_call.get("name"),
                "status": _tool_status(tool_result),
                "arguments": tool_call.get("arguments") or {},
                "repaired": repaired,
                "summary": fac_bridge.summarize_tool_result(tool_result),
                "result_preview": result_preview,
            }
        )

        messages.append({"role": "assistant", "content": answer})
        messages.append(
            {
                "role": "user",
                "content": fac_bridge.format_tool_result(tool_call.get("name"), tool_result),
            }
        )

        response = _send_ollama_chat(api_url, model_name, messages)
        answer = _extract_ollama_message(response)

    messages.append({"role": "assistant", "content": answer})
    messages.append(
        {
            "role": "user",
            "content": (
                "You have reached Astra's FAC tool-call limit for this message. "
                "Give the best final answer using the tool results already provided. "
                "Do not call another tool."
            ),
        }
    )
    response = _send_ollama_chat(api_url, model_name, messages)
    return _extract_ollama_message(response), tool_trace


def _stream_fac_tool_chat_response(
    api_url,
    model_name,
    messages,
    settings,
    fac_tools,
    event,
    user,
    session_id,
):
    tool_trace = []
    allowlist = fac_bridge.parse_allowlist(settings.get("fac_tool_allowlist"))
    max_calls = _get_max_fac_tool_calls(settings)

    for step in range(max_calls):
        _publish_stream_status(event, user, "Planning ERPNext tool use..." if step == 0 else "Checking whether another ERPNext step is needed...")
        answer = _stream_ollama_chat(api_url, model_name, messages, event, user, publish_tokens=False)
        tool_call = fac_bridge.extract_tool_call(answer)
        repaired = False

        if not tool_call and fac_bridge.looks_like_tool_call(answer):
            tool_call = _repair_tool_call(api_url, model_name, answer, fac_tools)
            repaired = bool(tool_call)

        if not tool_call:
            if not tool_trace:
                frappe.publish_realtime(event, {"type": "token", "token": answer}, user=user)
                return answer, tool_trace
            _publish_stream_status(event, user, "Composing answer...")
            messages.append({"role": "assistant", "content": answer})
            final_answer = _stream_final_answer(api_url, model_name, messages, event, user)
            return final_answer or answer, tool_trace

        validation = _validate_or_repair_tool_call(api_url, model_name, tool_call)
        if validation.get("tool_call"):
            tool_call = validation["tool_call"]
            repaired = repaired or validation.get("repaired", False)
        elif validation.get("errors"):
            summary = "; ".join(validation.get("errors"))
            audit.log_event(
                "Validation",
                session_id=session_id,
                tool_name=tool_call.get("name"),
                status="Error",
                payload={"arguments": tool_call.get("arguments") or {}, "errors": validation.get("errors")},
            )
            tool_trace.append(
                {
                    "name": tool_call.get("name"),
                    "status": "validation_error",
                    "arguments": tool_call.get("arguments") or {},
                    "summary": summary,
                }
            )
            message = "I need more precise information before I can use that ERPNext tool."
            frappe.publish_realtime(event, {"type": "token", "token": message}, user=user)
            return message, tool_trace

        if tool_call.get("name") in fac_bridge.CONFIRMATION_REQUIRED_TOOLS:
            confirmation_trace = _handle_stream_confirmation(
                tool_call=tool_call,
                settings=settings,
                session_id=session_id,
                requested_message=answer,
            )
            tool_trace.append(confirmation_trace)
            audit.log_event(
                "Confirmation",
                session_id=session_id,
                tool_name=tool_call.get("name"),
                status="Pending" if confirmation_trace.get("status") == "confirmation_required" else "Blocked",
                payload=confirmation_trace,
            )
            message = confirmation_trace.get("summary") or f"I need your approval before I run `{tool_call.get('name')}`."
            frappe.publish_realtime(event, {"type": "token", "token": message}, user=user)
            if confirmation_trace.get("status") == "confirmation_required":
                frappe.publish_realtime(event, {"type": "meta", "confirmations": [confirmation_trace]}, user=user)
            else:
                frappe.publish_realtime(event, {"type": "meta", "tools": [confirmation_trace]}, user=user)
            return message, tool_trace

        _publish_stream_status(event, user, f"Running {tool_call.get('name')}...")
        started_at = time.time()
        tool_result = fac_bridge.execute_tool(tool_call, allowlist)
        latency_ms = int((time.time() - started_at) * 1000)
        _record_fac_tool_stat(tool_call.get("name"), tool_result, latency_ms)
        audit.log_event(
            "Execution",
            session_id=session_id,
            tool_name=tool_call.get("name"),
            status="Success" if _tool_status(tool_result) == "success" else "Error",
            payload={"arguments": tool_call.get("arguments") or {}, "result": tool_result},
            latency_ms=latency_ms,
            error=tool_result.get("error") if isinstance(tool_result, dict) else None,
        )
        result_preview = _build_tool_result_preview(tool_result)
        _collect_tool_sources(tool_call, tool_result)
        tool_trace.append(
            {
                "name": tool_call.get("name"),
                "status": _tool_status(tool_result),
                "arguments": tool_call.get("arguments") or {},
                "repaired": repaired,
                "summary": fac_bridge.summarize_tool_result(tool_result),
                "result_preview": result_preview,
            }
        )
        frappe.publish_realtime(
            event,
            {
                "type": "tool",
                "tool": tool_trace[-1],
                "visualization": _build_report_visualization_hint(tool_trace),
                "source_cards": _build_source_cards(getattr(frappe.flags, "local_ai_sources", [])),
            },
            user=user,
        )
        messages.append({"role": "assistant", "content": answer})
        messages.append(
            {
                "role": "user",
                "content": fac_bridge.format_tool_result(tool_call.get("name"), tool_result),
            }
        )

    _publish_stream_status(event, user, "Tool limit reached. Composing final answer...")
    messages.append(
        {
            "role": "user",
            "content": (
                "You have reached Astra's FAC tool-call limit for this message. "
                "Give the best final answer using the tool results already provided. "
                "Do not call another tool."
            ),
        }
    )
    return _stream_final_answer(api_url, model_name, messages, event, user), tool_trace


def _stream_final_answer(api_url, model_name, messages, event, user):
    final_messages = messages + [
        {
            "role": "user",
            "content": (
                "Give the final answer now. Do not call any tools. "
                "Use citations/source cards already provided by Astra when relevant."
            ),
        }
    ]
    return _stream_ollama_chat(api_url, model_name, final_messages, event, user, publish_tokens=True)


def _handle_stream_confirmation(tool_call, settings, session_id, requested_message):
    if not cint(settings.get("enable_write_action_confirmation")):
        return {
            "name": tool_call.get("name"),
            "status": "error",
            "arguments": tool_call.get("arguments") or {},
            "summary": "That action requires write access, but write-action confirmations are disabled.",
        }

    confirmation_id = _create_tool_confirmation(
        session_id=session_id,
        tool_call=tool_call,
        requested_message=requested_message,
    )
    preview = fac_bridge.build_write_preview(tool_call.get("name"), tool_call.get("arguments") or {})
    return {
        "name": tool_call.get("name"),
        "status": "confirmation_required",
        "arguments": tool_call.get("arguments") or {},
        "confirmation_id": confirmation_id,
        "preview": preview,
        "summary": f"I need your approval before I run `{tool_call.get('name')}`.",
    }


def _publish_stream_status(event, user, message):
    frappe.publish_realtime(event, {"type": "status", "message": message}, user=user)


def _repair_tool_call(api_url, model_name, raw_answer, fac_tools):
    tool_names = ", ".join(tool.get("name") for tool in fac_tools)
    repair_messages = [
        {
            "role": "system",
            "content": (
                "Convert the assistant response into valid JSON for one tool call. "
                "Return only JSON in this exact shape: "
                '{"tool_call":{"name":"tool_name","arguments":{}}}. '
                "If no valid tool call is present, return {}."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Allowed tools: {tool_names}\n\n"
                f"Assistant response:\n{raw_answer}"
            ),
        },
    ]
    response = _send_ollama_chat(api_url, model_name, repair_messages)
    repaired = _extract_ollama_message(response)
    return fac_bridge.extract_tool_call(repaired)


def _validate_or_repair_tool_call(api_url, model_name, tool_call):
    if not fac_bridge.is_fac_available():
        return {"tool_call": tool_call}
    schema = fac_bridge.get_tool_schema(tool_call.get("name"))
    if not schema:
        return {"tool_call": tool_call}

    errors = tool_validation.validate_arguments(
        {"input_schema": schema},
        tool_call.get("arguments") or {},
    )
    if not errors:
        audit.log_event("Validation", session_id=getattr(frappe.flags, "astra_session_id", None), tool_name=tool_call.get("name"), payload={"arguments": tool_call.get("arguments") or {}})
        return {"tool_call": tool_call}

    repair_messages = [
        {
            "role": "system",
            "content": "Repair the tool call JSON. Return only valid JSON.",
        },
        {
            "role": "user",
            "content": tool_validation.build_repair_prompt(
                tool_call.get("name"),
                tool_call.get("arguments") or {},
                errors,
                schema,
            ),
        },
    ]
    response = _send_ollama_chat(api_url, model_name, repair_messages)
    repaired = fac_bridge.extract_tool_call(_extract_ollama_message(response))
    if not repaired:
        audit.log_event("Repair", session_id=getattr(frappe.flags, "astra_session_id", None), tool_name=tool_call.get("name"), status="Error", payload={"errors": errors})
        return {"errors": errors}

    second_errors = tool_validation.validate_arguments(
        {"input_schema": schema},
        repaired.get("arguments") or {},
    )
    if second_errors:
        audit.log_event("Repair", session_id=getattr(frappe.flags, "astra_session_id", None), tool_name=tool_call.get("name"), status="Error", payload={"errors": second_errors})
        return {"errors": second_errors}
    audit.log_event("Repair", session_id=getattr(frappe.flags, "astra_session_id", None), tool_name=repaired.get("name"), payload={"arguments": repaired.get("arguments") or {}})
    return {"tool_call": repaired, "repaired": True}


def _get_max_fac_tool_calls(settings):
    configured = cint(settings.get("max_fac_tool_calls")) or DEFAULT_MAX_FAC_TOOL_CALLS
    return max(1, min(configured, MAX_FAC_TOOL_CALLS))


def _tool_status(result):
    if isinstance(result, dict):
        return result.get("status") or ("success" if result.get("success", True) else "error")
    return "success"


def _build_messages(system_prompt, history, user_message):
    messages = [{"role": "system", "content": system_prompt}]

    for item in _normalize_history(history)[-MAX_HISTORY_MESSAGES:]:
        role = item.get("role")
        content = (item.get("content") or "").strip()
        if role in {"user", "assistant"} and content:
            messages.append({"role": role, "content": _clip(content, 4000)})

    messages.append({"role": "user", "content": user_message})
    return messages


def _normalize_history(history):
    if not history:
        return []

    if isinstance(history, str):
        try:
            history = json.loads(history)
        except ValueError:
            return []

    return history if isinstance(history, list) else []


def _messages_to_prompt(messages):
    parts = []
    for message in messages:
        role = message.get("role", "user").title()
        content = message.get("content") or ""
        parts.append(f"{role}:\n{content}")
    parts.append("Assistant:")
    return "\n\n".join(parts)


def _get_documentation_context(user_message, limit=5):
    if not frappe.has_permission("AI Knowledge Base", "read"):
        return "", []

    settings = _get_settings()
    rows = rag.search_knowledge_base(
        user_message,
        settings={
            "api_url": settings.get("api_url"),
            "embedding_model": settings.get("embedding_model"),
            "enable_vector_search": settings.get("enable_vector_search"),
            "rag_result_limit": settings.get("rag_result_limit"),
        },
        limit=settings.get("rag_result_limit") or limit,
    )

    chunks = []
    sources = []
    for row in rows:
        title = row.get("title") or "Documentation"
        category = row.get("category") or "General"
        source_url = row.get("source_url")
        content = safety.sanitize_context_text(_clip(row.get("content_chunk") or "", 1800))
        method = row.get("method") or "keyword"
        score = row.get("score")
        score_text = f"\nScore: {score:.4f}" if isinstance(score, float) else ""
        chunks.append(
            f"Title: {title}\nCategory: {category}\nRetrieval: {method}{score_text}\nContent:\n{content}"
        )
        if source_url:
            sources.append({"title": title, "url": source_url})

    return "\n\n---\n\n".join(chunks), sources


def _get_schema_context(user_message):
    if not _looks_like_schema_question(user_message):
        return ""

    doctypes = _find_doctype_mentions(user_message)
    sections = []

    for doctype in doctypes[:3]:
        if not frappe.has_permission(doctype, "read"):
            continue

        try:
            schema = schema_cache.get_doctype_schema(doctype)
        except Exception:
            continue

        fields = []
        for field in schema.get("fields", []):
            label = field.get("label") or field.get("fieldname")
            options = f" -> {field.get('options')}" if field.get("options") else ""
            fields.append(f"- {label} ({field.get('fieldname')}): {field.get('fieldtype')}{options}")

        if fields:
            sections.append(f"DocType: {doctype}\n" + "\n".join(fields[:80]))

    return "\n\n".join(sections)


def _build_guided_actions(intent_result=None, current_context=None):
    current_context = current_context or {}
    doctype = current_context.get("doctype")
    if not doctype or not frappe.has_permission(doctype, "read"):
        return []

    actions = []
    try:
        schema = schema_cache.get_doctype_schema(doctype)
        required_fields = [
            field
            for field in schema.get("fields", [])
            if field.get("reqd") and field.get("fieldname")
        ][:4]
        for field in required_fields:
            actions.append(
                {
                    "label": f"Check {field.get('label') or field.get('fieldname')}",
                    "target": field.get("fieldname"),
                    "type": "field",
                }
            )
    except Exception:
        pass

    if current_context.get("is_dirty"):
        actions.append({"label": "Save current document", "target": "primary-action", "type": "button"})
    elif (intent_result or {}).get("intent") in {"write_action", "docs_help", "doctype_schema"}:
        actions.append({"label": f"Review {doctype} form", "target": "form-layout", "type": "area"})

    return actions[:5]


def _looks_like_schema_question(message):
    message = message.lower()
    return any(
        token in message
        for token in (
            "doctype",
            "field",
            "fields",
            "form",
            "schema",
            "table",
            "what is on",
            "what's on",
        )
    )


def _find_doctype_mentions(message):
    try:
        names = frappe.get_all("DocType", pluck="name", limit_page_length=5000)
    except Exception:
        return []

    normalized_message = re.sub(r"\s+", " ", message).lower()
    matches = []
    for name in names:
        if name.lower() in normalized_message:
            matches.append(name)

    return sorted(matches, key=len, reverse=True)


def _extract_keywords(message):
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", message.lower())
    seen = set()
    keywords = []

    for word in words:
        if word in STOP_WORDS or word in seen:
            continue
        seen.add(word)
        keywords.append(word)

    return keywords


def _post_to_ollama(url, payload):
    headers = _get_ollama_headers()
    try:
        import requests

        response = requests.post(url, json=payload, headers=headers, timeout=(5, 120))
        response.raise_for_status()
        return response.json()
    except ImportError:
        return frappe.make_post_request(url, json=payload, headers=headers or None)


def _get_from_ollama(url):
    headers = _get_ollama_headers()
    try:
        import requests

        response = requests.get(url, headers=headers, timeout=(3, 20))
        response.raise_for_status()
        return response.json()
    except ImportError:
        return frappe.make_get_request(url, headers=headers or None)


def _send_ollama_chat(api_url, model_name, messages):
    payload = {
        "model": model_name,
        "messages": messages,
        "stream": False,
    }

    try:
        return _post_to_ollama(f"{api_url}/api/chat", payload)
    except Exception:
        generate_payload = {
            "model": model_name,
            "prompt": _messages_to_prompt(messages),
            "stream": False,
        }
        try:
            return _post_to_ollama(f"{api_url}/api/generate", generate_payload)
        except Exception as exc:
            frappe.log_error(frappe.get_traceback(), "Astra: Ollama Error")
            frappe.throw(_friendly_exception_message(exc))


def _stream_ollama_chat(api_url, model_name, messages, event, user, publish_tokens=True):
    try:
        import requests
    except ImportError:
        response = _send_ollama_chat(api_url, model_name, messages)
        answer = _extract_ollama_message(response)
        if publish_tokens:
            frappe.publish_realtime(event, {"type": "token", "token": answer}, user=user)
        return answer

    payload = {
        "model": model_name,
        "messages": messages,
        "stream": True,
    }
    answer_parts = []
    response = requests.post(
        f"{api_url}/api/chat",
        json=payload,
        headers=_get_ollama_headers(),
        timeout=(5, 180),
        stream=True,
    )
    response.raise_for_status()

    for line in response.iter_lines():
        if not line:
            continue
        data = json.loads(line.decode("utf-8"))
        token = ""
        if isinstance(data.get("message"), dict):
            token = data["message"].get("content") or ""
        elif data.get("response"):
            token = data.get("response") or ""

        if token:
            answer_parts.append(token)
            if publish_tokens:
                frappe.publish_realtime(event, {"type": "token", "token": token}, user=user)

        if data.get("done"):
            break

    return "".join(answer_parts).strip()


def _friendly_exception_message(exc):
    message = str(exc or "")
    lowered = message.lower()

    if "connection refused" in lowered or "failed to establish" in lowered:
        return "Ollama is not reachable. Start Ollama and check the API URL in Ollama Settings."
    if "404" in lowered or ("not found" in lowered and "model" in lowered):
        return "The selected Ollama model was not found. Pull it with Ollama or update Ollama Settings."
    if "permission" in lowered or "not permitted" in lowered:
        return "You do not have permission to access that Astra resource or ERPNext data."
    if "timed out" in lowered or "timeout" in lowered:
        return "Ollama took too long to respond. Try again or use a smaller local model."
    if "name or service not known" in lowered or "nodename nor servname" in lowered:
        return "Astra could not resolve the configured Ollama host. Use localhost or 127.0.0.1."

    return f"Astra could not complete the request: {message}"


def _extract_ollama_message(response):
    if not isinstance(response, dict):
        return ""

    if isinstance(response.get("message"), dict):
        return response["message"].get("content", "").strip()

    return (response.get("response") or "").strip()


def _clip(text, max_chars):
    text = text or ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 20].rstrip() + "\n...[truncated]"
