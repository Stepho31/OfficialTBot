
"""
Market Sentiment Analyzer for Enhanced 4H Trading
Integrates key market indicators for better trade timing
"""

import os
import requests
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple
from dataclasses import dataclass

@dataclass
class MarketSentiment:
    """Market sentiment indicators"""
    dxy_trend: str          # USD strength trend
    dxy_level: float        # Current DXY level
    vix_level: float        # Market fear/volatility
    risk_sentiment: str     # 'risk_on', 'risk_off', 'neutral'
    bond_yield_trend: str   # Treasury yield trend
    overall_score: float    # Composite sentiment score (-100 to +100)
    confidence: str         # 'high', 'medium', 'low'
    timestamp: datetime

class MarketSentimentAnalyzer:
    """Analyzes broader market sentiment for forex trading context"""
    
    def __init__(self):
        self.api_key = os.getenv("TWELVE_DATA_API_KEY")
        self.fred_api_key = os.getenv("FRED_API_KEY")  # For economic data (unused here)
        self.cache = {}
        self.cache_expiry = timedelta(hours=1)  # Cache for 1 hour
        
    def get_market_sentiment(self) -> Optional[MarketSentiment]:
        """Get comprehensive market sentiment analysis"""
        try:
            # Check cache first
            if self._is_cache_valid():
                print("[SENTIMENT] Using cached sentiment data")
                return self.cache.get("sentiment")
            
            print("[SENTIMENT] 📊 Analyzing market sentiment...")
            
            # Get individual indicators
            dxy_data = self._get_dxy_sentiment()
            vix_data = self._get_vix_sentiment()
            risk_sentiment = self._analyze_risk_sentiment(dxy_data, vix_data)
            bond_data = self._get_bond_yield_sentiment()
            
            if not any([dxy_data, vix_data, bond_data]):
                print("[SENTIMENT] ❌ Failed to get sufficient sentiment data")
                return None
            
            # Calculate composite sentiment
            sentiment = self._calculate_composite_sentiment(
                dxy_data, vix_data, risk_sentiment, bond_data
            )
            
            # Cache the result
            self._cache_sentiment(sentiment)
            
            self._print_sentiment_summary(sentiment)
            
            return sentiment
            
        except Exception as e:
            print(f"[SENTIMENT] ❌ Error analyzing market sentiment: {e}")
            return None

    def _resolve_symbol(self, primary_query: str, prefer_type: str = "Index",
                       fallbacks: list[str] | None = None) -> Optional[str]:
        """Find a Twelve Data symbol by query, preferring instrument_type (e.g., 'Index').
        If not found or not available on plan, try known fallbacks (ETF/ETN proxies)."""
        fallbacks = fallbacks or []
        try:
            r = requests.get(
                "https://api.twelvedata.com/symbol_search",
                params={"symbol": primary_query, "apikey": self.api_key},
                timeout=10
            )
            data = r.json()
            # Prefer exact instrument type (Index)
            if isinstance(data, dict) and "data" in data:
                for item in data["data"]:
                    if item.get("instrument_type") == prefer_type:
                        return item.get("symbol")
            # Otherwise accept ETF/ETN as a proxy
            if isinstance(data, dict) and "data" in data:
                for item in data["data"]:
                    if item.get("instrument_type") in ("ETF", "ETN"):
                        return item.get("symbol")
        except Exception as e:
            print(f"[SENTIMENT] ⚠️ symbol_search error for {primary_query}: {e}")

        # Try hard-coded fallbacks by probing if they return values
        for fb in fallbacks:
            try:
                t = requests.get(
                    "https://api.twelvedata.com/time_series",
                    params={"symbol": fb, "interval": "1day", "outputsize": 2, "apikey": self.api_key},
                    timeout=8
                ).json()
                if "values" in t:
                    return fb
            except Exception:
                pass
        
        return None
    
    def _get_dxy_sentiment(self) -> Optional[Dict]:
        """Analyze USD strength via DXY (with resolver + proxies)."""
        try:
            if not self.api_key:
                print("[SENTIMENT] ⚠️ Missing TwelveData API key for DXY")
                return self._get_fallback_dxy()

            symbol = self._resolve_symbol(
                primary_query="US Dollar Index",
                prefer_type="Index",
                fallbacks=["USDX", "DXY", "UUP"]  # UUP = USD bullish ETF proxy
            )
            if not symbol:
                print("[SENTIMENT] ⚠️ Could not resolve DXY symbol; using fallback")
                return self._get_fallback_dxy()

            response = requests.get(
                "https://api.twelvedata.com/time_series",
                params={"symbol": symbol, "interval": "4h", "outputsize": "50", "apikey": self.api_key},
                timeout=12
            )

            if response.status_code != 200:
                print(f"[SENTIMENT] ⚠️ DXY API error ({response.status_code}), using fallback")
                return self._get_fallback_dxy()

            data = response.json()
            if "values" not in data or not data["values"]:
                print("[SENTIMENT] ⚠️ Invalid DXY response (no 'values'), using fallback")
                print("[DEBUG] DXY raw response:", data)
                return self._get_fallback_dxy()

            values = data["values"]
            if len(values) < 20:
                print("[SENTIMENT] ⚠️ DXY series too short, using fallback")
                return self._get_fallback_dxy()

            current_price = float(values[0]["close"])
            prices = [float(v["close"]) for v in values[:20]]

            short_trend = 0.0 if prices[4] == 0 else (prices[0] - prices[4]) / prices[4] * 100.0
            medium_trend = 0.0 if prices[19] == 0 else (prices[0] - prices[19]) / prices[19] * 100.0

            if short_trend > 0.4 and medium_trend > 0.8:
                trend = "strong_bullish"
            elif short_trend > 0.15 or medium_trend > 0.35:
                trend = "bullish"
            elif short_trend < -0.4 and medium_trend < -0.8:
                trend = "strong_bearish"
            elif short_trend < -0.15 or medium_trend < -0.35:
                trend = "bearish"
            else:
                trend = "neutral"

            return {
                "level": current_price,
                "trend": trend,
                "short_change": short_trend,
                "medium_change": medium_trend
            }

        except Exception as e:
            print(f"[SENTIMENT] ❌ Error getting DXY data: {e}")
            return self._get_fallback_dxy()
    
    def _get_fallback_dxy(self) -> Dict:
        """Fallback DXY analysis when API unavailable"""
        # Simple fallback - assume neutral conditions
        return {
            "level": 104.0,  # Approximate recent level
            "trend": "neutral",
            "short_change": 0.0,
            "medium_change": 0.0
        }
    
    def _get_vix_sentiment(self) -> Optional[Dict]:
        """Analyze market volatility via VIX (with resolver + proxies)."""
        try:
            if not self.api_key:
                print("[SENTIMENT] ⚠️ Missing TwelveData API key for VIX")
                return self._get_fallback_vix()

            symbol = self._resolve_symbol(
                primary_query="VIX",
                prefer_type="Index",
                fallbacks=["CBOE:VIX", "^VIX", "VIXY", "VXX"]  # ETF/ETN proxies as last resort
            )
            if not symbol:
                print("[SENTIMENT] ⚠️ Could not resolve VIX symbol; using fallback")
                return self._get_fallback_vix()

            response = requests.get(
                "https://api.twelvedata.com/time_series",
                params={"symbol": symbol, "interval": "4h", "outputsize": "20", "apikey": self.api_key},
                timeout=12
            )

            if response.status_code != 200:
                print(f"[SENTIMENT] ⚠️ VIX API error ({response.status_code}), using fallback")
                return self._get_fallback_vix()

            data = response.json()
            if "values" not in data or not data["values"]:
                print("[SENTIMENT] ⚠️ Invalid VIX response (no 'values'), using fallback")
                print("[DEBUG] VIX raw response:", data)
                return self._get_fallback_vix()

            current_vix = float(data["values"][0]["close"])

            if current_vix > 30:
                fear_level = "high_fear"
            elif current_vix > 22:
                fear_level = "elevated_fear"
            elif current_vix > 15:
                fear_level = "normal"
            else:
                fear_level = "complacency"

            return {"level": current_vix, "fear_level": fear_level}

        except Exception as e:
            print(f"[SENTIMENT] ❌ Error getting VIX data: {e}")
            return self._get_fallback_vix()
    
    def _get_fallback_vix(self) -> Dict:
        """Fallback VIX when API unavailable"""
        return {
            "level": 18.0,  # Approximate normal level
            "fear_level": "normal"
        }
    
    def _analyze_risk_sentiment(self, dxy_data: Optional[Dict], vix_data: Optional[Dict]) -> str:
        """
        Heuristic risk sentiment:
        - Lower VIX + USD weakening → risk_on
        - Higher VIX + USD strengthening → risk_off
        - Otherwise neutral
        """
        try:
            if not dxy_data or not vix_data:
                return "neutral"
            
            vix = vix_data.get("level", 18.0)
            dxy_trend = dxy_data.get("trend", "neutral")

            # Clear risk-off: rising fear + stronger USD
            if vix >= 22 and dxy_trend in ("bullish", "strong_bullish"):
                return "risk_off"
            # Clear risk-on: low fear + weaker USD
            if vix <= 16 and dxy_trend in ("bearish", "strong_bearish"):
                return "risk_on"
            
            return "neutral"
        except Exception as e:
            print(f"[SENTIMENT] ❌ Error analyzing risk sentiment: {e}")
            return "neutral"
    
    def _get_bond_yield_sentiment(self) -> Optional[Dict]:
        """Analyze bond yield trends (placeholder, neutral by default)"""
        try:
            # This would typically use FRED API for Treasury yields
            return {
                "trend": "neutral",
                "level": 4.5  # Approximate current 10Y yield
            }
        except Exception as e:
            print(f"[SENTIMENT] ❌ Error getting bond data: {e}")
            return {"trend": "neutral", "level": 4.5}
    
    def _calculate_composite_sentiment(self, dxy_data: Dict, vix_data: Dict, 
                                     risk_sentiment: str, bond_data: Dict) -> MarketSentiment:
        """Calculate composite market sentiment score"""
        
        score = 0.0
        confidence_factors = 0.0
        
        # -------------------------
        # CATEGORICAL CONTRIBUTIONS
        # -------------------------
        # DXY contribution (±30 points)
        if dxy_data:
            dxy_trend = dxy_data.get("trend", "neutral")
            if dxy_trend == "strong_bullish":
                score += 30; confidence_factors += 1
            elif dxy_trend == "bullish":
                score += 15; confidence_factors += 0.5
            elif dxy_trend == "strong_bearish":
                score -= 30; confidence_factors += 1
            elif dxy_trend == "bearish":
                score -= 15; confidence_factors += 0.5
        
        # VIX contribution (±25 points)
        if vix_data:
            fear_level = vix_data.get("fear_level", "normal")
            if fear_level == "high_fear":
                score -= 25; confidence_factors += 1
            elif fear_level == "elevated_fear":
                score -= 10; confidence_factors += 0.5
            elif fear_level == "complacency":
                score += 10; confidence_factors += 0.5  # modest boost
        
        # Risk sentiment contribution (±20 points)
        if risk_sentiment == "risk_on":
            score += 20; confidence_factors += 1
        elif risk_sentiment == "risk_off":
            score -= 20; confidence_factors += 1
        
        # Bond yield contribution (±15 points)
        if bond_data:
            bond_trend = bond_data.get("trend", "neutral")
            if bond_trend == "rising":
                score += 10  # USD positive
            elif bond_trend == "falling":
                score -= 10  # USD negative
        
        # -------------------------
        # CONTINUOUS CONTRIBUTIONS (prevent permanent zero)
        # -------------------------
        # DXY magnitude (±12 cap) — medium_change % over ~20 periods
        if dxy_data:
            mid = float(dxy_data.get("medium_change", 0.0))
            score += max(-12.0, min(12.0, mid * 6.0))  # 0.5% → ~3 pts, 2% → ~12 pts
        
        # VIX deviation from 20 (±10 cap) — lower VIX → risk-on (+)
        if vix_data:
            vix = float(vix_data.get("level", 18.0))
            score += max(-10.0, min(10.0, (20.0 - vix)))  # 15 → +5, 25 → -5
        
        # Normalize score to -100 to +100
        score = max(-100.0, min(100.0, score))
        
        # Determine confidence
        if confidence_factors >= 2.5:
            confidence = "high"
        elif confidence_factors >= 1.5:
            confidence = "medium"
        else:
            confidence = "low"
        
        # Determine overall risk sentiment bucket from final score
        if score > 20:
            overall_risk = "risk_on"
        elif score < -20:
            overall_risk = "risk_off"
        else:
            overall_risk = "neutral"
        
        return MarketSentiment(
            dxy_trend=dxy_data.get("trend", "neutral") if dxy_data else "neutral",
            dxy_level=float(dxy_data.get("level", 104.0)) if dxy_data else 104.0,
            vix_level=float(vix_data.get("level", 18.0)) if vix_data else 18.0,
            risk_sentiment=overall_risk,
            bond_yield_trend=bond_data.get("trend", "neutral") if bond_data else "neutral",
            overall_score=score,
            confidence=confidence,
            timestamp=datetime.now()
        )
    
    def _is_cache_valid(self) -> bool:
        """Check if cached sentiment is still valid"""
        if "sentiment" not in self.cache or "timestamp" not in self.cache:
            return False
        
        cache_time = self.cache["timestamp"]
        return datetime.now() - cache_time < self.cache_expiry
    
    def _cache_sentiment(self, sentiment: MarketSentiment):
        """Cache sentiment data"""
        self.cache = {
            "sentiment": sentiment,
            "timestamp": datetime.now()
        }
    
    def _print_sentiment_summary(self, sentiment: MarketSentiment):
        """Print sentiment analysis summary"""
        print(f"\n[SENTIMENT] 🌍 MARKET SENTIMENT ANALYSIS:")
        print(f"[SENTIMENT] Overall Score: {sentiment.overall_score:+.1f} (range −100…+100, {sentiment.confidence} confidence)")
        print(f"[SENTIMENT] Risk Sentiment: {sentiment.risk_sentiment}")
        print(f"[SENTIMENT] DXY: {sentiment.dxy_level:.1f} ({sentiment.dxy_trend})")
        print(f"[SENTIMENT] VIX: {sentiment.vix_level:.1f}")
        print(f"[SENTIMENT] Bond Yield Trend: {sentiment.bond_yield_trend}")
        
        # Provide trading context
        if sentiment.overall_score > 30:
            print("[SENTIMENT] 🟢 Strong risk-on environment - favor commodity currencies")
        elif sentiment.overall_score > 10:
            print("[SENTIMENT] 🟡 Mild risk-on bias - cautious optimism")
        elif sentiment.overall_score < -30:
            print("[SENTIMENT] 🔴 Strong risk-off environment - favor safe havens")
        elif sentiment.overall_score < -10:
            print("[SENTIMENT] 🟡 Mild risk-off bias - defensive positioning")
        else:
            print("[SENTIMENT] ⚪ Neutral environment - focus on technical analysis")


