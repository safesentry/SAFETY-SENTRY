"""
ERPNext live-site tools.
"""

import base64
import json
import os
import subprocess

from ...exceptions import ToolExecutionError
from ...settings import REPO_ROOT
from ...service_tools import ServiceToolRegistry


_REGISTRY = ServiceToolRegistry(service_id="erpnext")


def erpnext_tool(name, description, params, required=None, is_write=False, group="", short_description=""):
    return _REGISTRY.register(
        name=name,
        description=description,
        params=params,
        required=required,
        is_write=is_write,
        group=group,
        short_description=short_description,
    )


def get_all_schemas():
    return _REGISTRY.get_all_schemas()


def call_tool(name, args):
    return _REGISTRY.call_tool(name, args)


def get_tool_names():
    return _REGISTRY.get_tool_names()


def _backend_container():
    return os.environ.get("ERPNEXT_BACKEND_CONTAINER", "pipeline-erpnext-backend")


def _site_name():
    return os.environ.get("ERPNEXT_SITE_NAME", "frontend")


def _site_ops_script():
    return "/opt/pipeline/scripts/erpnext_site_ops.py"


def _run_site_action(action, payload=None):
    payload = payload or {}
    payload_b64 = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii")
    cmd = [
        "docker",
        "exec",
        "-e",
        f"PIPELINE_JSON_PAYLOAD_B64={payload_b64}",
        _backend_container(),
        "bash",
        "-lc",
        (
            "cd /home/frappe/frappe-bench && "
            f"/home/frappe/frappe-bench/env/bin/python {_site_ops_script()} {_site_name()} {action} "
        ),
    ]
    result = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise ToolExecutionError(f"[ERPNext Action Failed] {detail}")
    try:
        return json.loads(result.stdout.strip() or "null")
    except json.JSONDecodeError as exc:
        raise ToolExecutionError(f"[ERPNext Returned Non-JSON Output] {result.stdout[:500]}") from exc


def _format_json(data):
    if isinstance(data, str):
        return data
    return json.dumps(data, ensure_ascii=False, indent=2)


@erpnext_tool(
    "list_invoices",
    "List sales invoices, optionally filtered by status or customer.",
    {
        "status": {"type": "string", "description": "Status, such as Unpaid, Paid, or Overdue"},
        "customer": {"type": "string", "description": "Customer name"},
    },
    group="invoices",
    short_description="List sales invoices with optional status or customer filters.",
)
def list_invoices(status="", customer=""):
    return _format_json(_run_site_action("list_invoices", {"status": status, "customer": customer}))


@erpnext_tool(
    "list_customer_invoices",
    "List invoices for a specific customer, optionally filtered by status.",
    {
        "customer_name": {"type": "string", "description": "Customer name"},
        "status": {"type": "string", "description": "Status, such as Unpaid, Paid, or Overdue"},
    },
    required=["customer_name"],
    group="invoices",
    short_description="List invoices for one customer with optional status filtering.",
)
def list_customer_invoices(customer_name, status=""):
    return _format_json(
        _run_site_action(
            "list_customer_invoices",
            {"customer_name": customer_name, "status": status},
        )
    )


@erpnext_tool(
    "list_overdue_invoices",
    "List invoices that are overdue and still have an outstanding balance.",
    {},
    group="invoices",
    short_description="List overdue invoices that still have outstanding balance.",
)
def list_overdue_invoices():
    return _format_json(_run_site_action("list_overdue_invoices"))


@erpnext_tool(
    "get_invoice",
    "Get details for a single sales invoice, including line items, comments, and linked payments.",
    {
        "invoice_name": {"type": "string", "description": "Invoice number"},
    },
    required=["invoice_name"],
    group="invoices",
    short_description="Read one invoice with items, comments, and linked payments.",
)
def get_invoice(invoice_name):
    payload = _run_site_action("get_invoice", {"invoice_name": invoice_name})
    if not payload:
        raise ToolExecutionError(f"[Error] Invoice not found: {invoice_name}")
    return _format_json(payload)


@erpnext_tool(
    "list_invoice_comments",
    "List comment history for a specific invoice.",
    {
        "invoice_name": {"type": "string", "description": "Invoice number"},
    },
    required=["invoice_name"],
    group="invoice_comments",
    short_description="List comment history attached to one invoice.",
)
def list_invoice_comments(invoice_name):
    return _format_json(_run_site_action("list_invoice_comments", {"invoice_name": invoice_name}))


