# Enhanced 4H Forex Trading System

A sophisticated automated forex trading system optimized for 4-hour timeframe trading with advanced risk management, technical analysis, and AI-powered trade evaluation.

## 🚀 Key Features

### 🤖 Fully Automated Operation
- **24/7 Market Monitoring**: Continuous market scanning during favorable trading hours
- **Multi-Trade Capability**: Up to 3 concurrent trades on different pairs
- **Manual Trade Detection**: Automatically detects and handles manually closed trades
- **Intelligent Scheduling**: Trades during optimal sessions for each currency pair

### Enhanced Trade Placement Accuracy
- **Dynamic Position Sizing**: ATR-based position sizing with configurable risk management
- **Advanced Price Precision**: Multi-sample price validation to reduce slippage
- **Market Condition Validation**: Spread analysis and favorable trading hours detection
- **Technical Analysis Integration**: 4H-optimized RSI, momentum, and trend validation

### Risk Management
- **Volatility-Based Stops**: ATR-calculated stop losses and take profits
- **Trailing Stops**: Automatic trailing stop activation after profit targets
- **Partial Profit Taking**: Systematic profit taking at predetermined levels
- **Position Size Limits**: Configurable minimum and maximum position sizes

### Technical Analysis (4H Optimized)
- **4H Timeframe Focus**: Specialized 4-hour chart analysis
- **Enhanced RSI Validation**: 4H-specific momentum ranges and confirmation
- **Trend Analysis**: 20-period trend validation for stronger 4H signals
- **Support/Resistance**: 10-candle (40-hour) dynamic levels
- **Confluence Detection**: Multi-factor alignment scoring

### Monitoring & Exit Strategy
- **Real-time Monitoring**: Continuous trade monitoring with advanced exit logic
- **Trailing Stops**: ATR-based or fixed trailing stops
- **Partial Exits**: Automatic partial profit taking
- **Detailed Logging**: Comprehensive trade performance tracking

## 📋 Configuration

### Environment Variables

#### Core API Settings
```bash
OANDA_API_KEY=your_oanda_api_key
OANDA_ACCOUNT_ID=your_account_id
OPENAI_API_KEY=your_openai_key
TWELVE_DATA_API_KEY=your_twelve_data_key  # Optional fallback
```

#### Risk Management
```bash
RISK_PERCENT=1.0                    # Risk per trade (% of account balance)
ATR_SL_MULTIPLIER=2.0              # Stop loss distance (ATR multiplier)
ATR_TP_MULTIPLIER=3.0              # Take profit distance (ATR multiplier)
MIN_VALIDATION_SCORE=60.0          # Minimum technical validation score (%)
```

#### Trade Execution
```bash
TP_THRESHOLD=0.55                  # Minimum GPT score for trade execution
SL_THRESHOLD=0.30                  # Score below which trades are rejected
DRY_RUN=false                      # Set to 'true' for simulation mode
```

### Trading Configuration

The system uses a centralized configuration in `trading_config.py`:

#### Risk Management Settings
- **Position Sizing**: Dynamic calculation based on account balance and ATR
- **Risk Limits**: Configurable risk per trade (default 1% of account)
- **Position Limits**: Min 1,000 / Max 100,000 units
- **Concurrent Trades**: Maximum 3 open trades

#### Entry Validation
- **Spread Limits**: 
  - Regular pairs: 0.3 pips max
  - JPY pairs: 3 pips max
  - Precious metals: 5 pips max
- **Technical Thresholds**:
  - RSI ranges for buy/sell validation
  - Momentum threshold: ±2%

#### Exit Management (4H Optimized)
- **Trailing Stop Activation**: After 20 pips profit (4H threshold)
- **Partial Profit Taking**: 40% at 35 pips profit (keep more for trends)
- **ATR-Based Distances**: 21-period EMA ATR for 4H responsiveness
- **Tighter Trailing**: 1.2x ATR trailing distance for 4H precision

## 🔧 Installation

1. **Clone the repository**
```bash
git clone <repository-url>
cd trading-system
```

2. **Install dependencies**
```bash
pip install -r requirements.txt
```

3. **Set up environment variables**
```bash
cp .env.example .env
# Edit .env with your API keys and settings
```

4. **Run the system**
```bash
# Fully Automated Mode (Recommended)
python start_automated_trading.py

# Manual/Testing Modes
python main.py          # Single execution
python monitor_loop.py  # Monitor existing trades only
```

## 📊 System Architecture

### Core Components

