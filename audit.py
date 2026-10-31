from __future__ import annotations

from db import get_db
from util import utcnow


async def audit(actor: str, action: str, target: str = "", details: dict | None = None) -> None:
    await get_db().audit_logs.insert_one(
        {"actor": actor, "action": action, "target": target, "details": details or {}, "created_at": utcnow()}
    )
