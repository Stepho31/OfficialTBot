from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
import time
import re
import json
from typing import List, Dict

CRYPTO_KEYWORDS = [
    "bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "sol", 
    "doge", "xrp", "binance", "bnb", "shiba", "cardano", "ada", 
    "usdt", "tether", "crypto.com", "coinbase", "cryptoexchange"
]

FOREX_PAIRS = [
    "EUR/USD", "USD/JPY", "GBP/USD", "USD/CHF", "AUD/USD", "USD/CAD", "NZD/USD", "XAU/USD",
    "EURUSD", "USDJPY", "GBPUSD", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD", "XAUUSD"
]

NAV_WORDS = {"search", "products", "community", "markets", "brokers", "more"}

def looks_like_nav(text: str) -> bool:
    words = {w.lower() for w in re.findall(r"[A-Za-z]+", text)}
    return len(words & NAV_WORDS) >= 3

def is_crypto_idea(text):
    lowered = text.lower()
    for word in CRYPTO_KEYWORDS:
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            print(f"[DEBUG] Marked as crypto due to word match: {word}")
            return True
    if re.search(r"\b(btc|eth|sol|xrp|bnb|shib|ada)[/ ]?usdt?\b", lowered):
        print(f"[DEBUG] Marked as crypto due to pair format match")
        return True
    return False

def contains_forex_pair(text):
    normalized_text = text.lower().replace("/", "").replace(" ", "").replace(".", "")
    return any(pair.lower().replace("/", "").replace(" ", "").replace(".", "") in normalized_text for pair in FOREX_PAIRS)

def _regex_idea_links_from_html(html: str) -> List[str]:
    # Find /ideas/... links even if the DOM isn't hydrated
    candidates = set()
    for m in re.finditer(r'"/ideas/[a-z0-9\-_/]*/"', html, flags=re.I):
        path = m.group(0).strip('"')
        # ignore image or asset paths that sometimes sneak in
        if path.endswith(('.png', '.jpg', '.jpeg', '.webp', '.svg')):
            continue
        candidates.add("https://www.tradingview.com" + path)
    return list(candidates)

def _extract_article_from_json_scripts(page) -> str:
    """Fallback: scan all <script type='application/ld+json'> and __NEXT_DATA__ for article body/description."""
    texts = []
    try:
        for el in page.locator("script[type='application/ld+json']").all():
            try:
                data = json.loads(el.inner_text() or "{}")
                # Some pages wrap in a list
                for node in (data if isinstance(data, list) else [data]):
                    if isinstance(node, dict):
                        # Common fields for articles
                        for key in ("articleBody", "description"):
                            if key in node and isinstance(node[key], str):
                                texts.append(node[key])
            except Exception:
                continue
    except Exception:
        pass

    # __NEXT_DATA__ (Next.js) often contains the article content too
    try:
        nd = page.locator("script#__NEXT_DATA__").first
        if nd and nd.count():
            data = json.loads(nd.inner_text() or "{}")
            # Generic search for string fields that look like article bodies
            def walk(obj):
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        if isinstance(v, str) and len(v) > 120 and re.search(r"[A-Za-z]{20}", v):
                            if k.lower() in ("content", "articlebody", "text", "body", "description"):
                                texts.append(v)
                        else:
                            walk(v)
                elif isinstance(obj, list):
                    for it in obj:
                        walk(it)
            walk(data)
    except Exception:
        pass

    if not texts:
        return ""
    # Pick the longest plausible text
    best = max(texts, key=len)
    return re.sub(r"\s+", " ", best).strip()

