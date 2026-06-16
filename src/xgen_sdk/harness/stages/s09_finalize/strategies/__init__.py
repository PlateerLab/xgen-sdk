"""s09_finalize strategies — persist (구 s10_save 격하)."""

from .persist import persist_execution_record, PERSIST_DEFAULTS, register_persist_defaults

__all__ = ["persist_execution_record", "PERSIST_DEFAULTS", "register_persist_defaults"]
