"""Shared operator-token boundary for protected Hub features.

The existing server setting is retained for deployment compatibility. This is
one operator credential, not a user/role system; never expose its value.
"""
import secrets
from typing import Annotated

from fastapi import Header, HTTPException

from inventory_hub.settings import settings


def _require_token(authorization: str | None, *, ai: bool = False) -> None:
    expected = settings.AI_CONTENT_ACCESS_TOKEN.get_secret_value()
    prefix = "ai" if ai else "hub"
    if len(expected) < 24:
        raise HTTPException(503, detail={
            "code": f"{prefix}_access_not_configured",
            "message": "Configure the Hub operator access token on the server",
        })
    token = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
    if not secrets.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(401, detail={
            "code": f"{prefix}_access_required",
            "message": "Unlock this feature with the Hub operator access token",
        })


def operator_access(authorization: Annotated[str | None, Header()] = None) -> None:
    _require_token(authorization)


def ai_access(authorization: Annotated[str | None, Header()] = None) -> None:
    _require_token(authorization, ai=True)
