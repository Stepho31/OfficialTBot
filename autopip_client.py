import os
import requests
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


class AutopipClient:
    def __init__(self) -> None:
        base = os.getenv("AUTOPIP_API_BASE_URL")
        if not base:
            raise RuntimeError("AUTOPIP_API_BASE_URL is not set")
        self.base_url = base.rstrip("/")
        self.bot_key = os.getenv("BOT_API_KEY")
        if not self.bot_key:
            raise RuntimeError("BOT_API_KEY is required for AutopipClient")
        
        print(
            f"[AutopipClient] Using BOT_API_KEY prefix={self.bot_key[:4]!r} "
            f"len={len(self.bot_key)} base_url={self.base_url}"
        )

    def _headers(self) -> Dict[str, str]:
        return {"x-bot-key": self.bot_key, "Content-Type": "application/json"}

    def get_entitlements(self, user_id: int) -> Dict[str, Any]:
        resp = requests.get(
            f"{self.base_url}/v1/internal/entitlements",
            params={"userId": user_id},
            headers={"x-bot-key": self.bot_key},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def get_broker(self, user_id: int) -> Dict[str, str]:
        resp = requests.get(
            f"{self.base_url}/v1/internal/broker",
            params={"userId": user_id},
            headers={"x-bot-key": self.bot_key},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def post_trade(self, payload: Dict[str, Any]) -> None:
        resp = requests.post(
            f"{self.base_url}/v1/internal/trades",
            json=payload,
            headers=self._headers(),
            timeout=10,
        )
        resp.raise_for_status()

    def post_equity_snapshot(
        self,
        user_id: int,
        oanda_account_id: str,
        balance: float,
        equity: Optional[float] = None,
        margin_used: Optional[float] = None,
        timestamp: Optional[datetime] = None,
    ) -> None:
        payload = {
            "userId": user_id,
            "oandaAccountId": oanda_account_id,
            "timestamp": (timestamp or datetime.now(timezone.utc)).isoformat(),
            "balance": balance,
            "equity": equity if equity is not None else balance,
            "marginUsed": margin_used if margin_used is not None else 0.0,
        }
        resp = requests.post(
            f"{self.base_url}/v1/internal/equity",
            json=payload,
            headers=self._headers(),
            timeout=10,
        )
        resp.raise_for_status()

    def get_tier2_users(self) -> List[Dict[str, Any]]:
        """Fetch all Tier-2 users eligible for automation with their broker credentials."""
        resp = requests.get(
            f"{self.base_url}/v1/internal/tier2-users",
            headers={"x-bot-key": self.bot_key},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("users", [])

    def get_user_settings(self, user_id: int) -> Dict[str, Any]:
        """Fetch user settings including trade_allocation."""
        resp = requests.get(
            f"{self.base_url}/v1/internal/user-settings",
            params={"userId": user_id},
            headers={"x-bot-key": self.bot_key},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def get_weekly_trades(self, from_dt: str, to_dt: str) -> List[Dict[str, Any]]:
        """
        Fetch all closed trades for a date range (for weekly reports).
        Returns trades from the database instead of file system.
        """
        resp = requests.get(
            f"{self.base_url}/v1/internal/weekly-trades",
            params={"from_dt": from_dt, "to_dt": to_dt},
            headers={"x-bot-key": self.bot_key},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

