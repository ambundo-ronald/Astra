import frappe
from frappe.model.document import Document


class AstraMessageFeedback(Document):
    def before_insert(self):
        if not self.user:
            self.user = frappe.session.user
