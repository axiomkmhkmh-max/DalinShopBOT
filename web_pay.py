"""Public return page for Zarinpal (no admin login: the customer's browser lands here).

The page only DISPLAYS the outcome. Everything that matters happens in topups.settle(), which re-verifies with Zarinpal
on the server. A forged URL can at worst cancel a pending top-up whose authority the attacker would have to know.
"""
from __future__ import annotations

import html
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

import topups

router = APIRouter()
_hits: dict[str, deque] = defaultdict(deque)
_LIMIT, _WINDOW = 30, 60.0

MESSAGES = {
    "PAID": ("✅ پرداخت تأیید شد", "کیف پول شما شارژ شد. به ربات برگردید."),
    "ALREADY_PAID": ("✅ پرداخت قبلاً تأیید شده است", "کیف پول شما شارژ شده است. به ربات برگردید."),
    "CANCELLED": ("پرداخت لغو شد", "مبلغی از کیف پول شما کم یا زیاد نشد. می‌توانید دوباره تلاش کنید."),
    "NOT_PAID": ("پرداخت تأیید نشد", "اگر مبلغی از حساب شما کم شده، نگران نباشید: در صورت پرداخت واقعی، کیف پول تا چند دقیقه شارژ می‌شود. در غیر این صورت به پشتیبانی پیام دهید."),
    "UNAVAILABLE": ("در حال بررسی", "بررسی پرداخت هنوز ممکن نیست. اگر پرداخت کرده‌اید، نتیجه خودکار تا چند دقیقه اعمال می‌شود؛ نیازی به پرداخت مجدد نیست."),
    "UNKNOWN": ("درخواست نامعتبر", "این لینک معتبر نیست."),
}


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    doc = (f'<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">'
           f'<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>'
           f'<link rel="stylesheet" href="/static/style.css"></head><body><main class="card"><h1>{html.escape(title)}</h1>'
           f'<p>{html.escape(body)}</p></main></body></html>')
    return HTMLResponse(doc, status_code=status, headers={"Cache-Control": "no-store"})


@router.get("/pay/zarinpal/callback", response_class=HTMLResponse)
async def zarinpal_callback(request: Request, t: str = "", authority: str = Query("", alias="Authority"),
                            status: str = Query("", alias="Status")):
    ip = request.client.host if request.client else "?"
    now = time.monotonic()
    q = _hits[ip]
    while q and now - q[0] > _WINDOW:
        q.popleft()
    if len(q) >= _LIMIT:
        return _page("تعداد درخواست زیاد است", "کمی بعد دوباره تلاش کنید.", 429)
    q.append(now)
    if not (t.isalnum() and len(t) == 32):
        return _page(*MESSAGES["UNKNOWN"], status=400)
    result = await topups.settle(t, authority.strip(), status.strip())
    return _page(*MESSAGES[result.code], status=400 if result.code == "UNKNOWN" else 200)