@erpnext_tool(
    "add_invoice_comment",
    "Append a comment to an invoice.",
    {
        "invoice_name": {"type": "string", "description": "Invoice number"},
        "comment": {"type": "string", "description": "Comment content"},
        "author": {"type": "string", "description": "Comment author"},
    },
    required=["invoice_name", "comment"],
    is_write=True,
    group="invoice_comments",
    short_description="Append a finance note to an existing invoice.",
)
def add_invoice_comment(invoice_name, comment, author="accounts-bot"):
    return _format_json(
        _run_site_action(
            "add_invoice_comment",
            {
                "invoice_name": invoice_name,
                "comment": comment,
                "author": author,
            },
        )
    )


@erpnext_tool(
    "update_invoice_due_date",
    "Update the due date of a sales invoice.",
    {
        "invoice_name": {"type": "string", "description": "Invoice number"},
        "due_date": {"type": "string", "description": "New due date, for example 2026-04-30"},
    },
    required=["invoice_name", "due_date"],
    is_write=True,
    group="invoice_updates",
    short_description="Change the due date of one existing invoice.",
)
def update_invoice_due_date(invoice_name, due_date):
    return _format_json(
        _run_site_action(
            "update_invoice_due_date",
            {"invoice_name": invoice_name, "due_date": due_date},
        )
    )


@erpnext_tool(
    "update_invoice_status",
    "Update the status of an invoice. Currently supports marking a sales invoice as Paid.",
    {
        "invoice_name": {"type": "string", "description": "Invoice number"},
        "status": {"type": "string", "description": "New status, such as Paid"},
    },
    required=["invoice_name", "status"],
    is_write=True,
    group="invoice_updates",
    short_description="Mark one invoice as paid by creating the matching payment entry.",
)
def update_invoice_status(invoice_name, status):
    return _format_json(
        _run_site_action(
            "update_invoice_status",
            {"invoice_name": invoice_name, "status": status},
        )
    )


@erpnext_tool(
    "cancel_invoice",
    "Cancel a sales invoice. Only submitted invoices can be cancelled.",
    {
        "invoice_name": {"type": "string", "description": "Invoice number"},
    },
    required=["invoice_name"],
    is_write=True,
    group="invoice_updates",
    short_description="Cancel a submitted invoice.",
)
def cancel_invoice(invoice_name):
    return _format_json(_run_site_action("cancel_invoice", {"invoice_name": invoice_name}))


@erpnext_tool(
    "create_invoice",
    "Create a sales invoice.",
    {
        "customer": {"type": "string", "description": "Customer name"},
        "items": {
            "type": "array",
            "items": {"type": "object"},
            "description": "Invoice line items. Each item includes item_code, qty, rate, and optional item_name.",
        },
        "due_date": {"type": "string", "description": "Due date, for example 2026-04-15"},
    },
    required=["customer", "items"],
    is_write=True,
    group="invoices",
    short_description="Create a submitted sales invoice for a customer.",
)
def create_invoice(customer, items, due_date=""):
    return _format_json(
        _run_site_action(
            "create_invoice",
            {
                "customer": customer,
                "items": items,
                "due_date": due_date,
            },
        )
    )


@erpnext_tool(
    "list_payments",
    "List payment entries, optionally filtered by status or customer.",
    {
        "status": {"type": "string", "description": "Status, such as Draft or Submitted"},
        "party": {"type": "string", "description": "Customer name"},
    },
    group="payments",
    short_description="List payment entries with optional status or customer filter.",
)
def list_payments(status="", party=""):
    return _format_json(_run_site_action("list_payments", {"status": status, "party": party}))


@erpnext_tool(
    "list_invoice_payments",
    "List payment entries linked to a specific invoice.",
    {
        "invoice_name": {"type": "string", "description": "Invoice number"},
    },
    required=["invoice_name"],
    group="payments",
    short_description="List payment entries linked to one invoice.",
)
def list_invoice_payments(invoice_name):
    return _format_json(_run_site_action("list_invoice_payments", {"invoice_name": invoice_name}))


