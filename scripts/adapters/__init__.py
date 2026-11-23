"""Source adapters: raw API JSON -> Higle event items."""

import importlib

from .base_adapter import BaseAdapter

__all__ = ["BaseAdapter", "load_adapter"]


def load_adapter(source: dict) -> BaseAdapter:
    """Instantiate the adapter named by ``source["adapter"]`` (a module in this package)."""
    module = importlib.import_module(f"{__name__}.{source['adapter']}")
    adapter_classes = [
        obj
        for obj in vars(module).values()
        if isinstance(obj, type) and issubclass(obj, BaseAdapter) and obj is not BaseAdapter
    ]
    if len(adapter_classes) != 1:
        raise ImportError(f"{source['adapter']} must define exactly one BaseAdapter subclass")
    return adapter_classes[0](source)
