# Cloud-Native Order Management Microservices

Three FastAPI services (orders, inventory, notifications) with JWT auth + role-based access, PostgreSQL, Docker Compose and a GitHub Actions CI/CD pipeline (tests on every push, SSH deploy to AWS EC2 from `main`).

```
client -> orders (JWT, RBAC, Postgres) -> inventory (atomic stock reservation, Postgres)
                                       -> notifications (best-effort events)
```

## Run
```bash
docker compose up --build        # orders :8002, inventory :8001, notifications :8003 (Swagger at /docs)
```
Quick flow: `POST :8001/products` -> `POST :8002/auth/register` -> `/auth/login` -> `POST :8002/orders` with the Bearer token.

## Test
```bash
cd orders && pip install -r requirements.txt pytest && python -m pytest
```

## Design notes
- `UPDATE ... WHERE stock >= qty` makes reservation atomic, so concurrent orders cannot oversell.
- Indexes: unique `sku`, composite `(user_id, created_at)` for "my recent orders".
- Notification failures never fail an order. Every response carries `X-Process-Time-ms`.
- Deploy job needs `EC2_HOST`, `EC2_USER`, `EC2_SSH_KEY` secrets and Docker installed on the instance.

## TODO before putting numbers on a resume
Load-test (Locust / `ab`) and record real latency before/after adding the index.
