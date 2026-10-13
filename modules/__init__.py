"""Finance System business modules.

The package intentionally avoids eager imports because several legacy modules
still support direct execution.  New code should prefer ``modules.<name>``.
"""

__all__ = [
    "arap",
    "closing",
    "einvoice",
    "expense",
    "export_service",
    "feishu",
    "feishu_pull",
    "integration",
    "inventory",
    "invoice_gui",
    "ledger",
    "mail_processor",
    "mail_service",
    "mail_sync",
    "money",
    "reports",
]
