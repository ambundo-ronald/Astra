import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime


class AstraChatSession(Document):
    def before_insert(self):
        if not self.user:
            self.user = frappe.session.user
        if not self.status:
            self.status = "Open"
        if not self.last_message_at:
            self.last_message_at = now_datetime()

    def validate(self):
        if not self.title:
            self.title = "New chat"

