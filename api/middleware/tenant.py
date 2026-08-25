"""Nexus Finance — tenant middleware (feature 16).

**This is a single-tenant PLACEHOLDER. It is NOT authentication, NOT
isolation, and NOT access control.** It resolves a tenant identity for
each request in this precedence order — `X-Nexus-Tenant` request header,
else `NEXUS_TENANT_ID` environment variable, else `DEFAULT_TENANT_ID` —
and stamps that value onto `request.state.tenant_id` so the resolved
tenant shapes writes and query filters consistently. It grants **zero
security guarantee**: the header is unauthenticated, any caller may set it
to any well-formed UUID, any caller may read another tenant's connectors
by guessing its id, and no request is ever rejected on identity or
ownership grounds. The single rejection path is a malformed-*form* `400`
— a validation error, not an authorization decision. Real authentication,
JWT claim extraction, and cross-tenant rejection arrive with feature 17
(see FOLLOW-UP 16-A in
`features/infrastructure/connectors-audit-infra.md`); until then, calling
this tenant isolation, RLS, or multi-tenancy would misstate the system's
security posture.

`DEFAULT_TENANT_ID` is a binding, not a value: it is `core.graph.pg.
BOOTSTRAP_TENANT_ID` re-exported by identity, never a UUID literal typed
here — that identity is asserted by `tests/test_audit.py` /
`tests/test_connectors_api.py` and is the seam that keeps 10a's Python
constant and 10c's bootstrap-migration SQL from drifting apart.
"""

from __future__ import annotations

import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from core.graph.pg import BOOTSTRAP_TENANT_ID

TENANT_HEADER = "X-Nexus-Tenant"
TENANT_ENV_VAR = "NEXUS_TENANT_ID"

# Binding, not a value — see module docstring. No independently-chosen
# UUID literal may originate in this feature.
DEFAULT_TENANT_ID = BOOTSTRAP_TENANT_ID


def _is_valid_uuid(candidate: str) -> bool:
    try:
        uuid.UUID(candidate)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


class TenantMiddleware(BaseHTTPMiddleware):
    """Resolves `request.state.tenant_id`. Not authentication; see module docstring."""

    async def dispatch(self, request: Request, call_next):
        import os

        header_value = request.headers.get(TENANT_HEADER)
        if header_value is not None:
            if not _is_valid_uuid(header_value):
                return JSONResponse(
                    status_code=400,
                    content={
                        "detail": (
                            f"{TENANT_HEADER} header must be a syntactically valid "
                            f"UUID; got {header_value!r}"
                        )
                    },
                )
            tenant_id = header_value
        else:
            env_value = os.environ.get(TENANT_ENV_VAR)
            if env_value:
                if not _is_valid_uuid(env_value):
                    return JSONResponse(
                        status_code=400,
                        content={
                            "detail": (
                                f"{TENANT_ENV_VAR} must be a syntactically valid "
                                f"UUID; got {env_value!r}"
                            )
                        },
                    )
                tenant_id = env_value
            else:
                tenant_id = DEFAULT_TENANT_ID

        request.state.tenant_id = tenant_id
        return await call_next(request)
