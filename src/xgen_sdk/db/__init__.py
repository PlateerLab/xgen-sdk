"""
xgen_sdk.db - 데이터베이스 모듈

직접 PostgreSQL 연결을 제공하는 psycopg3 기반 데이터베이스 매니저.

Usage:
    from xgen_sdk.db import XgenDB, BaseModel

    db = XgenDB(database_config)
    db.initialize_database()

    # Model-based CRUD (xgen-core)
    record_id = db.insert(user_model)
    records = db.find_by_condition(UserModel, {"status": "active"})

    # Table-name CRUD (xgen-workflow, xgen-documents)
    result = db.insert_record("users", {"name": "test"})
    result = db.find_records_by_condition("users", {"is_active": True})

    # Raw SQL
    result = db.execute_raw_query("SELECT * FROM users WHERE id = %s", (1,))
"""

from xgen_sdk.db.base_model import BaseModel
from xgen_sdk.db.app_db import XgenDB
from xgen_sdk.db.database_config import DatabaseConfig, database_config, ConfigValue

# 하위 호환 alias
AppDatabaseManager = XgenDB

__all__ = ["XgenDB", "AppDatabaseManager", "BaseModel", "DatabaseConfig", "database_config", "ConfigValue"]
