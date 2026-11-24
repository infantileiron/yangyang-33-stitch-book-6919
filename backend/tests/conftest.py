import os
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
os.environ.setdefault('DATABASE_URL', 'sqlite:////tmp/desk_corrections_test.db')
os.environ.setdefault('REDIS_URL', 'redis://redis-unavailable:6399/0')
os.environ.setdefault('SEED_DEMO', 'false')
os.environ.setdefault('ARTIFACT_ROOT', '/tmp/desk_test_artifacts')


class FakePipeline:
    def incr(self, *_args, **_kwargs):
        return self

    def expire(self, *_args, **_kwargs):
        return self

    def lpush(self, *_args, **_kwargs):
        return self

    def ltrim(self, *_args, **_kwargs):
        return self

    def execute(self):
        return [1, True]

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class FakeRedis:
    def pipeline(self):
        return FakePipeline()

    def ping(self):
        return True

    def lpush(self, *_args, **_kwargs):
        return 1

    def ltrim(self, *_args, **_kwargs):
        return True

    def set(self, *_args, **_kwargs):
        return True

    def get(self, *_args, **_kwargs):
        return 'yes'

    def blpop(self, *_args, **_kwargs):
        return None


@pytest.fixture
def redis_stub(monkeypatch):
    stub = FakeRedis()
    from src.routes import session as session_route
    from src.services import broker
    monkeypatch.setattr(session_route, 'redis', stub)
    monkeypatch.setattr(broker, 'redis', stub)
    return stub


@pytest.fixture
def database():
    from src.models.base import Base, engine
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


@pytest.fixture
def client(database, redis_stub):
    from fastapi.testclient import TestClient
    from src.main import app
    with TestClient(app) as test_client:
        yield test_client


def _token(client, role):
    passwords = {
        'analyst': 'Analyst-2026!',
        'supervisor': 'Supervisor-2026!',
        'viewer': 'Viewer-2026!',
    }
    response = client.post('/api/auth/login',
                           json={'email': f'{role}@demo.local', 'password': passwords[role]})
    assert response.status_code == 200, response.text
    return response.json()['token']


@pytest.fixture
def auth(client):
    return {role: {'Authorization': f'Bearer {_token(client, role)}'}
            for role in ('analyst', 'supervisor', 'viewer')}


@pytest.fixture
def seed_ledger(database):
    from src.models.base import Session
    from src.services.observations import add_observations, clean_observation
    rows = [
        dict(process='植鞣皮补色', sheet='SC-01', severity=4, delay=3, count=8),
        dict(process='植鞣皮补色', sheet='SC-01', severity=3, delay=2, count=5),
        dict(process='搭扣换簧', sheet='SC-02', severity=2, delay=1, count=4),
    ]
    with Session.begin() as db:
        add_observations(db, [clean_observation(row) for row in rows], 'analyst@demo.local',
                         'observation.created')
    return rows
