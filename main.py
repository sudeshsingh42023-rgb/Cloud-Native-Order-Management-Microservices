"""Orders service: JWT auth + RBAC, order placement across inventory & notification services."""
import hashlib
import hmac
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import jwt
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, Float, ForeignKey, Index, Integer, String, create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./orders.db")
JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-me")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
INVENTORY_URL = os.getenv("INVENTORY_URL", "http://inventory:8001")
NOTIFICATIONS_URL = os.getenv("NOTIFICATIONS_URL", "http://notifications:8003")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False)
Base = declarative_base()
log = logging.getLogger("orders")
logging.basicConfig(level=logging.INFO)
bearer = HTTPBearer()


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, index=True, nullable=False)
    password_hash = Column(String(256), nullable=False)
    role = Column(String(16), nullable=False, default="user")


class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    sku = Column(String(64), nullable=False)
    quantity = Column(Integer, nullable=False)
    total = Column(Float, nullable=False)
    status = Column(String(16), nullable=False, default="confirmed")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # composite index serves "my orders, newest first" without a sort
    __table_args__ = (Index("ix_orders_user_created", "user_id", "created_at"),)


# ---------- schemas ----------
class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8)


class OrderIn(BaseModel):
    sku: str
    quantity: int = Field(gt=0, le=1000)


class OrderOut(BaseModel):
    id: int
    sku: str
    quantity: int
    total: float
    status: str
    model_config = {"from_attributes": True}


# ---------- security helpers ----------
def hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()
    return f"{salt}${digest}"


def verify_password(pw: str, stored: str) -> bool:
    salt, digest = stored.split("$")
    check = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()
    return hmac.compare_digest(check, digest)


def make_token(user: User) -> str:
    payload = {"sub": user.username, "uid": user.id, "role": user.role,
               "exp": datetime.now(timezone.utc) + timedelta(hours=1)}
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def current_user(creds: HTTPAuthorizationCredentials = Depends(bearer)) -> dict:
    try:
        return jwt.decode(creds.credentials, JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired token")


# ---------- downstream clients (patched in tests) ----------
def fetch_product(sku: str) -> dict:
    r = httpx.get(f"{INVENTORY_URL}/products/{sku}", timeout=5)
    if r.status_code == 404:
        raise HTTPException(404, "Product not found")
    r.raise_for_status()
    return r.json()


def reserve_stock(sku: str, qty: int) -> None:
    r = httpx.post(f"{INVENTORY_URL}/products/{sku}/reserve", json={"quantity": qty}, timeout=5)
    if r.status_code == 409:
        raise HTTPException(409, "Insufficient stock")
    r.raise_for_status()


def send_notification(user: str, order_id: int, message: str) -> None:
    try:  # best effort: a notification outage must not fail the order
        httpx.post(f"{NOTIFICATIONS_URL}/notify", timeout=3,
                   json={"user": user, "order_id": order_id, "message": message})
    except httpx.HTTPError as exc:
        log.warning("notification failed: %s", exc)


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(engine)
    yield


app = FastAPI(title="Orders Service", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def timing(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    ms = (time.perf_counter() - start) * 1000
    response.headers["X-Process-Time-ms"] = f"{ms:.2f}"
    log.info("%s %s -> %s in %.1fms", request.method, request.url.path, response.status_code, ms)
    return response


@app.get("/health")
def health():
    return {"status": "ok", "service": "orders"}


@app.post("/auth/register", status_code=201)
def register(body: Credentials, db: Session = Depends(get_db)):
    if db.query(User).filter_by(username=body.username).first():
        raise HTTPException(409, "Username taken")
    role = "admin" if body.username == ADMIN_USERNAME else "user"
    db.add(User(username=body.username, password_hash=hash_password(body.password), role=role))
    db.commit()
    return {"username": body.username, "role": role}


@app.post("/auth/login")
def login(body: Credentials, db: Session = Depends(get_db)):
    user = db.query(User).filter_by(username=body.username).first()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Bad credentials")
    return {"access_token": make_token(user), "token_type": "bearer"}


@app.post("/orders", response_model=OrderOut, status_code=201)
def create_order(body: OrderIn, who: dict = Depends(current_user), db: Session = Depends(get_db)):
    product = fetch_product(body.sku)
    reserve_stock(body.sku, body.quantity)
    order = Order(user_id=who["uid"], sku=body.sku, quantity=body.quantity,
                  total=round(product["price"] * body.quantity, 2))
    db.add(order)
    db.commit()
    send_notification(who["sub"], order.id, f"Order {order.id} confirmed")
    return order


@app.get("/orders", response_model=list[OrderOut])
def list_orders(who: dict = Depends(current_user), db: Session = Depends(get_db)):
    q = db.query(Order)
    if who["role"] != "admin":  # RBAC: admins see everything, users only their own
        q = q.filter(Order.user_id == who["uid"])
    return q.order_by(Order.created_at.desc()).limit(100).all()


@app.get("/orders/{order_id}", response_model=OrderOut)
def get_order(order_id: int, who: dict = Depends(current_user), db: Session = Depends(get_db)):
    order = db.get(Order, order_id)
    if not order or (who["role"] != "admin" and order.user_id != who["uid"]):
        raise HTTPException(404, "Order not found")
    return order
