"""
Centralized email sending logic for trading bot.
Separates admin diagnostic emails from user-facing trade signals.
"""

import os
from typing import Dict, List, Optional, Any
from email_utils import send_email

# Cache for admin emails (from env var)
_ADMIN_EMAILS_CACHE: Optional[List[str]] = None

# Cache for user emails (from API)
_USER_EMAILS_CACHE: Optional[List[str]] = None
_USER_EMAILS_CACHE_TIMESTAMP: Optional[float] = None
_CACHE_TTL_SECONDS = 300  # 5 minutes


def get_admin_emails() -> List[str]:
    """
    Get super-admin email addresses from SIGNAL_SUPERADMIN_EMAIL or ADMIN_EMAILS env var.
    These emails receive ALL diagnostic emails (accepted, rejected, validation errors).
    
    Priority: SIGNAL_SUPERADMIN_EMAIL > ADMIN_EMAILS
    """
    global _ADMIN_EMAILS_CACHE
    
    if _ADMIN_EMAILS_CACHE is not None:
        return _ADMIN_EMAILS_CACHE
    
    # Check for dedicated super-admin email first
    superadmin = os.getenv("SIGNAL_SUPERADMIN_EMAIL", "").strip()
    if superadmin:
        _ADMIN_EMAILS_CACHE = [superadmin.lower()]
        return _ADMIN_EMAILS_CACHE
    
    # Fall back to ADMIN_EMAILS
    admin_csv = os.getenv("ADMIN_EMAILS", "")
    if not admin_csv:
        _ADMIN_EMAILS_CACHE = []
        return []
    
    # Parse comma-separated emails
    emails = []
    seen = set()
    for email in admin_csv.split(","):
        email = email.strip().lower()
        if email and email not in seen:
            emails.append(email)
            seen.add(email)
    
    _ADMIN_EMAILS_CACHE = emails
    return emails


