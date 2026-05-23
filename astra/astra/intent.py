import re


INTENT_DOCS = "docs_help"
INTENT_SCHEMA = "doctype_schema"
INTENT_DATA = "live_erp_data"
INTENT_REPORT = "report_analytics"
INTENT_WRITE = "write_action"
INTENT_ADMIN = "troubleshooting_admin"


def classify(message, current_context=None):
    text = (message or "").lower()
    context = current_context or {}

    if _contains_any(text, ("create ", "update ", "delete ", "submit ", "approve ", "cancel ", "change ")):
        intent = INTENT_WRITE
    elif _contains_any(text, ("report", "analytics", "summary", "trend", "total", "overdue", "balance")):
        intent = INTENT_REPORT
    elif _contains_any(text, ("field", "doctype", "schema", "form", "mandatory", "table")):
        intent = INTENT_SCHEMA
    elif _contains_any(text, ("error", "not working", "ollama", "fac", "permission", "setup", "install")):
        intent = INTENT_ADMIN
    elif _contains_any(text, ("show", "list", "find", "get", "which", "who", "status of")):
        intent = INTENT_DATA
    else:
        intent = INTENT_DOCS

    needs_followup = False
    followup_reason = ""
    if intent in {INTENT_REPORT, INTENT_DATA} and _is_underspecified(text):
        needs_followup = True
        followup_reason = "The request appears to need company, customer, date range, or document filters."

    return {
        "intent": intent,
        "needs_followup": needs_followup,
        "followup_reason": followup_reason,
        "current_doctype": context.get("doctype"),
        "current_docname": context.get("docname"),
        "guidance": _guidance(intent, needs_followup, followup_reason),
    }


def build_prompt(intent_result):
    if not intent_result:
        return ""

    lines = [
        f"Detected intent: {intent_result.get('intent')}.",
        intent_result.get("guidance") or "",
    ]

    if intent_result.get("current_doctype"):
        lines.append(
            "Current Desk document context: "
            f"{intent_result.get('current_doctype')} {intent_result.get('current_docname') or ''}".strip()
        )

    return "\n".join(line for line in lines if line)


def _contains_any(text, terms):
    return any(term in text for term in terms)


def _is_underspecified(text):
    has_filter = bool(
        re.search(r"\b(from|to|between|for|customer|supplier|company|warehouse|today|this month|last month|year)\b", text)
    )
    return not has_filter


def _guidance(intent, needs_followup, reason):
    if needs_followup:
        return (
            f"{reason} Ask a concise follow-up question before running reports or live data tools."
        )
    if intent == INTENT_WRITE:
        return "Treat this as a write/action request. Use confirmation-only tools and never execute without approval."
    if intent == INTENT_REPORT:
        return "Prefer report discovery, requirements, and report execution tools. Ask for missing filters."
    if intent == INTENT_DATA:
        return "Prefer permission-aware ERPNext data tools. Do not invent live business data."
    if intent == INTENT_SCHEMA:
        return "Prefer DocType metadata and schema context."
    if intent == INTENT_ADMIN:
        return "Prefer diagnostics and setup guidance."
    return "Prefer documentation RAG and concise ERPNext guidance."
