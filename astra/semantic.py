GLOSSARY = {
    "outstanding": "Amount still unpaid or not settled.",
    "posting date": "Accounting date used for ledger impact.",
    "gl entry": "General Ledger Entry created by accounting transactions.",
    "submitted": "Docstatus 1; finalized and usually ledger/stock impacting.",
    "reserved qty": "Stock quantity reserved for demand such as Sales Orders.",
    "valuation rate": "Inventory value per unit used for stock accounting.",
    "landed cost": "Additional import/freight costs allocated to received items.",
    "perpetual inventory": "Mode where stock transactions post accounting entries automatically.",
    "docstatus": "0 Draft, 1 Submitted, 2 Cancelled.",
}

FLOWS = {
    "Sales": ["Lead", "Opportunity", "Quotation", "Sales Order", "Delivery Note", "Sales Invoice", "Payment Entry"],
    "Buying": ["Material Request", "Supplier Quotation", "Purchase Order", "Purchase Receipt", "Purchase Invoice", "Payment Entry"],
    "Stock": ["Item", "Warehouse", "Stock Entry", "Stock Ledger Entry"],
    "Accounts": ["Sales Invoice", "Purchase Invoice", "Payment Entry", "Journal Entry", "GL Entry"],
    "Manufacturing": ["BOM", "Production Plan", "Work Order", "Job Card", "Stock Entry"],
}


def context_for_message(message):
    text = (message or "").lower()
    glossary_hits = {
        term: definition for term, definition in GLOSSARY.items() if term in text
    }
    flow_hits = {
        name: flow for name, flow in FLOWS.items() if name.lower() in text or any(dt.lower() in text for dt in flow)
    }
    lines = []
    if glossary_hits:
        lines.append("ERPNext glossary:")
        lines.extend(f"- {term}: {definition}" for term, definition in glossary_hits.items())
    if flow_hits:
        lines.append("ERPNext workflow relationships:")
        lines.extend(f"- {name}: " + " -> ".join(flow) for name, flow in flow_hits.items())
    return "\n".join(lines)
