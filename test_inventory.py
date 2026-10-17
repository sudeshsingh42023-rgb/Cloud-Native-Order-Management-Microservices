import os
os.environ["DATABASE_URL"] = "sqlite:///./test_inventory.db"
import pytest
from fastapi.testclient import TestClient
from app.main import app, Base, engine


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    with TestClient(app) as c:
        yield c


def seed(c, stock=5):
    return c.post("/products", json={"sku": "A1", "name": "Mug", "price": 9.5, "stock": stock})


def test_create_and_get(client):
    assert seed(client).status_code == 201
    r = client.get("/products/A1")
    assert r.status_code == 200 and r.json()["stock"] == 5


def test_duplicate_sku_conflict(client):
    seed(client)
    assert seed(client).status_code == 409


def test_reserve_decrements_stock(client):
    seed(client)
    r = client.post("/products/A1/reserve", json={"quantity": 3})
    assert r.status_code == 200 and r.json()["stock"] == 2


def test_cannot_oversell(client):
    seed(client, stock=2)
    assert client.post("/products/A1/reserve", json={"quantity": 3}).status_code == 409
    assert client.get("/products/A1").json()["stock"] == 2


def test_reserve_unknown_sku(client):
    assert client.post("/products/NOPE/reserve", json={"quantity": 1}).status_code == 404
