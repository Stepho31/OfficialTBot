import os
import json
from datetime import datetime, timedelta
from typing import List, Dict


def _load_events(path: str) -> List[Dict]:

	if not os.path.exists(path):
		return []
	try:
		with open(path, "r") as f:
			data = json.load(f)
			return data if isinstance(data, list) else []
	except Exception:
		return []


def _instrument_currencies(instrument: str) -> List[str]:

	clean = instrument.upper().replace("_", "").replace("/", "")
	if len(clean) >= 6:
		return [clean[:3], clean[3:6]]
	return []


def is_news_blackout(instrument: str) -> bool:

	enabled = os.getenv("ENABLE_NEWS_BLACKOUT", "true").lower() == "true"
	if not enabled:
		return False

	events_file = os.getenv("NEWS_EVENTS_FILE", "news_events.json")
	impact_levels = [s.strip().lower() for s in os.getenv("NEWS_BLACKOUT_IMPACTS", "high").split(",")]
	mins_before = int(os.getenv("NEWS_BLACKOUT_MINUTES_BEFORE", "30"))
	mins_after = int(os.getenv("NEWS_BLACKOUT_MINUTES_AFTER", "15"))

	events = _load_events(events_file)
	if not events:
		return False

	now = datetime.utcnow()
	currencies = set(_instrument_currencies(instrument))

	for ev in events:
		try:
			cur = str(ev.get("currency", "")).upper()
			impact = str(ev.get("impact", "")).lower()
			start_iso = ev.get("start")
			end_iso = ev.get("end")
			if not (cur and start_iso and end_iso and impact in impact_levels):
				continue
			if cur not in currencies:
				continue
			start = datetime.fromisoformat(start_iso)
			end = datetime.fromisoformat(end_iso)
			window_start = start - timedelta(minutes=mins_before)
			window_end = end + timedelta(minutes=mins_after)
			if window_start <= now <= window_end:
				return True
		except Exception:
			continue

	return False

