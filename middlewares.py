from __future__ import annotations

import time
from collections import defaultdict, deque

from aiogram import BaseMiddleware

from users import upsert_user


class UserMiddleware(BaseMiddleware):
    """Registers the user (Telegram already authenticated them) + simple per-user rate limit."""
    LIMIT, WINDOW = 25, 10.0

    def __init__(self):
        self.hits: dict[int, deque] = defaultdict(deque)

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None or user.is_bot:
            return await handler(event, data)
        now = time.monotonic()
        q = self.hits[user.id]
        while q and now - q[0] > self.WINDOW:
            q.popleft()
        if len(q) >= self.LIMIT:
            return None
        q.append(now)
        ref = None
        text = getattr(event, "text", None)
        if text and text.startswith("/start ") and text.split(maxsplit=1)[1].startswith("ref_"):
            ref = text.split(maxsplit=1)[1][4:]
        doc = await upsert_user(user, ref)
        if doc.get("banned"):
            return None
        data["db_user"] = doc
        return await handler(event, data)