def get_trade_ideas(limit: int = 10):
    def goto_with_retry(page, url: str, tries: int = 3):
        for i in range(1, tries + 1):
            try:
                page.goto(url, wait_until="networkidle", timeout=180_000)
                return
            except PWTimeout:
                if i == tries:
                    raise
                page.wait_for_timeout(3000 * i)  # simple backoff

    with sync_playwright() as p:
        # Hardened launch flags to reduce bot-detection flakiness
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-zygote",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36"),
            viewport={"width": 1366, "height": 768},
            java_script_enabled=True,
            locale="en-US",
        )
        # Remove webdriver flag
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")

        page = context.new_page()
        page.set_default_navigation_timeout(180_000)
        page.set_default_timeout(60_000)

        # 1) Open the ideas feed
        goto_with_retry(page, "https://www.tradingview.com/ideas/forex/")

        # Best-effort cookie/consent dismissal (won't crash if not present)
        try:
            page.locator("#onetrust-accept-btn-handler").click(timeout=2000)
        except Exception:
            pass
        for txt in ("Accept all", "Allow all", "I agree", "Accept", "Got it"):
            try:
                page.get_by_text(txt, exact=False).first.click(timeout=1500)
                break
            except Exception:
                pass

        # Wait for idea cards/links to exist; if not, fallback to regex
        idea_selectors = [
            "a[data-name='card-title']",
            "a[data-name='content-card-title']",
            "div[data-widget-name='idea-card'] a[href^='/ideas/']",
            "article:has(a[href^='/ideas/']) a[href^='/ideas/']",
        ]

        found_selector = None
        for _ in range(3):  # try a few scroll cycles
            for sel in idea_selectors:
                try:
                    if page.locator(sel).count():
                        found_selector = sel
                        break
                except Exception:
                    pass
            if found_selector:
                break
            # scroll to trigger lazy load
            for _ in range(6):
                page.mouse.wheel(0, 800)
                page.wait_for_timeout(250)
            page.wait_for_timeout(1000)

        links, final_ideas = [], []

        if found_selector:
            anchors = page.query_selector_all(found_selector)
            for a in anchors:
                href = (a.get_attribute("href") or "").strip()
                if not href or not href.startswith("/"):
                    try:
                        a2 = a.evaluate_handle("el => el.closest('a')")
                        if a2:
                            href2 = a2.get_attribute("href")
                            if href2:
                                href = href2.strip()
                    except Exception:
                        pass
                if href and href.startswith("/ideas/"):
                    href = f"https://www.tradingview.com{href}"
                    if href not in links:
                        links.append(href)
                if len(links) >= max(30, limit * 3):
                    break
            print(f"[SCRAPER] Total idea links collected (DOM): {len(links)}")

        # Fallback: no DOM cards found; parse HTML for /ideas/ links
        if not links:
            html = page.content() or ""
            links = _regex_idea_links_from_html(html)
            print(f"[SCRAPER] Total idea links collected (HTML regex): {len(links)}")
            if not links:
                try:
                    page.screenshot(path="tv_ideas_timeout.png", full_page=True)
                except Exception:
                    pass
                preview = html[:600]
                print("[SCRAPER] Could not find idea cards. HTML preview:\n", preview)
                browser.close()
                raise PWTimeout("Idea cards not found; see tv_ideas_timeout.png")

        # 3) Visit links and extract text defensively
        for link in links:
            try:
                goto_with_retry(page, link, tries=2)
                page.wait_for_timeout(1200)  # small settle time

                # Title (pair)
                pair_title = ""
                try:
                    h1 = page.locator("h1").first
                    if h1 and h1.count():
                        pair_title = h1.inner_text().strip()
                except Exception:
                    pass

                # Primary attempt: DOM article body
                article_text = ""
                try:
                    page.wait_for_selector("[itemprop='articleBody'], article", timeout=15_000)
                    block = page.locator("[itemprop='articleBody'], article")
                    if block.count():
                        article_text = block.first.inner_text().strip()
                        article_text = re.sub(r"\s+", " ", article_text)
                except Exception:
                    pass

                # Fallback: parse JSON scripts for article body/description
                if len(article_text) < 120:
                    article_text = _extract_article_from_json_scripts(page)

                combined_text = f"{pair_title} {article_text}"
                print(f"\n[SCRAPER] Header: {pair_title}")
                print(f"[SCRAPER] Raw scraped idea:\n{article_text[:300]}")
                print(f"[DEBUG] Contains Forex Pair: {contains_forex_pair(combined_text)} | Is Crypto: {is_crypto_idea(combined_text)}")

                # Basic sanity checks
                if len(article_text) < 150 or looks_like_nav(article_text):
                    print("[SCRAPER] Skipped: too short or looks like navigation text.\n")
                    continue

                if not is_crypto_idea(combined_text) and contains_forex_pair(combined_text):
                    final_ideas.append({"url": link, "title": pair_title, "description": article_text})
                else:
                    print("[SCRAPER] Skipped: Not a valid Forex pair or is crypto-related.\n")

                if len(final_ideas) >= limit:
                    break

            except Exception as e:
                print(f"[SCRAPER] Skipping {link} due to error: {e}")
                continue

        browser.close()
        return final_ideas
