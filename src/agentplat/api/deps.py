from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request

from agentplat.container import Container


@dataclass(frozen=True, slots=True)
class Principal:
    tenant_id: str
    user_id: str


def get_container(request: Request) -> Container:
    c: Container = request.app.state.container
    return c


async def get_principal(
    x_tenant_id: Annotated[str | None, Header()] = None,
    x_user_id: Annotated[str | None, Header()] = None,
) -> Principal:
    """Development identity: trusts X-Tenant-Id / X-User-Id headers.

    In production these come from a verified JWT (or an API gateway that
    verified it). Everything downstream only sees a Principal, so swapping the
    mechanism touches this one function.
    """
    if not x_tenant_id or not x_user_id:
        raise HTTPException(401, "missing X-Tenant-Id / X-User-Id")
    return Principal(x_tenant_id, x_user_id)


ContainerDep = Annotated[Container, Depends(get_container)]
PrincipalDep = Annotated[Principal, Depends(get_principal)]
