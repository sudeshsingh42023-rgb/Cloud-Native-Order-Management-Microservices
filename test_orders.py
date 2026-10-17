import os
os.environ["DATABASE_URL"] = "sqlite:///./test_orders.db"
os.environ["ADMIN_USERNAME"] = "boss"
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from app import main
from app.main import app, Base, engine

NOTIFIED = []


@pytest.fixture()
def client(monkeypatch):
    Base.metadata.drop_all(engine)
    NOTIFIED.clear()
    stock = {"A1": 5}
    monkeypatch.setattr(main, "fetch_product", lambda sku: {"sku": sku, "price": 10.0})

    def reserve(sku, qty):
        if stock.get(sku, 0) < qty:
            raise HTTPException(409, "Insufficient stock")
        stock[sku] -= qty

    monkeypatch.setattr(main, "reserve_stock", reserve)
    monkeypatch.setattr(main, "send_notification", lambda u, oid, m: NOTIFIED.append((u, oid)))
    with TestClient(app) as c:
        yield c


def token(c, name="sam"):
    c.post("/auth/register", json={"username": name, "password": "password123"})
    return {"Authorization": "Bearer " + c.post(
        "/auth/login", json={"username": name, "password": "password123"}).json()["access_token"]}


def test_auth_required(client):
    assert client.get("/orders").status_code in (401, 403)


def test_bad_password_rejected(client):
    token(client)
    r = client.post("/auth/login", json={"username": "sam", "password": "wrongpass1"})
    assert r.status_code == 401


def test_place_order_computes_total_and_notifies(client):
    h = token(client)
    r = client.post("/orders", json={"sku": "A1", "quantity": 2}, headers=h)
    assert r.status_code == 201 and r.json()["total"] == 20.0
    assert NOTIFIED == [("sam", r.json()["id"])]


def test_insufficient_stock(client):
    h = token(client)
    assert client.post("/orders", json={"sku": "A1", "quantity": 99}, headers=h).status_code == 409


def test_rbac_user_sees_only_own_orders(client):
    a, b = token(client, "alice"), token(client, "bobby")
    oid = client.post("/orders", json={"sku": "A1", "quantity": 1}, headers=a).json()["id"]
    assert client.get("/orders", headers=b).json() == []
    assert client.get(f"/orders/{oid}", headers=b).status_code == 404


def test_admin_sees_all_orders(client):
    a = token(client, "alice")
    client.post("/orders", json={"sku": "A1", "quantity": 1}, headers=a)
    admin = token(client, "boss")
    assert len(client.get("/orders", headers=admin).json()) == 1
