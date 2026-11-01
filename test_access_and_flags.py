"""Personal-test gate (TESTING_MODE / TESTER_USER_IDS), environment switches and Stars delivery flags."""
from types import SimpleNamespace

import pytest

import access, config, orders, stars_delivery, topups, wallet
import settings as st
from payment_base import ProviderDisabled
from payment_wallet import WalletProvider


def cfg(**kw):
    base = dict(testing_mode=False, tester_user_ids="", auto_stars_delivery=True, manual_stars_delivery=True,
                zarinpal_enabled=None, wallet_payments_enabled=None, custom_stars_enabled=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_gate_rules(monkeypatch):
    monkeypatch.setattr(access, "get_config", lambda: cfg())
    assert access.check_payment_access(1)[0]                                   # normal mode: open
    monkeypatch.setattr(access, "get_config", lambda: cfg(testing_mode=True, tester_user_ids="5, 9"))
    assert access.check_payment_access(5)[0] and access.check_payment_access(9)[0]
    assert not access.check_payment_access(6)[0]
    monkeypatch.setattr(access, "get_config", lambda: cfg(testing_mode=True, tester_user_ids=""))
    assert not access.check_payment_access(5)[0]                               # empty tester list: nobody pays


def test_empty_tester_list_logs_an_error(monkeypatch, caplog):
    monkeypatch.setattr(access, "get_config", lambda: cfg(testing_mode=True))
    with caplog.at_level("ERROR"):
        access.check_payment_access(5)
    assert "TESTER_USER_IDS is empty" in caplog.text


async def test_non_tester_cannot_order_pay_or_top_up(tg, monkeypatch):
    await st.set_setting("wallet_payment_enabled", "true")
    monkeypatch.setattr(access, "get_config", lambda: cfg(testing_mode=True, tester_user_ids="5"))
    with pytest.raises(orders.OrderError):
        await orders.create_order(6, tg.stars, 1, "wallet")
    o = await orders.create_order(5, tg.stars, 1, "wallet", target_username="@Bob_99")      # the tester can
    await wallet.credit(6, 10**6, idem_key="s")
    stranger = {**o, "user_id": 6}
    with pytest.raises(ProviderDisabled):
        await WalletProvider().pay(stranger)
    assert await wallet.get_balance(6) == 10**6
    await st.set_setting("zarinpal_topup_enabled", "true")
    monkeypatch.setattr(topups, "get_config", lambda: SimpleNamespace(zarinpal_merchant_id="m", base_url="https://x.test"))
    with pytest.raises(topups.TopupError):
        await topups.create_topup(6, 100_000, client=object())


async def test_stars_pre_checkout_refused_for_non_tester(tg, monkeypatch):
    from payment_manager import get_provider
    o = await orders.create_order(5, tg.stars, 1, "stars")
    monkeypatch.setattr(access, "get_config", lambda: cfg(testing_mode=True, tester_user_ids="6"))
    ok, _ = await get_provider("stars").pre_checkout(5, o["public_id"], "XTR", o["stars_amount"])
    assert not ok


async def test_env_switches_override_dashboard(db, monkeypatch):
    await st.set_setting("wallet_payment_enabled", "false")
    monkeypatch.setattr(config, "get_config", lambda: cfg(wallet_payments_enabled=True))
    assert await st.get_bool("wallet_payment_enabled")                         # ENV on beats dashboard off
    monkeypatch.setattr(config, "get_config", lambda: cfg(wallet_payments_enabled=False))
    await st.set_setting("wallet_payment_enabled", "true")
    assert not await st.get_bool("wallet_payment_enabled")                     # ENV off beats dashboard on
    monkeypatch.setattr(config, "get_config", lambda: cfg())
    assert await st.get_bool("wallet_payment_enabled")                         # unset ENV: dashboard decides


async def test_auto_stars_delivery_off_keeps_orders_manual_even_with_a_sender(tg, stars_sender, monkeypatch):
    monkeypatch.setattr(stars_delivery, "get_config", lambda: cfg(auto_stars_delivery=False))
    auto, why = await stars_delivery.auto_plan({"kind": "stars", "amount": 500}, 1)
    assert not auto and "AUTO_STARS_DELIVERY" in why


async def test_manual_delivery_disabled_blocks_stars_orders_without_a_sender(tg, monkeypatch):
    monkeypatch.setattr(stars_delivery, "get_config", lambda: cfg(manual_stars_delivery=False))
    with pytest.raises(orders.OrderError):
        await orders.create_order(5, tg.stars, 1, "wallet")
