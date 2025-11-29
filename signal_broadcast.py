# import os
# import json
# import smtplib
# from typing import Dict, List, Optional, Tuple
# from email.message import EmailMessage
# from datetime import datetime

# from access_control import get_all_tier1_emails

# SIGNALS_LOG_FILE = os.getenv("SIGNALS_LOG_FILE", "signals_log.json")


# def _load_signals_log() -> Dict:
#     if not os.path.exists(SIGNALS_LOG_FILE):
#         return {"sent": {}}
#     try:
#         with open(SIGNALS_LOG_FILE, "r") as f:
#             data = json.load(f)
#             if not isinstance(data, dict):
#                 return {"sent": {}}
#             data.setdefault("sent", {})
#             return data
#     except Exception:
#         return {"sent": {}}


# def _save_signals_log(log: Dict) -> None:
#     tmp_path = SIGNALS_LOG_FILE + ".tmp"
#     with open(tmp_path, "w") as f:
#         json.dump(log, f, indent=2)
#     os.replace(tmp_path, SIGNALS_LOG_FILE)


# def _get_admin_emails() -> List[str]:
#     admin_csv = os.getenv("ADMIN_EMAILS", "")
#     fallback = os.getenv("EMAIL_TO")
#     emails = []
#     if admin_csv:
#         parts = [e.strip() for e in admin_csv.split(",") if e.strip()]
#         emails.extend(parts)
#     if fallback:
#         emails.append(fallback.strip())
#     # Deduplicate while preserving order
#     seen = set()
#     uniq = []
#     for e in emails:
#         lower = e.lower()
#         if lower not in seen:
#             seen.add(lower)
#             uniq.append(e)
#     return uniq


# def _format_signal_email(signal: Dict) -> Tuple[str, str]:
#     """Return (subject, body) for a concise signal email.
#     Expected keys: signal_id, type ('OPEN'|'CLOSE'), pair, direction, entry, sl?, tp?, rationale
#     """
#     sig_type = (signal.get("type") or "OPEN").upper()
#     pair = (signal.get("pair") or "").replace("_", "").upper()
#     direction = (signal.get("direction") or "").upper()
#     entry = signal.get("entry")
#     sl = signal.get("sl")
#     tp = signal.get("tp")
#     rationale = signal.get("rationale") or ""

#     if isinstance(entry, float):
#         entry_str = f"{entry:.5f}"
#     else:
#         entry_str = str(entry) if entry is not None else "N/A"

#     subj_prefix = "Signal"
#     if sig_type == "CLOSE":
#         subject = f"[Signal CLOSE] {pair} {direction} @ {entry_str}"
#     else:
#         subject = f"[Signal OPEN] {pair} {direction} @ {entry_str}"

#     body_lines = [
#         f"Pair: {pair}",
#         f"Direction: {direction}",
#         f"Entry: {entry_str}",
#     ]
#     if sl is not None:
#         body_lines.append(f"SL: {sl:.5f}" if isinstance(sl, float) else f"SL: {sl}")
#     if tp is not None:
#         body_lines.append(f"TP: {tp:.5f}" if isinstance(tp, float) else f"TP: {tp}")
#     if rationale:
#         body_lines.append("")
#         body_lines.append(rationale.strip())

#     body = "\n".join(body_lines)
#     return subject, body


# def _send_to_many(subject: str, body: str, recipients: List[str]) -> None:
#     email_user = os.getenv("EMAIL_USER")
#     email_pass = os.getenv("EMAIL_PASS")
#     if not email_user or not email_pass:
#         print("[SIGNAL] Missing EMAIL_USER/EMAIL_PASS; cannot send broadcast.")
#         return

#     # Send individually to avoid exposing addresses and reduce per-message size
#     for rcpt in recipients:
#         try:
#             msg = EmailMessage()
#             msg["From"] = email_user
#             msg["To"] = rcpt
#             msg["Subject"] = subject
#             msg.set_content(body)

