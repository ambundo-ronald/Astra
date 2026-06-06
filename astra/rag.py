import hashlib
import json
import math
import re
from urllib.parse import urlparse

import frappe
from frappe import _
from frappe.utils import cint


DEFAULT_CHUNK_SIZE = 1800
DEFAULT_CHUNK_OVERLAP = 180
MAX_CHUNK_SIZE = 5000
DEFAULT_RAG_LIMIT = 5
ALLOWED_DOC_HOSTS = {
    "docs.frappe.io",
    "frappeframework.com",
    "docs.erpnext.com",
    "erpnext.com",
}


@frappe.whitelist()
def ingest_knowledge_base_url(
    url,
    category=None,
    title=None,
    source_group=None,
    doc_version=None,
    chunk_size=DEFAULT_CHUNK_SIZE,
    chunk_overlap=DEFAULT_CHUNK_OVERLAP,
    rebuild_embeddings=1,
):
    _require_system_manager()
    url = _validate_source_url(url)
    text = _fetch_url_text(url)
    title = title or _title_from_text(text) or url
    return ingest_knowledge_base_text(
        title=title,
        content=text,
        category=category or _category_from_url(url),
        source_url=url,
        source_group=source_group or _source_group_from_url(url),
        doc_version=doc_version,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        rebuild_embeddings=rebuild_embeddings,
    )


@frappe.whitelist()
def ingest_knowledge_base_text(
    title,
    content,
    category=None,
    source_url=None,
    source_group=None,
    doc_version=None,
    chunk_size=DEFAULT_CHUNK_SIZE,
    chunk_overlap=DEFAULT_CHUNK_OVERLAP,
    rebuild_embeddings=1,
):
    _require_system_manager()
    title = (title or "Documentation").strip()
    content = _clean_text(content or "")
    if not content:
        frappe.throw(_("No content was provided for ingestion."))

    chunk_size = min(max(cint(chunk_size) or DEFAULT_CHUNK_SIZE, 500), MAX_CHUNK_SIZE)
    chunk_overlap = min(max(cint(chunk_overlap) or DEFAULT_CHUNK_OVERLAP, 0), chunk_size - 1)
    chunks = chunk_text(content, chunk_size=chunk_size, overlap=chunk_overlap)
    settings = get_rag_settings()
    created = []

    for index, chunk in enumerate(chunks, start=1):
        chunk_title = f"{title} - {index}" if len(chunks) > 1 else title
        doc = frappe.get_doc(
            {
                "doctype": "AI Knowledge Base",
                "title": chunk_title,
                "category": category,
                "source_url": source_url,
                "source_group": source_group or _source_group_from_url(source_url),
                "doc_version": doc_version,
                "quality_score": _chunk_quality_score(chunk),
                "stale": 0,
                "content_chunk": chunk,
            }
        )

        if cint(rebuild_embeddings) and cint(settings.get("enable_vector_search")):
            vector = get_embedding(chunk, settings)
            doc.embedding_model = settings.get("embedding_model")
            doc.embedding_vector = _json_dumps(vector)

        doc.insert(ignore_permissions=True)
        created.append(doc.name)

    return {
        "created": len(created),
        "documents": created,
        "source_url": source_url,
    }


@frappe.whitelist()
def rebuild_knowledge_base_embeddings(limit=None):
    _require_system_manager()
    settings = get_rag_settings()
    limit = cint(limit) or 0
    rows = frappe.db.get_list(
        "AI Knowledge Base",
        fields=["name", "content_chunk", "content_hash"],
        order_by="modified desc",
        limit_page_length=limit or 5000,
        ignore_permissions=True,
    )
    updated = 0

    for row in rows:
        vector = get_embedding(row.content_chunk or "", settings)
        frappe.db.set_value(
            "AI Knowledge Base",
            row.name,
            {
                "embedding_model": settings.get("embedding_model"),
                "embedding_vector": _json_dumps(vector),
                "content_hash": hashlib.sha256((row.content_chunk or "").encode("utf-8")).hexdigest(),
                "quality_score": _chunk_quality_score(row.content_chunk or ""),
                "stale": 0,
            },
            update_modified=False,
        )
        updated += 1

    return {"updated": updated, "embedding_model": settings.get("embedding_model")}


@frappe.whitelist()
def mark_knowledge_base_stale(source_url=None, source_group=None):
    _require_system_manager()
    filters = {}
    if source_url:
        filters["source_url"] = source_url
    if source_group:
        filters["source_group"] = source_group
    if not filters:
        frappe.throw(_("Provide a source URL or source group to mark stale."))

    rows = frappe.db.get_list(
        "AI Knowledge Base",
        fields=["name"],
        filters=filters,
        ignore_permissions=True,
        limit_page_length=5000,
    )
    for row in rows:
        frappe.db.set_value("AI Knowledge Base", row.name, "stale", 1, update_modified=False)
    return {"stale": len(rows)}


