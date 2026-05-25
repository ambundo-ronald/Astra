import frappe


PACKS = {
    "Sales": "Sales flow: Lead, Opportunity, Quotation, Sales Order, Delivery Note, Sales Invoice, Payment Entry.",
    "Buying": "Buying flow: Material Request, Supplier Quotation, Purchase Order, Purchase Receipt, Purchase Invoice.",
    "Stock": "Stock flow: Item, Warehouse, Stock Entry, Delivery Note, Purchase Receipt, Serial and Batch tracking.",
    "Accounts": "Accounts flow: Chart of Accounts, Journal Entry, Payment Entry, Sales Invoice, Purchase Invoice, reports.",
    "HR": "HR flow: Employee, Leave Application, Attendance, Salary Structure, Payroll Entry.",
    "Manufacturing": "Manufacturing flow: BOM, Work Order, Job Card, Stock Entry, Production Plan.",
    "CRM": "CRM flow: Lead, Opportunity, Customer, Contact, Communication, follow-up activities.",
    "Projects": "Projects flow: Project, Task, Timesheet, Billing, milestones, project profitability.",
}


def seed_workflow_packs():
    created = []
    for category, content in PACKS.items():
        title = f"ERPNext {category} Workflow Pack"
        if frappe.db.exists("AI Knowledge Base", {"title": title}):
            continue
        doc = frappe.get_doc(
            {
                "doctype": "AI Knowledge Base",
                "title": title,
                "category": "Workflow Pack",
                "source_url": "",
                "content_chunk": content,
            }
        )
        doc.insert(ignore_permissions=True)
        created.append(doc.name)
    return {"created": len(created), "documents": created}
