import frappe
from frappe.model.document import Document


class AstraChatMessage(Document):
    def validate(self):
        if self.session and not self.user:
            self.user = frappe.db.get_value("Astra Chat Session", self.session, "user")

