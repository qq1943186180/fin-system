"""Finance System business modules.

The package intentionally avoids eager imports because several legacy modules
still support direct execution.  New code should prefer ``modules.<name>``.
"""

__all__ = [
    "arap",
    "expense",
    "feishu",
    "invoice_gui",
    "integration",
    "ledger",
    "ocr_invoice",
    "reports",
]
