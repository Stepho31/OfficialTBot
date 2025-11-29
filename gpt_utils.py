# Evaluates ideas using GPT

from dotenv import load_dotenv
from openai import OpenAI


import os
import json
import hashlib

load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

CACHE_FILE = "gpt_cache.json"

# Load cache if it exists
if os.path.exists(CACHE_FILE):
    with open(CACHE_FILE, "r") as f:
        gpt_cache = json.load(f)
else:
    gpt_cache = {}

def save_cache():
    with open(CACHE_FILE, "w") as f:
        json.dump(gpt_cache, f)

def hash_ideas(ideas):
    combined = "".join(ideas)
    return hashlib.sha256(combined.encode()).hexdigest()

def evaluate_trade_idea(trade_idea):
    prompt = f"""
You're a professional forex trader and trading coach. Evaluate the following trade idea for quality, clarity, and profitability. Score it from 0 to 1 (1 being the best) and explain why.

Trade Idea:
{trade_idea}

Respond in the following JSON format:
{{
    "score": 0.85,
    "reason": "Clear entry/exit, solid technicals, sound fundamentals"
}}
"""
    try:
        response = client.chat.completions.create(model="gpt-4",
        messages=[
            {"role": "system", "content": "You are a professional forex analyst."},
            {"role": "user", "content": proDmpt}
        ],
        temperature=0.7)
        content = response.choices[0].message.content.strip()
        print("[GPT RESPONSE]", content)
        return content
    except Exception as e:
        print("[GPT ERROR]", e)
        return None

def evaluate_top_ideas(ideas):
    key = hash_ideas(ideas)
    if key in gpt_cache:
        print("[GPT CACHE] Using cached GPT result.")
        return gpt_cache[key]

    prompt = (
        "You're a professional forex trader. Here are 3 trade ideas scraped from TradingView:\n\n"
        + "\n".join(f"{i+1}. {idea}" for i, idea in enumerate(ideas)) +
        "\n\nPick the single best trade idea based on clarity, technicals, and profitability.\n\n"
        "⚠️ IMPORTANT: In your JSON response, you MUST paste the FULL TEXT of the selected idea in the `idea` field.\n"
        "DO NOT write 'the second idea' or similar — copy and paste the actual idea.\n\n"
        "Respond ONLY in this JSON format:\n"
        '{ "idea": "FULL TEXT OF BEST IDEA HERE", "score": 0.87, "reason": "why it\'s the best" }'
    )



    try:
        response = client.chat.completions.create(model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.5)
        answer = response.choices[0].message.content
        parsed = json.loads(answer)
        # Validate the result before caching
        if "the second idea" in parsed.get("idea", "").lower():
            print("[GPT WARNING] Placeholder response detected — retrying without cache.")
            return evaluate_top_ideas_fresh(ideas)  # Call the retry function below

        gpt_cache[key] = parsed
        save_cache()
        return parsed

    except Exception as e:
        print("[GPT ERROR]", e)
        return None

def evaluate_top_ideas_fresh(ideas):
    prompt = (
        "You're a professional forex trader. Here are 3 trade ideas scraped from TradingView:\n\n"
        + "\n".join(f"{i+1}. {idea}" for i, idea in enumerate(ideas)) +
        "\n\nPick the single best trade idea based on clarity, technicals, and profitability.\n\n"
        "⚠️ IMPORTANT: In your JSON response, you MUST paste the FULL TEXT of the selected idea in the `idea` field.\n"
        "DO NOT write 'the second idea' or similar — copy and paste the actual idea.\n\n"
        "Respond ONLY in this JSON format:\n"
        '{ "idea": "FULL TEXT OF BEST IDEA HERE", "score": 0.87, "reason": "why it\'s the best" }'
    )
    try:
        response = client.chat.completions.create(model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.5)
        answer = response.choices[0].message.content
        parsed = json.loads(answer)
        return parsed
    except Exception as e:
        print("[GPT ERROR - FRESH]", e)
        return None
