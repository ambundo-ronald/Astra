import frappe
from frappe.model.document import Document


class AstraToolConfirmation(Document):
    def before_insert(self):
        if not self.user:
            self.user = frappe.session.user
        if not self.status:
            self.status = "Pending"