1. **main.py** - Main execution engine
2. **trader.py** - Enhanced trade placement logic
3. **monitor.py** - Advanced trade monitoring and exit management
4. **validators.py** - Multi-timeframe technical analysis
5. **trading_config.py** - Centralized configuration management

### Data Flow

```
Trade Ideas (scraper.py) 
    ↓
Rule-based Filtering (filters.py)
    ↓
GPT Evaluation (gpt_utils.py)
    ↓
Technical Validation (validators.py)
    ↓
Enhanced Trade Placement (trader.py)
    ↓
Advanced Monitoring (monitor.py)
```

## 🎯 Trading Strategy

### Entry Logic
1. **Idea Evaluation**: GPT-4 scores trade ideas (0-1 scale)
2. **Technical Validation**: Multi-timeframe analysis (H1 + H4)
3. **Market Conditions**: Spread and trading hours validation
4. **Risk Assessment**: Position sizing based on volatility

### Exit Logic
1. **Trailing Stops**: Activated after 15 pips profit
2. **Partial Profits**: 50% at 25 pips, remainder trails
3. **Take Profit**: ATR-based or percentage targets
4. **Stop Loss**: ATR-based with trailing capability

### Risk Management
- **1% Risk Rule**: Maximum 1% account risk per trade
- **ATR Position Sizing**: Volatility-adjusted position sizes
- **Maximum Drawdown**: Limited concurrent positions
- **Spread Protection**: Reject trades with excessive spreads

## 📈 Performance Tracking

### Logging Features
- **Pip Performance**: Detailed pip profit/loss tracking
- **Dollar Amounts**: Actual profit/loss in account currency
- **Win/Loss Ratios**: Success rate analysis
- **Trailing Stop Usage**: Strategy effectiveness metrics
- **Partial Profit Analytics**: Exit strategy performance

### Trade Cache
- **Active Trades**: Real-time tracking of open positions
- **Historical Data**: Complete trade history with metrics
- **Performance Analytics**: Strategy performance analysis

## 🔍 Validation & Quality Assurance

### Technical Analysis Validation
- **RSI Validation**: Momentum-based entry confirmation
- **Trend Alignment**: EMA-based trend validation
- **Multi-Timeframe**: H1 and H4 confirmation required
- **Minimum Score**: 60% validation score required

### Market Condition Checks
- **Trading Hours**: Optimal session timing
- **Spread Analysis**: Liquidity assessment
- **Volatility Check**: ATR-based market condition analysis

### Entry Signal Quality
- **Signal Words**: Breakout, bounce, rejection detection
- **Direction Clarity**: Clear buy/sell signal identification
- **Risk/Reward**: Minimum 1.5:1 ratio enforcement

## 🚨 Safety Features

### Risk Controls
- **Position Limits**: Automatic size validation
- **Spread Protection**: Wide spread rejection
- **Time-based Exits**: Optional time-based trade closure
- **Account Protection**: Balance-based position sizing

### Error Handling
- **API Failures**: Graceful error handling and retry logic
- **Network Issues**: Automatic reconnection
- **Data Validation**: Input sanitization and validation
- **Logging**: Comprehensive error and activity logging

## 📋 Monitoring & Maintenance

### Health Checks
- **API Connectivity**: Automatic connection monitoring
- **Trade Status**: Real-time position tracking
- **Performance Metrics**: Continuous performance analysis

### Maintenance Tasks
- **Cache Cleanup**: Automatic cleanup of old data
- **Log Rotation**: Automatic log file management
- **Configuration Updates**: Runtime configuration reloading

## 🔧 Customization

### Configuration Files
- **trading_config.py**: Main configuration management
- **Environment Variables**: Runtime parameter overrides
- **Filters**: Customizable idea filtering rules

### Strategy Modification
- **Entry Criteria**: Adjustable validation thresholds
- **Exit Strategy**: Configurable trailing and profit taking
- **Risk Parameters**: Flexible risk management settings

## 📞 Support & Documentation

### Key Metrics to Monitor
- **Win Rate**: Percentage of profitable trades
- **Average Pip Profit**: Average pips per trade
- **Risk/Reward Ratio**: Actual vs. expected ratios
- **Maximum Drawdown**: Largest losing streak
- **Trailing Stop Effectiveness**: Percentage of trades using trailing stops

### Common Issues
- **API Rate Limits**: Implement appropriate delays
- **Network Connectivity**: Use backup data sources
- **Market Hours**: Respect broker trading hours
- **Position Sizing**: Monitor account balance changes

For detailed technical documentation, see the inline code comments and docstrings.