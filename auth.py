from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request
from pymongo.errors import DuplicateKeyError

from constants import has_permission
from db import get_db
from util import utcnow


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return salt.hex() + "$" + dk.hex()


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, dk_hex = stored.split("$")
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


async def bootstrap_owner(username: str, password: str) -> bool:
    """Creates the first Owner from env vars, only when no admin exists."""
    db = get_db()
    if await db.admin_users.count_documents({}) > 0 or not password or len(password) < 10:
        return False
    try:
        await db.admin_users.insert_one({"username": username, "password_hash": hash_password(password),
                                         "role": "owner", "active": True, "created_at": utcnow()})
    except DuplicateKeyError:
        return False
    return True


_attempts: dict[str, deque] = defaultdict(deque)


def login_rate_ok(ip: str) -> bool:
    now = time.monotonic()
    q = _attempts[ip]
    while q and now - q[0] > 300:
        q.popleft()
    return len(q) < 8


def login_failed(ip: str) -> None:
    _attempts[ip].append(time.monotonic())


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(24)
        request.session["csrf"] = tok
    return tok


def check_csrf(request: Request, token: str) -> None:
    if not token or not hmac.compare_digest(token, request.session.get("csrf", "")):
        raise HTTPException(403, "invalid CSRF token")


async def current_admin(request: Request) -> dict:
    username = request.session.get("admin")
    if not username:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    admin = await get_db().admin_users.find_one({"username": username, "active": True})
    if not admin:
        request.session.clear()
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return admin


def need(admin: dict, perm: str) -> None:
    if not has_permission(admin["role"], perm):
        raise HTTPException(403, "دسترسی کافی ندارید")
