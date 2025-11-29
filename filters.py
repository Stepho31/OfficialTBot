from validators import is_forex_pair

CRYPTO_KEYWORDS = ["bitcoin", "btc", "eth", "ethereum", "crypto"]

KEYWORDS = [
    # Trade intentions
    "buy", "sell", "entry", "long", "short", "tp", "sl", "stop loss", "take profit", "target",

    # Setup language
    "setup", "formation", "pullback", "breakout", "break down", "support", "resistance",
    "bullish", "bearish", "rejection", "continuation", "reversal", "bounce", "zone",

    # Chart pattern terms
    "triangle", "channel", "trendline", "double top", "double bottom", "ascending", "descending",

    # Risk/management
    "risk", "reward", "r:r", "risk-to-reward", "1%", "position size",

    # Indicators and analysis tools
    "ichimoku", "rsi", "macd", "sma", "ema", "moving average", "volume", "momentum", "structure",

    # Scenario terms
    "scenario", "forecast", "outlook", "expect", "watching", "likely", "looking for", "anticipate",

    # Trading terms
    "swing trade", "scalp", "day trade", "entry point", "exit point", "price action", "confirmation"
]


def is_crypto_idea(text):
    lines = text.lower().splitlines()
    idea_text = "\n".join(lines[:30])
    return any(word in idea_text for word in CRYPTO_KEYWORDS)


def extract_forex_symbol(text):
    import re
    matches = re.findall(r"\b([A-Z]{3})/?([A-Z]{3})\b", text.upper())
    for match in matches:
        symbol = "".join(match)
        if is_forex_pair(symbol):
            return symbol
    return None


def rule_based_filter(description):
    description = description.lower()

    # Step 1: Reject crypto-related content
    if is_crypto_idea(description):
        print("[FILTER] ❌ Skipping: Crypto-related idea.")
        return False

    # Step 2: Try to extract a valid Forex symbol
    symbol = extract_forex_symbol(description)
    if not symbol:
        print("[FILTER] ❌ Skipping: No valid Forex symbol found.")
        return False

    print(f"[FILTER] ✅ Valid Forex symbol found: {symbol}")

    # Step 3: Match content keywords
    matched = [kw for kw in KEYWORDS if kw in description]
    print(f"[FILTER] ✅ Matched keywords: {matched}")

    return len(matched) >= 1
