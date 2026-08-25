"""Nexus Finance — API entry point."""
from fastapi import FastAPI

from api.middleware.audit import AuditMiddleware
from api.middleware.tenant import DEFAULT_TENANT_ID, TenantMiddleware
from api.routers import connectors
from core.graph import pg

app = FastAPI(title="Nexus Finance", version="0.1.0")

app.include_router(connectors.router)

# Tenant middleware added last so it is outermost — it validates/resolves
# the tenant identity, including rejecting malformed headers with a 400,
# before the request reaches the audit middleware or any route handler.
app.add_middleware(AuditMiddleware)
app.add_middleware(TenantMiddleware)


def require_tenant_row(conn, tenant_id: str) -> None:
    """Fail loudly when `tenant_id` has no `tenants` row.

    Never INSERTs, never calls into `core.graph.tenants`, never falls
    back to another tenant, never downgrades to a warning. See feature
    16's Owner Decision 6
    (`features/infrastructure/connectors-audit-infra.md`) — a missing
    tenant row is provisioned exclusively by feature 10c's
    tenant-bootstrap migration.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM tenants WHERE id = %s", (tenant_id,))
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(
            f"tenants row missing for tenant_id {tenant_id!r}; "
            "run feature 10c's tenant-bootstrap migration to provision it"
        )


@app.on_event("startup")
def _check_tenant_provisioned() -> None:
    if not pg.is_available():
        return
    import os

    tenant_id = os.environ.get("NEXUS_TENANT_ID", DEFAULT_TENANT_ID)
    conn = pg.connect()
    try:
        require_tenant_row(conn, tenant_id)
    finally:
        conn.close()


@app.get("/health")
def health():
    return {"status": "ok"}
