import asyncio
import os
import tempfile

import pytest

_TMP = tempfile.mkdtemp(prefix="bot_test_")
os.environ.update({
    "BOT_AUTOSTART": "false",
    "EXECUTOR_URL": "",
    "PAPER_MODE": "true",
    "SETTINGS_FILE": os.path.join(_TMP, "settings.json"),
    "STATS_FILE": os.path.join(_TMP, "stats.jsonl"),
    "RECOVERY_FILE": os.path.join(_TMP, "recovery.json"),
    "STATE_FILE": os.path.join(_TMP, "state.json"),
    "UPSTASH_REDIS_REST_URL": "",
    "UPSTASH_REDIS_REST_TOKEN": "",
})

import app  # noqa: E402

SYM = "FOOUSDT"


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(app, "DEFAULT_STOP_LOSS_USD", -8.0)
    monkeypatch.setattr(app, "TAKE_PROFIT_FRACTION", 0.14284)
    monkeypatch.setattr(app, "TP_OVERRIDES", {})
    monkeypatch.setattr(app, "REVENGE_ENABLED", True)
    monkeypatch.setattr(app, "REVENGE_MULT", 2.0)
    monkeypatch.setattr(app, "REVENGE_RECOVER_PCT", 50.0)
    monkeypatch.setattr(app, "UI_SETTINGS", {})


def _bot():
    b = app.TradingBot()
    b.store.writes_enabled = True
    return b


def _short(bot, entry=1.0, qty=5.0, notional=5.0, sl=-1.0, trade_id=7):
    pos = app.BotPosition(symbol=SYM, trade_id=trade_id, sl_usd=sl, sl_manual=True,
                          fills=[app.Fill(level=50, notional=notional, entry_price=entry, qty=qty)])
    bot.positions[SYM] = pos
    return pos


async def _close(bot, price, reason):
    ok = await bot._close_position(SYM, price, reason)
    while bot._bg_tasks:
        await asyncio.gather(*list(bot._bg_tasks))
    return ok


def test_sl_opens_revenge_long():
    bot = _bot()
    _short(bot)
    assert asyncio.run(_close(bot, 1.3, "SL"))          # pnl = -1.5
    rev = bot.positions[SYM]
    assert rev.side == "LONG" and rev.revenge and rev.revenge_of == 7
    assert rev.notional == pytest.approx(10.0)            # 5 × 2
    assert rev.target_usd() == pytest.approx(0.75)        # 50% de 1.5
    assert rev.sl_usd == -8.0                             # SL global
    assert rev.sl_mode == "global" and rev.tp_mode == "venganza"
    assert bot.closed_trades[0]["revenge"] is False


def test_revenge_tp_closes_and_is_marked_in_history():
    bot = _bot()
    _short(bot)
    asyncio.run(_close(bot, 1.3, "SL"))
    rev = bot.positions[SYM]
    assert rev.unrealized_pnl(1.4) > 0                    # un long gana si sube
    assert rev.price_for_pnl(rev.sl_usd) < rev.avg_entry  # su SL está por debajo
    win_px = rev.avg_entry + 0.8 / rev.qty
    assert asyncio.run(_close(bot, win_px, "TP"))
    assert SYM not in bot.positions
    row = bot.closed_trades[0]
    assert row["revenge"] is True and row["side"] == "LONG" and row["reason"] == "TP"
    assert row["pnl"] == pytest.approx(0.8, rel=1e-6)
    assert bot.trade_stats[-1]["direction"] == "LONG" and bot.trade_stats[-1]["revenge"]


def test_losing_revenge_does_not_chain():
    bot = _bot()
    _short(bot)
    asyncio.run(_close(bot, 1.3, "SL"))
    rev = bot.positions[SYM]
    lose_px = rev.avg_entry + (rev.sl_usd - 0.1) / rev.qty
    assert asyncio.run(_close(bot, lose_px, "SL"))
    assert SYM not in bot.positions


@pytest.mark.parametrize("reason,price", [("TP", 0.8), ("MANUAL", 1.3)])
def test_only_sl_triggers_revenge(reason, price):
    bot = _bot()
    _short(bot)
    asyncio.run(_close(bot, price, reason))
    assert SYM not in bot.positions


