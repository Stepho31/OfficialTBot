"""
Enhanced Market Scanner for 4H Forex Trading
Monitors all major pairs simultaneously and ranks opportunities
"""

import os
import time
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed

import oandapyV20
import oandapyV20.endpoints.instruments as instruments
import oandapyV20.endpoints.pricing as pricing
from oandapyV20.endpoints.accounts import AccountInstruments

from validators import (
    get_rsi, get_ema, get_momentum_signals, 
    calculate_rsi_from_data, get_oanda_data
)
from trading_config import get_config
from market_sentiment import get_market_sentiment, adjust_opportunity_for_sentiment

@dataclass
class MarketOpportunity:
    """Represents a market opportunity with scoring"""
    symbol: str
    direction: str  # 'buy' or 'sell'
    score: float    # 0-100 composite score
    rsi: float
    trend: str
    momentum: Dict
    range_position: float
    volatility: float
    session_strength: float
    correlation_risk: float
    reasons: List[str]
    entry_price: float
    suggested_sl: float
    suggested_tp: float
    confidence: str  # 'high', 'medium', 'low'

class MarketScanner:
    """Comprehensive market scanner for 4H forex trading"""
    
    # Major forex pairs for scanning
    MAJOR_PAIRS = [
        # Top liquid FX majors & crosses
        "EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF",
        "AUD_USD", "USD_CAD", "NZD_USD", "EUR_GBP",
        "EUR_JPY", "GBP_JPY", "AUD_JPY",
        # Metals & indices (if tradable on account)
        "XAU_USD", "XAG_USD", "SPX500_USD", "NAS100_USD", "US30_USD"
    ]
    
    # Currency strength components for analysis
    CURRENCIES = ["USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD"]
    
    def __init__(self):
        self.config = get_config()
        self.client = None
        self.account_id = os.getenv("OANDA_ACCOUNT_ID")
        self._initialize_client()
        self.correlation_matrix = {}
        self.currency_strength = {}

        self.tradable_instruments = self._get_tradable_instruments()
        print(f"[SCANNER] ✅ Loaded {len(self.tradable_instruments)} tradable instruments")

        
    def _initialize_client(self):
        """Initialize OANDA client"""
        try:
            token = os.getenv("OANDA_API_KEY")
            if not token:
                raise ValueError("OANDA_API_KEY not found")
            self.client = oandapyV20.API(access_token=token, environment="live")
            print("[SCANNER] ✅ OANDA client initialized")
        except Exception as e:
            print(f"[SCANNER] ❌ Failed to initialize client: {e}")
            raise

    def _get_tradable_instruments(self):
        """Fetch tradable instruments for this OANDA account"""
        try:
            r = AccountInstruments(accountID=self.account_id)
            self.client.request(r)
            instruments_list = [i["name"] for i in r.response.get("instruments", [])]
            return set(instruments_list)
        except Exception as e:
            print(f"[SCANNER] ❌ Failed to fetch tradable instruments: {e}")
            return set()
    
    def scan_all_pairs(self, max_opportunities: int = 5) -> List[MarketOpportunity]:
        """Scan all pairs and return ranked opportunities"""
        print(f"[SCANNER] 🔍 Scanning {len(self.MAJOR_PAIRS)} pairs for 4H opportunities...")
        
        # Get market sentiment first
        market_sentiment = get_market_sentiment()
        
        opportunities = []
        
        # Use threading for parallel analysis
        with ThreadPoolExecutor(max_workers=6) as executor:
            future_to_pair = {
                executor.submit(self._analyze_pair, pair): pair 
                for pair in self.MAJOR_PAIRS
            }
            
            for future in as_completed(future_to_pair):
                pair = future_to_pair[future]
                try:
                    pair_opportunities = future.result()
                    if pair_opportunities:
                        opportunities.extend(pair_opportunities)
                except Exception as e:
                    print(f"[SCANNER] ❌ Error analyzing {pair}: {e}")
        
        # Apply sentiment analysis adjustments
        if market_sentiment:
            self._apply_sentiment_adjustments(opportunities, market_sentiment)
        
        # Calculate correlation risks
        self._calculate_correlation_risks(opportunities)
        
        # Sort by score and apply filters
        opportunities.sort(key=lambda x: x.score, reverse=True)
        
        # Filter by minimum score and correlation
        filtered_opportunities = self._filter_opportunities(opportunities)
        
        # Return top opportunities
        top_opportunities = filtered_opportunities[:max_opportunities]
        
        print(f"[SCANNER] 📊 Found {len(top_opportunities)} high-quality opportunities")
        self._print_opportunity_summary(top_opportunities)
        
        return top_opportunities
    
    def _analyze_pair(self, pair: str) -> List[MarketOpportunity]:
        """Analyze a single pair for trading opportunities"""
        try:
            print(f"[SCANNER] Analyzing {pair}...")
            # Skip instruments not tradable in this account
            if self.tradable_instruments and pair not in self.tradable_instruments:
                print(f"[SCANNER] Skipping {pair}: not tradable on this account")
                return []
            
            # Get market data
            candles = get_oanda_data(pair, "H4", 100)
            if not candles or len(candles) < 50:
                print(f"[SCANNER] ❌ Insufficient data for {pair}")
                return []
            
            # Extract price data
            prices = [float(c["mid"]["c"]) for c in candles]
            highs = [float(c["mid"]["h"]) for c in candles]
            lows = [float(c["mid"]["l"]) for c in candles]
            current_price = prices[-1]
            
            # Get current spread and check liquidity
            spread, bid, ask = self._get_current_spread(pair)
            if not self._is_spread_acceptable(pair, spread):
                print(f"[SCANNER] ❌ {pair} spread too wide: {spread}")
                return []
            
            opportunities = []
            
            # Analyze both directions
            for direction in ['buy', 'sell']:
                opportunity = self._evaluate_direction(
                    pair, direction, candles, prices, highs, lows, current_price
                )
                if opportunity and opportunity.score >= 60:  # Minimum threshold
                    opportunities.append(opportunity)
            
            return opportunities
            
        except Exception as e:
            print(f"[SCANNER] ❌ Error analyzing {pair}: {e}")
            return []
    
    def _evaluate_direction(self, pair: str, direction: str, candles: List, 
                          prices: List[float], highs: List[float], 
                          lows: List[float], current_price: float) -> Optional[MarketOpportunity]:
        """Evaluate a specific direction for a pair"""
        
        score = 0
        reasons = []
        
        # 1. RSI Analysis (25 points max)
        rsi = calculate_rsi_from_data(prices, 14)
        rsi_score = self._score_rsi(rsi, direction)
        score += rsi_score
        if rsi_score > 15:
            reasons.append(f"Strong RSI signal: {rsi:.1f}")
        
        # 2. Trend Analysis (25 points max)
        trend = self._get_trend_from_prices(prices)
        trend_score = self._score_trend(trend, direction)
        score += trend_score
        if trend_score > 15:
            reasons.append(f"Favorable trend: {trend}")
        
        # 3. Momentum Analysis (20 points max)
        momentum = self._calculate_momentum_signals(prices, highs, lows)
        momentum_score = self._score_momentum(momentum, direction)
        score += momentum_score
        if momentum_score > 12:
            reasons.append("Strong momentum alignment")
        
        # 4. Range Position (15 points max)
        range_position = self._calculate_range_position(prices, highs, lows)
        range_score = self._score_range_position(range_position, direction)
        score += range_score
        if range_score > 10:
            reasons.append(f"Good range position: {range_position:.2f}")
        
        # 5. Session Timing (10 points max)
        session_strength = self._calculate_session_strength(pair)
        session_score = session_strength * 10
        score += session_score
        if session_score > 6:
            reasons.append("Favorable session timing")
        
        # 6. Volatility Analysis (5 points max)
        volatility = self._calculate_volatility(prices)
        volatility_score = self._score_volatility(volatility)
        score += volatility_score
        
        # Calculate suggested levels
        atr = self._calculate_atr_from_data(candles)
        sl_price, tp_price = self._calculate_levels(current_price, direction, atr, pair)
        
        # Determine confidence level
        confidence = self._determine_confidence(score, len(reasons))
        
        return MarketOpportunity(
            symbol=pair,
            direction=direction,
            score=score,
            rsi=rsi or 50,
            trend=trend,
            momentum=momentum,
            range_position=range_position,
            volatility=volatility,
            session_strength=session_strength,
            correlation_risk=0,  # Will be calculated later
            reasons=reasons + [f"RSI={rsi:.1f}", f"ATR%≈{(self._calculate_volatility(prices)):.2f}"],
            entry_price=current_price,
            suggested_sl=sl_price,
            suggested_tp=tp_price,
            confidence=confidence
        )
    
    def _score_rsi(self, rsi: float, direction: str) -> float:
        """Score RSI for given direction"""
        if not rsi:
            return 0
        
        if direction == 'buy':
            if 20 <= rsi <= 50:
                return 25  # Optimal oversold zone (widened)
            elif 15 <= rsi <= 60:
                return 15  # Good zone (widened)
            elif rsi <= 70:
                return 8   # Acceptable (widened)
            else:
                return 0   # Overbought
        else:  # sell
            if 50 <= rsi <= 80:
                return 25  # Optimal overbought zone (widened)
            elif 40 <= rsi <= 85:
                return 15  # Good zone (widened)
            elif rsi >= 30:
                return 8   # Acceptable (widened)
            else:
                return 0   # Oversold
    
    def _get_trend_from_prices(self, prices: List[float]) -> str:
        """Determine trend from price data"""
        if len(prices) < 50:
            return "neutral"
        
        # Use EMA crossover
        ema20 = self._calculate_ema(prices[-20:], 20)
        ema50 = self._calculate_ema(prices[-50:], 50)
        
        if ema20 and ema50:
            if ema20 > ema50 * 1.001:  # 0.1% buffer
                return "bullish"
            elif ema20 < ema50 * 0.999:
                return "bearish"
        
        return "neutral"
    
    def _score_trend(self, trend: str, direction: str) -> float:
        """Score trend alignment"""
        if (direction == 'buy' and trend == 'bullish') or \
           (direction == 'sell' and trend == 'bearish'):
            return 25
        elif trend == 'neutral':
            return 12
        else:
            return 0
    
    def _calculate_momentum_signals(self, prices: List[float], 
                                  highs: List[float], lows: List[float]) -> Dict:
        """Calculate momentum indicators"""
        if len(prices) < 20:
            return {"short": 0, "medium": 0, "strength": 0}
        
        # Price momentum over different periods
        short_momentum = (prices[-1] / prices[-5] - 1) * 100 if len(prices) >= 5 else 0
        medium_momentum = (prices[-1] / prices[-20] - 1) * 100 if len(prices) >= 20 else 0
        
        # Volume-like momentum using range
        recent_ranges = [(h - l) for h, l in zip(highs[-10:], lows[-10:])]
        avg_range = sum(recent_ranges) / len(recent_ranges) if recent_ranges else 0
        current_range = highs[-1] - lows[-1]
        range_expansion = (current_range / avg_range - 1) * 100 if avg_range > 0 else 0
        
        return {
            "short": short_momentum,
            "medium": medium_momentum,
            "strength": range_expansion
        }
    
    def _score_momentum(self, momentum: Dict, direction: str) -> float:
        """Score momentum alignment"""
        score = 0
        
        if direction == 'buy':
            if momentum["short"] > 0.5:
                score += 8
            if momentum["medium"] > 1.0:
                score += 8
            if momentum["strength"] > 10:
                score += 4
        else:  # sell
            if momentum["short"] < -0.5:
                score += 8
            if momentum["medium"] < -1.0:
                score += 8
            if momentum["strength"] > 10:
                score += 4
        
        return min(score, 20)
    
    def _calculate_range_position(self, prices: List[float], 
                                highs: List[float], lows: List[float]) -> float:
        """Calculate where current price sits in recent range"""
        if len(prices) < 20:
            return 0.5
        
        recent_high = max(highs[-20:])
        recent_low = min(lows[-20:])
        current_price = prices[-1]
        
        if recent_high == recent_low:
            return 0.5
        
        return (current_price - recent_low) / (recent_high - recent_low)
    
    def _score_range_position(self, range_position: float, direction: str) -> float:
        """Score range position for direction"""
        if direction == 'buy':
            if range_position <= 0.3:
                return 15  # Near support
            elif range_position <= 0.4:
                return 10
            elif range_position <= 0.6:
                return 5
            else:
                return 0
        else:  # sell
            if range_position >= 0.7:
                return 15  # Near resistance
            elif range_position >= 0.6:
                return 10
            elif range_position >= 0.4:
                return 5
            else:
                return 0
    
    def _calculate_session_strength(self, pair: str) -> float:
        """Calculate session strength for the pair"""
        now = datetime.utcnow()
        hour = now.hour
        
        pair_upper = pair.upper()
        
        # Peak session hours with higher weights
        if "JPY" in pair_upper:
            # Asian session peak
            if 1 <= hour <= 6:
                return 1.0
            elif 22 <= hour <= 24 or 0 <= hour <= 8:
                return 0.8
            else:
                return 0.3
        elif any(curr in pair_upper for curr in ["EUR", "GBP", "CHF"]):
            # European session peak
            if 8 <= hour <= 12:
                return 1.0
            elif 6 <= hour <= 16:
                return 0.8
            else:
                return 0.3
        elif any(curr in pair_upper for curr in ["USD", "CAD"]):
            # American session peak
            if 14 <= hour <= 18:
                return 1.0
            elif 12 <= hour <= 20:
                return 0.8
            else:
                return 0.3
        else:
            # General overlap periods
            if 8 <= hour <= 12 or 14 <= hour <= 17:
                return 0.9
            else:
                return 0.4
    
    def _calculate_volatility(self, prices: List[float]) -> float:
        """Calculate normalized volatility"""
        if len(prices) < 20:
            return 0
        
        # Calculate returns
        returns = [(prices[i] / prices[i-1] - 1) for i in range(1, len(prices))]
        
        # Standard deviation of returns
        mean_return = sum(returns) / len(returns)
        variance = sum((r - mean_return) ** 2 for r in returns) / len(returns)
        volatility = variance ** 0.5
        
        return volatility * 100  # Convert to percentage
    
    def _score_volatility(self, volatility: float) -> float:
        """Score volatility (prefer moderate volatility)"""
        if 0.8 <= volatility <= 2.0:
            return 5  # Optimal for 4H
        elif 0.5 <= volatility <= 3.0:
            return 3
        else:
            return 1  # Too low or too high
    
    def _calculate_atr_from_data(self, candles: List) -> float:
        """Calculate ATR from candle data"""
        if len(candles) < 21:
            return None
        
        true_ranges = []
        for i in range(1, len(candles)):
            current = candles[i]
            previous = candles[i-1]
            
            high = float(current["mid"]["h"])
            low = float(current["mid"]["l"])
            prev_close = float(previous["mid"]["c"])
            
            tr1 = high - low
            tr2 = abs(high - prev_close)
            tr3 = abs(low - prev_close)
            
            true_ranges.append(max(tr1, tr2, tr3))
        
        # Use EMA for ATR
        return self._calculate_ema(true_ranges[-20:], 20)
    
    def _calculate_ema(self, values: List[float], period: int) -> float:
        """Calculate EMA"""
        if len(values) < period:
            return None
        
        multiplier = 2.0 / (period + 1)
        ema = values[0]
        
        for value in values[1:]:
            ema = (value * multiplier) + (ema * (1 - multiplier))
        
        return ema
    
    def _calculate_levels(self, price: float, direction: str, 
                         atr: float, pair: str) -> Tuple[float, float]:
        """Calculate suggested SL and TP levels"""
        if atr:
            if direction == 'buy':
                sl = price - (atr * 2.0)
                tp = price + (atr * 3.0)
            else:
                sl = price + (atr * 2.0)
                tp = price - (atr * 3.0)
        else:
            # Fallback percentage-based
            if "JPY" in pair.upper():
                sl_distance = price * 0.008  # 0.8%
                tp_distance = price * 0.016  # 1.6%
            else:
                sl_distance = price * 0.006  # 0.6%
                tp_distance = price * 0.012  # 1.2%
            
            if direction == 'buy':
                sl = price - sl_distance
                tp = price + tp_distance
            else:
                sl = price + sl_distance
                tp = price - tp_distance
        
        return self._round_price(pair, sl), self._round_price(pair, tp)
    
    def _round_price(self, pair: str, price: float) -> float:
        """Round price according to pair precision"""
        if "JPY" in pair.upper():
            return round(price, 3)
        else:
            return round(price, 5)
    
    def _determine_confidence(self, score: float, reason_count: int) -> str:
        """Determine confidence level"""
        if score >= 80 and reason_count >= 4:
            return "high"
        elif score >= 65 and reason_count >= 3:
            return "medium"
        else:
            return "low"
    
    def _get_current_spread(self, pair: str) -> Tuple[float, float, float]:
        """Get current spread for pair"""
        try:
            r = pricing.PricingInfo(accountID=self.account_id, params={"instruments": pair})
            self.client.request(r)
            prices = r.response["prices"][0]
            bid = float(prices["bids"][0]["price"])
            ask = float(prices["asks"][0]["price"])
            spread = ask - bid
            return spread, bid, ask
        except Exception as e:
            print(f"[SCANNER] Error getting spread for {pair}: {e}")
            return None, None, None
    
    def _is_spread_acceptable(self, pair: str, spread: float) -> bool:
        """Check if spread is acceptable"""
        if spread is None:
            return False
        
        max_spread = self.config.get_max_spread(pair)
        return spread <= max_spread
    
    def _calculate_correlation_risks(self, opportunities: List[MarketOpportunity]):
        """Calculate correlation risk for each opportunity"""
        # Simplified correlation calculation
        for opp in opportunities:
            correlation_risk = 0
            base_currency = opp.symbol[:3]
            quote_currency = opp.symbol[4:7]
            
            for other in opportunities:
                if other.symbol == opp.symbol:
                    continue
                
                other_base = other.symbol[:3]
                other_quote = other.symbol[4:7]
                
                # Check for currency overlap
                if (base_currency == other_base or quote_currency == other_quote or
                    base_currency == other_quote or quote_currency == other_base):
                    if opp.direction == other.direction:
                        correlation_risk += 0.3  # Same direction = higher risk
                    else:
                        correlation_risk += 0.1  # Opposite direction = lower risk
            
            opp.correlation_risk = min(correlation_risk, 1.0)
    
    def _apply_sentiment_adjustments(self, opportunities: List[MarketOpportunity], sentiment):
        """Apply market sentiment adjustments to opportunity scores"""
        print(f"[SCANNER] 🌍 Applying market sentiment adjustments...")
        
        for opp in opportunities:
            original_score = opp.score
            adjusted_score, reason = adjust_opportunity_for_sentiment(
                opp.score, opp.symbol, opp.direction, sentiment
            )
            
            if abs(adjusted_score - original_score) > 2:
                opp.score = adjusted_score
                opp.reasons.append(f"Market sentiment: {reason}")
                print(f"[SCANNER] 🔄 {opp.symbol} {opp.direction}: {original_score:.1f} → {adjusted_score:.1f} ({reason})")
    
    def _filter_opportunities(self, opportunities: List[MarketOpportunity]) -> List[MarketOpportunity]:
        """Filter opportunities by quality and correlation"""
        filtered = []
        
        for opp in opportunities:
            # Adjust score based on correlation risk
            adjusted_score = opp.score * (1 - opp.correlation_risk * 0.5)
            
            # Minimum adjusted score threshold
            if adjusted_score >= 60:
                opp.score = adjusted_score
                filtered.append(opp)
        
        return filtered
    
    def _print_opportunity_summary(self, opportunities: List[MarketOpportunity]):
        """Print summary of opportunities"""
        if not opportunities:
            print("[SCANNER] No opportunities found meeting criteria")
            return
        
        print("\n[SCANNER] 🎯 TOP TRADING OPPORTUNITIES (4H):")
        print("=" * 80)
        
        for i, opp in enumerate(opportunities, 1):
            print(f"\n{i}. {opp.symbol} {opp.direction.upper()} - Score: {opp.score:.1f} ({opp.confidence} confidence)")
            print(f"   💰 Entry: {opp.entry_price:.5f} | SL: {opp.suggested_sl:.5f} | TP: {opp.suggested_tp:.5f}")
            print(f"   📊 RSI: {opp.rsi:.1f} | Trend: {opp.trend} | Range: {opp.range_position:.2f}")
            print(f"   ⚡ Session: {opp.session_strength:.1f} | Correlation Risk: {opp.correlation_risk:.1f}")
            print(f"   🎯 Reasons: {', '.join(opp.reasons[:3])}")
            
            # Risk-reward calculation
            if opp.direction == 'buy':
                risk = opp.entry_price - opp.suggested_sl
                reward = opp.suggested_tp - opp.entry_price
            else:
                risk = opp.suggested_sl - opp.entry_price
                reward = opp.entry_price - opp.suggested_tp
            
            rr_ratio = reward / risk if risk > 0 else 0
            print(f"   💎 Risk:Reward = 1:{rr_ratio:.2f}")
        
        print("\n" + "=" * 80)

def get_market_opportunities(max_results: int = 5) -> List[MarketOpportunity]:
    """Main function to get market opportunities"""
    scanner = MarketScanner()
    return scanner.scan_all_pairs(max_results)

if __name__ == "__main__":
    # Test the scanner
    opportunities = get_market_opportunities(5)
    print(f"\nFound {len(opportunities)} trading opportunities")