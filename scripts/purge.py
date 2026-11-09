"""Run under a separately authorized maintenance database role, not the API role."""

from app.config import Settings
from app.database import Store

if __name__ == "__main__":
    cfg = Settings()
    if not 1 <= cfg.retention_days <= 3650:
        raise ValueError("Review the configured retention period")
    Store(cfg.database_url).purge(cfg.retention_days)
    print("Expired query, session and audit records purged under the configured retention policy.")
