from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from .config import settings


@lru_cache
def engine():
    return create_engine(settings().database_url, pool_pre_ping=True)


def get_db():
    with Session(engine(), expire_on_commit=False) as db:
        yield db