@erpnext_tool(
    "get_payment",
    "Get details for a single payment entry.",
    {
        "payment_name": {"type": "string", "description": "Payment entry number"},
    },
    required=["payment_name"],
    group="payments",
    short_description="Read one payment entry including linked references.",
)
def get_payment(payment_name):
    payload = _run_site_action("get_payment", {"payment_name": payment_name})
    if not payload:
        raise ToolExecutionError(f"[Error] Payment entry not found: {payment_name}")
    return _format_json(payload)


@erpnext_tool(
    "create_payment_entry",
    "Create a payment entry.",
    {
        "invoice_name": {"type": "string", "description": "Linked invoice number"},
        "amount": {"type": "number", "description": "Payment amount"},
        "mode_of_payment": {"type": "string", "description": "Mode of payment, such as Cash or Wire Transfer"},
    },
    required=["invoice_name", "amount"],
    is_write=True,
    group="payments",
    short_description="Create a payment entry against one invoice.",
)
def create_payment_entry(invoice_name, amount, mode_of_payment="Cash"):
    return _format_json(
        _run_site_action(
            "create_payment_entry",
            {
                "invoice_name": invoice_name,
                "amount": amount,
                "mode_of_payment": mode_of_payment,
            },
        )
    )


@erpnext_tool(
    "delete_payment_entry",
    "Delete a payment entry.",
    {
        "payment_name": {"type": "string", "description": "Payment entry number"},
    },
    required=["payment_name"],
    is_write=True,
    group="payments",
    short_description="Delete a payment entry, cancelling it first if needed.",
)
def delete_payment_entry(payment_name):
    return _format_json(_run_site_action("delete_payment_entry", {"payment_name": payment_name}))


@erpnext_tool(
    "list_customers",
    "List customers, optionally filtered by name.",
    {
        "name_query": {"type": "string", "description": "Customer name keyword"},
    },
    group="customers",
    short_description="List customer master records with optional name filtering.",
)
def list_customers(name_query=""):
    return _format_json(_run_site_action("list_customers", {"name_query": name_query}))


@erpnext_tool(
    "get_customer",
    "Get details for a single customer, including linked invoices.",
    {
        "customer_name": {"type": "string", "description": "Customer name"},
    },
    required=["customer_name"],
    group="customers",
    short_description="Read one customer profile with linked invoices.",
)
def get_customer(customer_name):
    payload = _run_site_action("get_customer", {"customer_name": customer_name})
    if not payload:
        raise ToolExecutionError(f"[Error] Customer not found: {customer_name}")
    return _format_json(payload)


@erpnext_tool(
    "create_customer",
    "Create a customer master record.",
    {
        "customer_name": {"type": "string", "description": "Customer name"},
        "customer_type": {"type": "string", "description": "Customer type, such as Company or Individual"},
        "customer_group": {"type": "string", "description": "Customer group. Default: All Customer Groups"},
        "territory": {"type": "string", "description": "Territory. Default: All Territories"},
    },
    required=["customer_name"],
    is_write=True,
    group="customers",
    short_description="Create a new customer master record.",
)
def create_customer(customer_name, customer_type="Company", customer_group="All Customer Groups", territory="All Territories"):
    return _format_json(
        _run_site_action(
            "create_customer",
            {
                "customer_name": customer_name,
                "customer_type": customer_type,
                "customer_group": customer_group,
                "territory": territory,
            },
        )
    )


@erpnext_tool(
    "list_suppliers",
    "List suppliers, optionally filtered by name.",
    {
        "name_query": {"type": "string", "description": "Supplier name keyword"},
    },
    group="suppliers",
    short_description="List supplier master records with optional name filtering.",
)
def list_suppliers(name_query=""):
    return _format_json(_run_site_action("list_suppliers", {"name_query": name_query}))


@erpnext_tool(
    "get_supplier",
    "Get details for a single supplier, including linked purchase invoices.",
    {
        "supplier_name": {"type": "string", "description": "Supplier name"},
    },
    required=["supplier_name"],
    group="suppliers",
    short_description="Read one supplier profile with linked purchase invoices.",
)
def get_supplier(supplier_name):
    payload = _run_site_action("get_supplier", {"supplier_name": supplier_name})
    if not payload:
        raise ToolExecutionError(f"[Error] Supplier not found: {supplier_name}")
    return _format_json(payload)


