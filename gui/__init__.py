"""Tk desktop entry points for FinanceSystem."""

__all__ = ["FinanceApp", "main"]


def __getattr__(name):
    if name in __all__:
        from .main import FinanceApp, main

        return {"FinanceApp": FinanceApp, "main": main}[name]
    raise AttributeError(name)