#             with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
#                 smtp.login(email_user, email_pass)
#                 smtp.send_message(msg)
#             print(f"[SIGNAL] Sent to {rcpt}: {subject}")
#         except Exception as e:
#             print(f"[SIGNAL] Failed to send to {rcpt}: {e}")


# def send_signal(signal: Dict) -> bool:
#     """Broadcast a trading signal email to ADMIN_EMAILS ∪ paid users.
#     Idempotent by signal_id: send at most once per unique signal_id.

#     Returns True if a send occurred (was not previously sent)."""
#     signal_id = signal.get("signal_id")
#     if not signal_id:
#         print("[SIGNAL] Missing signal_id; not sending.")
#         return False

#     log = _load_signals_log()
#     if signal_id in log.get("sent", {}):
#         print(f"[SIGNAL] Already sent signal_id={signal_id}; skipping.")
#         return False

#     # Build recipient list
#     admin_emails = _get_admin_emails()
#     paid_emails = get_all_tier1_emails()

#     # Union and dedupe
#     recipients = []
#     seen = set()
#     for e in admin_emails + paid_emails:
#         if not e:
#             continue
#         lower = e.strip().lower()
#         if lower not in seen:
#             seen.add(lower)
#             recipients.append(e.strip())

#     if not recipients:
#         print("[SIGNAL] No recipients found; skipping send.")
#         return False

#     subject, body = _format_signal_email(signal)
#     _send_to_many(subject, body, recipients)

#     log["sent"][signal_id] = {
#         "timestamp": datetime.utcnow().isoformat() + "Z",
#         "subject": subject,
#         "count": len(recipients)
#     }
#     _save_signals_log(log)
#     return True

# signal_broadcast.py
import os
import time
import re
from typing import Dict, List, Set, Union
import stripe
try:
    # Optional local DB fallback if Stripe metadata is not used
    from access_control import get_all_tier1_emails as _get_local_tier1
except Exception:
    _get_local_tier1 = None

from email_utils import send_email  # your existing function
from trade_cache import get_active_pairs
from trade_email_helpers import (
    send_admin_trade_notification,
    send_user_trade_signal,
)

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
ADMIN_EMAILS = [e.strip() for e in (os.getenv("ADMIN_EMAILS") or "").split(",") if e.strip()]

# Simple in-process idempotency so the same signal_id isn't sent twice in one run.
# (If you want cross-process idempotency, you can persist this to a small sqlite db.)
_SENT_IDS: Set[str] = set()


def _normalize_pair(pair: str) -> str:
    """Return uppercase pair without separators (e.g., 'EURUSD')."""
    if not pair:
        return ""
    return re.sub(r"[^A-Z]", "", str(pair).upper())


def _active_pair_blocked(pair: str, signal_type: str) -> bool:
    """Return True when notifications for this pair+type should be suppressed."""
    normalized_pair = _normalize_pair(pair)
    if not normalized_pair:
        return False

    # Only suppress OPEN/REJECT style updates while the pair is already being traded.
    if signal_type not in {"OPEN", "REJECT"}:
        return False

    try:
        active = {_normalize_pair(p) for p in get_active_pairs()}
    except Exception as exc:
        print(f"[broadcast] ⚠️ unable to read active trades for suppression: {exc}")
        return False

    return normalized_pair in active