@erpnext_tool(
    "create_supplier",
    "Create a supplier master record.",
    {
        "supplier_name": {"type": "string", "description": "Supplier name"},
        "supplier_type": {"type": "string", "description": "Supplier type, such as Company or Individual"},
        "supplier_group": {"type": "string", "description": "Supplier group. Default: All Supplier Groups"},
    },
    required=["supplier_name"],
    is_write=True,
    group="suppliers",
    short_description="Create a new supplier master record.",
)
def create_supplier(supplier_name, supplier_type="Company", supplier_group="All Supplier Groups"):
    return _format_json(
        _run_site_action(
            "create_supplier",
            {
                "supplier_name": supplier_name,
                "supplier_type": supplier_type,
                "supplier_group": supplier_group,
            },
        )
    )


@erpnext_tool(
    "list_items",
    "List items, optionally filtered by item code or item name keyword.",
    {
        "name_query": {"type": "string", "description": "Item code or name keyword"},
    },
    group="items_catalog",
    short_description="List item master records with optional code or name filtering.",
)
def list_items(name_query=""):
    return _format_json(_run_site_action("list_items", {"name_query": name_query}))


@erpnext_tool(
    "get_item",
    "Get details for a single item.",
    {
        "item_code": {"type": "string", "description": "Item code"},
    },
    required=["item_code"],
    group="items_catalog",
    short_description="Read one item master record by item code.",
)
def get_item(item_code):
    payload = _run_site_action("get_item", {"item_code": item_code})
    if not payload:
        raise ToolExecutionError(f"[Error] Item not found: {item_code}")
    return _format_json(payload)


@erpnext_tool(
    "create_item",
    "Create a new item master record.",
    {
        "item_code": {"type": "string", "description": "Item code"},
        "item_name": {"type": "string", "description": "Item name"},
        "item_group": {"type": "string", "description": "Item group. Default: All Item Groups"},
        "stock_uom": {"type": "string", "description": "Stock UOM. Default: Nos"},
        "is_stock_item": {"type": "integer", "description": "Whether this is a stock item: 0 or 1"},
    },
    required=["item_code", "item_name"],
    is_write=True,
    group="items_catalog",
    short_description="Create a new item master record.",
)
def create_item(item_code, item_name, item_group="All Item Groups", stock_uom="Nos", is_stock_item=0):
    return _format_json(
        _run_site_action(
            "create_item",
            {
                "item_code": item_code,
                "item_name": item_name,
                "item_group": item_group,
                "stock_uom": stock_uom,
                "is_stock_item": is_stock_item,
            },
        )
    )


@erpnext_tool(
    "list_purchase_invoices",
    "List purchase invoices, optionally filtered by status or supplier.",
    {
        "status": {"type": "string", "description": "Status, such as Unpaid, Paid, or Overdue"},
        "supplier": {"type": "string", "description": "Supplier name"},
    },
    group="purchase_invoices",
    short_description="List purchase invoices with optional status or supplier filters.",
)
def list_purchase_invoices(status="", supplier=""):
    return _format_json(
        _run_site_action("list_purchase_invoices", {"status": status, "supplier": supplier})
    )


@erpnext_tool(
    "list_supplier_purchase_invoices",
    "List purchase invoices for a supplier, optionally filtered by status.",
    {
        "supplier_name": {"type": "string", "description": "Supplier name"},
        "status": {"type": "string", "description": "Status, such as Unpaid, Paid, or Overdue"},
    },
    required=["supplier_name"],
    group="purchase_invoices",
    short_description="List purchase invoices for one supplier with optional status filtering.",
)
def list_supplier_purchase_invoices(supplier_name, status=""):
    return _format_json(
        _run_site_action(
            "list_supplier_purchase_invoices",
            {"supplier_name": supplier_name, "status": status},
        )
    )


@erpnext_tool(
    "list_overdue_purchase_invoices",
    "List purchase invoices that are overdue and still have an outstanding balance.",
    {},
    group="purchase_invoices",
    short_description="List overdue purchase invoices that still have outstanding balance.",
)
def list_overdue_purchase_invoices():
    return _format_json(_run_site_action("list_overdue_purchase_invoices"))


@erpnext_tool(
    "get_purchase_invoice",
    "Get details for a single purchase invoice, including line items and comments.",
    {
        "purchase_invoice_name": {"type": "string", "description": "Purchase invoice number"},
    },
    required=["purchase_invoice_name"],
    group="purchase_invoices",
    short_description="Read one purchase invoice with items and comments.",
)
def get_purchase_invoice(purchase_invoice_name):
    payload = _run_site_action("get_purchase_invoice", {"purchase_invoice_name": purchase_invoice_name})
    if not payload:
        raise ToolExecutionError(f"[Error] Purchase invoice not found: {purchase_invoice_name}")
    return _format_json(payload)


