import frappe
from frappe.model.document import Document


class OllamaSettings(Document):
    def validate(self):
        if not self.api_url:
            self.api_url = "http://localhost:11434"
        if not self.model_name:
            self.model_name = "llama3"
        if not self.embedding_model:
            self.embedding_model = "nomic-embed-text"

        self.api_url = self.api_url.rstrip("/")

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
