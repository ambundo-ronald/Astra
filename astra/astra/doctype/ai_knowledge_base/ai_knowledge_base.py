import hashlib

from frappe.model.document import Document


class AIKnowledgeBase(Document):
    def validate(self):
        content = self.content_chunk or ""
        self.content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
