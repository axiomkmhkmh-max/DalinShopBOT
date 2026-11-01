"""Personal-test gate.

TESTING_MODE=true  -> only the Telegram user IDs in TESTER_USER_IDS may create orders / pay / top up.
TESTING_MODE=true with an EMPTY TESTER_USER_IDS -> payments are refused for everybody and an ERROR is logged.
TESTING_MODE=false -> no restriction (normal operation).
This gate never touches admin login: the dashboard keeps its own authentication and roles.
Verified money that already arrived (Zarinpal verify / Telegram payment) is still booked; only NEW payments are gated.
"""
from __future__ import annotations

import logging

from config import get_config

log = logging.getLogger(__name__)
MSG_DENIED = "ربات در حالت آزمایشی است و فعلاً فقط برای تست‌کننده فعال است."


def parse_ids(raw: str) -> set[int]:
    return {int(p) for p in str(raw or "").replace(" ", "").split(",") if p.lstrip("-").isdigit()}


def check_payment_access(user_id: int) -> tuple[bool, str]:
    cfg = get_config()
    if not cfg.testing_mode:
        return True, ""
    ids = parse_ids(cfg.tester_user_ids)
    if not ids:
        log.error("TESTING_MODE is on but TESTER_USER_IDS is empty: ALL payments are disabled. "
                  "Set TESTER_USER_IDS=<your numeric Telegram id>.")
        return False, MSG_DENIED
    if int(user_id) not in ids:
        log.info("payment denied for non-tester user %s", user_id)
        return False, MSG_DENIED
    return True, ""


def startup_report() -> None:
    cfg = get_config()
    if cfg.testing_mode:
        ids = parse_ids(cfg.tester_user_ids)
        if ids:
            log.warning("TESTING_MODE on: payments allowed only for %d tester id(s)", len(ids))
        else:
            log.error("TESTING_MODE on but TESTER_USER_IDS is empty: payments are DISABLED for everyone")
    if cfg.auto_stars_delivery:
        log.warning("AUTO_STARS_DELIVERY=true, but no official Stars sender exists: Stars orders stay manual")
