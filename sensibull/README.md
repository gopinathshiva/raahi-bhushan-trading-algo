# Sensibull Position Tracker

## Overview
A Flask-based application designed to track, visualize, and analyze the trading positions of verified Sensibull profiles. It provides a centralized dashboard for monitoring multiple profiles and a granular daily activity view to understand trading behaviors minute-by-minute.

## Key Features
- **Multi-Profile Dashboard**: Monitor multiple verified traders in a single view.
- **Daily Activity Timeline**: specific timeline of every snapshot recorded throughout the trading day.
- **Smart Diff Calculation**: Automatically highlights `ADDED`, `REMOVED`, and `MODIFIED` positions between snapshots.
- **Daily Change Log**: A consolidated, chronological table of all modifications for the day.

## Visual Tour

### Main Dashboard
The entry point of the application, listing all tracked profiles.
![Dashboard](https://github.com/Raahi-Bhushan/trading-algo/blob/main/sensibull/assets/sensibull_app_walkthrough_1765520111499.webp)

### Daily Activity View
A detailed timeline showing exactly when changes were detected.
![Daily View](https://github.com/Raahi-Bhushan/trading-algo/blob/main/sensibull/assets/sensibull_details_popup_walkthrough_1765520300012.webp)

### Position Details (Timeline)
Clicking "View Details" on a **Daily Timeline** entry expands an inline section on the page that shows **Recent Changes** (Added, Removed, Modified). It no longer opens a popup for timeline entries and does not include the Overall Position table in that inline view.
![Popup Details](https://github.com/Raahi-Bhushan/trading-algo/blob/main/sensibull/assets/sensibull_popup_noprice_verification_1765523585229.webp)

### Daily Change Log
The **"See All Changes"** button provides a comprehensive log of every trade made during the day, sorted chronologically with the latest on top. This view isolates the *actions* taken without the noise of price fluctuations.
![Daily Log](https://github.com/Raahi-Bhushan/trading-algo/blob/main/sensibull/assets/sensibull_log_no_price_verification_retry_1765529636828.webp)

## Value Adds: Understanding Profitable Traders

This tool is designed to reverse-engineer the psychology and strategy of successful traders by revealing patterns that static P&L screenshots miss:

### 1. Decoding Conviction vs. Hedging
By observing the sequence of trades, you can distinguish between a directional bet and a hedge.
*   *Example*: If a trader adds Calls and then later adds Puts, are they hedging a profit or reacting to a reversal? The chronological log reveals the intent.

### 2. Identifying Position Management Styles
*   **Pyramiding**: Does the trader add quantity to winning positions? This is a hallmark of high-conviction trend following.
*   **Averaging Down**: Do they add to losing positions? This might indicate a mean-reversion strategy or a lack of discipline. This tool makes such patterns obvious.

### 3. Reaction to Market Events
The minute-by-minute log allows you to correlate trade entries with market price action.
*   *Insight*: You can see if a trader panic-closed a position during a sudden spike or if they held firm. Understanding this "pain tolerance" is key to understanding their edge.

### 4. Noise Filtering
The "Recent Changes" view filters out the noise of mark-to-market P&L swings and focuses purely on *execution*. Knowing *what they did* is far more actionable than knowing *how much they made*.

## Setup and Usage

### Prerequisites
- Python 3.x
- `pip`

### Installation
1.  **Clone/download the repository** to your local machine.
2.  **Install dependencies**:
    ```bash
    pip install -r requirements.txt
    ```
    *(If running in a virtual environment, ensure it is activated first)*

### Running the Application
1.  Navigate to the project directory:
    ```bash
    cd sensibull
    ```
2.  Start the Flask server:
    ```bash
    python3 app.py
    ```
3.  Access the dashboard in your browser at: `http://localhost:6060/`

---

## Verification Logic

We have implemented a strict verification mechanism to ensure the data integrity of the "Recent Changes" view.

### The Algorithm
The application calculates differences using a deterministic logic:
`Current State = Previous State + (Added Positions - Removed Positions + Modified Positions)`

### Verification Script
A dedicated test script `verify_diff.py` was created to validate this logic mathematically against real database records.

**How it works:**
1.  Fetches a specific `change_id` from the database.
2.  Retrieves the **Previous Snapshot** (state before the change).
3.  Retrieves the **Computed Diff** from the API.
4.  Retrieves the **Current Snapshot** (state after the change).
5.  Reconstructs the expected state by applying the Diff to the Previous Snapshot.
6.  Compares the Reconstructed State vs. Actual Current State.

**Result:**
The script confirmed a **perfect match**, proving that the diff calculation is 100% accurate and no data is lost or misrepresented in the transition.