def adjust_opportunity_for_sentiment(opportunity_score: float, symbol: str, 
                                   direction: str, sentiment: MarketSentiment) -> Tuple[float, str]:
    """Adjust opportunity score based on market sentiment"""
    
    if not sentiment:
        return opportunity_score, "No sentiment data"
    
    adjustment = 0.0
    reason = ""
    
    # Currency-specific adjustments
    base_currency = symbol[:3]
    quote_currency = symbol[4:7] if len(symbol) > 6 else symbol[3:6]
    
    # USD strength adjustments
    if sentiment.dxy_trend in ["strong_bullish", "bullish"]:
        if base_currency == "USD" and direction == "buy":
            adjustment += 10; reason += "USD strength favors long USD; "
        elif quote_currency == "USD" and direction == "sell":
            adjustment += 8; reason += "USD strength favors short vs USD; "
        elif base_currency == "USD" and direction == "sell":
            adjustment -= 8; reason += "USD strength conflicts with short USD; "
    
    elif sentiment.dxy_trend in ["strong_bearish", "bearish"]:
        if base_currency == "USD" and direction == "sell":
            adjustment += 10; reason += "USD weakness favors short USD; "
        elif quote_currency == "USD" and direction == "buy":
            adjustment += 8; reason += "USD weakness favors long vs USD; "
        elif base_currency == "USD" and direction == "buy":
            adjustment -= 8; reason += "USD weakness conflicts with long USD; "
    
    # Risk sentiment adjustments
    risk_on_currencies = ["AUD", "NZD", "CAD", "NOK", "SEK"]
    safe_haven_currencies = ["JPY", "CHF", "USD"]
    
    if sentiment.risk_sentiment == "risk_on":
        if base_currency in risk_on_currencies and direction == "buy":
            adjustment += 8; reason += "Risk-on favors commodity currencies; "
        elif base_currency in safe_haven_currencies and direction == "sell":
            adjustment += 6; reason += "Risk-on disfavors safe havens; "
    
    elif sentiment.risk_sentiment == "risk_off":
        if base_currency in safe_haven_currencies and direction == "buy":
            adjustment += 8; reason += "Risk-off favors safe havens; "
        elif base_currency in risk_on_currencies and direction == "sell":
            adjustment += 6; reason += "Risk-off disfavors risk currencies; "
    
    # VIX adjustments
    if sentiment.vix_level > 25:  # High fear
        if "JPY" in symbol and direction == "buy":
            adjustment += 5; reason += "High VIX favors JPY safe haven; "
    
    # Reduced cap from ±15 to ±10 to prevent sentiment from overpowering technical signals
    # Sentiment is a confidence modifier, not a hard blocker
    adjustment = max(-10.0, min(10.0, adjustment))
    
    # Ensure sentiment doesn't push a technically valid trade (≥40) below minimum threshold
    # If original score is strong (≥48), sentiment can only reduce by max 8 points
    # This prevents sentiment from vetoing technically valid setups
    if opportunity_score >= 48 and adjustment < 0:
        adjustment = max(adjustment, -8.0)
    
    adjusted_score = opportunity_score + adjustment
    
    if abs(adjustment) > 2:
        reason = f"Sentiment adjustment: {adjustment:+.1f} points. " + reason.strip()
    else:
        reason = "Minimal sentiment impact"
    
    return adjusted_score, reason


# Global analyzer instance
_sentiment_analyzer = None

def get_market_sentiment() -> Optional[MarketSentiment]:
    """Get current market sentiment"""
    global _sentiment_analyzer
    
    if _sentiment_analyzer is None:
        _sentiment_analyzer = MarketSentimentAnalyzer()
    
    return _sentiment_analyzer.get_market_sentiment()

if __name__ == "__main__":
    # Test the sentiment analyzer
    sentiment = get_market_sentiment()
    if sentiment:
        print(f"Market sentiment score: {sentiment.overall_score:+.1f}")
    else:
        print("Failed to get market sentiment")