def search_knowledge_base(query, settings=None, limit=None):
    if not query or not frappe.has_permission("AI Knowledge Base", "read"):
        return []

    settings = settings or get_rag_settings()
    limit = cint(limit) or cint(settings.get("rag_result_limit")) or DEFAULT_RAG_LIMIT

    if cint(settings.get("enable_vector_search")):
        try:
            results = _vector_search(query, settings, limit)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Astra RAG Vector Search Error")
            results = []
        if results:
            return results

    return keyword_search(query, limit=limit)


def keyword_search(query, limit=DEFAULT_RAG_LIMIT):
    keywords = extract_keywords(query)
    if not keywords:
        return []

    or_filters = []
    for term in keywords[:8]:
        pattern = f"%{term}%"
        or_filters.extend(
            [
                ["AI Knowledge Base", "title", "like", pattern],
                ["AI Knowledge Base", "category", "like", pattern],
                ["AI Knowledge Base", "content_chunk", "like", pattern],
            ]
        )

    rows = frappe.db.get_list(
        "AI Knowledge Base",
        fields=["name", "title", "category", "content_chunk", "source_url"],
        or_filters=or_filters,
        filters={"stale": 0},
        order_by="modified desc",
        limit_page_length=limit,
    )

    return [_row_to_result(row, score=None, method="keyword") for row in rows]


def chunk_text(text, chunk_size=DEFAULT_CHUNK_SIZE, overlap=DEFAULT_CHUNK_OVERLAP):
    text = _clean_text(text)
    if len(text) <= chunk_size:
        return [text] if text else []

    chunks = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        if end < len(text):
            boundary = max(text.rfind("\n\n", start, end), text.rfind(". ", start, end))
            if boundary > start + int(chunk_size * 0.55):
                end = boundary + 1

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        if end >= len(text):
            break
        start = max(end - overlap, start + 1)

    return chunks


def get_embedding(text, settings=None):
    settings = settings or get_rag_settings()
    api_url = _prepare_ollama_connection(settings)
    model = settings.get("embedding_model") or "nomic-embed-text"
    payload = {"model": model, "input": text}

    try:
        response = _post_to_ollama(f"{api_url}/api/embed", payload)
        embeddings = response.get("embeddings")
        if embeddings and isinstance(embeddings, list):
            return embeddings[0]
    except Exception:
        pass

    response = _post_to_ollama(
        f"{api_url}/api/embeddings",
        {"model": model, "prompt": text},
    )
    embedding = response.get("embedding")
    if not embedding:
        frappe.throw(_("Ollama did not return an embedding."))
    return embedding


def _safe_get_password(doc, fieldname):
    if not hasattr(doc, "get_password") or not getattr(doc, fieldname, None):
        return ""
    try:
        return doc.get_password(fieldname, raise_exception=False) or ""
    except TypeError:
        try:
            return doc.get_password(fieldname) or ""
        except Exception:
            return ""
    except Exception:
        return ""


def get_rag_settings():
    try:
        settings = frappe.get_single("Ollama Settings")
        return {
            "provider": getattr(settings, "provider", None) or "Local Ollama",
            "api_url": (settings.api_url or "http://localhost:11434").rstrip("/"),
            "auth_type": getattr(settings, "auth_type", None) or "",
            "api_key": _safe_get_password(settings, "api_key"),
            "cf_access_client_id": getattr(settings, "cf_access_client_id", None) or "",
            "cf_access_client_secret": _safe_get_password(settings, "cf_access_client_secret"),
            "allow_remote_business_context": cint(getattr(settings, "allow_remote_business_context", 0)),
            "embedding_model": getattr(settings, "embedding_model", None) or "nomic-embed-text",
            "enable_vector_search": cint(getattr(settings, "enable_vector_search", 1)),
            "rag_result_limit": cint(getattr(settings, "rag_result_limit", 0)) or DEFAULT_RAG_LIMIT,
        }
    except Exception:
        return {
            "provider": "Local Ollama",
            "api_url": "http://localhost:11434",
            "auth_type": "None",
            "api_key": "",
            "cf_access_client_id": "",
            "cf_access_client_secret": "",
            "allow_remote_business_context": 0,
            "embedding_model": "nomic-embed-text",
            "enable_vector_search": 1,
            "rag_result_limit": DEFAULT_RAG_LIMIT,
        }


