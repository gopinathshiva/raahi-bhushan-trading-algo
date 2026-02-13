# Wave Trading Strategy - Comprehensive Guide

## Table of Contents
1. [Overview](#overview)
2. [Core Concept](#core-concept)
3. [Strategy Components](#strategy-components)
4. [How It Works](#how-it-works)
5. [Risk Management](#risk-management)
6. [Order Flow](#order-flow)
7. [Configuration Parameters](#configuration-parameters)
8. [Key Features](#key-features)
9. [Example Scenarios](#example-scenarios)

---

## Overview

The **Wave Strategy** is an automated market-making / mean-reversion trading system designed for Indian futures and options markets (NIFTY and BANKNIFTY). It continuously places simultaneous BUY and SELL orders at calculated price gaps around the current market price, profiting from price oscillations (waves) in the market.

### What Problem Does It Solve?
- **Captures small price movements** in both directions
- **Provides liquidity** to the market while earning the spread
- **Automatically manages risk** through delta hedging and position limits
- **Handles order lifecycle** including placement, updates, cancellations, and completions

---

## Core Concept

### The "Wave" Metaphor
The strategy is called "Wave" because it rides price movements like ocean waves:
- When price moves **UP** → the SELL order executes → strategy places new orders
- When price moves **DOWN** → the BUY order executes → strategy places new orders
- Each execution creates a new "wave" of orders around the new price

### Basic Mechanics
```
Current Price: 24,500

BUY Order  placed at: 24,475 (price - buy_gap of 25)
SELL Order placed at: 24,525 (price + sell_gap of 25)

If SELL executes at 24,525:
  → Cancel pending BUY order
  → Update reference price to 24,525
  → Place new wave of orders
```

---

## Strategy Components

### 1. **Initialization Phase**
When the strategy starts, it:
```python
def __init__(self, config: Dict, broker, order_tracker=None):
    # Load configuration parameters
    self.buy_gap = 25  # Price gap for buy orders
    self.sell_gap = 25  # Price gap for sell orders
    self.buy_quantity = 75  # Lot size for buying
    self.sell_quantity = 75  # Lot size for selling
    
    # Download market instruments
    self.broker.download_instruments()
    
    # Get current position
    self.initial_positions['position'] = self._get_position_for_symbol()
    
    # Get current market price
    quote = self.broker.get_quote(self.symbol_name)
    self.scraper_last_price = quote.last_price
```

**Key State Variables:**
- `scraper_last_price`: Last execution price (reference for next orders)
- `initial_positions`: Starting position to calculate drift
- `orders`: Dictionary tracking all active orders
- `prev_wave_buy_price / prev_wave_sell_price`: Previous order prices

### 2. **Gap Scaling System**
The strategy uses **dynamic gap scaling** based on position imbalance:

```python
def _generate_multiplier_scale(self, levels: int = 10):
    buy_scale = [1.3, 1.7, 2.5, 3, 10, 10, 10, 15, 15, 15]
    sell_scale = [1.3, 1.7, 2.5, 3, 10, 10, 10, 15, 15, 15]
    
    # If position_diff = +3 (bought 3 more lots than sold)
    # multiplier = [2.5, 1.0] → buy_gap *= 2.5, sell_gap *= 1.0
    # This WIDENS buy gap to discourage more buying
```

**Purpose**: Prevent runaway positions by making it harder to accumulate more of an imbalanced position.

**Example:**
```
Position Difference: +2 lots (net long)
Base gaps: buy_gap=25, sell_gap=25
Scaled gaps: buy_gap=42.5 (25 * 1.7), sell_gap=25 (25 * 1.0)

Result: Harder to buy more, easier to sell (mean-reversion)
```

### 3. **Price Calculation Engine**

The strategy calculates final order prices through multiple steps:

```python
def _prepare_final_prices(self, scaled_buy_gap, scaled_sell_gap):
    # Step 1: Get current price
    price = self.broker.get_quote(self.symbol_name).last_price
    
    # Step 2: Calculate initial prices using current price AND last execution price
    buy_1 = price - scaled_buy_gap
    buy_2 = self.scraper_last_price - scaled_buy_gap
    sell_1 = price + scaled_sell_gap
    sell_2 = self.scraper_last_price + scaled_sell_gap
    
    best_buy = min(buy_1, buy_2)    # Most aggressive buy
    best_sell = max(sell_1, sell_2)  # Most aggressive sell
    
    # Step 3: Wait for cool-off period
    time.sleep(self.cool_off_time)  # Default: 10 seconds
    
    # Step 4: Re-check price and take best of both
    price_after_wait = self.broker.get_quote(self.symbol_name).last_price
    final_buy = min(best_buy, price_after_wait - scaled_buy_gap)
    final_sell = max(best_sell, price_after_wait + scaled_sell_gap)
    
    return {'buy': final_buy, 'sell': final_sell}
```

**Why This Approach?**
- **Uses last execution price**: Ensures orders stay within reasonable range
- **Cool-off period**: Prevents chasing volatile moves
- **Best price selection**: Maximizes capture probability while maintaining spread

### 4. **Greeks-Based Risk Management**

The strategy calculates portfolio **delta** to prevent directional risk buildup:

```python
def _get_portfolio_greeks(self, index_name: str):
    # For NIFTY or BANKNIFTY, calculate total delta exposure
    
    total_delta = 0
    
    # Futures delta = 1:1 with position
    for futures_position in positions:
        total_delta += quantity  # +75 for long, -75 for short
    
    # Options delta = calculated using Black-Scholes
    for option_position in positions:
        if instrument_type == "CE":
            delta = bs.callDelta * quantity  # 0 to 1
        elif instrument_type == "PE":
            delta = bs.putDelta * quantity   # -1 to 0
        total_delta += delta
    
    return total_delta
```

**Delta Limits** (from config):
```yaml
min_nifty_delta: -100
max_nifty_delta: 100
```

**Enforcement:**
```python
if nifty_delta < -100:  # Too short
    # Restrict: futures sell, CE sell, PE buy
    nifty_restrictions['futures']['sell'] = "no"
    nifty_restrictions['ce']['sell'] = "no"
    nifty_restrictions['pe']['buy'] = "no"

elif nifty_delta > 100:  # Too long
    # Restrict: futures buy, CE buy, PE sell
    nifty_restrictions['futures']['buy'] = "no"
    nifty_restrictions['ce']['buy'] = "no"
    nifty_restrictions['pe']['sell'] = "no"
```

---

## How It Works

### Main Execution Flow

```
┌─────────────────────────────────────────────────────────────┐
│                    WAVE ORDER CYCLE                         │
└─────────────────────────────────────────────────────────────┘

1. place_wave_order() called
   │
   ├─→ Get current position
   │
   ├─→ Calculate position difference
   │   current_diff_scale = (buy_qty - sell_qty) / lot_size
   │
   ├─→ Get scaled gaps based on position imbalance
   │   _get_scaled_gaps(current_diff_scale)
   │
   ├─→ Calculate final prices with cool-off
   │   _prepare_final_prices(scaled_buy_gap, scaled_sell_gap)
   │
   ├─→ Check delta restrictions
   │   _get_dynamic_restrictions()
   │
   ├─→ Execute orders (if not restricted)
   │   _execute_orders(symbol, final_buy, final_sell, ...)
   │
   └─→ Wait for order updates via websocket


2. Order Update Received (via websocket)
   │
   ├─→ handle_order_update(order_data)
   │
   ├─→ If COMPLETE:
   │   ├─→ Cancel associated order (buy cancels sell, vice versa)
   │   ├─→ Update scraper_last_price to execution price
   │   └─→ CALL place_wave_order() → NEW CYCLE
   │
   ├─→ If CANCELLED/REJECTED:
   │   └─→ Remove order and associated order
   │
   └─→ If OPEN/UPDATE:
       └─→ Update prev_wave_buy_price / prev_wave_sell_price


3. Periodic Check (every 60 seconds)
   │
   ├─→ check_and_enforce_restrictions_on_active_orders()
   │   ├─→ Re-calculate delta
   │   ├─→ If restrictions changed → cancel violating orders
   │   └─→ If orders missing → re-place them
   │
   └─→ If no active orders → place_wave_order()
```

---

## Order Flow

### Order Placement Logic

```python
def _execute_orders(symbol, final_buy_price, final_sell_price, 
                    restrict_buy_order, restrict_sell_order):
    
    sell_order_id = -1
    
    # STEP 1: Place SELL order first (if not restricted)
    if restrict_sell_order == 0:
        sell_order_resp = broker.place_order(
            symbol=symbol,
            transaction_type=SELL,
            quantity=self.sell_quantity,
            price=final_sell_price
        )
        sell_order_id = sell_order_resp.order_id
        # Add to tracking with associated_order = -1 (no pair yet)
    
    # STEP 2: Place BUY order (if sell placed OR sell restricted)
    if (restrict_sell_order == 1 or sell_order_id != -1) and restrict_buy_order == 0:
        buy_order_resp = broker.place_order(
            symbol=symbol,
            transaction_type=BUY,
            quantity=self.buy_quantity,
            price=final_buy_price
        )
        buy_order_id = buy_order_resp.order_id
        
        # Link orders together as "associated orders"
        if sell_order_id != -1:
            orders[sell_order_id]['associated_order'] = buy_order_id
            orders[buy_order_id]['associated_order'] = sell_order_id
```

### Associated Order Concept

Orders are placed in **pairs** and linked:
```python
orders = {
    'order_123': {  # SELL order
        'price': 24525,
        'quantity': 75,
        'type': 'SELL',
        'associated_order': 'order_124'  # Linked BUY order
    },
    'order_124': {  # BUY order
        'price': 24475,
        'quantity': 75,
        'type': 'BUY',
        'associated_order': 'order_123'  # Linked SELL order
    }
}
```

**When one order executes:**
```python
def _complete_order(order_id):
    # 1. Update reference price
    self.scraper_last_price = order_info['price']
    
    # 2. Cancel the other side
    associated_order_id = order_info['associated_order']
    broker.cancel_order(associated_order_id)
    
    # 3. Place new wave
    self.place_wave_order()
```

---

## Risk Management

### 1. Position-Based Gap Scaling

```python
Position Difference = +5 lots (accumulated 5 more buys than sells)

Multiplier for level 5 = [10.0, 1.0]
buy_gap = 25 * 10.0 = 250   # Much wider
sell_gap = 25 * 1.0 = 25    # Normal

Effect: Makes buying extremely expensive, selling easy
→ Encourages mean-reversion back to neutral
```

### 2. Delta Limits

**NIFTY Example:**
```python
# Current portfolio delta: +120 (too bullish)
# max_nifty_delta: 100

# System restricts:
futures.buy = NO     # Can't add more long futures
ce.buy = NO          # Can't buy calls (bullish)
pe.sell = NO         # Can't sell puts (bullish)

# Allows:
futures.sell = YES   # Can reduce position
ce.sell = YES        # Can sell calls (bearish/neutral)
pe.buy = YES         # Can buy puts (bearish)
```

### 3. Option-Specific Rules

```python
symbol_type = "CE" or "PE"  # Call or Put option

if current_net == 0:
    # No position, allow one buy to initiate
    pass

elif current_net > 0:
    # Already long option, restrict more buys
    restrict_buy_order = 1
```

**Rationale**: Options strategies should not accumulate unlimited long positions.

### 4. Special NIFTY Rule

```python
if symbol_class == "nifty" and final_buy_price < 25:
    # Override buy restriction if price is very low
    # Reason: Closing cheap positions saves margin
    restrict_buy_order = 0
```

### 5. Margin Calculation

```python
def calculate_margin_requirement(spread_count, single_pe_ce, both_ce_pe):
    margin = (spread_count * margin_spread +          # For spreads
              single_pe_ce * margin_single_pe_ce +    # For naked CE or PE
              both_ce_pe * margin_both_pe_ce)         # For straddles/strangles
    return margin / 75  # Normalized per lot
```

**Margin Components:**
- `spread_count`: Total positive CE + PE positions (spreads use less margin)
- `single_pe_ce`: Excess of one side (naked positions use more margin)
- `both_ce_pe`: Minimum of both sides (straddle component)

---

## Configuration Parameters

### Core Trading Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `symbol_name` | NIFTY25SEPFUT | Trading instrument |
| `exchange` | NFO | Exchange (NFO for F&O) |
| `buy_gap` | 25 | Price gap below market for buy orders |
| `sell_gap` | 25 | Price gap above market for sell orders |
| `buy_quantity` | 75 | Quantity for buy orders |
| `sell_quantity` | 75 | Quantity for sell orders |
| `lot_size` | 75 | Contract lot size |
| `cool_off_time` | 10 | Seconds to wait during price calculation |

### Risk Management Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `min_nifty_delta` | -100 | Minimum allowed NIFTY delta |
| `max_nifty_delta` | 100 | Maximum allowed NIFTY delta |
| `min_bank_nifty_delta` | -100 | Minimum allowed BANKNIFTY delta |
| `max_bank_nifty_delta` | 100 | Maximum allowed BANKNIFTY delta |

### Greeks Calculation Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `interest_rate` | 10.0 | Risk-free rate (%) for BS model |
| `todays_volatility` | 20.0 | Implied volatility (%) for delta calculation |
| `delta_calculation_days` | 10 | Only consider options expiring within N days |

### Margin Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `margin_spread` | 100.0 | Margin per lot for spread positions |
| `margin_single_pe_ce` | 100.0 | Margin per lot for naked positions |
| `margin_both_pe_ce` | 100.0 | Margin per lot for straddle positions |

### Order Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `product_type` | NRML | Order product type (NRML/MIS) |
| `order_type` | LIMIT | Order type (LIMIT/MARKET) |
| `variety` | REGULAR | Order variety |
| `tag` | WaveScraper | Order tag for identification |

---

## Key Features

### 1. **Dual Order Execution**
Always places both BUY and SELL orders simultaneously:
- Captures profit regardless of market direction
- Earns the spread (sell_gap + buy_gap = 50 points per complete cycle)

### 2. **Price Stability Mechanism**
Uses multiple price references:
- Current market price
- Last execution price (`scraper_last_price`)
- Previous wave prices (`prev_wave_buy_price`, `prev_wave_sell_price`)

This prevents:
- Chasing spikes
- Missing profitable levels
- Getting stuck in volatile periods

### 3. **Self-Healing Order Management**

```python
def check_and_enforce_restrictions_on_active_orders():
    # Every 60 seconds:
    # 1. Re-check delta restrictions
    # 2. Cancel orders that violate new restrictions
    # 3. Re-place missing orders if restrictions lifted
```

**Example:**
```
t=0: Delta = 80 (OK), both orders active
t=60: Delta = 110 (exceeds max), BUY order cancelled
t=120: Delta = 95 (OK again), BUY order re-placed
```

### 4. **Websocket-Driven Updates**

Real-time order updates via websocket:
```python
def on_order_update(ws, data):
    trading_system.handle_order_update(data)
    # Instantly reacts to:
    # - Order executions → place new wave
    # - Order cancellations → cleanup
    # - Order modifications → update prices
```

### 5. **Deferred Update Handling**

```python
# If order update arrives BEFORE order is tracked:
if order_id not in self.orders:
    # Save it for later processing
    self.handle_order_update_call_tracker[order_id] = False
    self.handle_order_update_call_tracker_response_dict[order_id] = order_data

# After order is added to tracking:
if not self.handle_order_update_call_tracker[order_id]:
    self.handle_order_update(saved_update)
```

**Why?** Handles race condition between order placement response and websocket update.

### 6. **Portfolio Greeks Calculation**

Uses **Black-Scholes model** (via `mibian` library) to calculate option deltas:
```python
bs = mibian.BS(
    [spot_price, strike, interest_rate, days_to_expiry],
    volatility=todays_volatility
)

call_delta = bs.callDelta  # 0 to 1
put_delta = bs.putDelta    # -1 to 0
```

**Aggregates across entire portfolio:**
- Futures: delta = quantity (1:1)
- Calls: delta = BS callDelta * quantity
- Puts: delta = BS putDelta * quantity

---

## Example Scenarios

### Scenario 1: Normal Operation

```
Initial State:
- Symbol: NIFTY25SEPFUT
- Current Price: 24,500
- Position: 0 (neutral)
- scraper_last_price: 24,500

Step 1: place_wave_order()
- position_diff = 0 → multiplier = [1.0, 1.0]
- scaled_buy_gap = 25, scaled_sell_gap = 25
- SELL order @ 24,525 (qty: 75)
- BUY order @ 24,475 (qty: 75)

Step 2: Price moves to 24,530, SELL executes
- handle_order_update() receives COMPLETE status
- Cancel BUY order @ 24,475
- Update scraper_last_price = 24,525
- position_diff = -1 (sold 1 lot)

Step 3: New wave with position_diff = -1
- multiplier = [1.0, 1.3] (favor buying back)
- scaled_buy_gap = 25, scaled_sell_gap = 32.5
- SELL order @ 24,557.5 (24,525 + 32.5)
- BUY order @ 24,500 (24,525 - 25)

Result: Mean-reversion encouraged through wider sell gap
```

### Scenario 2: Delta Limit Hit

```
Portfolio State:
- NIFTY futures: +50 lots (delta = +50)
- NIFTY 24500 CE: +100 lots (delta ≈ +50, assuming 0.5 delta)
- Total NIFTY delta: +100 (at max limit)

Step 1: Periodic check runs
- _get_portfolio_greeks("NIFTY") returns delta = +100
- max_nifty_delta = +100 → AT LIMIT

Step 2: New restrictions applied
- futures.buy = NO
- ce.buy = NO
- pe.sell = NO

Step 3: check_and_enforce_restrictions_on_active_orders()
- Active BUY order found → violates restriction
- Cancel BUY order
- Keep SELL order active

Step 4: SELL order executes
- Position reduces: delta = +75 - 75 = 0
- New wave places both orders (restrictions lifted)
```

### Scenario 3: Position Imbalance

```
Accumulated Position:
- Bought: 5 lots
- Sold: 0 lots
- position_diff = +5

Step 1: Calculate scaled gaps
- current_diff_scale = +5
- multiplier = [10.0, 1.0]  # From multiplier_scale
- buy_gap = 25 * 10.0 = 250
- sell_gap = 25 * 1.0 = 25

Step 2: Place orders
- Current price: 24,500
- BUY order @ 24,250 (24,500 - 250) ← Very far
- SELL order @ 24,525 (24,500 + 25) ← Normal

Effect:
- BUY unlikely to execute (250 points away)
- SELL likely to execute (25 points away)
- Natural mean-reversion toward neutral position
```

### Scenario 4: Option Strategy

```
Symbol: NIFTY 24500 CE (Call Option)
Current Position: 0

Step 1: First order cycle
- No position, allow buy
- Place SELL @ 105, BUY @ 95 (assuming CE price = 100)

Step 2: BUY executes @ 95
- Position: +75 (long 1 lot of calls)
- current_net > 0 → restrict_buy_order = 1

Step 3: New wave with restriction
- Only SELL order placed @ new level
- BUY order blocked (already long option)

Step 4: SELL executes
- Position: 0 (closed)
- Restriction lifted, both orders allowed again

Rationale: Prevents unlimited long accumulation in options
```

---

## Important Implementation Details

### 1. Order Status Handling

The strategy handles multiple broker-specific status codes:

```python
# Zerodha statuses (string)
'COMPLETE', 'CANCELLED', 'REJECTED', 'OPEN', 'UPDATE'

# Fyers statuses (integer)
1 = CANCELLED
2 = COMPLETE  
5 = REJECTED
6 = OPEN/UPDATE
```

### 2. Asynchronous Order Tracking

```python
# Problem: Websocket update may arrive before place_order() returns
# Solution: Deferred processing

# On order placement:
self.handle_order_update_call_tracker[order_id] = False

# After adding to orders dict:
if not self.handle_order_update_call_tracker[order_id]:
    # Process saved update
    self.handle_order_update(saved_update)
```

### 3. Best Price Selection

```python
# Why compare with previous wave prices?
final_buy = min(
    current_price - gap,
    prev_wave_buy_price  # Maintains continuity
)

# Prevents gap widening in trending markets
# Ensures orders stay within reasonable range
```

### 4. Cool-Off Period Purpose

```python
# During _prepare_final_prices():
time.sleep(self.cool_off_time)  # Wait 10 seconds

# Allows:
# 1. Price to stabilize after volatility
# 2. Previous orders to potentially execute
# 3. Better price discovery
```

---

## Monitoring and Observability

### Status Reporting

```python
def print_current_status():
    current_position = self._get_position_for_symbol()
    
    logger.info("="*50)
    logger.info(f"STATUS as of {time.ctime()}")
    logger.info(f"Symbol: {self.symbol_name}")
    logger.info(f"Initial Position: {self.initial_positions['position']}")
    logger.info(f"Current Position: {current_position}")
    logger.info(f"Active Orders: {len(self.orders)}")
    logger.info(f"Tracked Orders: {self.orders}")
    logger.info("="*50)
```

### Order Tracker Integration

```python
# All orders logged to OrderTracker database
order_details = {
    'order_id': order_id,
    'price': price,
    'quantity': quantity,
    'transaction_type': 'BUY' or 'SELL',
    'symbol': symbol,
    'associated_order': associated_order_id,
    'timestamp': datetime.now().isoformat()
}
self.order_tracker.add_order(order_details)
```

---

## Running the Strategy

### Command Line Interface

```bash
# Use default configuration
python wave.py

# Override specific parameters
python wave.py --symbol-name NIFTY25SEPFUT --buy-gap 30 --sell-gap 30

# Full customization
python wave.py \
    --symbol-name BANKNIFTY25SEPFUT \
    --buy-gap 50 --sell-gap 50 \
    --buy-quantity 75 --sell-quantity 75 \
    --cool-off-time 15 \
    --min-bank-nifty-delta -150 \
    --max-bank-nifty-delta 150
```

### Main Loop

```python
while True:
    time.sleep(60)  # Wake every 60 seconds
    
    # 1. Print status
    trading_system.print_current_status()
    
    # 2. Check and enforce restrictions
    trading_system.check_and_enforce_restrictions_on_active_orders()
    
    # 3. Place new wave if no active orders
    if not trading_system.check_is_any_order_active():
        trading_system.place_wave_order()
```

---

## Summary

The Wave Strategy is a sophisticated **automated market-making system** that:

1. **Continuously provides liquidity** by placing BUY and SELL orders around current price
2. **Profits from oscillations** by capturing the spread on each complete cycle
3. **Manages risk dynamically** through:
   - Position-based gap scaling
   - Delta limits using Black-Scholes Greeks
   - Instrument-specific rules
4. **Self-heals** by monitoring and adjusting orders every minute
5. **Handles complexity** with websocket updates, associated orders, and deferred processing

**Key Insight**: The strategy doesn't predict market direction. Instead, it profits from **volatility** and **mean-reversion**, while carefully controlling **directional risk** through delta management.

**Best Use Cases**:
- Range-bound markets with regular oscillations
- Instruments with reasonable liquidity
- When spreads are wide enough to cover transaction costs
- Traders wanting automated, delta-neutral exposure

**Risks to Monitor**:
- Trending markets (one side keeps executing)
- Gap risk (overnight/sudden moves)
- Execution slippage
- Transaction costs eating into spread
- Margin requirements during volatile periods

---

## Additional Resources

- **Configuration File**: `strategy/configs/wave.yml`
- **Order Tracking**: Uses `orders.py` OrderTracker class
- **Broker Interface**: Via `brokers/` module (BrokerGateway)
- **Logging**: Comprehensive logging via `logger.py`

---

**Document Version**: 1.0  
**Last Updated**: 2026-02-12  
**Author**: Auto-generated documentation

---

## Visual Flowcharts and State Diagrams

### State Diagram: Wave Strategy Lifecycle

```
                    ┌─────────────────────────────────────┐
                    │     INITIALIZATION                  │
                    │  - Load config                      │
                    │  - Download instruments             │
                    │  - Get initial position             │
                    │  - Set scraper_last_price           │
                    │  - Subscribe to websocket           │
                    └──────────────┬──────────────────────┘
                                   │
                                   ▼
                    ┌──────────────────────────────────────┐
                    │     PLACE WAVE ORDER                 │
                    │  - Calculate position diff           │
                    │  - Get scaled gaps                   │
                    │  - Prepare final prices              │
                    │  - Check delta restrictions          │
                    │  - Execute BUY & SELL orders         │
                    └──────────────┬───────────────────────┘
                                   │
                                   ▼
                    ┌──────────────────────────────────────┐
                    │     MONITORING STATE                 │
                    │  - Both orders OPEN                  │
                    │  - Waiting for execution             │
                    │  - Listening to websocket updates    │
                    └──────────────┬───────────────────────┘
                                   │
                    ┌──────────────┴──────────────┐
                    │  Order Update Received      │
                    └──────────────┬──────────────┘
                                   │
                    ┌──────────────┴──────────────────┐
                    │                                 │
                    ▼                                 ▼
    ┌───────────────────────────┐   ┌────────────────────────────┐
    │   ORDER COMPLETE          │   │   ORDER CANCELLED/REJECTED │
    │                           │   │                            │
    │ 1. Update last_price      │   │ 1. Remove from tracking    │
    │ 2. Cancel associated ord  │   │ 2. Remove associated order │
    │ 3. Update positions       │   │ 3. Log event              │
    └───────────┬───────────────┘   └────────────────────────────┘
                │
                ▼
    ┌───────────────────────────┐
    │   PLACE NEW WAVE ORDER    │
    │   (Cycle repeats)         │
    └───────────────────────────┘


    ┌─────────────────────────────────────────────────────────┐
    │   PERIODIC CHECK (Every 60 seconds)                     │
    │                                                          │
    │   1. Re-calculate delta restrictions                    │
    │   2. Cancel orders violating new restrictions           │
    │   3. If no active orders → place_wave_order()           │
    └─────────────────────────────────────────────────────────┘
```


### Flowchart: Place Wave Order Decision Tree

```
                         ┌──────────────────┐
                         │  place_wave_order│
                         │     called       │
                         └────────┬─────────┘
                                  │
                                  ▼
                         ┌──────────────────┐
                         │  Get Position    │
                         │  buy_qty, sell_qty│
                         └────────┬─────────┘
                                  │
                                  ▼
                    ┌──────────────────────────┐
                    │  Calculate Position Diff │
                    │  diff = (buy-sell)/lot   │
                    └────────┬─────────────────┘
                             │
                             ▼
                    ┌──────────────────────────┐
                    │  Get Scaled Gaps         │
                    │  based on position diff  │
                    └────────┬─────────────────┘
                             │
                             ▼
                    ┌──────────────────────────────┐
                    │  Prepare Final Prices        │
                    │  1. Get current price        │
                    │  2. Calculate buy/sell       │
                    │  3. Cool-off wait           │
                    │  4. Re-check & take best    │
                    └────────┬─────────────────────┘
                             │
                             ▼
                    ┌──────────────────────────────┐
                    │  Check Delta Restrictions    │
                    │  _get_dynamic_restrictions() │
                    └────────┬─────────────────────┘
                             │
                             ▼
                    ┌──────────────────────────────┐
                    │  Can place BUY order?        │
                    └────────┬─────────────────────┘
                             │
                ┌────────────┴────────────┐
                │ YES                     │ NO
                ▼                         ▼
        ┌──────────────┐          ┌──────────────┐
        │ restrict_buy │          │ restrict_buy │
        │ = 0          │          │ = 1          │
        └──────┬───────┘          └──────┬───────┘
               │                         │
               └────────────┬────────────┘
                            │
                            ▼
                    ┌──────────────────────────────┐
                    │  Can place SELL order?       │
                    └────────┬─────────────────────┘
                             │
                ┌────────────┴────────────┐
                │ YES                     │ NO
                ▼                         ▼
        ┌──────────────┐          ┌──────────────┐
        │ restrict_sell│          │ restrict_sell│
        │ = 0          │          │ = 1          │
        └──────┬───────┘          └──────┬───────┘
               │                         │
               └────────────┬────────────┘
                            │
                            ▼
                    ┌──────────────────────────────┐
                    │  Execute Orders              │
                    │  _execute_orders()           │
                    └────────┬─────────────────────┘
                             │
                             ▼
                    ┌──────────────────────────────┐
                    │  SELL order first            │
                    │  (if not restricted)         │
                    └────────┬─────────────────────┘
                             │
                             ▼
                    ┌──────────────────────────────┐
                    │  BUY order second            │
                    │  (if not restricted)         │
                    └────────┬─────────────────────┘
                             │
                             ▼
                    ┌──────────────────────────────┐
                    │  Link as Associated Orders   │
                    │  buy ↔ sell                  │
                    └──────────────────────────────┘
```


### Flowchart: Order Update Handling

```
                         ┌──────────────────────┐
                         │  Websocket Update    │
                         │  Received            │
                         └────────┬─────────────┘
                                  │
                                  ▼
                         ┌──────────────────────┐
                         │  Order in tracking?  │
                         └────────┬─────────────┘
                                  │
                    ┌─────────────┴─────────────┐
                    │ NO                        │ YES
                    ▼                           ▼
          ┌──────────────────┐        ┌──────────────────┐
          │ Save for later   │        │ Process now      │
          │ (deferred)       │        │                  │
          └──────────────────┘        └────────┬─────────┘
                                               │
                                               ▼
                                      ┌──────────────────┐
                                      │ Order Status?    │
                                      └────────┬─────────┘
                                               │
                    ┌──────────────────────────┼──────────────────────────┐
                    │                          │                          │
                    ▼                          ▼                          ▼
          ┌──────────────────┐      ┌──────────────────┐      ┌──────────────────┐
          │   COMPLETE       │      │   CANCELLED      │      │   OPEN/UPDATE    │
          │                  │      │   REJECTED       │      │                  │
          └────────┬─────────┘      └────────┬─────────┘      └────────┬─────────┘
                   │                         │                         │
                   ▼                         ▼                         ▼
          ┌──────────────────┐      ┌──────────────────┐      ┌──────────────────┐
          │ 1. Update        │      │ 1. Remove order  │      │ 1. Update prev   │
          │    last_price    │      │ 2. Remove assoc  │      │    wave prices   │
          │ 2. Cancel assoc  │      │ 3. Log reason    │      │ 2. Update status │
          │    order         │      │                  │      │                  │
          │ 3. Remove both   │      └──────────────────┘      └──────────────────┘
          │ 4. Log trade     │
          │ 5. place_wave_   │
          │    order()       │
          └──────────────────┘
```

### Flowchart: Gap Scaling Logic

```
                         ┌──────────────────────┐
                         │  Calculate Position  │
                         │  Difference          │
                         └────────┬─────────────┘
                                  │
                                  ▼
                         ┌──────────────────────┐
                         │  diff = (buy - sell) │
                         │        / lot_size    │
                         └────────┬─────────────┘
                                  │
                    ┌─────────────┴─────────────┐
                    │                           │
                    ▼                           ▼
          ┌──────────────────┐        ┌──────────────────┐
          │  diff = 0        │        │  diff ≠ 0        │
          │  (neutral)       │        │  (imbalanced)    │
          └────────┬─────────┘        └────────┬─────────┘
                   │                           │
                   ▼                           ▼
          ┌──────────────────┐        ┌──────────────────────────┐
          │ multiplier       │        │ Get multiplier from      │
          │ = [1.0, 1.0]     │        │ multiplier_scale array   │
          │                  │        │ based on abs(diff)       │
          └────────┬─────────┘        └────────┬─────────────────┘
                   │                           │
                   │                           ▼
                   │              ┌──────────────────────────┐
                   │              │ diff > 0 (net long)?     │
                   │              └────────┬─────────────────┘
                   │                       │
                   │         ┌─────────────┴─────────────┐
                   │         │ YES                       │ NO (net short)
                   │         ▼                           ▼
                   │  ┌──────────────────┐      ┌──────────────────┐
                   │  │ buy_mult = scale │      │ buy_mult = 1.0   │
                   │  │ sell_mult = 1.0  │      │ sell_mult = scale│
                   │  │ (widen buy gap)  │      │ (widen sell gap) │
                   │  └──────────────────┘      └──────────────────┘
                   │           │                         │
                   └───────────┴─────────────────────────┘
                               │
                               ▼
                    ┌──────────────────────────┐
                    │ scaled_buy_gap =         │
                    │   buy_gap × buy_mult     │
                    │ scaled_sell_gap =        │
                    │   sell_gap × sell_mult   │
                    └──────────────────────────┘

Example:
  diff = +3 (bought 3 more than sold)
  multiplier_scale[3] = [2.5, 1.0]
  buy_gap = 25 × 2.5 = 62.5  (harder to buy more)
  sell_gap = 25 × 1.0 = 25   (easier to sell)
```


---

## Detailed Step-by-Step Example: Complete Trading Session

Let me walk you through a complete trading session to show exactly how the Wave strategy behaves:

### Setup
```yaml
Configuration:
  symbol_name: NIFTY25SEPFUT
  exchange: NFO
  
  buy_gap: 25
  sell_gap: 25
  
  buy_quantity: 75
  sell_quantity: 75
  lot_size: 75
  
  cool_off_time: 10
  product_type: NRML
  
  min_nifty_delta: -100
  max_nifty_delta: 100
```

### Timeline with Complete Details

---

#### **9:15 AM - Strategy Initialization**

```
Action: Strategy starts
Current NIFTY Futures: 24,500

Initialization:
  ✓ Download instruments from broker
  ✓ Get current position for NIFTY25SEPFUT
  ✓ Position: 0 (no existing position)
  ✓ Set scraper_last_price = 24,500
  ✓ Set initial_positions['position'] = 0
  ✓ Initialize orders = {}
  ✓ Subscribe to order websocket

Position Difference:
  diff = (0 - 0) / 75 = 0
  Multiplier: [1.0, 1.0] (neutral position)

Scaled Gaps:
  buy_gap = 25 × 1.0 = 25
  sell_gap = 25 × 1.0 = 25

Status: Ready to place first wave
```

---

#### **9:16 AM - First Wave Order Placement**

```
Step 1: Get Current Quote
  Current price: 24,500
  Last execution price: 24,500

Step 2: Calculate Initial Prices
  buy_1 = 24,500 - 25 = 24,475
  buy_2 = 24,500 - 25 = 24,475
  best_buy = min(24,475, 24,475) = 24,475
  
  sell_1 = 24,500 + 25 = 24,525
  sell_2 = 24,500 + 25 = 24,525
  best_sell = max(24,525, 24,525) = 24,525

Step 3: Cool-off Period
  Wait 10 seconds...
  
Step 4: Re-check Price After Cool-off
  Price after wait: 24,502
  
  final_buy = min(24,475, 24,502 - 25) = min(24,475, 24,477) = 24,475
  final_sell = max(24,525, 24,502 + 25) = max(24,525, 24,527) = 24,527

Step 5: Check Delta Restrictions
  Current NIFTY delta: 0
  Within limits (-100 to +100)? YES
  restrict_buy_order = 0
  restrict_sell_order = 0

Step 6: Execute Orders
  
  SELL Order:
    Symbol: NIFTY25SEPFUT
    Price: 24,527
    Quantity: 75
    Type: SELL
    Order ID: 1001
    Status: OPEN
  
  BUY Order:
    Symbol: NIFTY25SEPFUT
    Price: 24,475
    Quantity: 75
    Type: BUY
    Order ID: 1002
    Status: OPEN
  
  Link Orders:
    orders[1001]['associated_order'] = 1002
    orders[1002]['associated_order'] = 1001

Current State:
  Active Orders: 2 (BUY @ 24,475, SELL @ 24,527)
  Spread: 52 points
  Waiting for execution...
```

---

#### **9:25 AM - Price Rises, SELL Order Executes**

```
Market Movement:
  Price rises to 24,530

Websocket Update Received:
  Order ID: 1001 (SELL order)
  Status: COMPLETE
  Execution Price: 24,527
  Quantity: 75

Processing Order Update:
  
  Step 1: Update scraper_last_price
    scraper_last_price = 24,527
  
  Step 2: Get Associated Order
    associated_order_id = 1002 (BUY order)
  
  Step 3: Cancel BUY Order
    Cancel order 1002 @ 24,475
    LOG: "Cancelling associated BUY order"
  
  Step 4: Update Position
    Current position: 0 → -75 (sold 1 lot)
  
  Step 5: Clean Up Tracking
    Remove order 1001 from orders dict
    Remove order 1002 from orders dict
  
  Step 6: Trigger New Wave
    Call place_wave_order()

LOG: "SELL order executed @ 24,527, placing new wave"

Current Position:
  NIFTY25SEPFUT: -75 (short 1 lot)
  Unrealized P&L: 0 (just entered)
```

---

#### **9:26 AM - Second Wave with Position Imbalance**

```
Calculate Position Difference:
  buy_qty = 0 (total bought)
  sell_qty = 75 (total sold)
  diff = (0 - 75) / 75 = -1

Get Scaled Gaps:
  diff_scale = -1
  multiplier = [1.0, 1.3]  (from multiplier_scale array)
  
  Explanation: Net short, so make selling harder
  buy_gap = 25 × 1.0 = 25 (normal - encourage buying back)
  sell_gap = 25 × 1.3 = 32.5 (wider - discourage more selling)

Prepare Final Prices:
  Current price: 24,530
  Last execution: 24,527
  
  buy_1 = 24,530 - 25 = 24,505
  buy_2 = 24,527 - 25 = 24,502
  best_buy = min(24,505, 24,502) = 24,502
  
  sell_1 = 24,530 + 32.5 = 24,562.5
  sell_2 = 24,527 + 32.5 = 24,559.5
  best_sell = max(24,562.5, 24,559.5) = 24,562.5
  
  Cool-off wait (10 seconds)...
  
  Price after wait: 24,528
  final_buy = min(24,502, 24,503) = 24,502
  final_sell = max(24,562.5, 24,560.5) = 24,562.5

Execute Orders:
  SELL @ 24,562.5, Order ID: 1003
  BUY @ 24,502, Order ID: 1004
  
Active Orders:
  BUY @ 24,502 (encourages closing short)
  SELL @ 24,562.5 (wider spread due to imbalance)
  Spread: 60.5 points (vs 52 in first wave)
```

---

#### **9:40 AM - Price Falls, BUY Order Executes**

```
Market Movement:
  Price falls to 24,495

Websocket Update:
  Order ID: 1004 (BUY order)
  Status: COMPLETE
  Execution Price: 24,502

Processing:
  Update scraper_last_price = 24,502
  Cancel SELL order 1003 @ 24,562.5
  
Position Update:
  Previous: -75 (short)
  Change: +75 (buy)
  New Position: 0 (flat)

Realized P&L Calculation:
  Sold @ 24,527
  Bought @ 24,502
  Profit: (24,527 - 24,502) × 75 = 1,875 points
  
LOG: "BUY order executed @ 24,502, Realized P&L: +1,875"

Trigger New Wave:
  Position now neutral (0)
```

---

#### **9:41 AM - Third Wave (Back to Neutral)**

```
Position Difference:
  buy_qty = 75
  sell_qty = 75
  diff = (75 - 75) / 75 = 0

Scaled Gaps:
  multiplier = [1.0, 1.0] (neutral again)
  buy_gap = 25
  sell_gap = 25

Prepare Prices:
  Current: 24,495
  Last execution: 24,502
  
  buy_1 = 24,495 - 25 = 24,470
  buy_2 = 24,502 - 25 = 24,477
  best_buy = min(24,470, 24,477) = 24,470
  
  sell_1 = 24,495 + 25 = 24,520
  sell_2 = 24,502 + 25 = 24,527
  best_sell = max(24,520, 24,527) = 24,527
  
  After cool-off:
  final_buy = 24,470
  final_sell = 24,527

Execute Orders:
  BUY @ 24,470, Order ID: 1005
  SELL @ 24,527, Order ID: 1006
  Spread: 57 points
```

---

#### **10:00 AM - Volatile Market, Multiple Quick Executions**

```
Scenario: Price whipsaws between 24,520 and 24,540

T=10:00: SELL executes @ 24,527
  → Position: -75
  → Place new wave (wider sell gap due to imbalance)
  
T=10:05: BUY executes @ 24,505
  → Position: 0
  → Profit: 22 × 75 = 1,650 points
  
T=10:07: SELL executes @ 24,530
  → Position: -75
  
T=10:12: BUY executes @ 24,508
  → Position: 0
  → Profit: 22 × 75 = 1,650 points

Total in 12 minutes: 2 complete cycles, +3,300 points
```

---

#### **11:00 AM - Delta Limit Approaching**

```
Portfolio Check:
  NIFTY Futures: -75 (from wave strategy)
  Other Positions:
    NIFTY 24500 CE: -100 lots (delta ≈ -50 with 0.5 delta)
    NIFTY 24400 PE: +50 lots (delta ≈ -25 with -0.5 delta)
  
Calculate Total Delta:
  Futures: -75
  CE: -50
  PE: -25
  Total NIFTY Delta: -150

Check Limits:
  min_nifty_delta = -100
  Current delta = -150
  Delta < min_nifty_delta? YES (violation!)

Dynamic Restrictions Applied:
  nifty_restrictions['futures']['sell'] = "no"
  nifty_restrictions['ce']['sell'] = "no"
  nifty_restrictions['pe']['buy'] = "no"
  
  Allowed:
  nifty_restrictions['futures']['buy'] = "yes"
  nifty_restrictions['ce']['buy'] = "yes"
  nifty_restrictions['pe']['sell'] = "yes"

Impact on Wave Strategy:
  Current position: -75 (short futures)
  Trying to place new wave...
  
  BUY order: ALLOWED (helps reduce short exposure)
  SELL order: BLOCKED (would increase short exposure)
  
Execute Orders:
  BUY @ 24,480, Order ID: 1015
  SELL: NOT PLACED (restricted)

LOG: "SELL order restricted due to delta limit"

Current State:
  Only BUY order active
  Strategy will only execute BUY side
  If BUY executes → closes short → delta improves → both sides allowed again
```

---

#### **11:05 AM - BUY Executes, Delta Improves**

```
BUY Order Executes @ 24,480:
  Position: -75 → 0
  
New Delta Calculation:
  Futures: 0 (was -75)
  CE: -50
  PE: -25
  Total: -75

Check Limits:
  -75 within [-100, +100]? YES
  
Restrictions Lifted:
  All futures trades allowed again
  
New Wave:
  BUY @ 24,455, Order ID: 1016
  SELL @ 24,505, Order ID: 1017
  
Both orders placed normally
```

---

#### **1:00 PM - Periodic Check Runs**

```
Periodic Check (runs every 60 seconds):
  
Step 1: Re-calculate Delta
  Current delta: -50 (within limits)
  
Step 2: Check Active Orders
  Order 1016 (BUY @ 24,455): OPEN
  Order 1017 (SELL @ 24,505): OPEN
  
Step 3: Verify Restrictions
  Delta OK? YES
  Orders match restrictions? YES
  
Step 4: Check for Missing Orders
  Should have 2 orders? YES
  Have 2 orders? YES
  
Action: No changes needed

LOG: "Periodic check: All systems normal"
```

---

#### **2:30 PM - Position Accumulation Scenario**

```
After Several Trades:
  buy_qty = 75 (bought once)
  sell_qty = 375 (sold 5 times)
  diff = (75 - 375) / 75 = -4

Position Imbalance:
  Net short: 300 lots (-4 × 75)
  
Scaled Gaps:
  multiplier = [1.0, 3.0] (from multiplier_scale array)
  buy_gap = 25 × 1.0 = 25 (easy to buy back)
  sell_gap = 25 × 3.0 = 75 (very hard to sell more)

Prepare Prices:
  Current: 24,550
  Last execution: 24,545
  
  buy = 24,525 (24,550 - 25)
  sell = 24,625 (24,550 + 75)
  
Execute Orders:
  BUY @ 24,525 (normal spread)
  SELL @ 24,625 (100 points away!)
  
Effect:
  BUY likely to execute (close spread)
  SELL unlikely (very far from market)
  Encourages mean-reversion toward neutral position
```

---

#### **3:15 PM - End of Day Summary**

```
Trading Statistics:
  Total Waves Placed: 15
  Complete Cycles: 12
  Incomplete Cycles: 3 (open positions)
  
Position History:
  Buy executions: 14
  Sell executions: 12
  Current position: -150 (net short 2 lots)

P&L Summary:
  Average profit per cycle: 30 points
  Total cycles: 12
  Gross P&L: 12 × 30 × 75 = 27,000 points
  (Actual varies based on execution prices)

Delta Management:
  Times delta limit hit: 1
  Orders restricted: 3
  Auto-adjustments: 1

Gap Scaling Events:
  Neutral position (1.0x): 8 waves
  Small imbalance (1.3x): 4 waves
  Medium imbalance (2.5x): 2 waves
  Large imbalance (3.0x): 1 wave

Key Observations:
  ✓ Strategy adapted to position imbalance
  ✓ Delta limits prevented excessive risk
  ✓ Self-healing check maintained operations
  ✓ Collected spread on most cycles
```

---

### Key Observations from This Example

1. **Spread Collection**: Consistently captures 25-30 point spreads
2. **Gap Scaling**: Automatically widens gaps when position imbalanced
3. **Delta Management**: Restricts orders when limits approached
4. **Self-Correction**: Mean-reversion through gap scaling
5. **Price Stability**: Uses both current and last execution prices
6. **Associated Orders**: One execution always cancels the other
7. **Periodic Monitoring**: Every 60 seconds validates state

---