def _fetch_paid_emails_from_api() -> List[str]:
    """Fetch emails that should receive signals from the API entitlements endpoint."""
    api_base = os.getenv("API_BASE_URL", "").rstrip("/")
    if not api_base:
        print("[broadcast] ⚠️ API_BASE_URL not set; cannot fetch entitlements from API")
        return []
    
    try:
        import requests
        resp = requests.get(
            f"{api_base}/access/paid-emails",
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("emails", [])
    except Exception as e:
        print(f"[broadcast] ⚠️ failed to fetch emails from API: {e}")
        return []


def _fetch_paid_emails() -> List[str]:
    """Return emails that should receive signals based on entitlements API.
    This respects FREE_SIGNALS_UNTIL cutoff date and proper tier checks.
    """
    emails: List[str] = []
    
    # Primary: use API entitlements endpoint (respects FREE_SIGNALS_UNTIL and tier logic)
    try:
        emails.extend(_fetch_paid_emails_from_api())
    except Exception as e:
        print(f"[broadcast] ⚠️ API fetch failed: {e}")
    
    # Fallback: try local access DB if available
    try:
        if _get_local_tier1:
            emails.extend(_get_local_tier1())
    except Exception:
        pass
    
    # de-dupe preserve order
    seen = set()
    out: List[str] = []
    for e in emails:
        if e and e not in seen:
            out.append(e)
            seen.add(e)
    return out

def _fmt_price(val: Union[float, int, str, None]) -> str:
    """Format numeric price to 5 decimals when possible; otherwise str()."""
    if val is None:
        return "N/A"
    try:
        f = float(val)
        return f"{f:.5f}"
    except Exception:
        return str(val)


def _format_human(signal: Dict) -> str:
    """
    Make a simple, human-friendly message. Expects fields:
      signal_id, type ('OPEN'/'CLOSE'), pair, direction, entry, sl, tp, rationale
    """
    s_type = (signal.get("type", "OPEN") or "OPEN").upper()
    pair = signal.get("pair", "UNKNOWN")
    direction = signal.get("direction", "")
    entry = signal.get("entry", None)
    sl = signal.get("sl", None)
    tp = signal.get("tp", None)
    rationale = signal.get("rationale", "")

    lines = []
    if s_type == "OPEN":
        lines.append(f"📈 New Trade Signal: {pair} {direction}")
        if entry is not None:
            lines.append(f"Entry: {_fmt_price(entry)}")
        if sl is not None:
            lines.append(f"Stop Loss: {_fmt_price(sl)}")
        if tp is not None:
            lines.append(f"Take Profit: {_fmt_price(tp)}")
        if rationale:
            lines.append(f"Why: {rationale}")
            # Add a plain-English summary for non-traders
            simple_open = _plain_open_summary(rationale, direction)
            if simple_open:
                lines.append(f"In simple terms: {simple_open}")
    elif s_type == "REJECT":
        lines.append(f"❌ Trade Rejected: {pair} {direction}")
        if entry is not None:
            lines.append(f"Suggested Entry: {_fmt_price(entry)}")
        if rationale:
            lines.append(f"Reason: {rationale}")
            # Translate common gate/validation jargon to plain English
            friendly = _plain_reject_reason(rationale)
            if friendly:
                lines.append(f"In simple terms: {friendly}")
    elif s_type == "CLOSE":
        lines.append(f"📉 Trade Closed: {pair} {direction}")
        if rationale:
            lines.append(f"Outcome: {rationale}")
            # Provide plain-English wrap-up
            simple = _plain_close_summary(rationale)
            if simple:
                lines.append(f"In simple terms: {simple}")
    else:
        lines.append(f"📉 Trade Update: {pair} {direction} — {s_type}")
        if rationale:
            lines.append(f"Note: {rationale}")

    return "\n".join(lines)


def _plain_reject_reason(rationale: str) -> str:
    """Convert internal gate/validation reasons to simple, trust-building language.
    Handles tokens like REGIME gate, COOLDOWN_*, STALE_IDEA, and generic validation.
    """
    if not rationale:
        return ""
    text = rationale or ""
    upper = text.upper()
    messages: List[str] = []

    # Regime/validation blockers
    if "REGIME GATE BLOCKED" in upper:
        messages.append(
            "Market conditions weren't healthy enough (trend/volatility failed our safety checks). We'll wait for a clearer setup."
        )
    if "VALIDATION FAILED" in upper:
        messages.append(
            "A last-minute check showed the setup no longer met our rules."
        )

    # Cooldown: time between similar trades
    m_time = re.search(r"COOLDOWN_TIME\(([-0-9\.]+)h<([-0-9\.]+)h\)", text, re.IGNORECASE)
    if m_time:
        try:
            hours_since = m_time.group(1)
            wait_hours = m_time.group(2)
            messages.append(
                f"We recently traded this pair {hours_since}h ago. We wait {wait_hours}h between similar trades to avoid overtrading."
            )
        except Exception:
            messages.append(
                "We recently traded this pair and are observing a cooldown to avoid overtrading."
            )

    # Cooldown: not enough price movement
    if "COOLDOWN_PRICE" in upper:
        m_pct = re.search(r"pct=([0-9\.]+)%<=([0-9\.]+)%", text, re.IGNORECASE)
        if m_pct:
            messages.append(
                f"Price hasn't moved enough since the last trade ({m_pct.group(1)}% < {m_pct.group(2)}%). We prefer fresh, higher-quality moves."
            )
        else:
            messages.append(
                "Price hasn't moved enough since the last trade. We prefer fresh, higher-quality moves."
            )

    # Stale/duplicate idea
    if "STALE_IDEA" in upper:
        m_sim = re.search(r"STALE_IDEA\(similarity=([0-9\.]+)\)", text, re.IGNORECASE)
        if m_sim:
            messages.append(
                f"This idea was too similar to a recent one (similarity {m_sim.group(1)}). We avoid repeating the same setup."
            )
        else:
            messages.append(
                "This idea was too similar to a recent one. We avoid repeating the same setup."
            )

    # Generic gate blocked catch-all
    if not messages and "GATE BLOCKED" in upper:
        messages.append("A safety rule blocked this trade to keep quality high.")

    return " ".join(messages)


def _plain_open_summary(rationale: str, direction: str) -> str:
    """Turn an OPEN rationale into simple language. Extracts score if present."""
    if not rationale:
        return ""
    # Try to extract "Auto scan score X.Y"
    score_match = re.search(r"auto\s*scan\s*score\s*([0-9]+(?:\.[0-9]+)?)", rationale, re.IGNORECASE)
    score_txt = f"{score_match.group(1)}/100" if score_match else None
    # Remove that prefix from the reason for a cleaner sentence
    simple_reason = re.sub(r"auto\s*scan\s*score\s*[0-9]+(?:\.[0-9]+)?\.?\s*", "", rationale, flags=re.IGNORECASE).strip()
    verb = "buying" if str(direction).upper() == "BUY" else ("selling" if str(direction).upper() == "SELL" else "trading")

    parts: List[str] = []
    if simple_reason:
        # Ensure sentence ends with a period
        if not simple_reason.endswith("."):
            simple_reason += "."
        parts.append(f"We're {verb} because {simple_reason}")
    if score_txt:
        parts.append(f"Confidence score: {score_txt} (higher means stronger).")
    return " ".join(parts)


def _plain_close_summary(rationale: str) -> str:
    """Simplify CLOSE outcomes for non-traders."""
    if not rationale:
        return ""
    r = rationale.strip().upper()
    if "WIN" in r or "TP" in r or "TAKE PROFIT" in r:
        return "The target was reached and the trade closed in profit."
    if "LOSS" in r or "SL" in r or "STOP" in r:
        return "The stop loss was hit, limiting the loss as planned."
    if "BREAKEVEN" in r or "BE" in r:
        return "The trade closed around entry price with minimal impact."
    if "MANUAL" in r or "MANUALLY" in r:
        return "The trade was closed manually for risk management."
    return "The trade was closed based on our rules."

def send_signal(signal: Dict) -> None:
    """
    Broadcast trade signals with separated admin diagnostic emails and user-facing signals.
    
    Behavior:
    - OPEN signals: Send admin diagnostic email + clean user signal email
    - REJECT/CLOSE signals: Send admin diagnostic email only (no user emails)
    
    Idempotent per signal_id for this process.
    
    signal = {
      "signal_id": "TRADEID:OPEN",   # required for idempotency
      "type": "OPEN" | "CLOSE" | "REJECT",
      "pair": "EURUSD",
      "direction": "BUY" | "SELL",
      "entry": 1.12345,
      "sl": 1.12000,
      "tp": 1.13000,
      "rationale": "Breakout above resistance with bullish EMA slope."
    }
    """
    sid = str(signal.get("signal_id") or "").strip()
    if not sid:
        # No idempotency key provided; still attempt to send but warn.
        print("[broadcast] ⚠️ signal_id missing; sending without idempotency.")
    elif sid in _SENT_IDS:
        print(f"[broadcast] ⏭️ already sent {sid}, skipping.")
        return

    signal_type = str(signal.get("type", "OPEN") or "OPEN").upper()
    pair = signal.get("pair", "")

    if _active_pair_blocked(pair, signal_type):
        print(f"[broadcast] ⛔ skipping {signal_type} email for active pair {pair}.")
        return

    # Map signal type to admin event type
    event_type_map = {
        "OPEN": "ACCEPTED",
        "REJECT": "REJECTED",
        "CLOSE": "CLOSED",
    }
    admin_event_type = event_type_map.get(signal_type, signal_type)

    # Extract signal data
    direction = signal.get("direction", "").upper()
    entry = signal.get("entry")
    sl = signal.get("sl")
    tp = signal.get("tp")
    rationale = signal.get("rationale", "")
    
    # Extract optional admin context fields
    score = signal.get("score")
    quality_score = signal.get("quality_score")
    trade_details = signal.get("trade_details")
    validation_errors = signal.get("validation_errors")
    gate_blocks = signal.get("gate_blocks")
    error_message = signal.get("error_message")
    
    # Build additional context from signal
    additional_context = {
        "signal_id": sid,
        "signal_type": signal_type,
    }
    # Merge any additional context from signal
    if signal.get("additional_context"):
        additional_context.update(signal.get("additional_context"))

    # Always send admin diagnostic email
    admin_sent = send_admin_trade_notification(
        event_type=admin_event_type,
        pair=pair,
        direction=direction,
        entry=entry,
        sl=sl,
        tp=tp,
        rationale=rationale,
        score=score,
        quality_score=quality_score,
        trade_details=trade_details,
        validation_errors=validation_errors,
        gate_blocks=gate_blocks,
        error_message=error_message,
        additional_context=additional_context,
    )

    # Only send user signals for OPEN trades (executed trades)
    user_sent = 0
    if signal_type == "OPEN":
        # If user_id is provided, send only to that specific user
        user_id = signal.get("user_id")
        if user_id:
            # Send to specific user only
            try:
                from autopip_client import AutopipClient
                client = AutopipClient()
                # Get user email from API (we need to fetch user details)
                # For now, we'll use the user_helpers to get user info
                from user_helpers import get_tier2_users_for_automation
                tier2_users = get_tier2_users_for_automation()
                user_email = None
                for u in tier2_users:
                    if u.user_id == user_id:
                        user_email = u.email
                        break
                
                if user_email:
                    from trade_email_helpers import format_user_trade_signal, send_email
                    subject, body = format_user_trade_signal(
                        pair=pair,
                        direction=direction,
                        entry=entry,
                        sl=sl,
                        tp=tp,
                        rationale=rationale,
                    )
                    send_email(subject, body, to=user_email)
                    user_sent = 1
                    print(f"[broadcast] ✅ Sent signal to user {user_id} ({user_email})")
                else:
                    print(f"[broadcast] ⚠️ User {user_id} not found in Tier-2 users")
            except Exception as e:
                print(f"[broadcast] ⚠️ Error sending per-user signal: {e}")
        else:
            # Send to all eligible users (legacy behavior)
            user_sent = send_user_trade_signal(
                pair=pair,
                direction=direction,
                entry=entry,
                sl=sl,
                tp=tp,
                rationale=rationale,
                signal_id=sid,
            )
    else:
        print(f"[broadcast] ℹ️ Skipping user emails for {signal_type} signal (only OPEN trades get user signals)")

    if sid:
        _SENT_IDS.add(sid)
    
    print(f"[broadcast] ✅ Signal {sid or '(no-id)'}: {admin_sent} admin emails, {user_sent} user emails")
