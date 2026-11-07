from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from courseware.auth import hasher
from courseware.config import settings
from courseware.db import engine
from courseware.main import app
from courseware.models import Base, User

PASSWORD = "test-password-for-courseware"


@pytest.fixture(scope="session", autouse=True)
def migrate():
    assert engine().url.database == "courseware_test", (
        "Tests only operate on the dedicated courseware_test database"
    )
    command.upgrade(Config("alembic.ini"), "head")


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    with engine().begin() as connection:
        names = ", ".join('"' + table + '"' for table in Base.metadata.tables)
        connection.execute(text(f"TRUNCATE {names} CASCADE"))
    monkeypatch.setenv("CW_STORAGE_PATH", str(tmp_path / "storage"))
    settings.cache_clear()
    (settings().storage_path / "releases").mkdir(parents=True)
    yield
    settings.cache_clear()


@pytest.fixture
def db():
    with Session(engine(), expire_on_commit=False) as session:
        yield session


@pytest.fixture
def user_factory(db):
    def create(username="admin", role="admin", must_change=False):
        user = User(
            username=username,
            display_name=username,
            password_hash=hasher.hash(PASSWORD),
            role=role,
            status="active",
            must_change_password=must_change,
        )
        db.add(user)
        db.commit()
        return user

    return create


@pytest.fixture
def client():
    with TestClient(app, base_url=settings().public_origin) as client:
        yield client


def login(client, username="admin", password=PASSWORD):
    token = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
        headers={"Origin": settings().public_origin, "X-CSRF-Token": token},
    )
    assert response.status_code == 200, response.text
    client.headers.update({"Origin": settings().public_origin, "X-CSRF-Token": response.json()["csrf_token"]})
    return response


@pytest.fixture
def demo(tmp_path):
    import shutil

    source = Path(__file__).resolve().parents[2] / "courseware-demo"
    target = tmp_path / "demo"
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(".git", "node_modules"))
    return target
