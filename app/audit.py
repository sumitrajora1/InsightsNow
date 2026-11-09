import hashlib

import sqlglot
from sqlglot import exp


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def redact_sql(sql: str | None) -> str | None:
    if not sql:
        return None
    try:
        tree = sqlglot.parse_one(sql, read="trino")
        return tree.transform(
            lambda n: exp.Literal.string("[REDACTED]") if isinstance(n, exp.Literal) else n
        ).sql("trino")
    except Exception:
        return "[UNPARSEABLE SQL REDACTED]"
