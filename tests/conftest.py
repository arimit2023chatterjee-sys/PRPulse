import os
import sys
from pathlib import Path

# Ensure root project directory is in python path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Set testing environment before importing app
os.environ["DATABASE_URL"] = "sqlite:///./test_pr_reviewer.db"
os.environ["LLM_PROVIDER"] = "mock"
os.environ["GITHUB_WEBHOOK_SECRET"] = "test-webhook-secret-12345"
os.environ["ENABLE_RUFF"] = "true"

from app.database import Base, get_db
from app.main import app

test_engine = create_engine(
    "sqlite:///./test_pr_reviewer.db",
    connect_args={"check_same_thread": False},
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    Base.metadata.drop_all(bind=test_engine)
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)
    if os.path.exists("./test_pr_reviewer.db"):
        try:
            os.remove("./test_pr_reviewer.db")
        except OSError:
            pass


@pytest.fixture
def db_session():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def client(db_session):
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
