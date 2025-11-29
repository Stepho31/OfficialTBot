import os

import pytest

import OfficialTBot.main as bot_main


class FakeAutopipClient:
    def __init__(self):
        self.post_trade_called = False

    def get_entitlements(self, user_id):
        return {"canTrade": False, "canReceiveEmailSignals": True}

    def get_broker(self, user_id):
        raise AssertionError("Broker fetch should not occur when trading disabled")

    def post_trade(self, payload):
        self.post_trade_called = True

    def post_equity_snapshot(self, *args, **kwargs):
        raise AssertionError("Equity snapshot should not be posted when trading disabled")


@pytest.fixture(autouse=True)
def env_setup(monkeypatch):
    monkeypatch.setenv("AUTOPIP_USER_ID", "1")
    monkeypatch.setenv("API_BASE_URL", "https://api.example.com")
    monkeypatch.setenv("BOT_API_KEY", "key")
    monkeypatch.setenv("DRY_RUN", "false")
    yield
    for key in ["AUTOPIP_USER_ID", "API_BASE_URL", "BOT_API_KEY", "DRY_RUN"]:
        os.environ.pop(key, None)


def test_disables_trade_when_entitlement_denied(monkeypatch):
    fake_client = FakeAutopipClient()
    monkeypatch.setattr(bot_main, "AutopipClient", lambda: fake_client)
    monkeypatch.setattr(bot_main, "get_trade_ideas", lambda: [{"description": "Buy EURUSD"}])
    monkeypatch.setattr(bot_main, "filter_fresh_ideas_by_registry", lambda ideas: ideas)
    monkeypatch.setattr(
        bot_main,
        "evaluate_top_ideas",
        lambda descs: {"idea": "EURUSD setup", "score": 95, "reason": "momentum"},
    )
    monkeypatch.setattr(bot_main, "extract_direction_from_text", lambda text: "buy")
    monkeypatch.setattr(bot_main, "is_forex_pair", lambda sym: True)
    monkeypatch.setattr(bot_main, "is_trade_active", lambda symbol, direction: False)
    monkeypatch.setattr(bot_main, "get_daily_performance", lambda days: [])
    monkeypatch.setattr(bot_main, "load_log", lambda: [])
    monkeypatch.setattr(
        bot_main,
        "evaluate_trade_gate",
        lambda *args, **kwargs: {"allow": True, "blocks": []},
    )
    monkeypatch.setattr(bot_main, "_default_spread_pips", lambda symbol: 0.0001)
    monkeypatch.setattr(
        bot_main,
        "plan_trade",
        lambda symbol, direction, spread_pips: {"quality_score": 90, "risk_pct": 0.01, "exits": {"sl": 1.0, "tp1": 1.1, "tp2": 1.2, "trail_start_r": 1.0, "trail_step_pips": 5.0}},
    )
    # Prevent any downstream execution if code were to run past entitlement gate.
    monkeypatch.setattr(bot_main, "place_trade", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("place_trade should not be called")))
    monkeypatch.setattr(bot_main, "API", lambda **kwargs: object())
    monkeypatch.setattr(bot_main, "extract_instrument_from_text", lambda text, client: "EUR_USD")

    bot_main.main()

    assert not fake_client.post_trade_called, "Trade sync should not occur when canTrade is false"