@erpnext_tool(
    "list_purchase_invoice_comments",
    "List comment history for a specific purchase invoice.",
    {
        "purchase_invoice_name": {"type": "string", "description": "Purchase invoice number"},
    },
    required=["purchase_invoice_name"],
    group="purchase_invoice_comments",
    short_description="List comment history attached to one purchase invoice.",
)
def list_purchase_invoice_comments(purchase_invoice_name):
    return _format_json(
        _run_site_action(
            "list_purchase_invoice_comments",
            {"purchase_invoice_name": purchase_invoice_name},
        )
    )


@erpnext_tool(
    "add_purchase_invoice_comment",
    "Append a comment to a purchase invoice.",
    {
        "purchase_invoice_name": {"type": "string", "description": "Purchase invoice number"},
        "comment": {"type": "string", "description": "Comment content"},
        "author": {"type": "string", "description": "Comment author"},
    },
    required=["purchase_invoice_name", "comment"],
    is_write=True,
    group="purchase_invoice_comments",
    short_description="Append an AP note to an existing purchase invoice.",
)
def add_purchase_invoice_comment(purchase_invoice_name, comment, author="ap-bot"):
    return _format_json(
        _run_site_action(
            "add_purchase_invoice_comment",
            {
                "purchase_invoice_name": purchase_invoice_name,
                "comment": comment,
                "author": author,
            },
        )
    )


@erpnext_tool(
    "update_purchase_invoice_due_date",
    "Update the due date of a purchase invoice.",
    {
        "purchase_invoice_name": {"type": "string", "description": "Purchase invoice number"},
        "due_date": {"type": "string", "description": "New due date, for example 2026-04-30"},
    },
    required=["purchase_invoice_name", "due_date"],
    is_write=True,
    group="purchase_invoice_updates",
    short_description="Change the due date of one existing purchase invoice.",
)
def update_purchase_invoice_due_date(purchase_invoice_name, due_date):
    return _format_json(
        _run_site_action(
            "update_purchase_invoice_due_date",
            {"purchase_invoice_name": purchase_invoice_name, "due_date": due_date},
        )
    )


@erpnext_tool(
    "cancel_purchase_invoice",
    "Cancel a purchase invoice. Only submitted purchase invoices can be cancelled.",
    {
        "purchase_invoice_name": {"type": "string", "description": "Purchase invoice number"},
    },
    required=["purchase_invoice_name"],
    is_write=True,
    group="purchase_invoice_updates",
    short_description="Cancel a submitted purchase invoice.",
)
def cancel_purchase_invoice(purchase_invoice_name):
    return _format_json(
        _run_site_action(
            "cancel_purchase_invoice",
            {"purchase_invoice_name": purchase_invoice_name},
        )
    )


@erpnext_tool(
    "create_purchase_invoice",
    "Create a purchase invoice.",
    {
        "supplier": {"type": "string", "description": "Supplier name"},
        "items": {
            "type": "array",
            "items": {"type": "object"},
            "description": "Purchase invoice line items. Each item includes item_code, qty, rate, and optional item_name.",
        },
        "due_date": {"type": "string", "description": "Due date, for example 2026-04-15"},
    },
    required=["supplier", "items"],
    is_write=True,
    group="purchase_invoices",
    short_description="Create a submitted purchase invoice for a supplier.",
)
def create_purchase_invoice(supplier, items, due_date=""):
    return _format_json(
        _run_site_action(
            "create_purchase_invoice",
            {
                "supplier": supplier,
                "items": items,
                "due_date": due_date,
            },
        )
    )


@erpnext_tool(
    "list_companies",
    "List companies in the current ERPNext site.",
    {},
    group="reference_data",
    short_description="List configured companies in the ERPNext site.",
)
def list_companies():
    return _format_json(_run_site_action("list_companies"))


@erpnext_tool(
    "list_payment_modes",
    "List currently available payment modes.",
    {},
    group="reference_data",
    short_description="List available payment modes such as Cash or Wire Transfer.",
)
def list_payment_modes():
    return _format_json(_run_site_action("list_payment_modes"))
