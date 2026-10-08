from __future__ import annotations

import json

API_KEY = "sk-bf-user-api-key"
VIRTUAL_KEY_ID = "vk-id-1"
ORGANIZATION_ID = "org-1"


class FakeExchange:
    """Stands in for portal-api's POST /api/api-keys/exchange."""

    def __init__(
        self,
        status: int = 200,
        body: str | None = None,
        error: Exception | None = None,
        organization_id: str = ORGANIZATION_ID,
    ) -> None:
        self.calls: list[tuple[str, dict[str, str], dict]] = []
        self.status = status
        self.body = body
        self.error = error
        self.organization_id = organization_id

    def __call__(self, url: str, headers: dict[str, str], body: bytes) -> tuple[int, str]:
        self.calls.append((url, headers, json.loads(body)))
        if self.error is not None:
            raise self.error
        if self.body is not None:
            return self.status, self.body
        return self.status, json.dumps(
            {
                "accessToken": "token",
                "userId": "user-1",
                "organizationId": self.organization_id,
                "virtualKeyId": VIRTUAL_KEY_ID,
                "tokenType": "Bearer",
            }
        )
