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
        "EMAIL_TO"
        "OANDA_API_KEY",
        "OANDA_ACCOUNT_ID",
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
        print("Some features (like email notifications) may not work.")
    
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
    """Display startup information and configuration"""
    print("\n🤖 AUTOMATED 4H FOREX TRADING SYSTEM")
    print("=" * 50)
    print("📊 Configuration:")
    print(f"  • Risk per trade: {os.getenv('RISK_PERCENT', '1.0')}%")
    print(f"  • Max daily trades: 10")
    print(f"  • Max concurrent trades: 3") 
    print(f"  • ATR SL multiplier: {os.getenv('ATR_SL_MULTIPLIER', '1.8')}")
    print(f"  • ATR TP multiplier: {os.getenv('ATR_TP_MULTIPLIER', '3.5')}")
    print(f"  • Minimum R:R ratio: 1.6:1")
    print(f"  • Dry run mode: {os.getenv('DRY_RUN', 'false')}")
    
    print("\n🕐 Trading Schedule:")
    print("  • European pairs: 06:00-18:00 UTC")
    print("  • American pairs: 12:00-22:00 UTC") 
    print("  • Asian pairs: 22:00-10:00 UTC")
    print("  • Scan frequency: Every 30 minutes")
    
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
    
    # Confirm start
    print("\n" + "=" * 50)
    
    if os.getenv('DRY_RUN', 'false').lower() == 'true':
        print("🔬 DRY RUN MODE - No real trades will be placed")
    else:
        print("💰 LIVE TRADING MODE - Real trades will be placed!")
        
        response = input("\nAre you sure you want to start live trading? (yes/no): ")
        if response.lower() not in ['yes', 'y']:
            print("❌ Startup cancelled by user.")
            sys.exit(0)
    
    print("\n🎬 Starting automated trading system...")
    print("Press Ctrl+C to stop the system gracefully")
    print("=" * 50)
    
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