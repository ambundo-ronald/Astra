import frappe
from frappe.model.document import Document


class AstraUserPreferences(Document):
    def before_insert(self):
        if not self.user:
            self.user = frappe.session.user

