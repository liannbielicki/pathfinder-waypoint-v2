"""Client for the existing n8n/Snowflake context flow.

n8n holds the Snowflake credential; Waypoint holds none. Only the versioned,
allowlisted, PII-safe brief crosses this boundary (extra fields are rejected,
never stored). Redirects are refused so the bearer token is never forwarded.
"""

from decimal import Decimal
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field


class ContextUnavailable(Exception):
    """The context flow could not produce a valid batch. Explicit, never empty."""


class OrgBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pro_id: str
    org_id: str
    open_invoice_count: int = Field(ge=0)
    open_due_usd: Decimal = Field(ge=0)
    lifecycle_stage: str


class OrgContextBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["org_context_v1"]
    organizations: list[OrgBrief]


class N8NContextClient:
    def __init__(
        self,
        url: str,
        token: str,
        batch_size: int = 5,
        timeout: float = 120.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        # ponytail: 5-id batches match the existing n8n validate-node cap.
        self.url = url
        self.batch_size = batch_size
        self._client = client or httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            headers={"authorization": f"Bearer {token}"},
        )

    async def fetch(self, pro_ids: list[str]) -> OrgContextBatch:
        organizations: list[OrgBrief] = []
        for start in range(0, len(pro_ids), self.batch_size):
            chunk = pro_ids[start : start + self.batch_size]
            try:
                response = await self._client.post(self.url, json={"pro_ids": chunk})
            except httpx.HTTPError as error:
                raise ContextUnavailable(f"n8n context flow unreachable: {error}") from error
            if response.status_code != 200:
                raise ContextUnavailable(
                    f"n8n context flow returned {response.status_code} for {len(chunk)} ids"
                )
            try:
                batch = OrgContextBatch.model_validate_json(response.content)
            except ValueError as error:
                raise ContextUnavailable(f"n8n context contract violation: {error}") from error
            organizations.extend(batch.organizations)
        return OrgContextBatch(contract_version="org_context_v1", organizations=organizations)
