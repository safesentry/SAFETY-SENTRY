#!/usr/bin/env python3
import json
import os
import base64
import sys
import traceback

import frappe
from frappe.desk.page.setup_wizard.setup_wizard import setup_complete


def _parse_args():
    if len(sys.argv) < 3:
        raise SystemExit("usage: erpnext_site_ops.py <site> <action> [payload_json]")
    site = sys.argv[1]
    action = sys.argv[2]
    payload = {}
    payload_file = os.environ.get("PIPELINE_JSON_PAYLOAD_FILE", "")
    if payload_file and os.path.isfile(payload_file):
        with open(payload_file, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return site, action, payload
    payload_b64 = os.environ.get("PIPELINE_JSON_PAYLOAD_B64", "")
    if payload_b64:
        payload = json.loads(base64.b64decode(payload_b64).decode("utf-8"))
    elif len(sys.argv) > 3 and sys.argv[3]:
        payload = json.loads(sys.argv[3])
    return site, action, payload


def _json_print(payload):
    print(json.dumps(payload, ensure_ascii=False))


def _invoice_comment_count(invoice_name):
    return frappe.db.count(
        "Comment",
        {
            "reference_doctype": "Sales Invoice",
            "reference_name": invoice_name,
        },
    )


def _invoice_payment_names(invoice_name):
    rows = frappe.get_all(
        "Payment Entry Reference",
        filters={
            "reference_doctype": "Sales Invoice",
            "reference_name": invoice_name,
        },
        fields=["parent"],
        order_by="parent asc",
    )
    return [row["parent"] for row in rows if row.get("parent")]


def _invoice_payment_count(invoice_name):
    return len(_invoice_payment_names(invoice_name))


def _purchase_invoice_comment_count(invoice_name):
    return frappe.db.count(
        "Comment",
        {
            "reference_doctype": "Purchase Invoice",
            "reference_name": invoice_name,
        },
    )


def _payment_references(doc):
    references = []
    for ref in getattr(doc, "references", []) or []:
        references.append(
            {
                "reference_doctype": ref.reference_doctype,
                "reference_name": ref.reference_name,
                "allocated_amount": float(ref.allocated_amount or 0),
                "total_amount": float(ref.total_amount or 0),
                "outstanding_amount": float(ref.outstanding_amount or 0),
            }
        )
    return references


def _invoice_summary(doc):
    return {
        "name": doc.name,
        "customer": doc.customer,
        "status": doc.status,
        "posting_date": str(doc.posting_date or ""),
        "due_date": str(doc.due_date or ""),
        "currency": doc.currency or "",
        "outstanding_amount": float(doc.outstanding_amount or 0),
        "grand_total": float(doc.grand_total or 0),
        "comment_count": _invoice_comment_count(doc.name),
        "payment_count": _invoice_payment_count(doc.name),
    }


def _payment_summary(doc):
    return {
        "name": doc.name,
        "party": doc.party,
        "status": doc.status,
        "posting_date": str(doc.posting_date or ""),
        "mode_of_payment": doc.mode_of_payment or "",
        "paid_amount": float(doc.paid_amount or 0),
        "reference_no": doc.reference_no,
        "references": _payment_references(doc),
    }


def _purchase_invoice_summary(doc):
    return {
        "name": doc.name,
        "supplier": doc.supplier,
        "status": doc.status,
        "posting_date": str(doc.posting_date or ""),
        "due_date": str(doc.due_date or ""),
        "currency": doc.currency or "",
        "outstanding_amount": float(doc.outstanding_amount or 0),
        "grand_total": float(doc.grand_total or 0),
        "comment_count": _purchase_invoice_comment_count(doc.name),
    }


def _customer_summary(doc, include_invoices=False):
    invoices = frappe.get_all(
        "Sales Invoice",
        filters={"customer": doc.name},
        fields=["name", "status", "grand_total", "outstanding_amount"],
        order_by="name asc",
    )
    payload = {
        "name": doc.name,
        "customer_name": doc.customer_name,
        "customer_type": doc.customer_type or "",
        "customer_group": doc.customer_group or "",
        "territory": doc.territory or "",
        "invoice_count": len(invoices),
        "outstanding_total": float(sum(float(inv.get("outstanding_amount") or 0) for inv in invoices)),
    }
    if include_invoices:
        payload["invoices"] = [
            {
                "name": inv["name"],
                "status": inv.get("status", ""),
                "grand_total": float(inv.get("grand_total") or 0),
                "outstanding_amount": float(inv.get("outstanding_amount") or 0),
            }
            for inv in invoices
        ]
    return payload


def _supplier_summary(doc, include_purchase_invoices=False):
    invoices = frappe.get_all(
        "Purchase Invoice",
        filters={"supplier": doc.name},
        fields=["name", "status", "grand_total", "outstanding_amount"],
        order_by="name asc",
    )
    payload = {
        "name": doc.name,
        "supplier_name": doc.supplier_name,
        "supplier_type": doc.supplier_type or "",
        "supplier_group": doc.supplier_group or "",
        "purchase_invoice_count": len(invoices),
        "outstanding_total": float(sum(float(inv.get("outstanding_amount") or 0) for inv in invoices)),
    }
    if include_purchase_invoices:
        payload["purchase_invoices"] = [
            {
                "name": inv["name"],
                "status": inv.get("status", ""),
                "grand_total": float(inv.get("grand_total") or 0),
                "outstanding_amount": float(inv.get("outstanding_amount") or 0),
            }
            for inv in invoices
        ]
    return payload


def _item_usage_count(item_code):
    return frappe.db.count("Sales Invoice Item", {"item_code": item_code})


def _item_summary(doc):
    return {
        "item_code": doc.item_code,
        "item_name": doc.item_name,
        "item_group": doc.item_group or "",
        "stock_uom": doc.stock_uom or "",
        "disabled": bool(doc.disabled),
        "invoice_usage_count": _item_usage_count(doc.item_code),
    }


def _company_summary(doc):
    return {
        "name": doc.name,
        "abbr": doc.abbr or "",
        "default_currency": doc.default_currency or "",
        "country": doc.country or "",
    }


def action_bootstrap(payload):
    if frappe.db.exists("Company", payload["company_name"]):
        return {"bootstrapped": False, "company_name": payload["company_name"]}

    args = {
        "language": payload.get("language", "English"),
        "country": payload.get("country", "United States"),
        "timezone": payload.get("timezone", "UTC"),
        "currency": payload.get("currency", "USD"),
        "full_name": payload.get("full_name", "Pipeline Admin"),
        "email": payload.get("email", "admin@example.com"),
        "password": payload.get("password", "admin"),
        "domain": payload.get("domain", "Services"),
        "company_name": payload["company_name"],
        "company_abbr": payload["company_abbr"],
        "company_tagline": payload.get("company_tagline", payload["company_name"]),
        "fy_start_date": payload.get("fy_start_date", "2026-01-01"),
        "fy_end_date": payload.get("fy_end_date", "2026-12-31"),
        "setup_website": 0,
        "add_sample_data": 0,
    }
    setup_complete(args)
    frappe.clear_cache()
    return {"bootstrapped": True, "company_name": payload["company_name"]}


def _normalize_customer_entry(customer_entry):
    if isinstance(customer_entry, str):
        return {
            "customer_name": customer_entry,
            "customer_type": "Company",
            "customer_group": "All Customer Groups",
            "territory": "All Territories",
        }
    return {
        "customer_name": customer_entry["customer_name"],
        "customer_type": customer_entry.get("customer_type", "Company"),
        "customer_group": customer_entry.get("customer_group", "All Customer Groups"),
        "territory": customer_entry.get("territory", "All Territories"),
    }


def _ensure_customer(customer_entry):
    payload = _normalize_customer_entry(customer_entry)
    customer_name = payload["customer_name"]
    if frappe.db.exists("Customer", customer_name):
        return frappe.get_doc("Customer", customer_name)
    doc = frappe.get_doc(
        {
            "doctype": "Customer",
            "customer_name": customer_name,
            "customer_type": payload["customer_type"],
            "customer_group": payload["customer_group"],
            "territory": payload["territory"],
        }
    )
    doc.insert(ignore_permissions=True)
    return doc


def _normalize_supplier_entry(supplier_entry):
    if isinstance(supplier_entry, str):
        return {
            "supplier_name": supplier_entry,
            "supplier_type": "Company",
            "supplier_group": "All Supplier Groups",
        }
    return {
        "supplier_name": supplier_entry["supplier_name"],
        "supplier_type": supplier_entry.get("supplier_type", "Company"),
        "supplier_group": supplier_entry.get("supplier_group", "All Supplier Groups"),
    }


def _ensure_supplier(supplier_entry):
    payload = _normalize_supplier_entry(supplier_entry)
    supplier_name = payload["supplier_name"]
    if frappe.db.exists("Supplier", supplier_name):
        return frappe.get_doc("Supplier", supplier_name)
    doc = frappe.get_doc(
        {
            "doctype": "Supplier",
            "supplier_name": supplier_name,
            "supplier_type": payload["supplier_type"],
            "supplier_group": payload["supplier_group"],
        }
    )
    doc.insert(ignore_permissions=True)
    return doc


def _normalize_item_entry(item_entry):
    return {
        "item_code": item_entry["item_code"],
        "item_name": item_entry.get("item_name", item_entry["item_code"]),
        "item_group": item_entry.get("item_group", "All Item Groups"),
        "stock_uom": item_entry.get("stock_uom", "Nos"),
        "is_stock_item": int(item_entry.get("is_stock_item", 0)),
    }


def _ensure_item(item_entry, item_name=None):
    if isinstance(item_entry, str):
        payload = _normalize_item_entry({"item_code": item_entry, "item_name": item_name or item_entry})
    else:
        payload = _normalize_item_entry(item_entry)
    item_code = payload["item_code"]
    if frappe.db.exists("Item", item_code):
        return frappe.get_doc("Item", item_code)
    doc = frappe.get_doc(
        {
            "doctype": "Item",
            "item_code": payload["item_code"],
            "item_name": payload["item_name"],
            "item_group": payload["item_group"],
            "stock_uom": payload["stock_uom"],
            "is_stock_item": payload["is_stock_item"],
        }
    )
    doc.insert(ignore_permissions=True)
    return doc


def _ensure_sales_invoice(entry, company_name):
    if frappe.db.exists("Sales Invoice", entry["name"]):
        doc = frappe.get_doc("Sales Invoice", entry["name"])
        if doc.docstatus == 0:
            doc.submit()
        return doc

    invoice_items = entry.get("items") or [
        {
            "item_code": entry["item_code"],
            "item_name": entry.get("item_name", entry["item_code"]),
            "qty": entry.get("qty", 1),
            "rate": float(entry["amount"]),
        }
    ]

    doc = frappe.get_doc(
        {
            "doctype": "Sales Invoice",
            "naming_series": "ACC-SINV-.YYYY.-.####",
            "customer": entry["customer"],
            "company": company_name,
            "posting_date": entry["posting_date"],
            "due_date": entry["due_date"],
            "set_posting_time": 1,
            "items": [
                {
                    "item_code": item["item_code"],
                    "qty": item.get("qty", 1),
                    "rate": float(item.get("rate", 0)),
                }
                for item in invoice_items
            ],
        }
    )
    doc.insert(ignore_permissions=True)
    doc.submit()
    doc.reload()
    return doc


def _ensure_purchase_invoice(entry, company_name):
    if frappe.db.exists("Purchase Invoice", entry["name"]):
        doc = frappe.get_doc("Purchase Invoice", entry["name"])
        if doc.docstatus == 0:
            doc.submit()
        return doc

    invoice_items = entry.get("items") or [
        {
            "item_code": entry["item_code"],
            "item_name": entry.get("item_name", entry["item_code"]),
            "qty": entry.get("qty", 1),
            "rate": float(entry["amount"]),
        }
    ]

    doc = frappe.get_doc(
        {
            "doctype": "Purchase Invoice",
            "naming_series": "ACC-PINV-.YYYY.-.####",
            "supplier": entry["supplier"],
            "company": company_name,
            "posting_date": entry["posting_date"],
            "due_date": entry["due_date"],
            "set_posting_time": 1,
            "items": [
                {
                    "item_code": item["item_code"],
                    "qty": item.get("qty", 1),
                    "rate": float(item.get("rate", 0)),
                }
                for item in invoice_items
            ],
        }
    )
    doc.insert(ignore_permissions=True)
    doc.submit()
    doc.reload()
    return doc


def _ensure_invoice_comment(invoice_name, comment, author):
    existing = frappe.get_all(
        "Comment",
        filters={
            "reference_doctype": "Sales Invoice",
            "reference_name": invoice_name,
            "content": comment,
        },
        fields=["name", "comment_email", "owner", "content"],
        limit=1,
    )
    if existing:
        return existing[0]
    doc = frappe.get_doc("Sales Invoice", invoice_name)
    comment_doc = doc.add_comment("Comment", comment)
    if author:
        comment_doc.comment_email = author
        comment_doc.save(ignore_permissions=True)
    return {
        "name": comment_doc.name,
        "comment_email": comment_doc.comment_email,
        "content": comment_doc.content,
    }


def _ensure_purchase_invoice_comment(invoice_name, comment, author):
    existing = frappe.get_all(
        "Comment",
        filters={
            "reference_doctype": "Purchase Invoice",
            "reference_name": invoice_name,
            "content": comment,
        },
        fields=["name", "comment_email", "owner", "content"],
        limit=1,
    )
    if existing:
        return existing[0]
    doc = frappe.get_doc("Purchase Invoice", invoice_name)
    comment_doc = doc.add_comment("Comment", comment)
    if author:
        comment_doc.comment_email = author
        comment_doc.save(ignore_permissions=True)
    return {
        "name": comment_doc.name,
        "comment_email": comment_doc.comment_email,
        "content": comment_doc.content,
    }


def _ensure_payment_entry(entry):
    if frappe.db.exists("Payment Entry", entry["name"]):
        doc = frappe.get_doc("Payment Entry", entry["name"])
        if doc.docstatus == 0:
            doc.submit()
            doc.reload()
        return doc

    from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

    payment_doc = get_payment_entry("Sales Invoice", entry["invoice_name"])
    payment_doc.naming_series = "ACC-PAY-.YYYY.-.####"
    payment_doc.reference_no = entry.get("reference_no") or entry["name"]
    payment_doc.reference_date = entry.get("reference_date") or entry["posting_date"]
    payment_doc.posting_date = entry["posting_date"]
    payment_doc.mode_of_payment = entry.get("mode_of_payment", payment_doc.mode_of_payment or "Cash")
    payment_doc.paid_amount = float(entry["amount"])
    payment_doc.received_amount = float(entry["amount"])
    remaining = float(entry["amount"])
    for ref in payment_doc.references or []:
        alloc = min(remaining, float(ref.allocated_amount or 0))
        ref.allocated_amount = alloc
        remaining -= alloc
        if remaining <= 0:
            remaining = 0
    payment_doc.insert(ignore_permissions=True)
    payment_doc.submit()
    payment_doc.reload()
    return payment_doc


def action_seed(payload):
    company_name = payload["bootstrap"]["company_name"]
    customers = []
    for customer_entry in payload.get("customers", []):
        customers.append(_customer_summary(_ensure_customer(customer_entry)))

    suppliers = []
    for supplier_entry in payload.get("suppliers", []):
        suppliers.append(_supplier_summary(_ensure_supplier(supplier_entry)))

    items = []
    for item in payload.get("items", []):
        items.append(_item_summary(_ensure_item(item)))

    invoices = []
    for invoice_entry in payload.get("invoices", []):
        _ensure_customer(invoice_entry["customer"])
        for item in invoice_entry.get("items") or [{"item_code": invoice_entry["item_code"], "item_name": invoice_entry.get("item_name", "")}]:
            _ensure_item(item)
        invoice_doc = _ensure_sales_invoice(invoice_entry, company_name)
        for comment_entry in invoice_entry.get("comments", []):
            _ensure_invoice_comment(invoice_doc.name, comment_entry["content"], comment_entry.get("author", ""))
        invoices.append(_invoice_summary(frappe.get_doc("Sales Invoice", invoice_doc.name)))

    purchase_invoices = []
    for invoice_entry in payload.get("purchase_invoices", []):
        _ensure_supplier(invoice_entry["supplier"])
        for item in invoice_entry.get("items") or [{"item_code": invoice_entry["item_code"], "item_name": invoice_entry.get("item_name", "")}]:
            _ensure_item(item)
        invoice_doc = _ensure_purchase_invoice(invoice_entry, company_name)
        for comment_entry in invoice_entry.get("comments", []):
            _ensure_purchase_invoice_comment(invoice_doc.name, comment_entry["content"], comment_entry.get("author", ""))
        purchase_invoices.append(_purchase_invoice_summary(frappe.get_doc("Purchase Invoice", invoice_doc.name)))

    payments = []
    for payment_entry in payload.get("payments", []):
        payment_doc = _ensure_payment_entry(payment_entry)
        payments.append(_payment_summary(payment_doc))

    return {
        "customers": customers,
        "suppliers": suppliers,
        "items": items,
        "invoices": invoices,
        "purchase_invoices": purchase_invoices,
        "payments": payments,
    }


def action_list_invoices(payload):
    filters = {}
    if payload.get("customer"):
        filters["customer"] = payload["customer"]
    docs = frappe.get_all(
        "Sales Invoice",
        filters=filters,
        fields=["name"],
        order_by="name asc",
    )
    requested_status = payload.get("status")
    results = []
    for row in docs:
        doc = frappe.get_doc("Sales Invoice", row["name"])
        if requested_status and doc.status != requested_status:
            continue
        results.append(_invoice_summary(doc))
    return results


def action_get_invoice(payload):
    name = payload["invoice_name"]
    if not frappe.db.exists("Sales Invoice", name):
        return None
    doc = frappe.get_doc("Sales Invoice", name)
    comments = frappe.get_all(
        "Comment",
        filters={
            "reference_doctype": "Sales Invoice",
            "reference_name": name,
        },
        fields=["name", "comment_email", "content", "creation"],
        order_by="creation asc",
    )
    data = _invoice_summary(doc)
    data["items"] = [
        {
            "item_code": item.item_code,
            "item_name": item.item_name,
            "qty": float(item.qty or 0),
            "rate": float(item.rate or 0),
            "amount": float(item.amount or 0),
        }
        for item in getattr(doc, "items", []) or []
    ]
    data["payments"] = [
        _payment_summary(frappe.get_doc("Payment Entry", payment_name))
        for payment_name in _invoice_payment_names(name)
        if frappe.db.exists("Payment Entry", payment_name)
    ]
    data["comments"] = [
        {
            "id": item["name"],
            "author": item.get("comment_email") or "",
            "comment": item.get("content") or "",
            "creation": str(item.get("creation") or ""),
        }
        for item in comments
    ]
    return data


def action_list_payments(payload):
    filters = {}
    if payload.get("status"):
        filters["status"] = payload["status"]
    if payload.get("party"):
        filters["party"] = payload["party"]
    docs = frappe.get_all(
        "Payment Entry",
        filters=filters,
        fields=["name"],
        order_by="name asc",
    )
    return [_payment_summary(frappe.get_doc("Payment Entry", row["name"])) for row in docs]


def action_get_payment(payload):
    name = payload["payment_name"]
    if not frappe.db.exists("Payment Entry", name):
        return None
    return _payment_summary(frappe.get_doc("Payment Entry", name))


def action_list_invoice_comments(payload):
    invoice_name = payload["invoice_name"]
    if not frappe.db.exists("Sales Invoice", invoice_name):
        raise frappe.DoesNotExistError(f"Sales Invoice {invoice_name} does not exist")
    invoice = action_get_invoice({"invoice_name": invoice_name}) or {}
    return invoice.get("comments", [])


def action_list_invoice_payments(payload):
    invoice_name = payload["invoice_name"]
    if not frappe.db.exists("Sales Invoice", invoice_name):
        raise frappe.DoesNotExistError(f"Sales Invoice {invoice_name} does not exist")
    return [
        _payment_summary(frappe.get_doc("Payment Entry", payment_name))
        for payment_name in _invoice_payment_names(invoice_name)
        if frappe.db.exists("Payment Entry", payment_name)
    ]


def action_add_invoice_comment(payload):
    invoice_name = payload["invoice_name"]
    if not frappe.db.exists("Sales Invoice", invoice_name):
        raise frappe.DoesNotExistError(f"Sales Invoice {invoice_name} does not exist")
    comment_doc = _ensure_invoice_comment(invoice_name, payload["comment"], payload.get("author", ""))
    return {
        "invoice_name": invoice_name,
        "comment_id": comment_doc["name"],
        "author": comment_doc.get("comment_email") or payload.get("author", ""),
        "comment": comment_doc["content"],
    }


def action_update_invoice_due_date(payload):
    invoice_name = payload["invoice_name"]
    due_date = payload["due_date"]
    if not frappe.db.exists("Sales Invoice", invoice_name):
        raise frappe.DoesNotExistError(f"Sales Invoice {invoice_name} does not exist")
    frappe.db.set_value("Sales Invoice", invoice_name, "due_date", due_date, update_modified=False)
    doc = frappe.get_doc("Sales Invoice", invoice_name)
    return {"invoice_name": invoice_name, "due_date": str(doc.due_date or "")}


def action_update_invoice_status(payload):
    invoice_name = payload["invoice_name"]
    status = payload["status"]
    if status.lower() != "paid":
        raise ValueError(f"unsupported invoice status transition: {status}")
    if not frappe.db.exists("Sales Invoice", invoice_name):
        raise frappe.DoesNotExistError(f"Sales Invoice {invoice_name} does not exist")
    invoice = frappe.get_doc("Sales Invoice", invoice_name)
    if invoice.status == "Paid":
        payment_name = frappe.db.get_value(
            "Payment Entry Reference",
            {"reference_doctype": "Sales Invoice", "reference_name": invoice_name},
            "parent",
        )
        return {"invoice_name": invoice_name, "status": invoice.status, "payment_name": payment_name}

    from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

    payment_doc = get_payment_entry("Sales Invoice", invoice_name)
    payment_doc.naming_series = "ACC-PAY-.YYYY.-.####"
    payment_doc.reference_no = f"PIPELINE-{invoice_name}"
    payment_doc.reference_date = frappe.utils.nowdate()
    payment_doc.posting_date = frappe.utils.nowdate()
    payment_doc.paid_amount = float(invoice.outstanding_amount or payment_doc.paid_amount or 0)
    payment_doc.received_amount = float(invoice.outstanding_amount or payment_doc.received_amount or 0)
    payment_doc.insert(ignore_permissions=True)
    payment_doc.submit()
    invoice.reload()
    return {"invoice_name": invoice_name, "status": invoice.status, "payment_name": payment_doc.name}


def action_delete_payment_entry(payload):
    payment_name = payload["payment_name"]
    if not frappe.db.exists("Payment Entry", payment_name):
        raise frappe.DoesNotExistError(f"Payment Entry {payment_name} does not exist")
    payment_doc = frappe.get_doc("Payment Entry", payment_name)
    if payment_doc.docstatus == 1:
        payment_doc.cancel()
    frappe.delete_doc("Payment Entry", payment_name, ignore_permissions=True, force=1)
    return {"deleted_payment_name": payment_name}


def action_list_customers(payload):
    name_query = payload.get("name_query", "")
    filters = None
    or_filters = None
    if name_query:
        or_filters = {
            "name": ["like", f"%{name_query}%"],
            "customer_name": ["like", f"%{name_query}%"],
        }
    docs = frappe.get_all(
        "Customer",
        filters=filters,
        or_filters=or_filters,
        fields=["name"],
        order_by="name asc",
    )
    return [_customer_summary(frappe.get_doc("Customer", d["name"])) for d in docs]


def action_get_customer(payload):
    customer_name = payload["customer_name"]
    if not frappe.db.exists("Customer", customer_name):
        return None
    doc = frappe.get_doc("Customer", customer_name)
    return _customer_summary(doc, include_invoices=True)


def action_create_customer(payload):
    doc = _ensure_customer(
        {
            "customer_name": payload["customer_name"],
            "customer_type": payload.get("customer_type", "Company"),
            "customer_group": payload.get("customer_group", "All Customer Groups"),
            "territory": payload.get("territory", "All Territories"),
        }
    )
    return _customer_summary(doc, include_invoices=True)


def action_list_suppliers(payload):
    name_query = payload.get("name_query", "")
    filters = None
    or_filters = None
    if name_query:
        or_filters = {
            "name": ["like", f"%{name_query}%"],
            "supplier_name": ["like", f"%{name_query}%"],
        }
    docs = frappe.get_all(
        "Supplier",
        filters=filters,
        or_filters=or_filters,
        fields=["name"],
        order_by="name asc",
    )
    return [_supplier_summary(frappe.get_doc("Supplier", d["name"])) for d in docs]


def action_get_supplier(payload):
    supplier_name = payload["supplier_name"]
    if not frappe.db.exists("Supplier", supplier_name):
        return None
    doc = frappe.get_doc("Supplier", supplier_name)
    return _supplier_summary(doc, include_purchase_invoices=True)


def action_create_supplier(payload):
    doc = _ensure_supplier(
        {
            "supplier_name": payload["supplier_name"],
            "supplier_type": payload.get("supplier_type", "Company"),
            "supplier_group": payload.get("supplier_group", "All Supplier Groups"),
        }
    )
    return _supplier_summary(doc, include_purchase_invoices=True)


def action_list_customer_invoices(payload):
    return action_list_invoices({"customer": payload["customer_name"], "status": payload.get("status", "")})


def action_list_overdue_invoices(payload):
    docs = frappe.get_all(
        "Sales Invoice",
        filters={"docstatus": 1, "due_date": ["<", frappe.utils.nowdate()]},
        fields=["name"],
        order_by="due_date asc, name asc",
    )
    results = []
    for row in docs:
        doc = frappe.get_doc("Sales Invoice", row["name"])
        if float(doc.outstanding_amount or 0) <= 0:
            continue
        if doc.status in {"Paid", "Cancelled"}:
            continue
        results.append(_invoice_summary(doc))
    return results


def action_list_purchase_invoices(payload):
    filters = {}
    if payload.get("supplier"):
        filters["supplier"] = payload["supplier"]
    docs = frappe.get_all(
        "Purchase Invoice",
        filters=filters,
        fields=["name"],
        order_by="name asc",
    )
    requested_status = payload.get("status")
    results = []
    for row in docs:
        doc = frappe.get_doc("Purchase Invoice", row["name"])
        if requested_status and doc.status != requested_status:
            continue
        results.append(_purchase_invoice_summary(doc))
    return results


def action_list_supplier_purchase_invoices(payload):
    return action_list_purchase_invoices(
        {"supplier": payload["supplier_name"], "status": payload.get("status", "")}
    )


def action_list_overdue_purchase_invoices(payload):
    docs = frappe.get_all(
        "Purchase Invoice",
        filters={"docstatus": 1, "due_date": ["<", frappe.utils.nowdate()]},
        fields=["name"],
        order_by="due_date asc, name asc",
    )
    results = []
    for row in docs:
        doc = frappe.get_doc("Purchase Invoice", row["name"])
        if float(doc.outstanding_amount or 0) <= 0:
            continue
        if doc.status in {"Paid", "Cancelled"}:
            continue
        results.append(_purchase_invoice_summary(doc))
    return results


def action_get_purchase_invoice(payload):
    name = payload["purchase_invoice_name"]
    if not frappe.db.exists("Purchase Invoice", name):
        return None
    doc = frappe.get_doc("Purchase Invoice", name)
    comments = frappe.get_all(
        "Comment",
        filters={
            "reference_doctype": "Purchase Invoice",
            "reference_name": name,
        },
        fields=["name", "comment_email", "content", "creation"],
        order_by="creation asc",
    )
    data = _purchase_invoice_summary(doc)
    data["items"] = [
        {
            "item_code": item.item_code,
            "item_name": item.item_name,
            "qty": float(item.qty or 0),
            "rate": float(item.rate or 0),
            "amount": float(item.amount or 0),
        }
        for item in getattr(doc, "items", []) or []
    ]
    data["comments"] = [
        {
            "id": item["name"],
            "author": item.get("comment_email") or "",
            "comment": item.get("content") or "",
            "creation": str(item.get("creation") or ""),
        }
        for item in comments
    ]
    return data


def action_list_purchase_invoice_comments(payload):
    invoice_name = payload["purchase_invoice_name"]
    if not frappe.db.exists("Purchase Invoice", invoice_name):
        raise frappe.DoesNotExistError(f"Purchase Invoice {invoice_name} does not exist")
    invoice = action_get_purchase_invoice({"purchase_invoice_name": invoice_name}) or {}
    return invoice.get("comments", [])


def action_add_purchase_invoice_comment(payload):
    invoice_name = payload["purchase_invoice_name"]
    if not frappe.db.exists("Purchase Invoice", invoice_name):
        raise frappe.DoesNotExistError(f"Purchase Invoice {invoice_name} does not exist")
    comment_doc = _ensure_purchase_invoice_comment(invoice_name, payload["comment"], payload.get("author", ""))
    return {
        "purchase_invoice_name": invoice_name,
        "comment_id": comment_doc["name"],
        "author": comment_doc.get("comment_email") or payload.get("author", ""),
        "comment": comment_doc["content"],
    }


def action_update_purchase_invoice_due_date(payload):
    invoice_name = payload["purchase_invoice_name"]
    due_date = payload["due_date"]
    if not frappe.db.exists("Purchase Invoice", invoice_name):
        raise frappe.DoesNotExistError(f"Purchase Invoice {invoice_name} does not exist")
    frappe.db.set_value("Purchase Invoice", invoice_name, "due_date", due_date, update_modified=False)
    doc = frappe.get_doc("Purchase Invoice", invoice_name)
    return {"purchase_invoice_name": invoice_name, "due_date": str(doc.due_date or "")}


def action_list_items(payload):
    name_query = payload.get("name_query", "")
    filters = None
    or_filters = None
    if name_query:
        or_filters = {
            "name": ["like", f"%{name_query}%"],
            "item_name": ["like", f"%{name_query}%"],
        }
    docs = frappe.get_all(
        "Item",
        filters=filters,
        or_filters=or_filters,
        fields=["name"],
        order_by="name asc",
    )
    return [_item_summary(frappe.get_doc("Item", row["name"])) for row in docs]


def action_get_item(payload):
    item_code = payload["item_code"]
    if not frappe.db.exists("Item", item_code):
        return None
    return _item_summary(frappe.get_doc("Item", item_code))


def action_create_item(payload):
    doc = _ensure_item(
        {
            "item_code": payload["item_code"],
            "item_name": payload.get("item_name", payload["item_code"]),
            "item_group": payload.get("item_group", "All Item Groups"),
            "stock_uom": payload.get("stock_uom", "Nos"),
            "is_stock_item": payload.get("is_stock_item", 0),
        }
    )
    return _item_summary(doc)


def action_list_companies(payload):
    docs = frappe.get_all("Company", fields=["name"], order_by="name asc")
    return [_company_summary(frappe.get_doc("Company", row["name"])) for row in docs]


def action_list_payment_modes(payload):
    docs = frappe.get_all("Mode of Payment", fields=["name", "type", "enabled"], order_by="name asc")
    return [
        {
            "name": row["name"],
            "type": row.get("type", ""),
            "enabled": bool(row.get("enabled", 1)),
        }
        for row in docs
    ]


def action_create_invoice(payload):
    customer = payload["customer"]
    items = payload.get("items", [])
    due_date = payload.get("due_date") or frappe.utils.add_days(frappe.utils.nowdate(), 30)
    _ensure_customer(customer)
    company = frappe.db.get_single_value("Global Defaults", "default_company")
    invoice_items = []
    for item in items:
        _ensure_item(item["item_code"], item.get("item_name", item["item_code"]))
        invoice_items.append({
            "item_code": item["item_code"],
            "qty": item.get("qty", 1),
            "rate": float(item.get("rate", 0)),
        })
    doc = frappe.get_doc({
        "doctype": "Sales Invoice",
        "naming_series": "ACC-SINV-.YYYY.-.####",
        "customer": customer,
        "company": company,
        "posting_date": frappe.utils.nowdate(),
        "due_date": due_date,
        "set_posting_time": 1,
        "items": invoice_items,
    })
    doc.insert(ignore_permissions=True)
    doc.submit()
    doc.reload()
    return _invoice_summary(doc)


def action_create_purchase_invoice(payload):
    supplier = payload["supplier"]
    items = payload.get("items", [])
    due_date = payload.get("due_date") or frappe.utils.add_days(frappe.utils.nowdate(), 30)
    _ensure_supplier(supplier)
    company = frappe.db.get_single_value("Global Defaults", "default_company")
    invoice_items = []
    for item in items:
        _ensure_item(item["item_code"], item.get("item_name", item["item_code"]))
        invoice_items.append(
            {
                "item_code": item["item_code"],
                "qty": item.get("qty", 1),
                "rate": float(item.get("rate", 0)),
            }
        )
    doc = frappe.get_doc(
        {
            "doctype": "Purchase Invoice",
            "naming_series": "ACC-PINV-.YYYY.-.####",
            "supplier": supplier,
            "company": company,
            "posting_date": frappe.utils.nowdate(),
            "due_date": due_date,
            "set_posting_time": 1,
            "items": invoice_items,
        }
    )
    doc.insert(ignore_permissions=True)
    doc.submit()
    doc.reload()
    return _purchase_invoice_summary(doc)


def action_create_payment_entry(payload):
    invoice_name = payload["invoice_name"]
    amount = float(payload["amount"])
    mode_of_payment = payload.get("mode_of_payment", "Cash")
    if not frappe.db.exists("Sales Invoice", invoice_name):
        raise frappe.DoesNotExistError(f"Sales Invoice {invoice_name} does not exist")

    from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

    payment_doc = get_payment_entry("Sales Invoice", invoice_name)
    payment_doc.naming_series = "ACC-PAY-.YYYY.-.####"
    payment_doc.reference_no = f"PIPELINE-{invoice_name}"
    payment_doc.reference_date = frappe.utils.nowdate()
    payment_doc.posting_date = frappe.utils.nowdate()
    payment_doc.mode_of_payment = mode_of_payment
    payment_doc.paid_amount = amount
    payment_doc.received_amount = amount
    payment_doc.insert(ignore_permissions=True)
    payment_doc.submit()
    payment_doc.reload()
    return _payment_summary(payment_doc)


def action_cancel_invoice(payload):
    invoice_name = payload["invoice_name"]
    if not frappe.db.exists("Sales Invoice", invoice_name):
        raise frappe.DoesNotExistError(f"Sales Invoice {invoice_name} does not exist")
    doc = frappe.get_doc("Sales Invoice", invoice_name)
    if doc.docstatus != 1:
        raise ValueError(f"Invoice {invoice_name} has not been submitted yet (docstatus={doc.docstatus}) and cannot be canceled")
    doc.cancel()
    doc.reload()
    return {"invoice_name": invoice_name, "status": doc.status, "docstatus": doc.docstatus}


def action_cancel_purchase_invoice(payload):
    invoice_name = payload["purchase_invoice_name"]
    if not frappe.db.exists("Purchase Invoice", invoice_name):
        raise frappe.DoesNotExistError(f"Purchase Invoice {invoice_name} does not exist")
    doc = frappe.get_doc("Purchase Invoice", invoice_name)
    if doc.docstatus != 1:
        raise ValueError(f"Purchase invoice {invoice_name} has not been submitted yet (docstatus={doc.docstatus}) and cannot be canceled")
    doc.cancel()
    doc.reload()
    return {"purchase_invoice_name": invoice_name, "status": doc.status, "docstatus": doc.docstatus}


ACTIONS = {
    "bootstrap": action_bootstrap,
    "seed": action_seed,
    "list_invoices": action_list_invoices,
    "list_customer_invoices": action_list_customer_invoices,
    "list_overdue_invoices": action_list_overdue_invoices,
    "get_invoice": action_get_invoice,
    "list_invoice_comments": action_list_invoice_comments,
    "list_payments": action_list_payments,
    "list_invoice_payments": action_list_invoice_payments,
    "get_payment": action_get_payment,
    "add_invoice_comment": action_add_invoice_comment,
    "update_invoice_due_date": action_update_invoice_due_date,
    "update_invoice_status": action_update_invoice_status,
    "delete_payment_entry": action_delete_payment_entry,
    "list_customers": action_list_customers,
    "get_customer": action_get_customer,
    "create_customer": action_create_customer,
    "list_suppliers": action_list_suppliers,
    "get_supplier": action_get_supplier,
    "create_supplier": action_create_supplier,
    "list_items": action_list_items,
    "get_item": action_get_item,
    "create_item": action_create_item,
    "list_companies": action_list_companies,
    "list_payment_modes": action_list_payment_modes,
    "list_purchase_invoices": action_list_purchase_invoices,
    "list_supplier_purchase_invoices": action_list_supplier_purchase_invoices,
    "list_overdue_purchase_invoices": action_list_overdue_purchase_invoices,
    "get_purchase_invoice": action_get_purchase_invoice,
    "list_purchase_invoice_comments": action_list_purchase_invoice_comments,
    "add_purchase_invoice_comment": action_add_purchase_invoice_comment,
    "update_purchase_invoice_due_date": action_update_purchase_invoice_due_date,
    "create_invoice": action_create_invoice,
    "create_purchase_invoice": action_create_purchase_invoice,
    "create_payment_entry": action_create_payment_entry,
    "cancel_invoice": action_cancel_invoice,
    "cancel_purchase_invoice": action_cancel_purchase_invoice,
}


def main():
    site, action, payload = _parse_args()
    handler = ACTIONS.get(action)
    if handler is None:
        raise SystemExit(f"unknown action: {action}")

    os.makedirs("/home/frappe/logs", exist_ok=True)
    bench_site_path = f"/home/frappe/frappe-bench/{site}"
    actual_site_path = f"/home/frappe/frappe-bench/sites/{site}"
    if not os.path.exists(bench_site_path) and os.path.exists(actual_site_path):
        os.symlink(actual_site_path, bench_site_path)
    frappe.init(site=site, sites_path="sites")
    frappe.connect()
    try:
        result = handler(payload)
        frappe.db.commit()
        _json_print(result)
    except Exception:
        frappe.db.rollback()
        traceback.print_exc(file=sys.stderr)
        raise
    finally:
        frappe.destroy()


if __name__ == "__main__":
    main()
