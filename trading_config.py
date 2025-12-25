# Trading Configuration File
# Centralized configuration for all trading parameters

import os
from dataclasses import dataclass
from typing import Dict, List

# Score constants for consistent execution thresholds across scanner, filters, and execution
BASE_MIN_SCORE = 65  # Normal execution threshold
FREQUENCY_MIN_SCORE = 55  # Used only when frequency-first mode is active

@dataclass
class RiskManagement:
    """Risk management configuration"""
    default_risk_percent: float = 1.0  # Default risk per trade as % of account
    max_risk_percent: float = 3.0      # Maximum risk per trade
    min_position_size: int = 1000      # Minimum position size
    max_position_size: int = 100000    # Maximum position size
    max_open_trades: int = 3           # Maximum concurrent trades
    
    # ATR-based settings
    atr_sl_multiplier: float = 1.6     # Tuned SL distance in ATR (1.5–1.8)
    atr_tp_multiplier: float = 2.8     # Tuned TP distance in ATR (2.5–3.2)
    atr_trail_multiplier: float = 1.1  # Trail distance in ATR (1.0–1.2)

@dataclass 
class EntryValidation:
    """Entry validation configuration for 4H trading"""
    min_validation_score: float = 60.0  # Loosened ~10–15% to raise frequency
    max_spread_regular: float = 0.00030   # Slightly tighter to improve execution
    max_spread_jpy: float = 0.050        # Increased for JPY pairs (was 0.030) - allows normal spreads on volatile pairs like GBP_JPY
    max_spread_metals: float = 0.060      # Tighter for precious metals
    
    # 4H RSI thresholds (more specific ranges)
    rsi_buy_min: float = 25.0           # Optimal buy zone for 4H
    rsi_buy_max: float = 55.0
    rsi_buy_acceptable_max: float = 65.0 # Acceptable but not ideal
    rsi_sell_min: float = 45.0          # Optimal sell zone for 4H  
    rsi_sell_max: float = 75.0
    rsi_sell_acceptable_min: float = 35.0 # Acceptable but not ideal
    
    # 4H momentum thresholds
    momentum_long_threshold: float = 1.5  # 20-period momentum threshold
    momentum_short_threshold: float = 0.8 # 5-period momentum threshold

@dataclass
class ExitManagement:
    """Exit management configuration optimized for 4H trading"""
    trailing_activation_pips: float = 20.0   # Pips profit to activate trailing (4H)
    partial_profit_pips: float = 25.0        # Pips profit for partial exit (4H)
    partial_profit_pips_jpy: float = 20.0    # Pips for JPY pairs (4H)
    partial_profit_percent: float = 50.0     # % of position to close (keep more for trends)
    
    # 4H timeframe trail distances (tighter than intraday)
    default_trail_distance: float = 0.004    # For non-JPY pairs (4H)
    default_trail_distance_jpy: float = 0.04 # For JPY pairs (4H)
    atr_trail_multiplier: float = 1.2        # ATR multiplier for 4H trailing

@dataclass
class TradingHours:
    """Favorable trading hours configuration (UTC)"""
    asian_session: Dict[str, List[int]] = None
    european_session: Dict[str, List[int]] = None
    american_session: Dict[str, List[int]] = None
    overlap_periods: Dict[str, List[int]] = None
    
    def __post_init__(self):
        if self.asian_session is None:
            self.asian_session = {"hours": list(range(23, 24)) + list(range(0, 9))}
        if self.european_session is None:
            self.european_session = {"hours": list(range(7, 17))}
        if self.american_session is None:
            self.american_session = {"hours": list(range(13, 22))}
        if self.overlap_periods is None:
            self.overlap_periods = {
                "asian_european": list(range(7, 9)),
                "european_american": list(range(13, 17))
            }

@dataclass
class GPTConfig:
    """GPT evaluation configuration"""
    model: str = "gpt-3.5-turbo"
    temperature: float = 0.5
    min_score_threshold: float = 0.55
    max_score_threshold: float = 1.0
    cache_enabled: bool = True

