from fastapi.testclient import TestClient
from app.main import app, SENT

client = TestClient(app)


def test_notify_and_list():
    SENT.clear()
    r = client.post("/notify", json={"user": "sam", "order_id": 1, "message": "Order placed"})
    assert r.status_code == 202
    assert client.get("/notifications").json()[0]["order_id"] == 1


def test_notify_validates_payload():
    assert client.post("/notify", json={"user": "sam"}).status_code == 422
