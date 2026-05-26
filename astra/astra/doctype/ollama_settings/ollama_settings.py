import frappe
from frappe import _
from frappe.model.document import Document
from urllib.parse import urlparse


class OllamaSettings(Document):
    def validate(self):
        if not self.provider:
            self.provider = "Local Ollama"
        if not self.api_url:
            self.api_url = "http://localhost:11434"
        if not self.model_name:
            self.model_name = "llama3"
        if not self.embedding_model:
            self.embedding_model = "nomic-embed-text"
        if not getattr(self, "auth_type", None):
            self.auth_type = "Bearer Token" if getattr(self, "api_key", None) else "None"

        self.api_url = self.api_url.rstrip("/")
        parsed = urlparse(self.api_url)
        host = (parsed.hostname or "").lower()
        if self.provider == "Local Ollama":
            if parsed.scheme not in {"http", "https"} or host not in {"localhost", "127.0.0.1", "::1"}:
                frappe.throw(_("Local Ollama provider must use localhost, 127.0.0.1, or ::1."))
        elif self.provider == "Remote Ollama":
            if parsed.scheme != "https":
                frappe.throw(_("Remote Ollama provider must use HTTPS. Put Ollama behind a secure reverse proxy or tunnel."))
            if self.auth_type == "Cloudflare Access Service Token" and (
                not getattr(self, "cf_access_client_id", None)
                or not getattr(self, "cf_access_client_secret", None)
            ):
                frappe.throw(_("Cloudflare Access service token authentication requires a client ID and client secret."))
        else:
            frappe.throw(_("Unsupported Ollama provider."))

        if not self.system_prompt:
            self.system_prompt = (
                "You are a helpful ERPNext and Frappe assistant. Answer using the "
                "provided context when available, and do not claim access to records "
                "or data that were not explicitly provided."
            )

        if not self.fac_tool_allowlist:
            self.fac_tool_allowlist = "\n".join(
                [
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
                ]
            )

        if not self.max_fac_tool_calls or self.max_fac_tool_calls < 1:
            self.max_fac_tool_calls = 4
        if not self.rag_result_limit or self.rag_result_limit < 1:
            self.rag_result_limit = 5
