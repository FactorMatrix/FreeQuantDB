"""Optional upstream adapters, separate from trusted downstream BT catalogs."""

from .base import FetchRequest, adapter, describe_adapters, register_adapter

__all__ = ["FetchRequest", "adapter", "describe_adapters", "register_adapter"]
