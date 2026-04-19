"""Count public tables in Postgres. Used by CI to detect pre-existing databases."""

from sqlalchemy import create_engine, text
from ypl.backend.config import Settings

settings = Settings()
engine = create_engine(settings.db_url())
with engine.connect() as conn:
    result = conn.execute(
        text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'")
    )
    print(result.scalar())
