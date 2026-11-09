from alembic import context
from sqlalchemy import create_engine

from app.config import Settings
from app.database import Base

cfg = Settings()
if context.is_offline_mode():
    context.configure(url=cfg.database_url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    with create_engine(cfg.database_url).connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()
