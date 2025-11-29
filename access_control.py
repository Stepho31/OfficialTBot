import json
import os
from datetime import datetime
from typing import Dict, List, Optional

ACCESS_DB_FILE = os.getenv("TIER1_DB_FILE", "tier1_access.json")


def _normalize_email(email: Optional[str]) -> Optional[str]:
    if not email:
        return None
    return email.strip().lower()


def _load_db() -> Dict:
    if not os.path.exists(ACCESS_DB_FILE):
        return {"users": {}, "purchases": {}}
    try:
        with open(ACCESS_DB_FILE, "r") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return {"users": {}, "purchases": {}}
            # Ensure required keys
            data.setdefault("users", {})
            data.setdefault("purchases", {})
            return data
    except Exception:
        return {"users": {}, "purchases": {}}


def _save_db(db: Dict) -> None:
    tmp_path = ACCESS_DB_FILE + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(db, f, indent=2)
    os.replace(tmp_path, ACCESS_DB_FILE)


def grant_tier1(email: str, customer_id: Optional[str], purchase_id: Optional[str]) -> bool:
    """Grant Tier 1 access to an email.
    Idempotent on purchase_id: if already processed, returns False (no changes).

    Returns True if access state changed or a new purchase was recorded, else False.
    """
    normalized = _normalize_email(email)
    if not normalized:
        return False

    db = _load_db()

    # Idempotency on purchase
    if purchase_id:
        if purchase_id in db["purchases"]:
            return False

    user = db["users"].get(normalized, {
        "email": normalized,
        "active": False,
        "customer_id": None,
        "purchases": [],
        "revocations": []
    })

    changed = False

    # Record purchase if provided
    if purchase_id:
        db["purchases"][purchase_id] = {
            "email": normalized,
            "customer_id": customer_id,
            "timestamp": datetime.utcnow().isoformat() + "Z"
        }
        user["purchases"].append({
            "purchase_id": purchase_id,
            "timestamp": datetime.utcnow().isoformat() + "Z"
        })
        changed = True

    # Update customer_id if newly provided
    if customer_id and user.get("customer_id") != customer_id:
        user["customer_id"] = customer_id
        changed = True

    # Activate access
    if not user.get("active", False):
        user["active"] = True
        changed = True

    db["users"][normalized] = user
    if changed:
        _save_db(db)
    return changed


def revoke_tier1(email: str, reason: str = "revoked") -> bool:
    """Revoke Tier 1 access for an email. Returns True if state changed."""
    normalized = _normalize_email(email)
    if not normalized:
        return False

    db = _load_db()
    user = db["users"].get(normalized)
    if not user:
        # Create a tombstone record so we have a trail
        user = {
            "email": normalized,
            "active": False,
            "customer_id": None,
            "purchases": [],
            "revocations": []
        }

    changed = False
    if user.get("active", False):
        user["active"] = False
        changed = True

    user.setdefault("revocations", []).append({
        "reason": reason,
        "timestamp": datetime.utcnow().isoformat() + "Z"
    })

    db["users"][normalized] = user
    if changed:
        _save_db(db)
    else:
        # Save anyway to capture revocation reason trail
        _save_db(db)
    return changed


def has_tier1(email: str) -> bool:
    normalized = _normalize_email(email)
    if not normalized:
        return False
    db = _load_db()
    user = db["users"].get(normalized)
    return bool(user and user.get("active", False))


def get_all_tier1_emails() -> List[str]:
    db = _load_db()
    return [email for email, u in db.get("users", {}).items() if u.get("active", False)]