class TradingConfig:
    """Main trading configuration class"""
    
    def __init__(self):
        self.risk_management = RiskManagement()
        self.entry_validation = EntryValidation()
        self.exit_management = ExitManagement()
        self.trading_hours = TradingHours()
        self.gpt_config = GPTConfig()
        
        # Load environment overrides
        self._load_env_overrides()
    
    def _load_env_overrides(self):
        """Load configuration overrides from environment variables"""
        
        # Risk Management overrides
        self.risk_management.default_risk_percent = float(
            os.getenv("RISK_PERCENT", self.risk_management.default_risk_percent)
        )
        self.risk_management.atr_sl_multiplier = float(
            os.getenv("ATR_SL_MULTIPLIER", self.risk_management.atr_sl_multiplier)
        )
        self.risk_management.atr_tp_multiplier = float(
            os.getenv("ATR_TP_MULTIPLIER", self.risk_management.atr_tp_multiplier)
        )
        # Max concurrent trades
        try:
            self.risk_management.max_open_trades = int(
                os.getenv("MAX_CONCURRENT_TRADES", self.risk_management.max_open_trades)
            )
        except Exception:
            pass
        
        # GPT Config overrides
        self.gpt_config.min_score_threshold = float(
            os.getenv("TP_THRESHOLD", self.gpt_config.min_score_threshold)
        )
        
        # Entry validation overrides
        self.entry_validation.min_validation_score = float(
            os.getenv("MIN_VALIDATION_SCORE", self.entry_validation.min_validation_score)
        )

        # Spread overrides
        try:
            self.entry_validation.max_spread_regular = float(
                os.getenv("MAX_SPREAD_REGULAR", self.entry_validation.max_spread_regular)
            )
            self.entry_validation.max_spread_jpy = float(
                os.getenv("MAX_SPREAD_JPY", self.entry_validation.max_spread_jpy)
            )
            self.entry_validation.max_spread_metals = float(
                os.getenv("MAX_SPREAD_METALS", self.entry_validation.max_spread_metals)
            )
        except Exception:
            pass
    
    def get_max_spread(self, instrument: str) -> float:
        """Get maximum allowed spread for an instrument"""
        instrument = instrument.upper()
        # Volatile cross pairs (e.g., GBP_JPY) naturally have wider spreads on 4H timeframe
        # Apply more lenient thresholds for these pairs
        volatile_crosses = ["GBP_JPY", "GBPJPY", "AUD_JPY", "AUDJPY", "EUR_JPY", "EURJPY", "NZD_JPY", "NZDJPY"]
        if any(vc in instrument for vc in volatile_crosses):
            # Allow up to 0.06 for volatile JPY crosses (1.5x normal JPY threshold)
            return max(self.entry_validation.max_spread_jpy * 1.5, 0.06)
        elif "JPY" in instrument:
            return self.entry_validation.max_spread_jpy
        elif any(metal in instrument for metal in ["XAU", "XAG"]):
            return self.entry_validation.max_spread_metals
        else:
            return self.entry_validation.max_spread_regular
    
    def get_pip_value(self, instrument: str) -> float:
        """Get pip value for an instrument"""
        if "JPY" in instrument.upper():
            return 0.01
        else:
            return 0.0001
    
    def get_pip_multiplier(self, instrument: str) -> float:
        """Get pip multiplier for pip calculations"""
        if "JPY" in instrument.upper():
            return 100
        else:
            return 10000
    
    def get_trail_distance(self, instrument: str, atr: float = None) -> float:
        """Get trailing stop distance"""
        if atr:
            return atr * self.risk_management.atr_trail_multiplier
        
        if "JPY" in instrument.upper():
            return self.exit_management.default_trail_distance_jpy
        else:
            return self.exit_management.default_trail_distance
    
    def is_favorable_trading_time(self, instrument: str) -> bool:
        """Check if current time is favorable for trading the instrument"""
        from datetime import datetime
        
        now = datetime.utcnow()
        hour = now.hour
        
        instrument = instrument.upper()
        
        # JPY pairs - Asian session
        if "JPY" in instrument:
            return hour in self.trading_hours.asian_session["hours"]
        
        # European pairs - European session  
        elif any(curr in instrument for curr in ["EUR", "GBP", "CHF"]):
            return hour in self.trading_hours.european_session["hours"]
        
        # General overlap periods (most liquid times)
        else:
            all_overlap_hours = []
            for period in self.trading_hours.overlap_periods.values():
                all_overlap_hours.extend(period)
            return hour in all_overlap_hours
    
    def validate_position_size(self, calculated_size: int) -> int:
        """Validate and adjust position size within limits"""
        return max(
            self.risk_management.min_position_size,
            min(calculated_size, self.risk_management.max_position_size)
        )
    
    def should_use_atr_based_stops(self, atr: float = None) -> bool:
        """Determine if ATR-based stops should be used"""
        return atr is not None and atr > 0

# Global configuration instance
config = TradingConfig()

def get_config() -> TradingConfig:
    """Get the global trading configuration"""
    return config

def reload_config():
    """Reload configuration (useful for runtime updates)"""
    global config
    config = TradingConfig()
    return config

def get_dry_run() -> bool:
    """
    Get DRY_RUN setting with production override.
    
    Rules:
    1. Defaults to False unless explicitly set to "true"
    2. Always False in production environment
    3. Returns True only if explicitly set AND not in production
    
    Returns:
        bool: True if dry-run mode is enabled, False otherwise
    """
    # Force DRY_RUN off in production
    environment = os.getenv("ENVIRONMENT", "production").lower()
    if environment == "production":
        return False
    
    # Default to False unless explicitly set to "true"
    dry_run_env = os.getenv("DRY_RUN", "false").lower()
    return dry_run_env == "true"