def extract_keywords(message):
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", (message or "").lower())
    stop_words = {
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
    seen = set()
    keywords = []

    for word in words:
        if word in stop_words or word in seen:
            continue
        seen.add(word)
        keywords.append(word)

    return keywords


def _vector_search(query, settings, limit):
    query_vector = get_embedding(query, settings)
    rows = frappe.db.get_list(
        "AI Knowledge Base",
        fields=[
            "name",
            "title",
            "category",
            "content_chunk",
            "source_url",
            "stale",
            "embedding_model",
            "embedding_vector",
        ],
        filters={"embedding_model": settings.get("embedding_model")},
        limit_page_length=1000,
    )

    scored = []
    for row in rows:
        if cint(row.get("stale")):
            continue
        vector = _json_loads(row.get("embedding_vector"), None)
        if not vector:
            continue
        score = cosine_similarity(query_vector, vector)
        if score > 0:
            scored.append(_row_to_result(row, score=score, method="vector"))

    scored.sort(key=lambda item: item.get("score") or 0, reverse=True)
    return scored[:limit]


def cosine_similarity(left, right):
    if not left or not right or len(left) != len(right):
        return 0

    dot = sum(float(a) * float(b) for a, b in zip(left, right))
    left_norm = math.sqrt(sum(float(a) * float(a) for a in left))
    right_norm = math.sqrt(sum(float(b) * float(b) for b in right))
    if not left_norm or not right_norm:
        return 0
    return dot / (left_norm * right_norm)


def _row_to_result(row, score=None, method="keyword"):
    return {
        "name": row.get("name"),
        "title": row.get("title") or "Documentation",
        "category": row.get("category") or "General",
        "content_chunk": row.get("content_chunk") or "",
        "source_url": row.get("source_url"),
        "score": score,
        "method": method,
    }


def _fetch_url_text(url):
    try:
        import requests

        response = requests.get(url, timeout=(5, 60))
        response.raise_for_status()
        return response.text
    except ImportError:
        return frappe.make_get_request(url)


def _post_to_ollama(url, payload):
    headers = getattr(frappe.flags, "astra_ollama_headers", {}) or {}
    try:
        import requests

        response = requests.post(url, json=payload, headers=headers, timeout=(5, 120))
        response.raise_for_status()
        return response.json()
    except ImportError:
        return frappe.make_post_request(url, json=payload, headers=headers or None)


def _validate_source_url(url):
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in ALLOWED_DOC_HOSTS:
        frappe.throw(_("Only official Frappe and ERPNext documentation URLs are allowed."))
    return parsed.geturl()


def _prepare_ollama_connection(settings):
    api_url = (settings.get("api_url") or "http://localhost:11434").rstrip("/")
    provider = settings.get("provider") or "Local Ollama"
    parsed = urlparse(api_url)
    host = (parsed.hostname or "").lower()
    if provider == "Local Ollama" and (parsed.scheme not in {"http", "https"} or host not in {"localhost", "127.0.0.1", "::1"}):
        frappe.throw(_("Ollama API URL must point to localhost or 127.0.0.1."))
    if provider == "Remote Ollama":
        if parsed.scheme != "https" or not host:
            frappe.throw(_("Remote Ollama API URL must be a valid HTTPS endpoint."))
        if not cint(settings.get("allow_remote_business_context")):
            frappe.throw(_("Remote Ollama RAG embeddings require Allow Remote Business Context in Ollama Settings."))
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


def _require_system_manager():
    if "System Manager" not in frappe.get_roles():
        frappe.throw(_("Only System Managers can manage Astra knowledge ingestion."))


def _clean_text(text):
    text = re.sub(r"<script[\s\S]*?</script>", " ", text or "", flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _title_from_text(text):
    match = re.search(r"^\s*#\s+(.+)$", text or "", flags=re.MULTILINE)
    if match:
        return match.group(1).strip()
    return None


def _category_from_url(url):
    path = urlparse(url).path.strip("/")
    if not path:
        return "Documentation"
    return path.split("/")[0].replace("-", " ").title()


def _source_group_from_url(url):
    if not url:
        return None
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    if not path:
        return parsed.hostname
    return "/".join(path.split("/")[:2])


def _chunk_quality_score(chunk):
    text = (chunk or "").strip()
    if not text:
        return 0
    words = re.findall(r"\w+", text)
    headings = len(re.findall(r"^\s{0,3}#{1,4}\s+", text, flags=re.MULTILINE))
    links = len(re.findall(r"https?://", text))
    length_score = min(len(words) / 220, 1.0)
    structure_score = min((headings + links) / 4, 1.0)
    noise_penalty = 0.25 if len(words) < 40 or text.count("{") > 8 else 0
    return round(max(0, min(1, (length_score * 0.7) + (structure_score * 0.3) - noise_penalty)), 3)


def _json_dumps(value):
    return json.dumps(value, default=str, ensure_ascii=False, separators=(",", ":"))


def _json_loads(value, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except ValueError:
        return fallback
