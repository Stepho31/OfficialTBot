"""
Helper functions for fetching and managing Tier-2 users for automated trading.
"""

import os
from typing import List, Dict, Optional
from dataclasses import dataclass


@dataclass
class Tier2User:
    """Represents a Tier-2 user eligible for automated trading."""
    user_id: int
    email: str
    oanda_api_key: str
    oanda_account_id: str


def get_tier2_users_for_automation() -> List[Tier2User]:
    """
    Fetch all Tier-2 users eligible for automation from the API.
    Returns a list of users with their OANDA credentials.
    
    Returns:
        List of Tier2User objects with user_id, email, oanda_api_key, and oanda_account_id.
        Returns empty list if API is unavailable or no users found.
    """
    try:
        from autopip_client import AutopipClient
        client = AutopipClient()
        users_data = client.get_tier2_users()
        
        result = []
        for user_data in users_data:
            try:
                result.append(
                    Tier2User(
                        user_id=user_data["userId"],
                        email=user_data["email"],
                        oanda_api_key=user_data["oandaApiKey"],
                        oanda_account_id=user_data["oandaAccountId"],
                    )
                )
            except (KeyError, TypeError) as e:
                print(f"[USER_HELPERS] ⚠️ Skipping invalid user data: {e}")
                continue
        
        print(f"[USER_HELPERS] ✅ Found {len(result)} Tier-2 users eligible for automation")
        return result
        
    except ImportError as e:
        print(f"[USER_HELPERS] ⚠️ Optional API integration failed: {e}")
        print("[USER_HELPERS] Continuing without syncing trades to the dashboard.")
        return []
    except Exception as e:
        print(f"[USER_HELPERS] ❌ Error fetching Tier-2 users: {e}")
        return []

