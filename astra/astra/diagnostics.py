import frappe

from astra import fac_bridge, rag
from astra.api import DEFAULT_API_URL, DEFAULT_MODEL, _get_from_ollama, _get_settings


def run_smoke_test():
    checks = [
        _check_doctypes(),
        _check_roles(),
        _check_settings(),
        _check_ollama(),
        _check_fac(),
        _check_rag(),
        _check_realtime_hint(),
    ]
    ok = all(check.get("ok") for check in checks)
    return {"ok": ok, "checks": checks}


def _check_doctypes():
    required = [
        "Ollama Settings",
        "AI Knowledge Base",
        "Astra Chat Session",
        "Astra Chat Message",
        "Astra Tool Confirmation",
        "Astra User Preferences",
        "Astra Policy Version",
        "Astra Evaluation Case",
        "Astra Model Profile",
        "Astra Alert",
        "Astra Alert Rule",
        "Astra Tool Event",
        "Astra FAC Tool Contract",
        "Astra Attachment",
        "Astra Action History",
        "Astra Agent Plan",
        "Astra Entity Memory",
        "Astra FAC Tool Stat",
        "Astra Message Feedback",
    ]
    missing = [doctype for doctype in required if not frappe.db.exists("DocType", doctype)]
    return {
        "name": "doctypes",
        "ok": not missing,
        "message": "All Astra DocTypes are installed."
        if not missing
        else f"Missing DocTypes: {', '.join(missing)}",
    }


def _check_roles():
    missing = [role for role in ("Astra User", "Astra Admin") if not frappe.db.exists("Role", role)]
    return {
        "name": "roles",
        "ok": not missing,
        "message": "Astra roles exist." if not missing else f"Missing roles: {', '.join(missing)}",
    }


def _check_settings():
    try:
        settings = frappe.get_single("Ollama Settings")
        missing = [
            fieldname
            for fieldname in ("api_url", "model_name", "embedding_model")
            if not getattr(settings, fieldname, None)
        ]
        return {
            "name": "settings",
            "ok": not missing,
            "message": "Ollama Settings are configured."
            if not missing
            else f"Missing settings: {', '.join(missing)}",
        }
    except Exception as exc:
        return {"name": "settings", "ok": False, "message": str(exc)}


def _check_ollama():
    settings = _get_settings()
    api_url = (settings.get("api_url") or DEFAULT_API_URL).rstrip("/")
    model_name = settings.get("model_name") or DEFAULT_MODEL
    embedding_model = settings.get("embedding_model") or "nomic-embed-text"

    try:
        response = _get_from_ollama(f"{api_url}/api/tags")
        models = [item.get("name", "") for item in response.get("models", [])]
        chat_ok = _model_available(models, model_name)
        embed_ok = _model_available(models, embedding_model)
        return {
            "name": "ollama",
            "ok": chat_ok and embed_ok,
            "message": _ollama_message(chat_ok, embed_ok, model_name, embedding_model),
            "models": models,
        }
    except Exception as exc:
        return {
            "name": "ollama",
            "ok": False,
            "message": f"Ollama is not reachable at {api_url}: {exc}",
        }


def _check_fac():
    settings = _get_settings()
    enabled = bool(settings.get("enable_fac_tools"))
    installed = fac_bridge.is_fac_available()
    if not enabled:
        return {"name": "fac", "ok": True, "message": "FAC tools are disabled."}
    if not installed:
        return {
            "name": "fac",
            "ok": False,
            "message": "FAC tools are enabled but Frappe Assistant Core is not installed.",
        }

    allowlist = fac_bridge.parse_allowlist(
        settings.get("fac_tool_allowlist"),
        include_confirmation_tools=bool(settings.get("enable_write_action_confirmation")),
    )
    tools = fac_bridge.get_available_tools(
        allowlist,
        include_confirmation_tools=bool(settings.get("enable_write_action_confirmation")),
    )
    return {
        "name": "fac",
        "ok": bool(tools),
        "message": f"{len(tools)} FAC tools discovered."
        if tools
        else "FAC is installed, but no allowlisted tools were discovered.",
    }


def _check_rag():
    count = frappe.db.count("AI Knowledge Base") if frappe.db.exists("DocType", "AI Knowledge Base") else 0
    rows = frappe.db.get_list(
        "AI Knowledge Base",
        fields=["embedding_vector"],
        limit_page_length=5000,
        ignore_permissions=True,
    ) if count else []
    embedded = len([row for row in rows if row.get("embedding_vector")])
    settings = rag.get_rag_settings()
    return {
        "name": "rag",
        "ok": True,
        "message": f"{count} knowledge chunks, {embedded} with embeddings.",
        "vector_search": bool(settings.get("enable_vector_search")),
    }


def _check_realtime_hint():
    return {
        "name": "realtime",
        "ok": True,
        "message": "Realtime streaming requires a running bench socketio/realtime service.",
    }


def _model_available(models, model):
    return any(name == model or name.startswith(model + ":") for name in models)


def _ollama_message(chat_ok, embed_ok, model_name, embedding_model):
    missing = []
    if not chat_ok:
        missing.append(model_name)
    if not embed_ok:
        missing.append(embedding_model)
    if missing:
        return "Missing Ollama model(s): " + ", ".join(missing)
    return "Ollama is reachable and required models are available."
