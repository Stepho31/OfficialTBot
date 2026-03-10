#!/usr/bin/env python3
"""
Startup script for the Fully Automated 4H Forex Trading System
Run this script to start continuous automated trading
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Import centralized DRY_RUN configuration
from trading_config import get_dry_run

def check_environment():
    """Check if all required environment variables are set"""
    required_vars = [
        "OPENAI_API_KEY"
    ]
    
    optional_vars = [
        "TWELVE_DATA_API_KEY",
        "EMAIL_HOST",
        "EMAIL_PORT", 
        "EMAIL_USER",
        "EMAIL_PASSWORD",
        "EMAIL_TO",
        # Note: OANDA_API_KEY and OANDA_ACCOUNT_ID are no longer required at startup
        # They are supplied dynamically per-user via AutopipClient.get_tier2_users
    ]
    
    missing_required = []
    missing_optional = []
    
    print("🔍 Checking environment variables...")
    
    for var in required_vars:
        if not os.getenv(var):
            missing_required.append(var)
        else:
            print(f"  ✅ {var}: {'*' * 8}")
    
    for var in optional_vars:
        if not os.getenv(var):
            missing_optional.append(var)
        else:
            print(f"  ✅ {var}: ({'*' * 8})")
    
    if missing_required:
        print(f"\n❌ Missing required environment variables:")
        for var in missing_required:
            print(f"   - {var}")
        print("\nPlease set these variables before starting the system.")
        return False
    
    if missing_optional:
        print(f"\n⚠️ Missing optional environment variables:")
        for var in missing_optional:
            print(f"   - {var}")
        print("Email notifications will be disabled if EMAIL_USER, EMAIL_PASS, or EMAIL_TO are unset.")
    
    return True

def check_dependencies():
    """Check if all required Python packages are installed"""
    required_packages = [
        "oandapyV20",
        "openai", 
        "requests",
        "schedule",
        "bs4"
    ]
    
    missing_packages = []
    
    print("\n🐍 Checking Python dependencies...")
    
    for package in required_packages:
        try:
            __import__(package)
            print(f"  ✅ {package}")
        except ImportError:
            missing_packages.append(package)
            print(f"  ❌ {package}")
    
    if missing_packages:
        print(f"\n❌ Missing required packages:")
        for package in missing_packages:
            print(f"   - {package}")
        print("\nInstall missing packages with:")
        print(f"pip install {' '.join(missing_packages)}")
        return False
    
    return True

def display_startup_info():
    """Display startup information and configuration (values actually used at runtime)."""
    try:
        from trading_config import get_config
        _cfg = get_config()
        _max_concurrent = os.getenv("MAX_CONCURRENT_TRADES")
        max_concurrent = int(_max_concurrent) if _max_concurrent is not None else _cfg.risk_management.max_open_trades
        max_daily = os.getenv("MAX_TRADES_PER_DAY", "15")
    except Exception:
        max_concurrent = int(os.getenv("MAX_CONCURRENT_TRADES", "7"))
        max_daily = os.getenv("MAX_TRADES_PER_DAY", "15")
    print("\n🤖 AUTOMATED 4H FOREX TRADING SYSTEM")
    print("=" * 50)
    print("📊 Configuration (runtime values):")
    print("  • Execution: Enhanced (scanner -> ranking -> portfolio risk -> execution)")
    print(f"  • Risk per trade: {os.getenv('RISK_PERCENT', '1.0')}%")
    print(f"  • Max daily trades: {max_daily}")
    print(f"  • Max concurrent trades: {max_concurrent}")
    print(f"  • ATR SL multiplier: {os.getenv('ATR_SL_MULTIPLIER', '2.0')}")
    print(f"  • ATR TP multiplier: {os.getenv('ATR_TP_MULTIPLIER', '2.8')}")
    print(f"  • Minimum R:R ratio: 1.6:1")
    # DRY_RUN should always be False at this point due to startup abort check
    print(f"  • Dry run mode: false (enforced)")
    
    print("\n🕐 Trading Schedule:")
    print("  • European pairs: 06:00-18:00 UTC")
    print("  • American pairs: 12:00-22:00 UTC")
    print("  • Asian pairs: 22:00-10:00 UTC")
    print("  • Scan frequency: Every 15 minutes (favorable hours)")
    
    print("\n📧 Notifications:")
    print("  • Weekly reports: Sundays at 23:00 UTC")
    print("  • Trade notifications: Real-time")
    print("  • Health checks: Every hour")
    
    print("\n⚠️ IMPORTANT NOTES:")
    print("  • This system trades with REAL MONEY")
    print("  • Monitor your account regularly")
    print("  • Use appropriate position sizing")
    print("  • Stop the system if needed (Ctrl+C)")

def main():
    """Main startup function"""
    print("🚀 Starting Automated 4H Forex Trading System...")
    print("=" * 50)
    
    # Get DRY_RUN with production override
    DRY_RUN = get_dry_run()
    
    # Force DRY_RUN off in production
    if os.getenv("ENVIRONMENT", "production").lower() == "production":
        DRY_RUN = False
    
    # Prevent bot startup if DRY_RUN is still True
    if DRY_RUN:
        raise RuntimeError(
            "❌ Bot startup aborted: DRY_RUN is enabled. Disable DRY_RUN to execute real trades."
        )
    
    # Check environment
    if not check_environment():
        print("\n❌ Environment check failed. Exiting.")
        sys.exit(1)
    
    # Check dependencies
    if not check_dependencies():
        print("\n❌ Dependency check failed. Exiting.")
        sys.exit(1)
    
    # Display configuration
    display_startup_info()
    
    # Confirm mode (non-interactive)
    print("\n" + "=" * 50)
    
    # Add startup logging
    import logging
    logger = logging.getLogger(__name__)
    mode = "LIVE TRADING"
    logger.warning(f"[STARTUP MODE] Bot running in: {mode}")
    print(f"[STARTUP MODE] Bot running in: {mode}")
    
    print("\n🎬 Starting automated trading system...")
    print("Press Ctrl+C to stop the system gracefully (when running locally)")
    print("=" * 50)

    # One-time DB persistence startup validation
    try:
        from db_persistence import validate_db_persistence_startup
        validate_db_persistence_startup()
    except Exception as e:
        print("[DB] Startup validation error:", e)
    
    # Import and start the automated trader
    try:
        from automated_trader import AutomatedTrader
        
        trader = AutomatedTrader()
        trader.start_automation()
        
    except KeyboardInterrupt:
        print("\n🛑 Shutdown requested by user")
    except ImportError as e:
        print(f"\n❌ Import error: {e}")
        print("Make sure all required files are present in the current directory.")
    except Exception as e:
        print(f"\n💥 Startup error: {e}")
        print("Check your configuration and try again.")

if __name__ == "__main__":
    main()