def get_user_emails() -> List[str]:
    """
    Get normal signal recipient emails from /access/paid-emails API.
    These users only receive clean signal emails for executed trades (OPEN signals).
    Returns empty list if API is unavailable.
    """
    global _USER_EMAILS_CACHE, _USER_EMAILS_CACHE_TIMESTAMP
    
    import time
    
    # Return cached result if still valid
    if _USER_EMAILS_CACHE is not None and _USER_EMAILS_CACHE_TIMESTAMP is not None:
        age = time.time() - _USER_EMAILS_CACHE_TIMESTAMP
        if age < _CACHE_TTL_SECONDS:
            return _USER_EMAILS_CACHE
    
    # Fetch from API
    api_base = os.getenv("API_BASE_URL", "").rstrip("/")
    if not api_base:
        print("[EMAIL] ⚠️ API_BASE_URL not set; cannot fetch user emails from API")
        _USER_EMAILS_CACHE = []
        _USER_EMAILS_CACHE_TIMESTAMP = time.time()
        return []
    
    try:
        import requests
        resp = requests.get(
            f"{api_base}/access/paid-emails",
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        emails = data.get("emails", [])
        
        # Normalize to lowercase and dedupe
        normalized = []
        seen = set()
        for email in emails:
            email_lower = email.strip().lower()
            if email_lower and email_lower not in seen:
                normalized.append(email_lower)
                seen.add(email_lower)
        
        _USER_EMAILS_CACHE = normalized
        _USER_EMAILS_CACHE_TIMESTAMP = time.time()
        return normalized
    except Exception as e:
        print(f"[EMAIL] ⚠️ Failed to fetch user emails from API: {e}")
        # Return cached result if available, even if stale
        if _USER_EMAILS_CACHE is not None:
            return _USER_EMAILS_CACHE
        return []


def _format_price(val: Optional[float]) -> str:
    """Format price to 5 decimals."""
    if val is None:
        return "N/A"
    try:
        return f"{float(val):.5f}"
    except Exception:
        return str(val)


def format_user_trade_signal(
    pair: str,
    direction: str,
    entry: Optional[float],
    sl: Optional[float],
    tp: Optional[float],
    rationale: str = "",
) -> tuple[str, str]:
    """
    Format a clean trade signal email for normal users.
    Returns (subject, body) tuple.
    
    Args:
        pair: Trading pair (e.g., "EURUSD")
        direction: "BUY" or "SELL"
        entry: Entry price
        sl: Stop loss price
        tp: Take profit price
        rationale: Short explanation of why this is a good trade
    """
    pair_clean = pair.replace("_", "").upper()
    direction_upper = direction.upper()
    entry_str = _format_price(entry)
    
    subject = f"[Trade Signal] {pair_clean} {direction_upper} @ {entry_str}"
    
    body_lines = [
        f"Pair: {pair_clean}",
        f"Direction: {direction_upper}",
        f"Entry Price: {entry_str}",
    ]
    
    if sl is not None:
        body_lines.append(f"Stop Loss: {_format_price(sl)}")
    if tp is not None:
        body_lines.append(f"Take Profit: {_format_price(tp)}")
    
    if rationale:
        body_lines.append("")
        body_lines.append("Analysis:")
        body_lines.append(rationale.strip())
    
    body = "\n".join(body_lines)
    return subject, body


def format_admin_trade_notification(
    event_type: str,  # "ACCEPTED", "REJECTED", "VALIDATION_ERROR", "EXECUTION_ERROR", "CLOSED"
    pair: str,
    direction: str,
    entry: Optional[float] = None,
    sl: Optional[float] = None,
    tp: Optional[float] = None,
    rationale: str = "",
    trade_details: Optional[Dict[str, Any]] = None,
    error_message: Optional[str] = None,
    validation_errors: Optional[List[str]] = None,
    gate_blocks: Optional[List[str]] = None,
    score: Optional[float] = None,
    quality_score: Optional[float] = None,
    additional_context: Optional[Dict[str, Any]] = None,
) -> tuple[str, str]:
    """
    Format a detailed diagnostic email for super-admin.
    Returns (subject, body) tuple.
    
    This includes all diagnostic information: acceptance/rejection status,
    validation failures, error messages, full debug context.
    """
    pair_clean = pair.replace("_", "").upper()
    direction_upper = direction.upper()
    
    # Subject based on event type
    if event_type == "ACCEPTED":
        subject = f"✅ Trade ACCEPTED: {pair_clean} {direction_upper}"
    elif event_type == "REJECTED":
        subject = f"❌ Trade REJECTED: {pair_clean} {direction_upper}"
    elif event_type == "VALIDATION_ERROR":
        subject = f"⚠️ Validation FAILED: {pair_clean} {direction_upper}"
    elif event_type == "EXECUTION_ERROR":
        subject = f"💥 Execution ERROR: {pair_clean} {direction_upper}"
    elif event_type == "CLOSED":
        subject = f"📈 Trade CLOSED: {pair_clean} {direction_upper}"
    else:
        subject = f"📧 Trade Event: {pair_clean} {direction_upper} ({event_type})"
    
    body_lines = [
        f"Event Type: {event_type}",
        f"Pair: {pair_clean}",
        f"Direction: {direction_upper}",
        "",
    ]
    
    # Trade details
    if entry is not None:
        body_lines.append(f"Entry Price: {_format_price(entry)}")
    if sl is not None:
        body_lines.append(f"Stop Loss: {_format_price(sl)}")
    if tp is not None:
        body_lines.append(f"Take Profit: {_format_price(tp)}")
    
    # Scores
    if score is not None:
        body_lines.append(f"Idea Score: {score:.1f}/100")
    if quality_score is not None:
        body_lines.append(f"Quality Score: {quality_score:.1f}")
    
    # Rationale/reasoning
    if rationale:
        body_lines.append("")
        body_lines.append("Rationale:")
        body_lines.append(rationale)
    
    # Validation errors
    if validation_errors:
        body_lines.append("")
        body_lines.append("Validation Errors:")
        for err in validation_errors:
            body_lines.append(f"  ❌ {err}")
    
    # Gate blocks
    if gate_blocks:
        body_lines.append("")
        body_lines.append("Gate Blocks:")
        for block in gate_blocks:
            body_lines.append(f"  🚫 {block}")
    
    # Error message
    if error_message:
        body_lines.append("")
        body_lines.append("Error Message:")
        body_lines.append(f"  💥 {error_message}")
    
    # Full trade details
    if trade_details:
        body_lines.append("")
        body_lines.append("Full Trade Details:")
        for key, value in trade_details.items():
            if isinstance(value, (dict, list)):
                import json
                body_lines.append(f"  {key}: {json.dumps(value, indent=2)}")
            else:
                body_lines.append(f"  {key}: {value}")
    
    # Additional context
    if additional_context:
        body_lines.append("")
        body_lines.append("Additional Context:")
        for key, value in additional_context.items():
            if isinstance(value, (dict, list)):
                import json
                body_lines.append(f"  {key}: {json.dumps(value, indent=2)}")
            else:
                body_lines.append(f"  {key}: {value}")
    
    body = "\n".join(body_lines)
    return subject, body


def send_admin_trade_notification(
    event_type: str,
    pair: str,
    direction: str,
    **kwargs
) -> int:
    """
    Send detailed diagnostic email to all super-admin emails.
    
    Args:
        event_type: "ACCEPTED", "REJECTED", "VALIDATION_ERROR", "EXECUTION_ERROR", "CLOSED"
        pair: Trading pair
        direction: "BUY" or "SELL"
        **kwargs: Additional arguments passed to format_admin_trade_notification
    
    Returns:
        Number of emails sent
    """
    admin_emails = get_admin_emails()
    if not admin_emails:
        print("[EMAIL] ⚠️ No admin emails configured; skipping admin notification")
        return 0
    
    subject, body = format_admin_trade_notification(
        event_type=event_type,
        pair=pair,
        direction=direction,
        **kwargs
    )
    
    sent = 0
    for email in admin_emails:
        try:
            send_email(subject, body, to=email)
            sent += 1
        except Exception as e:
            print(f"[EMAIL] ⚠️ Failed to send admin notification to {email}: {e}")
    
    return sent


def send_user_trade_signal(
    pair: str,
    direction: str,
    entry: Optional[float],
    sl: Optional[float],
    tp: Optional[float],
    rationale: str = "",
    signal_id: Optional[str] = None,
) -> int:
    """
    Send clean trade signal email to all normal signal recipients.
    Only sends for executed trades (OPEN signals).
    
    Args:
        pair: Trading pair
        direction: "BUY" or "SELL"
        entry: Entry price
        sl: Stop loss price
        tp: Take profit price
        rationale: Short explanation
        signal_id: Optional signal ID for idempotency tracking
    
    Returns:
        Number of emails sent
    """
    user_emails = get_user_emails()
    if not user_emails:
        print("[EMAIL] ℹ️ No user emails found; skipping user signal")
        return 0
    
    # Filter out admin emails from user list (admins get admin emails, not user signals)
    admin_emails = get_admin_emails()
    admin_set = set(admin_emails)
    user_emails_filtered = [e for e in user_emails if e not in admin_set]
    
    if not user_emails_filtered:
        print("[EMAIL] ℹ️ No non-admin user emails; skipping user signal")
        return 0
    
    subject, body = format_user_trade_signal(
        pair=pair,
        direction=direction,
        entry=entry,
        sl=sl,
        tp=tp,
        rationale=rationale,
    )
    
    sent = 0
    import time
    for email in user_emails_filtered:
        try:
            send_email(subject, body, to=email)
            sent += 1
            time.sleep(0.05)  # Small delay to avoid rate limits
        except Exception as e:
            print(f"[EMAIL] ⚠️ Failed to send user signal to {email}: {e}")
    
    if signal_id:
        print(f"[EMAIL] ✅ Sent {sent} user signal emails for {signal_id}")
    else:
        print(f"[EMAIL] ✅ Sent {sent} user signal emails")
    
    return sent

