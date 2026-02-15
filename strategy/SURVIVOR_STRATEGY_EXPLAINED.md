# Survivor Options Trading Strategy - Comprehensive Guide

## Table of Contents
1. [Overview](#overview)
2. [Core Concept](#core-concept)
3. [Strategy Components](#strategy-components)
4. [How It Works](#how-it-works)
5. [Configuration Parameters](#configuration-parameters)
6. [Trading Logic Deep Dive](#trading-logic-deep-dive)
7. [Example Scenarios](#example-scenarios)
8. [Risk Management](#risk-management)
9. [Running the Strategy](#running-the-strategy)

---

## Overview

The **Survivor Strategy** is a systematic options selling strategy that profits from NIFTY index price movements by selling out-of-the-money (OTM) options when the market moves beyond predefined thresholds. It implements a dual-side approach, selling both Puts (PE) and Calls (CE) based on directional movements.

### What Problem Does It Solve?
- **Captures premium decay** from sold options
- **Scales positions dynamically** based on price movement magnitude
- **Automatically adjusts** reference points to prevent excessive position accumulation
- **Filters trades** to ensure adequate liquidity and premium

---

## Core Concept

### The "Survivor" Philosophy
The strategy "survives" market volatility by continuously selling options as the market moves, collecting premium while managing risk through:
- **Gap-based triggers**: Only trades when price moves significantly
- **Multiplier system**: Scales position size based on movement magnitude
- **Reset mechanism**: Prevents reference drift during trending markets
- **Premium filtering**: Ensures only liquid, worthwhile trades

### Basic Mechanics
```
Initial State: NIFTY @ 24,500
PE Reference: 24,500
CE Reference: 24,500

Scenario 1: NIFTY rises to 24,530
- Movement: +30 points (exceeds pe_gap of 20)
- Action: Sell PE options (out-of-the-money puts)
- New PE Reference: 24,520 (24,500 + 20*1)

Scenario 2: NIFTY falls to 24,470
- Movement: -30 points (exceeds ce_gap of 20)
- Action: Sell CE options (out-of-the-money calls)
- New CE Reference: 24,480 (24,500 - 20*1)
```

---

## Strategy Components

### 1. **Initialization Phase**

When the strategy starts:

```python
def __init__(self, broker, config, order_tracker):
    # Load all configuration parameters
    self.symbol_initials = "NIFTY25807"  # Option series
    self.index_symbol = "NSE:NIFTY 50"   # Underlying index
    
    # Download and filter instruments
    self.broker.download_instruments()
    self.instruments = broker.get_instruments()
    self.instruments = instruments[instruments['symbol'].str.contains(symbol_initials)]
    
    # Initialize reference values
    current_price = broker.get_quote(index_symbol).last_price
    self.nifty_pe_last_value = current_price  # PE reference
    self.nifty_ce_last_value = current_price  # CE reference
    
    # Initialize reset flags
    self.pe_reset_gap_flag = 0  # Enables PE reset after trade
    self.ce_reset_gap_flag = 0  # Enables CE reset after trade
    
    # Calculate strike difference (50, 100, etc.)
    self.strike_difference = self._get_strike_difference(symbol_initials)
```

**Key State Variables:**
- `nifty_pe_last_value`: Reference price for PE trading (updated after each PE trade)
- `nifty_ce_last_value`: Reference price for CE trading (updated after each CE trade)
- `pe_reset_gap_flag / ce_reset_gap_flag`: Control when reset logic is active
- `strike_difference`: Gap between consecutive strikes (e.g., 50 for NIFTY)

### 2. **Gap System**

The strategy uses **three types of gaps**:

#### a) **Trade Trigger Gaps** (`pe_gap` / `ce_gap`)
- **Purpose**: Determines when to execute trades
- **Default**: 20 points
- **Logic**: 
  - PE Trade: When `current_price - nifty_pe_last_value > pe_gap`
  - CE Trade: When `nifty_ce_last_value - current_price > ce_gap`

#### b) **Strike Selection Gaps** (`pe_symbol_gap` / `ce_symbol_gap`)
- **Purpose**: Determines which strike to sell
- **Default**: 200 points
- **Logic**:
  - PE Strike: `current_price - pe_symbol_gap` (below market)
  - CE Strike: `current_price + ce_symbol_gap` (above market)

#### c) **Reset Gaps** (`pe_reset_gap` / `ce_reset_gap`)
- **Purpose**: Adjusts reference values during favorable movements
- **Default**: 30 points
- **Logic**:
  - PE Reset: When `nifty_pe_last_value - current_price > pe_reset_gap`
  - CE Reset: When `current_price - nifty_ce_last_value > ce_reset_gap`


### 3. **Multiplier System**

The strategy scales position sizes based on **how much** the market has moved:

```python
# Calculate multiplier
price_diff = current_price - nifty_pe_last_value
sell_multiplier = int(price_diff / pe_gap)

# Example:
# pe_gap = 20, price_diff = 47
# sell_multiplier = int(47 / 20) = 2
# Total quantity = 2 × pe_quantity = 2 × 75 = 150
```

**Key Features:**
- **Integer division**: Ensures whole number multipliers
- **Threshold check**: Maximum multiplier limit (default: 5x)
- **Prevents oversizing**: Blocks trades when multiplier exceeds threshold

**Example Progression:**
```
Price Movement | Multiplier | Quantity (base=75)
---------------|------------|-------------------
0-19 points    | 0          | No trade
20-39 points   | 1          | 75
40-59 points   | 2          | 150
60-79 points   | 3          | 225
80-99 points   | 4          | 300
100+ points    | 5          | 375 (max at threshold=5)
```

### 4. **Strike Selection Engine**

The strategy finds appropriate option strikes through a smart matching algorithm:

```python
def _find_nifty_symbol_from_gap(option_type, ltp, gap):
    # Calculate target strike
    if option_type == "PE":
        target_strike = ltp - gap  # Below current price
    else:  # CE
        target_strike = ltp + gap  # Above current price
    
    # Filter instruments
    df = instruments[
        (instruments['symbol'].str.contains(symbol_initials)) &
        (instruments['instrument_type'] == option_type) &
        (instruments['segment'] == "NFO-OPT")
    ]
    
    # Find closest strike within tolerance
    df['target_strike_diff'] = (df['strike'] - target_strike).abs()
    tolerance = strike_difference / 2  # Half the strike gap
    df = df[df['target_strike_diff'] <= tolerance]
    
    # Return best match
    return df.sort_values('target_strike_diff').iloc[0]
```

**Example:**
```
Current NIFTY: 24,500
pe_symbol_gap: 200
strike_difference: 50

Target PE Strike: 24,500 - 200 = 24,300
Available strikes: [24,250, 24,300, 24,350]
Tolerance: 50 / 2 = 25 points
Best match: 24,300 (exact match)
```

### 5. **Premium Filtering**

Before executing trades, the strategy ensures adequate option premium:

```python
# Check if premium meets minimum threshold
while True:
    instrument = find_nifty_symbol_from_gap("PE", current_price, temp_gap)
    quote = broker.get_quote(instrument['symbol'])
    
    if quote.last_price < min_price_to_sell:
        # Premium too low, try closer strike
        temp_gap -= lot_size
        continue
    
    # Premium acceptable, execute trade
    place_order(instrument['symbol'], total_quantity)
    break
```

**Why This Matters:**
- **Liquidity**: Low premium options often have poor liquidity
- **Transaction costs**: Very cheap options may not be worth trading costs
- **Risk/Reward**: Ensures minimum potential profit per trade

**Example:**
```
Initial gap: 200 points
Min price to sell: 15 rupees

Attempt 1: 24,300 PE @ ₹12 → Too low
Attempt 2: 24,350 PE @ ₹18 → Acceptable, trade executed
```

---

## How It Works

### Main Execution Flow

```
┌─────────────────────────────────────────────────────────────┐
│              SURVIVOR STRATEGY EXECUTION FLOW               │
└─────────────────────────────────────────────────────────────┘

1. Tick Update Received
   │
   ├─→ Extract current_price from tick data
   │
   ├─→ Call _handle_pe_trade(current_price)
   │   │
   │   ├─→ Check: current_price > nifty_pe_last_value?
   │   │   └─→ NO: Skip PE trade
   │   │
   │   ├─→ Calculate: price_diff = current_price - nifty_pe_last_value
   │   │
   │   ├─→ Check: price_diff > pe_gap?
   │   │   └─→ NO: Skip PE trade
   │   │
   │   ├─→ Calculate: sell_multiplier = int(price_diff / pe_gap)
   │   │
   │   ├─→ Check: sell_multiplier <= threshold?
   │   │   └─→ NO: Block trade (risk management)
   │   │
   │   ├─→ Update: nifty_pe_last_value += pe_gap * sell_multiplier
   │   │
   │   ├─→ Find PE strike at (current_price - pe_symbol_gap)
   │   │
   │   ├─→ Check: option_premium >= min_price_to_sell?
   │   │   └─→ NO: Try closer strike
   │   │
   │   ├─→ Execute: Sell PE option
   │   │
   │   └─→ Set: pe_reset_gap_flag = 1
   │
   ├─→ Call _handle_ce_trade(current_price)
   │   │
   │   ├─→ Check: current_price < nifty_ce_last_value?
   │   │   └─→ NO: Skip CE trade
   │   │
   │   ├─→ Calculate: price_diff = nifty_ce_last_value - current_price
   │   │
   │   ├─→ Check: price_diff > ce_gap?
   │   │   └─→ NO: Skip CE trade
   │   │
   │   ├─→ Calculate: sell_multiplier = int(price_diff / ce_gap)
   │   │
   │   ├─→ Check: sell_multiplier <= threshold?
   │   │   └─→ NO: Block trade (risk management)
   │   │
   │   ├─→ Update: nifty_ce_last_value -= ce_gap * sell_multiplier
   │   │
   │   ├─→ Find CE strike at (current_price + ce_symbol_gap)
   │   │
   │   ├─→ Check: option_premium >= min_price_to_sell?
   │   │   └─→ NO: Try closer strike
   │   │
   │   ├─→ Execute: Sell CE option
   │   │
   │   └─→ Set: ce_reset_gap_flag = 1
   │
   └─→ Call _reset_reference_values(current_price)
       │
       ├─→ PE Reset Check:
       │   If (nifty_pe_last_value - current_price) > pe_reset_gap
       │   AND pe_reset_gap_flag == 1:
       │   └─→ Reset: nifty_pe_last_value = current_price + pe_reset_gap
       │
       └─→ CE Reset Check:
           If (current_price - nifty_ce_last_value) > ce_reset_gap
           AND ce_reset_gap_flag == 1:
           └─→ Reset: nifty_ce_last_value = current_price - ce_reset_gap
```

---

## Configuration Parameters

### Core Trading Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `index_symbol` | NSE:NIFTY 50 | Underlying index for price tracking |
| `symbol_initials` | NIFTY25807 | Option series identifier (expiry) |
| `exchange` | NFO | Exchange for trading |
| `order_type` | MARKET | Order type (MARKET/LIMIT) |
| `product_type` | NRML | Product type for margin |
| `trans_type` | SELL | Transaction type |
| `tag` | Survivor | Order tag for identification |

### Gap Parameters (Trade Triggers)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `pe_gap` | 20 | Points upward to trigger PE sell |
| `ce_gap` | 20 | Points downward to trigger CE sell |
| `pe_reset_gap` | 30 | Points to reset PE reference (favorable move) |
| `ce_reset_gap` | 30 | Points to reset CE reference (favorable move) |

### Strike Selection Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `pe_symbol_gap` | 200 | Distance below spot for PE strikes |
| `ce_symbol_gap` | 200 | Distance above spot for CE strikes |

### Position Sizing Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `pe_quantity` | 75 | Base quantity for PE trades |
| `ce_quantity` | 75 | Base quantity for CE trades |
| `sell_multiplier_threshold` | 5 | Maximum position multiplier |

### Risk Management Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `min_price_to_sell` | 15 | Minimum option premium (rupees) |
| `pe_start_point` | 0 | Initial PE reference (0 = use current price) |
| `ce_start_point` | 0 | Initial CE reference (0 = use current price) |

---

## Trading Logic Deep Dive

### PE (Put) Trading Logic

**When to Trade:** When NIFTY moves UP

```python
def _handle_pe_trade(current_price):
    # STEP 1: Check if price moved up
    if current_price <= nifty_pe_last_value:
        return  # No upward movement, skip
    
    # STEP 2: Calculate price difference
    price_diff = current_price - nifty_pe_last_value
    
    # STEP 3: Check if difference exceeds gap threshold
    if price_diff <= pe_gap:
        return  # Movement too small, skip
    
    # STEP 4: Calculate position multiplier
    sell_multiplier = int(price_diff / pe_gap)
    
    # STEP 5: Risk check - multiplier threshold
    if sell_multiplier > sell_multiplier_threshold:
        return  # Position too large, block trade
    
    # STEP 6: Update reference value
    nifty_pe_last_value += pe_gap * sell_multiplier
    
    # STEP 7: Calculate total quantity
    total_quantity = sell_multiplier * pe_quantity
    
    # STEP 8: Find suitable PE strike
    temp_gap = pe_symbol_gap
    while True:
        instrument = find_nifty_symbol_from_gap("PE", current_price, temp_gap)
        quote = broker.get_quote(instrument['symbol'])
        
        # STEP 9: Check premium threshold
        if quote.last_price < min_price_to_sell:
            temp_gap -= lot_size  # Try closer strike
            continue
        
        # STEP 10: Execute trade
        place_order(instrument['symbol'], total_quantity)
        pe_reset_gap_flag = 1  # Enable reset logic
        break
```

**Why Sell PEs When Market Goes Up?**
- When NIFTY rises, out-of-the-money PEs become safer to sell
- Lower probability of the market falling to those strike prices
- Collects premium as the sold PEs decay in value

### CE (Call) Trading Logic

**When to Trade:** When NIFTY moves DOWN

```python
def _handle_ce_trade(current_price):
    # STEP 1: Check if price moved down
    if current_price >= nifty_ce_last_value:
        return  # No downward movement, skip
    
    # STEP 2: Calculate price difference
    price_diff = nifty_ce_last_value - current_price
    
    # STEP 3: Check if difference exceeds gap threshold
    if price_diff <= ce_gap:
        return  # Movement too small, skip
    
    # STEP 4: Calculate position multiplier
    sell_multiplier = int(price_diff / ce_gap)
    
    # STEP 5: Risk check - multiplier threshold
    if sell_multiplier > sell_multiplier_threshold:
        return  # Position too large, block trade
    
    # STEP 6: Update reference value
    nifty_ce_last_value -= ce_gap * sell_multiplier
    
    # STEP 7: Calculate total quantity
    total_quantity = sell_multiplier * ce_quantity
    
    # STEP 8: Find suitable CE strike
    temp_gap = ce_symbol_gap
    while True:
        instrument = find_nifty_symbol_from_gap("CE", current_price, temp_gap)
        quote = broker.get_quote(instrument['symbol'])
        
        # STEP 9: Check premium threshold
        if quote.last_price < min_price_to_sell:
            temp_gap -= lot_size  # Try closer strike
            continue
        
        # STEP 10: Execute trade
        place_order(instrument['symbol'], total_quantity)
        ce_reset_gap_flag = 1  # Enable reset logic
        break
```

**Why Sell CEs When Market Goes Down?**
- When NIFTY falls, out-of-the-money CEs become safer to sell
- Lower probability of the market rising to those strike prices
- Collects premium as the sold CEs decay in value

### Reset Mechanism

**Purpose:** Prevents reference values from drifting too far from current market

```python
def _reset_reference_values(current_price):
    # PE RESET LOGIC
    # Triggered when market moves DOWN favorably after selling PEs
    if (nifty_pe_last_value - current_price) > pe_reset_gap and pe_reset_gap_flag:
        # Reset PE reference closer to current price
        nifty_pe_last_value = current_price + pe_reset_gap
        logger.info(f"PE reference reset to {nifty_pe_last_value}")
    
    # CE RESET LOGIC
    # Triggered when market moves UP favorably after selling CEs
    if (current_price - nifty_ce_last_value) > ce_reset_gap and ce_reset_gap_flag:
        # Reset CE reference closer to current price
        nifty_ce_last_value = current_price - ce_reset_gap
        logger.info(f"CE reference reset to {nifty_ce_last_value}")
```

**Why Reset?**
- **Prevents excessive drift**: Without resets, references could become very far from market
- **Maintains responsiveness**: Keeps strategy active in changing market conditions
- **Reduces position buildup**: Limits total short option exposure

---

## Example Scenarios

### Scenario 1: Uptrending Market (PE Trades)

```
Configuration:
- pe_gap: 20
- pe_symbol_gap: 200
- pe_quantity: 75
- pe_reset_gap: 30
- min_price_to_sell: 15

Timeline:

T=0: Initialization
- NIFTY: 24,500
- nifty_pe_last_value: 24,500
- pe_reset_gap_flag: 0

T=1: NIFTY rises to 24,525
- price_diff: 25 (> pe_gap of 20)
- sell_multiplier: int(25/20) = 1
- Update: nifty_pe_last_value = 24,520 (24,500 + 20*1)
- Target strike: 24,525 - 200 = 24,325
- Find: 24,300 PE @ ₹18 (> ₹15 min)
- Execute: SELL 75 qty of 24,300 PE
- Set: pe_reset_gap_flag = 1

T=2: NIFTY rises to 24,565
- price_diff: 45 (24,565 - 24,520)
- sell_multiplier: int(45/20) = 2
- Update: nifty_pe_last_value = 24,560 (24,520 + 20*2)
- Target strike: 24,565 - 200 = 24,365
- Find: 24,350 PE @ ₹16
- Execute: SELL 150 qty (2 × 75) of 24,350 PE

T=3: NIFTY falls to 24,520 (favorable movement)
- Reset check: (24,560 - 24,520) = 40 > pe_reset_gap(30)
- Reset: nifty_pe_last_value = 24,550 (24,520 + 30)
- Reason: Market moved favorably, reset reference closer

Result:
- Sold 2 PE positions totaling 225 qty
- References adjusted to prevent excessive drift
```

### Scenario 2: Downtrending Market (CE Trades)

```
Configuration:
- ce_gap: 20
- ce_symbol_gap: 200
- ce_quantity: 75
- ce_reset_gap: 30

Timeline:

T=0: Initialization
- NIFTY: 24,500
- nifty_ce_last_value: 24,500
- ce_reset_gap_flag: 0

T=1: NIFTY falls to 24,475
- price_diff: 25 (24,500 - 24,475)
- sell_multiplier: int(25/20) = 1
- Update: nifty_ce_last_value = 24,480 (24,500 - 20*1)
- Target strike: 24,475 + 200 = 24,675
- Find: 24,700 CE @ ₹20
- Execute: SELL 75 qty of 24,700 CE
- Set: ce_reset_gap_flag = 1

T=2: NIFTY falls to 24,420
- price_diff: 60 (24,480 - 24,420)
- sell_multiplier: int(60/20) = 3
- Update: nifty_ce_last_value = 24,420 (24,480 - 20*3)
- Execute: SELL 225 qty (3 × 75) of appropriate CE

T=3: NIFTY rises to 24,460 (favorable movement)
- Reset check: (24,460 - 24,420) = 40 > ce_reset_gap(30)
- Reset: nifty_ce_last_value = 24,430 (24,460 - 30)

Result:
- Sold 2 CE positions totaling 300 qty
- References adjusted for market conditions
```

### Scenario 3: Volatile Sideways Market

```
Configuration:
- pe_gap: 20, ce_gap: 20
- pe_quantity: 75, ce_quantity: 75

Timeline:

T=0: NIFTY @ 24,500
- PE ref: 24,500, CE ref: 24,500

T=1: NIFTY → 24,530 (UP 30)
- Trigger: PE trade
- Sell: 75 qty PE (multiplier=1)
- PE ref → 24,520

T=2: NIFTY → 24,470 (DOWN 50)
- Trigger: CE trade
- Sell: 150 qty CE (multiplier=2, from 24,520)
- CE ref → 24,480

T=3: NIFTY → 24,510 (UP 40)
- Trigger: PE trade
- Sell: 150 qty PE (multiplier=2, from 24,470)
- PE ref → 24,510
- Also: CE reset triggered (24,510 - 24,480 = 30)
- CE ref → 24,480 (reset to 24,510 - 30)

Result:
- Both PE and CE positions accumulated
- Collected premium from both sides
- References continuously adjusted
```

### Scenario 4: Large Gap Move (Multiplier Threshold)

```
Configuration:
- pe_gap: 20
- sell_multiplier_threshold: 5

Timeline:

T=0: NIFTY @ 24,500
- PE ref: 24,500

T=1: NIFTY → 24,650 (UP 150 points!)
- price_diff: 150
- sell_multiplier: int(150/20) = 7
- Check: 7 > threshold(5)
- Action: BLOCK TRADE (risk management)
- Log: "Sell multiplier 7 breached threshold 5"

Result:
- No trade executed
- Prevents excessive position during extreme moves
- Protects capital during volatility spikes
```

### Scenario 5: Premium Filtering

```
Configuration:
- pe_symbol_gap: 200
- min_price_to_sell: 15
- strike_difference: 50

Timeline:

T=1: NIFTY @ 24,500, PE trade triggered
- Initial gap: 200
- Target strike: 24,300

Attempt 1:
- Check: 24,300 PE @ ₹12
- Result: 12 < 15 (min threshold)
- Action: Adjust gap to 150 (200 - 50)

Attempt 2:
- Check: 24,350 PE @ ₹16
- Result: 16 >= 15 (acceptable)
- Action: SELL 24,350 PE

Result:
- Automatically adjusted strike to ensure adequate premium
- Traded 24,350 PE instead of 24,300 PE
```

---

## Risk Management

### 1. Multiplier Threshold

**Purpose:** Prevent excessive position sizing during large moves

```python
sell_multiplier_threshold = 5  # Maximum 5x base quantity

if sell_multiplier > sell_multiplier_threshold:
    logger.warning(f"Multiplier {sell_multiplier} exceeds threshold")
    return  # Block trade
```

**Example:**
```
Base quantity: 75
Maximum position: 5 × 75 = 375 lots
Maximum price move: 5 × 20 (gap) = 100 points in one direction
```

### 2. Premium Filtering

**Purpose:** Ensure adequate liquidity and value

```python
min_price_to_sell = 15  # Minimum ₹15 premium

if option_price < min_price_to_sell:
    # Try closer strike (more premium)
    temp_gap -= lot_size
```

**Why ₹15?**
- Below ₹15: Often illiquid, wide bid-ask spreads
- Transaction costs may eat into profits
- Risk/reward becomes unfavorable

### 3. Reset Mechanism

**Purpose:** Prevent runaway reference drift

```python
# Without reset:
# PE ref could drift to 25,000 while market at 24,500
# Would never trigger new PE trades

# With reset:
# PE ref stays within 30 points of market
# Maintains strategy responsiveness
```

### 4. Strike Selection Tolerance

**Purpose:** Ensure valid strikes are found

```python
tolerance = strike_difference / 2  # Half the gap

# Example: strike_difference = 50
# tolerance = 25 points
# Allows strikes within ±25 of target
```

### 5. Order Type (MARKET vs LIMIT)

**Default: MARKET**
- Ensures execution
- Accepts current market price
- Good for liquid options

**Alternative: LIMIT**
- Better price control
- May not execute immediately
- Requires additional logic

---

## Running the Strategy

### Command Line Interface

```bash
# Use default configuration
python survivor.py

# Override specific parameters
python survivor.py --symbol-initials NIFTY25FEB06 --pe-gap 25 --ce-gap 25

# Full customization
python survivor.py \
    --symbol-initials NIFTY25FEB06 \
    --index-symbol "NSE:NIFTY 50" \
    --pe-gap 25 --ce-gap 25 \
    --pe-symbol-gap 250 --ce-symbol-gap 250 \
    --pe-quantity 50 --ce-quantity 50 \
    --pe-reset-gap 40 --ce-reset-gap 40 \
    --min-price-to-sell 20 \
    --sell-multiplier-threshold 4

# Show configuration without running
python survivor.py --show-config
```

### Configuration Validation

The strategy includes built-in validation:

```python
# Checks if using default values
if all parameters are default:
    ERROR: Must update configuration before running
    
if some parameters are default:
    WARNING: Some values still at defaults
    Prompt: Continue? (yes/no)
    
if all parameters updated:
    SUCCESS: Validation passed, starting strategy
```

### Main Loop

```python
while True:
    # Get tick data from websocket
    tick_data = dispatcher._main_queue.get()
    
    # Extract price
    symbol_data = tick_data[0] if isinstance(tick_data, list) else tick_data
    
    # Process through strategy
    if 'last_price' in symbol_data or 'ltp' in symbol_data:
        strategy.on_ticks_update(symbol_data)
```

### Logging

The strategy provides comprehensive logging:

```
INFO: Nifty under control. PE=24520, CE=24480, Current=24500
INFO: Execute PE sell @ NIFTY25807 24300PE × 75, Market Price
INFO: Resetting PE value from 24520 to 24530
WARNING: Sell multiplier 6 breached threshold 5
ERROR: No suitable instrument found for CE with gap 150
```

---

## Important Implementation Details

### 1. Websocket Integration

```python
# Subscribe to NIFTY index
instrument_token = "NSE:NIFTY 50"
broker.symbols_to_subscribe([instrument_token])

# Callback processes each tick
def on_ticks(ws, ticks):
    dispatcher.dispatch(ticks)  # Send to strategy queue
```

### 2. Order Placement

```python
def _place_order(symbol, quantity):
    req = OrderRequest(
        symbol=symbol,
        exchange=Exchange.NFO,
        transaction_type=TransactionType.SELL,
        quantity=quantity,
        product_type=ProductType.MARGIN,
        order_type=OrderType.MARKET,
        price=None,
        tag="Survivor"
    )
    order_resp = broker.place_order(req)
    logger.info(f"Order ID: {order_resp.order_id}")
```

### 3. Instrument Filtering

```python
# Load all instruments
instruments = broker.get_instruments()

# Filter for specific series
instruments = instruments[
    instruments['symbol'].str.contains(symbol_initials)
]

# Filter for option type
pe_instruments = instruments[
    instruments['instrument_type'] == "PE"
]
```

### 4. Strike Difference Calculation

```python
def _get_strike_difference(symbol_initials):
    # Get CE instruments
    ce_instruments = instruments[
        instruments['symbol'].str.endswith('CE')
    ]
    
    # Sort by strike
    sorted_strikes = ce_instruments.sort_values('strike')
    
    # Calculate difference between first two
    strike_diff = sorted_strikes.iloc[1]['strike'] - sorted_strikes.iloc[0]['strike']
    
    return strike_diff  # e.g., 50 for NIFTY
```

---

## Differences from Wave Strategy

| Feature | Survivor | Wave |
|---------|----------|------|
| **Type** | Options selling | Market making |
| **Instruments** | Options only | Futures/Options |
| **Direction** | Sells options in move direction | Buys and sells simultaneously |
| **Position** | Accumulates short options | Delta neutral |
| **Trigger** | Gap-based directional moves | Any price change |
| **Multiplier** | Based on price movement | Based on position imbalance |
| **Reset** | Reference value adjustment | None (uses last execution price) |
| **Risk** | Unlimited (short options) | Limited spread |
| **Greeks** | Not calculated | Delta-managed |

---

## Summary

The Survivor Strategy is a **systematic options selling strategy** that:

1. **Sells OTM options** when market moves beyond thresholds
2. **Scales positions** based on movement magnitude via multipliers
3. **Manages risk** through:
   - Multiplier threshold caps
   - Premium filtering
   - Reference value resets
   - Strike selection tolerance
4. **Maintains responsiveness** via reset mechanism
5. **Ensures quality trades** through premium and liquidity filters

**Key Insight**: The strategy doesn't predict direction. Instead, it **reacts to movements** by selling options that are farther out-of-the-money, collecting premium while the market moves away from those strikes.

**Best Use Cases**:
- Volatile but range-bound markets
- When implied volatility is high (higher premiums)
- Trending markets (with proper position limits)
- Traders comfortable with unlimited risk of short options

**Risks to Monitor**:
- **Unlimited loss potential** (naked option selling)
- **Margin requirements** (can increase during volatility)
- **Gap risk** (overnight/sudden moves against positions)
- **Accumulation risk** (building large short option positions)
- **Assignment risk** (short options can be exercised)

**Capital Requirements**:
- High margin requirements for naked option selling
- Need buffer for volatility spikes
- Recommended: Conservative position sizing relative to capital

---

## Additional Resources

- **Configuration File**: `strategy/configs/survivor.yml`
- **Order Tracking**: Uses `orders.py` OrderTracker class
- **Broker Interface**: Via `brokers/` module (BrokerGateway)
- **Logging**: Comprehensive logging via `logger.py`
- **Data Dispatcher**: Real-time tick processing via `dispatcher.py`

---

**Document Version**: 1.0  
**Last Updated**: 2026-02-12  
**Author**: Auto-generated documentation

**⚠️ RISK WARNING**: This strategy involves selling naked options which carries unlimited risk. Only use with proper risk management, adequate capital, and full understanding of options trading risks.

---

## Visual Flowcharts and State Diagrams

### State Diagram: Strategy Lifecycle

```
                    ┌─────────────────────────────────────┐
                    │     INITIALIZATION                  │
                    │  - Load config                      │
                    │  - Download instruments             │
                    │  - Set initial references           │
                    │  - Subscribe to NIFTY index         │
                    └──────────────┬──────────────────────┘
                                   │
                                   ▼
                    ┌──────────────────────────────────────┐
                    │     MONITORING STATE                 │
                    │  - Waiting for tick updates          │
                    │  - nifty_pe_last_value: 24,500       │
                    │  - nifty_ce_last_value: 24,500       │
                    │  - pe_reset_gap_flag: 0              │
                    │  - ce_reset_gap_flag: 0              │
                    └──────────────┬───────────────────────┘
                                   │
                    ┌──────────────┴──────────────┐
                    │  Tick Update Received       │
                    │  Current Price: 24,XXX      │
                    └──────────────┬──────────────┘
                                   │
                    ┌──────────────┴──────────────┐
                    │                             │
                    ▼                             ▼
    ┌───────────────────────────┐   ┌───────────────────────────┐
    │   PE TRADE CHECK          │   │   CE TRADE CHECK          │
    │                           │   │                           │
    │ Price > PE Ref?           │   │ Price < CE Ref?           │
    │   └─→ NO: Skip            │   │   └─→ NO: Skip            │
    │   └─→ YES: ↓              │   │   └─→ YES: ↓              │
    │                           │   │                           │
    │ Diff > pe_gap?            │   │ Diff > ce_gap?            │
    │   └─→ NO: Skip            │   │   └─→ NO: Skip            │
    │   └─→ YES: ↓              │   │   └─→ YES: ↓              │
    │                           │   │                           │
    │ Multiplier ≤ threshold?   │   │ Multiplier ≤ threshold?   │
    │   └─→ NO: BLOCK           │   │   └─→ NO: BLOCK           │
    │   └─→ YES: ↓              │   │   └─→ YES: ↓              │
    └───────────┬───────────────┘   └───────────┬───────────────┘
                │                               │
                ▼                               ▼
    ┌───────────────────────────┐   ┌───────────────────────────┐
    │   PE EXECUTION STATE      │   │   CE EXECUTION STATE      │
    │                           │   │                           │
    │ 1. Update PE reference    │   │ 1. Update CE reference    │
    │ 2. Find PE strike         │   │ 2. Find CE strike         │
    │ 3. Check premium          │   │ 3. Check premium          │
    │ 4. Sell PE option         │   │ 4. Sell CE option         │
    │ 5. Set reset flag = 1     │   │ 5. Set reset flag = 1     │
    └───────────┬───────────────┘   └───────────┬───────────────┘
                │                               │
                └──────────────┬────────────────┘
                               │
                               ▼
                    ┌──────────────────────────────────────┐
                    │   RESET CHECK STATE                  │
                    │                                      │
                    │ PE Reset Eligible?                   │
                    │  - (PE_ref - Price) > pe_reset_gap   │
                    │  - pe_reset_gap_flag == 1            │
                    │    └─→ YES: Reset PE reference       │
                    │                                      │
                    │ CE Reset Eligible?                   │
                    │  - (Price - CE_ref) > ce_reset_gap   │
                    │  - ce_reset_gap_flag == 1            │
                    │    └─→ YES: Reset CE reference       │
                    └──────────────┬───────────────────────┘
                                   │
                                   ▼
                    ┌──────────────────────────────────────┐
                    │   BACK TO MONITORING                 │
                    │   (Loop continues)                   │
                    └──────────────────────────────────────┘
```

### Flowchart: PE Trade Decision Tree

```
                         ┌──────────────────┐
                         │  Tick Received   │
                         │  Price = P       │
                         └────────┬─────────┘
                                  │
                                  ▼
                         ┌──────────────────┐
                         │  P > PE_ref?     │
                         └────────┬─────────┘
                                  │
                    ┌─────────────┴─────────────┐
                    │ NO                        │ YES
                    ▼                           ▼
            ┌──────────────┐          ┌─────────────────┐
            │  Skip PE     │          │  diff = P - PE_ref│
            │  Trade       │          └────────┬─────────┘
            └──────────────┘                   │
                                              ▼
                                    ┌──────────────────┐
                                    │  diff > pe_gap?  │
                                    └────────┬─────────┘
                                             │
                               ┌─────────────┴─────────────┐
                               │ NO                        │ YES
                               ▼                           ▼
                       ┌──────────────┐          ┌─────────────────────┐
                       │  Skip PE     │          │  multiplier =       │
                       │  Trade       │          │  int(diff/pe_gap)   │
                       └──────────────┘          └────────┬────────────┘
                                                          │
                                                          ▼
                                              ┌──────────────────────────┐
                                              │  multiplier ≤ threshold? │
                                              └────────┬─────────────────┘
                                                       │
                                         ┌─────────────┴─────────────┐
                                         │ NO                        │ YES
                                         ▼                           ▼
                                 ┌──────────────┐          ┌──────────────────────┐
                                 │  BLOCK TRADE │          │ Update PE_ref:       │
                                 │  (Risk Mgmt) │          │ += pe_gap*multiplier │
                                 └──────────────┘          └────────┬─────────────┘
                                                                    │
                                                                    ▼
                                                        ┌─────────────────────────┐
                                                        │ qty = multiplier * base │
                                                        └────────┬────────────────┘
                                                                 │
                                                                 ▼
                                                      ┌──────────────────────────┐
                                                      │ Find PE strike:          │
                                                      │ target = P - symbol_gap  │
                                                      └────────┬─────────────────┘
                                                               │
                                                               ▼
                                                      ┌──────────────────────────┐
                                                      │ Get option premium       │
                                                      └────────┬─────────────────┘
                                                               │
                                                  ┌────────────┴────────────┐
                                                  │ premium ≥ min_price?    │
                                                  └────────────┬────────────┘
                                                               │
                                                  ┌────────────┴────────────┐
                                                  │ NO                      │ YES
                                                  ▼                         ▼
                                        ┌──────────────────┐     ┌──────────────────┐
                                        │ Adjust strike:   │     │ SELL PE OPTION   │
                                        │ symbol_gap -= 50 │     │ Set reset_flag=1 │
                                        └────────┬─────────┘     └──────────────────┘
                                                 │
                                                 └─────┐
                                                       │
                                        (Loop back to Find PE strike)
```

### Flowchart: Reset Logic

```
                         ┌──────────────────────────────┐
                         │  After PE/CE Trade           │
                         │  reset_gap_flag set to 1     │
                         └──────────────┬───────────────┘
                                        │
                                        ▼
                         ┌──────────────────────────────┐
                         │  Every Tick Update           │
                         │  Current Price = P           │
                         └──────────────┬───────────────┘
                                        │
                    ┌───────────────────┴───────────────────┐
                    │                                       │
                    ▼                                       ▼
        ┌────────────────────────┐              ┌────────────────────────┐
        │  PE RESET CHECK        │              │  CE RESET CHECK        │
        │                        │              │                        │
        │  Condition 1:          │              │  Condition 1:          │
        │  (PE_ref - P)          │              │  (P - CE_ref)          │
        │  > pe_reset_gap?       │              │  > ce_reset_gap?       │
        │                        │              │                        │
        │  Condition 2:          │              │  Condition 2:          │
        │  pe_reset_flag == 1?   │              │  ce_reset_flag == 1?   │
        └────────────┬───────────┘              └────────────┬───────────┘
                     │                                       │
         ┌───────────┴────────────┐              ┌───────────┴────────────┐
         │ BOTH YES               │ NO           │ BOTH YES               │ NO
         ▼                        ▼              ▼                        ▼
    ┌─────────────────┐   ┌────────────┐   ┌─────────────────┐   ┌────────────┐
    │ RESET PE_ref    │   │ No Reset   │   │ RESET CE_ref    │   │ No Reset   │
    │ = P + pe_reset  │   │            │   │ = P - ce_reset  │   │            │
    │                 │   │            │   │                 │   │            │
    │ Log: "PE reset" │   └────────────┘   │ Log: "CE reset" │   └────────────┘
    └─────────────────┘                    └─────────────────┘

    Why Reset?
    - Market moved FAVORABLY after trade
    - PE trade: Market went UP, then came DOWN (profit opportunity)
    - CE trade: Market went DOWN, then came UP (profit opportunity)
    - Reset brings reference closer to current price
    - Maintains strategy responsiveness
```

---

## Detailed Step-by-Step Example: Complete Trading Day

Let me walk you through a complete trading day to show exactly how the strategy behaves:

### Setup
```yaml
Configuration:
  symbol_initials: NIFTY25FEB06
  index_symbol: NSE:NIFTY 50
  
  pe_gap: 20
  ce_gap: 20
  pe_reset_gap: 30
  ce_reset_gap: 30
  
  pe_symbol_gap: 200
  ce_symbol_gap: 200
  
  pe_quantity: 75
  ce_quantity: 75
  
  sell_multiplier_threshold: 5
  min_price_to_sell: 15
```

### Timeline with Complete Details

---

#### **9:15 AM - Strategy Initialization**

```
Action: Strategy starts
Current NIFTY: 24,500

Initialization:
  ✓ Download instruments from broker
  ✓ Filter for NIFTY25FEB06 options
  ✓ Calculate strike_difference = 50
  ✓ Set nifty_pe_last_value = 24,500
  ✓ Set nifty_ce_last_value = 24,500
  ✓ Set pe_reset_gap_flag = 0
  ✓ Set ce_reset_gap_flag = 0
  ✓ Subscribe to NSE:NIFTY 50 websocket

Status: MONITORING - Waiting for price movements
```

---

#### **9:20 AM - First Price Movement (Upward)**

```
Tick Update Received:
  Current NIFTY: 24,525
  Change: +25 points

PE Trade Check:
  ✓ Price (24,525) > PE_ref (24,500)? YES
  ✓ Diff = 24,525 - 24,500 = 25
  ✓ Diff (25) > pe_gap (20)? YES
  ✓ sell_multiplier = int(25/20) = 1
  ✓ Multiplier (1) ≤ threshold (5)? YES
  
  → UPDATE PE Reference:
    nifty_pe_last_value = 24,500 + (20 × 1) = 24,520

Strike Selection:
  Target strike = 24,525 - 200 = 24,325
  Available strikes: [24,250, 24,300, 24,350, 24,400]
  Closest match = 24,300 (within tolerance of 25 points)
  
  Check premium: 24,300 PE quote
    Last Price: ₹18.50
    ✓ Premium (18.50) ≥ min_price (15)? YES

Order Execution:
  Symbol: NIFTY25FEB06 24300 PE
  Transaction: SELL
  Quantity: 75 (1 × 75)
  Order Type: MARKET
  Order ID: 12345678
  
  ✓ Set pe_reset_gap_flag = 1

LOG: "Execute PE sell @ NIFTY25FEB06 24300PE × 75, Market Price"

CE Trade Check:
  ✗ Price (24,525) < CE_ref (24,500)? NO
  → Skip CE trade

Reset Check:
  PE: (24,520 - 24,525) = -5, not > 30 → No reset
  CE: (24,525 - 24,500) = 25, not > 30 → No reset

Current State:
  PE_ref: 24,520
  CE_ref: 24,500
  PE flag: 1
  CE flag: 0
  Positions: Short 75 qty of 24,300 PE
```

---

#### **9:35 AM - Continued Upward Movement**

```
Tick Update:
  Current NIFTY: 24,570
  Change from last PE trade: +50 points

PE Trade Check:
  ✓ Price (24,570) > PE_ref (24,520)? YES
  ✓ Diff = 24,570 - 24,520 = 50
  ✓ Diff (50) > pe_gap (20)? YES
  ✓ sell_multiplier = int(50/20) = 2
  ✓ Multiplier (2) ≤ threshold (5)? YES
  
  → UPDATE PE Reference:
    nifty_pe_last_value = 24,520 + (20 × 2) = 24,560

Strike Selection:
  Target = 24,570 - 200 = 24,370
  Closest = 24,350 PE
  Premium = ₹22.00 (✓ > ₹15)

Order Execution:
  Symbol: NIFTY25FEB06 24350 PE
  Quantity: 150 (2 × 75)
  Order ID: 12345679

Current Positions:
  - Short 75 of 24,300 PE (from 9:20 AM)
  - Short 150 of 24,350 PE (new)
  Total PE exposure: 225 lots
```

---

#### **10:00 AM - Market Reversal (Downward)**

```
Tick Update:
  Current NIFTY: 24,520
  Change: -50 points from peak

PE Trade Check:
  ✗ Price (24,520) > PE_ref (24,560)? NO
  → Skip PE trade

CE Trade Check:
  ✓ Price (24,520) < CE_ref (24,500)? NO... Wait!
  ✗ Price (24,520) < CE_ref (24,500)? NO
  → Skip CE trade

Reset Check:
  PE Reset:
    ✓ pe_reset_gap_flag = 1? YES
    ✓ (PE_ref - Price) = (24,560 - 24,520) = 40
    ✓ 40 > pe_reset_gap (30)? YES
    
    → RESET PE REFERENCE!
      nifty_pe_last_value = 24,520 + 30 = 24,550
      pe_reset_gap_flag = 0 (consumed)
  
  CE Reset:
    ✗ ce_reset_gap_flag = 0? NO
    → No CE reset

LOG: "Resetting PE value from 24,560 to 24,550"

Explanation of Reset:
  - Sold PEs when market went UP (to 24,570)
  - Market came back DOWN (to 24,520)
  - Sold PEs are now MORE profitable (moved away from strike)
  - Reset PE reference closer to market (24,550)
  - Next PE trade triggers at 24,570 (24,550 + 20)
  - Without reset: Would need market at 24,580 (24,560 + 20)

Current State:
  PE_ref: 24,550 (RESET)
  CE_ref: 24,500
  PE flag: 0 (consumed)
  CE flag: 0
```

---

#### **10:30 AM - Sharp Downward Move**

```
Tick Update:
  Current NIFTY: 24,450
  Change: -70 points

PE Trade Check:
  ✗ Price (24,450) > PE_ref (24,550)? NO
  → Skip PE trade

CE Trade Check:
  ✓ Price (24,450) < CE_ref (24,500)? YES
  ✓ Diff = 24,500 - 24,450 = 50
  ✓ Diff (50) > ce_gap (20)? YES
  ✓ sell_multiplier = int(50/20) = 2
  ✓ Multiplier (2) ≤ threshold (5)? YES
  
  → UPDATE CE Reference:
    nifty_ce_last_value = 24,500 - (20 × 2) = 24,460

Strike Selection:
  Target = 24,450 + 200 = 24,650
  Closest = 24,650 CE
  Premium = ₹25.50 (✓ > ₹15)

Order Execution:
  Symbol: NIFTY25FEB06 24650 CE
  Quantity: 150 (2 × 75)
  Order ID: 12345680
  
  ✓ Set ce_reset_gap_flag = 1

Current Positions:
  PE side: Short 225 lots total
  CE side: Short 150 lots (new)
```

---

#### **11:00 AM - Volatile Sideways Movement**

```
Tick Update:
  Current NIFTY: 24,485

PE Trade Check:
  ✗ Price (24,485) > PE_ref (24,550)? NO
  → Skip

CE Trade Check:
  ✗ Price (24,485) < CE_ref (24,460)? NO
  → Skip

Reset Check:
  CE Reset:
    ✓ ce_reset_gap_flag = 1? YES
    ✓ (Price - CE_ref) = (24,485 - 24,460) = 25
    ✗ 25 > ce_reset_gap (30)? NO
    → No reset yet (not enough favorable movement)

Current State:
  PE_ref: 24,550
  CE_ref: 24,460
  Waiting for 35 point move in either direction
```

---

#### **11:30 AM - Small Upward Move**

```
Tick Update:
  Current NIFTY: 24,500

CE Trade Check:
  ✗ Price (24,500) < CE_ref (24,460)? NO

Reset Check:
  CE Reset:
    ✓ ce_reset_gap_flag = 1? YES
    ✓ (Price - CE_ref) = (24,500 - 24,460) = 40
    ✓ 40 > ce_reset_gap (30)? YES
    
    → RESET CE REFERENCE!
      nifty_ce_last_value = 24,500 - 30 = 24,470
      ce_reset_gap_flag = 0

LOG: "Resetting CE value from 24,460 to 24,470"

Current State:
  PE_ref: 24,550
  CE_ref: 24,470 (RESET)
  Both flags: 0
```

---

#### **2:00 PM - Large Spike (Risk Management)**

```
Tick Update:
  Current NIFTY: 24,680
  Change from PE_ref: +130 points!

PE Trade Check:
  ✓ Price (24,680) > PE_ref (24,550)? YES
  ✓ Diff = 24,680 - 24,550 = 130
  ✓ Diff (130) > pe_gap (20)? YES
  ✓ sell_multiplier = int(130/20) = 6
  ✗ Multiplier (6) ≤ threshold (5)? NO
  
  → BLOCK TRADE!

LOG: "Sell multiplier 6 breached threshold 5 - Trade blocked"

Explanation:
  - Multiplier of 6 would mean selling 450 lots (6 × 75)
  - Threshold is 5 (max 375 lots)
  - Risk management prevents excessive position
  - PE_ref NOT updated (no trade executed)

Current State:
  PE_ref: 24,550 (unchanged)
  CE_ref: 24,470
  Trade blocked for risk management
```

---

#### **2:15 PM - Price Stabilizes**

```
Tick Update:
  Current NIFTY: 24,630

PE Trade Check:
  ✓ Diff = 24,630 - 24,550 = 80
  ✓ sell_multiplier = int(80/20) = 4
  ✓ Multiplier (4) ≤ threshold (5)? YES
  
  → UPDATE PE Reference:
    nifty_pe_last_value = 24,550 + (20 × 4) = 24,630

Strike Selection:
  Target = 24,630 - 200 = 24,430
  Closest = 24,450 PE
  
  Premium Check:
    Attempt 1: 24,450 PE = ₹12 (✗ < ₹15)
    Adjust gap: 200 - 50 = 150
    
    Attempt 2: 24,500 PE = ₹17 (✓ ≥ ₹15)

Order Execution:
  Symbol: NIFTY25FEB06 24500 PE
  Quantity: 300 (4 × 75)
  Order ID: 12345681

LOG: "Premium too low at 24450PE, adjusted to 24500PE"

Current Positions Summary:
  PE positions:
    - 75 lots @ 24,300 PE
    - 150 lots @ 24,350 PE
    - 300 lots @ 24,500 PE
    Total: 525 PE lots
  
  CE positions:
    - 150 lots @ 24,650 CE
    Total: 150 CE lots
```

---

#### **3:15 PM - End of Day Snapshot**

```
Final NIFTY: 24,580

Current State:
  PE_ref: 24,630
  CE_ref: 24,470
  PE flag: 1 (reset available)
  CE flag: 0

Total Positions:
  Short PE: 525 lots across 3 strikes
  Short CE: 150 lots across 1 strike
  
Total Premium Collected (example):
  PE premiums: ₹18.50 + ₹22 + ₹17 = ~₹12,000
  CE premiums: ₹25.50 = ~₹3,800
  Total: ~₹15,800 (before costs)

Risk Exposure:
  Unlimited downside (short PEs)
  Unlimited upside (short CEs)
  Net directional bias: Slightly bearish (more PE exposure)

Trades Executed Today:
  1. 9:20 AM: Sell 75 PE (multiplier=1)
  2. 9:35 AM: Sell 150 PE (multiplier=2)
  3. 10:30 AM: Sell 150 CE (multiplier=2)
  4. 2:15 PM: Sell 300 PE (multiplier=4)
  
Trades Blocked:
  1. 2:00 PM: PE trade (multiplier=6 > threshold)

Reset Events:
  1. 10:00 AM: PE reset from 24,560 to 24,550
  2. 11:30 AM: CE reset from 24,460 to 24,470
```

---

### Key Observations from This Example

1. **Multiplier Scaling**: Larger moves = larger positions (1×, 2×, 4×)
2. **Reset Mechanism**: References adjusted 2 times to maintain responsiveness
3. **Risk Management**: One trade blocked due to excessive multiplier
4. **Premium Filtering**: One strike adjusted to ensure adequate premium
5. **Dual-Side Trading**: Collected premium from both PE and CE sides
6. **Asymmetric Exposure**: More PE than CE (market trended up overall)

---
