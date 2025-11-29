# Sends email alerts

import smtplib
from email.message import EmailMessage
import os
import json
import hashlib
import threading
import time


_STATE_FILE = os.getenv("EMAIL_DEDUP_CACHE", "email_state.json")
_state_lock = threading.Lock()
_state_cache = None


def _load_state():
    global _state_cache
    if _state_cache is not None:
        return _state_cache
    try:
        with open(_STATE_FILE, "r") as f:
            data = json.load(f)
            if isinstance(data, dict):
                _state_cache = data
            else:
                _state_cache = {}
    except FileNotFoundError:
        _state_cache = {}
    except Exception as e:
        print(f"[EMAIL] Warning: failed to load dedupe state: {e}")
        _state_cache = {}
    return _state_cache


def _save_state(state):
    try:
        tmp = f"{_STATE_FILE}.tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, _STATE_FILE)
    except Exception as e:
        print(f"[EMAIL] Warning: failed to save dedupe state: {e}")

def send_email(subject, body, to=None):
    """Send an email.

    If 'to' is provided, send to that recipient. Otherwise fall back to EMAIL_TO.
    This keeps backward compatibility with existing calls that don't pass 'to'.
    """
    email_user = os.getenv("EMAIL_USER")
    email_pass = os.getenv("EMAIL_PASS")
    email_to = to or os.getenv("EMAIL_TO")

    if subject is None:
        subject = ""
    if body is None:
        body = ""

    # De-duplicate emails per (recipient, subject) based on body content
    normalized_recipient = (email_to or "").strip().lower()
    dedupe_key = f"{normalized_recipient}|{subject.strip()}"
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

    with _state_lock:
        state = _load_state()
        cached = state.get(dedupe_key)
        if cached and cached.get("body_hash") == body_hash:
            print(f"[EMAIL SKIP] Duplicate content for {subject} → {email_to}")
            return False

    if not all([email_user, email_pass, email_to]):
        print("[ERROR] Missing email credentials or recipient in environment variables.")
        return

    try:
        msg = EmailMessage()
        msg["From"] = email_user
        msg["To"] = email_to
        msg["Subject"] = subject
        msg.set_content(body)

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(email_user, email_pass)
            smtp.send_message(msg)

        print("[EMAIL SENT] " + subject + f" → {email_to}")

        with _state_lock:
            state = _load_state()
            state[dedupe_key] = {
                "body_hash": body_hash,
                "updated_at": time.time(),
            }
            _save_state(state)

        return True
    except Exception as e:
        print("[EMAIL FAILED]", e)
        return False