def test_disabled_revenge(monkeypatch):
    monkeypatch.setattr(app, "REVENGE_ENABLED", False)
    bot = _bot()
    _short(bot)
    asyncio.run(_close(bot, 1.3, "SL"))
    assert SYM not in bot.positions


def test_recover_pct_and_mult_are_used(monkeypatch):
    monkeypatch.setattr(app, "REVENGE_MULT", 1.0)
    monkeypatch.setattr(app, "REVENGE_RECOVER_PCT", 120.0)
    bot = _bot()
    _short(bot)
    asyncio.run(_close(bot, 1.3, "SL"))
    rev = bot.positions[SYM]
    assert rev.notional == pytest.approx(5.0)
    assert rev.target_usd() == pytest.approx(1.8)


def test_no_tranches_added_to_revenge():
    bot = _bot()
    _short(bot)
    asyncio.run(_close(bot, 1.3, "SL"))
    assert not asyncio.run(bot._ensure_short(SYM, 75, 5, 1.3, 80))


def test_tp_per_tranche_and_manual(monkeypatch):
    pos = app.BotPosition(symbol=SYM, fills=[
        app.Fill(level=50, notional=5, entry_price=1, qty=5),
        app.Fill(level=75, notional=5, entry_price=1, qty=5)])
    assert pos.target_usd() == pytest.approx(10 * 0.14284)
    monkeypatch.setattr(app, "TP_OVERRIDES", {2: 3.0})
    assert pos.target_usd() == 3.0 and pos.tp_mode == "tramo fijo"
    pos.tp_manual, pos.tp_manual_usd = True, 1.1
    assert pos.target_usd() == 1.1 and pos.tp_mode == "manual"


def test_tp_config_rows():
    bot = _bot()
    bot.set_tp_settings(fraction=0.2, tranche=3, tranche_tp=4.0)
    rows = bot.tp_config()["tranches"]
    assert rows[0]["tp"] == pytest.approx(5 * 0.2)
    assert rows[2]["tp"] == 4.0 and rows[2]["auto"] == pytest.approx(20 * 0.2)
    bot.set_tp_settings(tranche=3, clear_tranche=True)
    assert bot.tp_config()["tranches"][2]["override"] is None


def test_settings_survive_recovery_doc():
    bot = _bot()
    bot.set_revenge_settings(enabled=True, mult=2, recover_pct=100)
    bot.set_tp_settings(tranche=2, tranche_tp=1.5)
    doc, _ = bot._recovery_doc()
    app.REVENGE_RECOVER_PCT, app.TP_OVERRIDES = 50.0, {}
    import json
    bot._apply_recovery(json.loads(json.dumps(doc)), [], "test")
    assert app.REVENGE_RECOVER_PCT == 100.0 and app.TP_OVERRIDES == {2: 1.5}


def test_api_routes():
    app.bot.store.writes_enabled = True
    c = app.app.test_client()
    r = c.post("/api/set-revenge", json={"enabled": True, "mult": 2, "recover_pct": 120})
    assert r.get_json() == {"ok": True, "enabled": True, "mult": 2.0, "recover_pct": 120.0}
    assert c.post("/api/set-revenge", json={"recover_pct": -5}).status_code == 400
    assert c.post("/api/set-tp-config", json={"tranche": 1, "tp_usd": 0.9}).get_json()["ok"]
    assert c.post("/api/set-tp-config", json={"tranche": 99, "tp_usd": 1}).status_code == 400
    app.bot.positions[SYM] = app.BotPosition(symbol=SYM, fills=[
        app.Fill(level=50, notional=5, entry_price=1, qty=5)])
    assert c.post(f"/api/set-tp/{SYM}", json={"tp_usd": 2.2}).get_json()["ok"]
    assert app.bot.positions[SYM].target_usd() == 2.2
    assert c.post(f"/api/set-tp/{SYM}", json={"auto": True}).get_json()["ok"]
    assert app.bot.positions[SYM].target_usd() == 0.9
    assert c.post(f"/api/set-tp/{SYM}", json={"tp_usd": -1}).status_code == 400
    snap = c.get("/api/status").get_json()
    assert snap["revenge"]["recover_pct"] == 120.0
    assert snap["positions"][0]["tp_mode"] == "tramo fijo"
    app.bot.positions.clear()
