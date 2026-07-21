# Sentient — Full Context Dump for Codex

## DO NOT EXECUTE ANYTHING
This is a context and architecture document only. Read, understand, and confirm
understanding. Do not write any new code, create any files, or run any commands
until explicitly instructed.

---

## What Is Sentient?

Sentient is a multi-user, agentic AI portfolio monitoring system built in Python.
Each user defines a personal watchlist of stocks with their own positions, motives,
and update intervals. A central engine manages all shared ticker data. An
observer/listener pattern fans out events to the right users. Codex (via LangGraph)
acts as the intelligent core — autonomously deciding what to investigate, running
multi-step research loops, and reasoning across each user's full portfolio.

The system delivers all output via WhatsApp (Twilio) and a read-only Streamlit
dashboard. It does not connect to any brokerage and does not execute trades.

---

## Core Design Principles

1. **Ticker data is shared, user context is personal.** NVDA's 150 datapoints are
   fetched and stored once regardless of how many users watch it. Each user's
   position context (avg price, shares, motive, P&L) is kept separate and passed
   individually into each agent run.

2. **The observer/listener pattern sits above the agent layer.** The event bus
   decides who gets notified and when. LangGraph decides what Codex does once
   triggered. These are separate concerns and do not interfere with each other.

3. **Nothing outside of database.py talks to Supabase directly.** All reads and
   writes go through a single database interface file. Agents receive typed Pydantic
   objects, not raw database rows.

4. **All agent prompts live in config/prompts.py.** Agent logic files contain
   graph structure and tool wiring. Prompt text is never hardcoded inside agent files.

5. **Agent API costs are user-financed.** Shared market data remains system-owned
   and fetched once per ticker, but LLM/agent calls must run with the API key
   belonging to the user whose context is being analyzed. Do not pass user API
   keys through `AgentContext`, prompts, logs, updates, alerts, or LangSmith
   traces. Store and retrieve user-owned provider credentials through the database
   layer, and resolve the correct key immediately before creating the agent model
   client.

---

## Project Directory Structure

```
sentient/
│
├── core/
│   ├── scheduler.py       # APScheduler setup, ticker interval management
│   ├── monitor.py         # Sharp move loop, price comparison every 15-30 min
│   └── event_bus.py       # Observer/listener pattern, event emit + fan-out
│
├── data/
│   ├── twelve_data.py     # Twelve Data client, rolling 150-point OHLCV insert
│   └── database.py        # Supabase client, ALL query functions (single interface)
│
├── agents/
│   ├── core.py            # Shared LangGraph setup, tool definitions, base graph
│   ├── sharp_move.py      # Agent 1
│   ├── cross_portfolio.py # Agent 2
│   ├── motive.py          # Agent 3
│   ├── hypothesis.py      # Agent 4
│   └── scheduled_review.py # Agent 5
│
├── notifications/
│   └── whatsapp.py        # Twilio client, message formatters per alert type
│
├── dashboard/
│   ├── app.py             # Streamlit entry point
│   ├── views/             # One file per page/view
│   └── components/        # Reusable UI pieces (chart, stock card, alert log)
│
├── models/
│   └── schemas.py         # All Pydantic models and enums (already written)
│
├── config/
│   ├── settings.py        # Loads env vars, constants, thresholds
│   └── prompts.py         # All Codex system prompts and prompt templates
│
├── tests/
│
├── .env
├── .env.example
├── requirements.txt
└── README.md
```

---

## Tech Stack

| Component | Tool |
|---|---|
| Language | Python |
| Agent orchestration | LangGraph (built on LangChain) |
| LLM | Codex via langchain-anthropic |
| Observability | LangSmith |
| Scheduler | APScheduler |
| Database | Supabase (Postgres, via supabase-py REST client) |
| Price data | Twelve Data (15-minute OHLCV) |
| Web search | Anthropic web search tool (Codex-triggered) |
| WhatsApp | Twilio |
| Dashboard | Streamlit |
| Hosting | Railway or local machine |

---

## Database Schema (Supabase)

Five tables. The `subscriptions` table is the personalization layer — one row per
user-ticker pair. Everything else is either shared infrastructure or output history.

### `tickers`
- `ticker` (PK), `last_fetched`, `next_fetch_time`, `current_price`
- One row per unique stock across all users

### `ticker_data`
- `ticker`, `timestamp`, `open`, `high`, `low`, `close`, `volume`
- Rolling 150 rows per ticker — oldest dropped when new one inserted
- Shared across all users watching that ticker

### `users`
- `user_id` (PK), `whatsapp_number`, `email`, `created_at`, `preferences`

### `subscriptions`
- `user_id`, `ticker`, `avg_price`, `shares`, `motive`, `update_interval`,
  `sharp_move_threshold`
- One row per user-ticker pair
- Adding a new user = inserting rows here, nothing else in the engine changes

### `updates`
- `user_id`, `ticker`, `timestamp`, `summary`, `recommendation`, `price_at_update`
- Full history of every Codex agent output per user

### `alerts`
- `user_id`, `ticker`, `timestamp`, `alert_type`, `message`, `trigger_details`
- Log of every WhatsApp notification sent

### `user_api_keys` — REQUIRED BEFORE AGENTS
- `user_id`, `provider`, `encrypted_api_key`, `created_at`, `updated_at`,
  `last_validated_at`
- One row per user/provider pair
- Stores user-owned LLM provider credentials so each user's agent runs are billed
  to that user, not to the Sentient backend owner
- API keys should be encrypted before production; plaintext storage is acceptable
  only for a temporary local prototype
- Keys must never be included in `AgentContext`, agent prompts, LangSmith traces,
  `updates`, `alerts`, or WhatsApp messages

---

## Pydantic Schemas (models/schemas.py) — ALREADY WRITTEN

This file is complete. Do not rewrite it. All models are defined here and should
be imported from here throughout the codebase.

### Enums
- `Motive` — `holding`, `short-term`, `watching`
- `UpdateInterval` — `daily`, `weekly`
- `EventType` — `scheduled_update`, `sharp_move`, `motive_check`, `hypothesis_scan`
- `Confidence` — `high`, `medium`, `low`
- `AlertType` — `sharp_move`, `motive_flag`, `hypothesis`, `cross_portfolio`, `scheduled`

### Core Data Models
- `OHLCVPoint` — one price datapoint (ticker, timestamp, open, high, low, close, volume)
  with validators rejecting negative values
- `Ticker` — maps to the `tickers` database row
- `TickerRegistry` — in-memory object the event bus works against; holds a ticker's
  current price state AND its full list of `Subscription` objects; has methods
  `add_subscriber()`, `remove_subscriber()`, `get_subscriber()`; properties
  `subscriber_count` and `is_active`

### User & Subscription Models
- `User` — user_id, whatsapp_number, email, preferences
- `Subscription` — user_id, ticker, avg_price, shares, motive, update_interval,
  sharp_move_threshold (default 1%); validators on avg_price/shares (must be > 0)
  and threshold (must be 0.1%–50%); computed property `position_value`

### Agent Input Models
- `AgentContext` — input for Agents 1, 3, 4, 5 (single ticker, single user); contains
  150 datapoints, the user's Subscription, event_type, current_price, unrealized_pnl,
  unrealized_pnl_pct; property `is_profitable`
- `PortfolioContext` — input for Agent 2 only (cross-portfolio); contains a list of
  AgentContext objects (all of a user's positions) plus latest AgentOutput objects
  from the current cycle; properties `tickers`, `total_portfolio_value`,
  `total_unrealized_pnl`

### Agent Output Models
- `AgentOutput` — output for Agents 1, 3, 5 (and base output for Agent 4); contains
  ticker, user_id, event_type, summary, recommendation, confidence, timestamp,
  price_at_update, searched_web
- `HypothesisOutput` — output specific to Agent 4; extends AgentOutput with
  `flagged` (bool) and `recommended_next_scan_days` (int) — Codex sets this value
  dynamically based on urgency; the scheduler reads this and adjusts the next
  hypothesis scan time accordingly
- `CrossPortfolioOutput` — output for Agent 2; not per-ticker; contains user_id,
  summary, correlations_flagged (list of strings), tickers_analyzed

### Notification Models
- `Alert` — what gets written to the `alerts` table; user_id, ticker, timestamp,
  alert_type, message, trigger_details
- `WhatsAppMessage` — what gets sent to Twilio; to (whatsapp number), body,
  alert_type, ticker, user_id

---

## Observer / Listener Pattern (core/event_bus.py)

The event bus is the orchestration layer sitting above LangGraph. It is responsible
for deciding which users get notified when a ticker event fires. LangGraph is
responsible for what Codex does once a user's agent job is triggered.

### In-Memory Registry
On startup, the event bus loads all subscriptions from the database and builds a
registry of `TickerRegistry` objects keyed by ticker symbol:

```python
registry: dict[str, TickerRegistry] = {
    "NVDA": TickerRegistry(ticker="NVDA", subscribers=[user1_sub, user2_sub]),
    "AAPL": TickerRegistry(ticker="AAPL", subscribers=[user1_sub]),
}
```

The scheduler and monitor loop work entirely against this in-memory registry —
no repeated database queries in the hot path.

### Event Types and Fan-out
When an event fires for a ticker, the event bus:
1. Looks up `registry[ticker].subscribers`
2. For each subscriber, builds an `AgentContext` with their personal position data
3. Queues a LangGraph agent run per user with their context

Event types:
- `scheduled_update:{TICKER}` — a stock's update interval has elapsed
- `sharp_move:{TICKER}` — price monitor detected movement beyond a user's threshold
- `motive_check:{TICKER}` — weekly motive reassessment timer fired
- `hypothesis_scan:{TICKER}` — Agent 4's dynamic cadence timer fired

### Subscription Updates Mid-Run
If a user changes their motive, threshold, or interval while the system is running,
`refresh_subscription()` replaces their entry in the in-memory registry without
requiring a full reload.

---

## The Five LangGraph Agents

All agents are implemented as LangGraph graphs. The shared graph setup, tool
definitions, and Codex model binding live in `agents/core.py`. Individual agent
files define their specific graph structure (nodes, edges, conditional branching).
All prompt text lives in `config/prompts.py`.

### Agent 1 — Sharp Move Investigation (agents/sharp_move.py)
**Trigger:** `sharp_move:{TICKER}` event, fires immediately outside normal schedule
**Input:** `AgentContext`
**Output:** `AgentOutput` with alert_type `SHARP_MOVE`

**Purpose:** When a sudden price spike or drop is detected, autonomously investigates
the cause and contextualizes it for the user's specific position.

**LangGraph flow:**
```
detect_move → search_for_catalyst → check_sector_etf
    → check_correlated_holdings → synthesize → output
         ↑__________retry if search results thin_________|
```

Codex decides whether to retry the search, whether the move is company-specific
or sector-wide, and whether any of the user's other holdings are likely affected.
The output is a contextualized alert, not just a percentage number.

---

### Agent 2 — Cross-Portfolio Reasoning (agents/cross_portfolio.py)
**Trigger:** Fires after all individual stock updates complete for a user's cycle
**Input:** `PortfolioContext` (all of a user's positions + latest AgentOutputs)
**Output:** `CrossPortfolioOutput` with alert_type `CROSS_PORTFOLIO`

**Purpose:** Looks across all of a user's holdings simultaneously to surface
correlations, sector trends, and combined risk that wouldn't be visible stock by stock.

**LangGraph flow:**
```
ingest_all_positions → reason_across_portfolio
    → flag_correlations → flag_combined_risk → synthesize → output
```

Agent 2 receives the summaries already produced by the individual stock runs in
the current cycle. It reasons *across* those outputs rather than re-analyzing each
ticker from scratch. This is the only agent that receives `PortfolioContext` instead
of `AgentContext`.

Example outputs:
- "3 of your AI names are down but your semis are flat — this is rotation, not broad weakness"
- "Your two largest positions are both showing weakness simultaneously — combined drawdown risk is meaningful"

---

### Agent 3 — Motive Reassessment (agents/motive.py)
**Trigger:** `motive_check:{TICKER}` — weekly cadence per subscription, independent
of the stock's normal update interval
**Input:** `AgentContext`
**Output:** `AgentOutput` with alert_type `MOTIVE_FLAG`

**Purpose:** Once a week, checks whether the user's stated reason for entering a
position still holds given what has actually happened since they entered.

**LangGraph flow:**
```
read_motive_and_position → analyze_price_action_since_entry
    → assess_thesis_validity → reaffirm or flag → output
```

This agent is not about what the stock is doing right now — it's about whether
the user's *belief* about the stock when they entered still matches reality. If a
user set motive to `short-term` four weeks ago and the stock is down 12% with
declining volume, Agent 3 flags that the thesis window has likely passed.

It does not tell the user what to do. It surfaces the drift between stated intent
and observed reality.

---

### Agent 4 — Proactive Hypothesis Generation (agents/hypothesis.py)
**Trigger:** `hypothesis_scan:{TICKER}` — dynamic cadence; Agent 4 itself sets
the next scan time via `recommended_next_scan_days` in its output
**Input:** `AgentContext`
**Output:** `HypothesisOutput` with alert_type `HYPOTHESIS`

**Purpose:** Scans each stock's price data with no specific question, looking for
anything structurally interesting before it becomes obvious. If it finds something,
it speculatively searches online for early confirming or denying signals.

**Key distinction from other agents:** Other agents search *reactively* (something
happened, find out why). Agent 4 searches *speculatively* (something looks like it
might happen, find early signals that confirm or deny it).

**Dynamic cadence:** After every scan, Codex returns `recommended_next_scan_days`.
The scheduler reads this and sets the next hypothesis scan time accordingly:
- Nothing interesting found → 3 days
- Something developing but not urgent → 1-2 days
- Something that looks like early-stage buildup → 1 day
- Something requiring immediate attention → fires `sharp_move` event instead,
  bypassing hypothesis cadence entirely

**LangGraph flow:**
```
scan_150_datapoints → anything_flagged?
    ↓ yes                          ↓ no
speculative_web_search         set_next_cadence(3 days) → output
    ↓
pattern_confirmed?
    ↓ yes              ↓ no
send_alert +       set_next_cadence(1-2 days) → output
set_earlier_cadence
```

---

### Agent 5 — Scheduled Position Review (agents/scheduled_review.py)
**Trigger:** `scheduled_update:{TICKER}` — daily or weekly per subscription
**Input:** `AgentContext`
**Output:** `AgentOutput` with alert_type `SCHEDULED`

**Purpose:** Provides the routine, general assessment for a stock the user already
owns or watches. This is the normal scheduled review path when no sharp-move,
motive, or hypothesis event is required.

The agent compares the current position with recent price behaviour and the most
recent stored update, evaluates whether risk or trend has materially changed, and
searches the web only when fresh external context is needed. It considers the
user's cost basis, shares, motive, P&L, rolling price behaviour, volatility, and
recent alerts before producing a measured recommendation.

**LangGraph flow:**
```
ingest_position_and_history → compare_with_previous_review
    → assess_trend_volatility_and_risk → external_context_needed?
        ↓ yes                              ↓ no
    targeted_web_search ───────────────→ synthesize → output
```

This agent reviews existing positions; it does not scan the wider market for new
investments. Its recommendations must explain uncertainty and remain monitoring
and advisory output, never brokerage actions.

---

## Future Opportunity Discovery Agent — NOT PART OF THE CURRENT FIVE

A separate, opt-in Opportunity Discovery Agent is planned for a later phase. It
will scan a deliberately bounded market universe, apply inexpensive quantitative
filters first, and use agent research only for a small shortlist of candidates.
Its output will be research opportunities with catalysts, risks, and reasons for
further investigation — not unsupported buy instructions.

This future agent remains separate because broad discovery has different market
data, licensing, cost, ranking, and safety requirements from reviewing stocks a
user already owns or watches. It must not be implemented until the five core
agents, notifications, dashboard, and provider-cost controls are stable.

---

## Data Flow — Full System End to End

```
Twelve Data fetch (per ticker, shared)
    → rolling 150-point insert into ticker_data table
    → update current_price in tickers table + TickerRegistry

APScheduler / Sharp move monitor
    → emits event into EventBus

EventBus
    → looks up TickerRegistry[ticker].subscribers
    → for each subscriber, builds AgentContext with personal position data
    → queues LangGraph agent run per user

LangGraph agent runs (per user, per ticker)
    → resolves that user's LLM provider API key from database.py
    → creates the model client for that user only
    → Codex reads AgentContext
    → Codex decides what to search (web search tool)
    → Codex runs multi-step investigation loop
    → returns typed output object (AgentOutput / HypothesisOutput / CrossPortfolioOutput)

Output handling
    → AgentOutput written to updates table
    → Alert written to alerts table
    → WhatsAppMessage formatted and sent via Twilio
    → Dashboard reads from updates + alerts tables via Supabase
```

---

## WhatsApp Message Types (notifications/whatsapp.py)

Five message types, each formatted differently:

| Alert Type | Trigger | Urgency |
|---|---|---|
| `SHARP_MOVE` | Immediate, outside schedule | High |
| `SCHEDULED` | Normal update interval | Routine |
| `MOTIVE_FLAG` | Weekly | Informational |
| `HYPOTHESIS` | Agent 4 dynamic cadence | Informational |
| `CROSS_PORTFOLIO` | After full user cycle completes | Informational |

---

## Streamlit Dashboard (dashboard/)

Read-only. No trading actions. Scoped per user via user_id.

Views:
- Portfolio overview — all tickers, current price, avg cost, shares, total P&L, % change
- Per-stock card — latest Codex summary + recommendation
- Price chart — last 1.5 days of intraday OHLCV (15-min candles) via Plotly
- Alert log — full history of all alert types
- Subscription manager — add/remove tickers, update motive, change threshold/interval

---

## Planned Build Order

1. Database schema — all five tables in Supabase
2. `data/twelve_data.py` — price fetcher, rolling 150-point insert
3. `data/database.py` — all query functions, single Supabase interface
4. `models/schemas.py` — ALREADY DONE
5. `core/event_bus.py` — TickerRegistry, observer/listener pattern, fan-out logic
6. `core/scheduler.py` — APScheduler, per-ticker interval management
7. `core/monitor.py` — sharp move detection loop
8. User-owned agent API key storage — `user_api_keys` table, database functions,
   encryption plan, and tests. This must be figured out before creating agents.
9. `agents/core.py` — LangGraph setup, per-user Codex binding, web search tool definition
10. `agents/scheduled_review.py` — Agent 5 routine daily/weekly position review
11. `agents/sharp_move.py` — Agent 1 graph
12. `agents/motive.py` — Agent 3 graph
13. `agents/cross_portfolio.py` — Agent 2 graph
14. `agents/hypothesis.py` — Agent 4 graph with dynamic cadence output
15. `notifications/whatsapp.py` — Twilio client, five message formatters
16. `dashboard/` — Streamlit app, views, components
17. Multi-user auth — scope all dashboard queries by user_id

The Opportunity Discovery Agent is a post-core expansion and is intentionally not
included in this build order or in the count of five required agents.

---

## Key Constraints and Decisions

- **Supabase connection:** Use the `supabase-py` REST client — no raw psycopg2,
  no connection pooling concerns at this scale
- **Twelve Data:** Use the official `twelvedata` Python client for 15-minute
  OHLCV data. The free Basic plan is suitable for local prototyping only; review
  commercial market-data licensing before making Sentient available to users.
- **LangGraph vs raw Anthropic API:** LangGraph is used for all agent graphs;
  the raw Anthropic API is not called directly anywhere in the agents layer
- **Per-user agent billing:** Agent model clients must be created with the user's
  own provider API key. The backend `.env` API key may be kept only as a local
  development fallback or admin override, not as the default production billing
  source.
- **LangSmith:** Enabled for tracing all agent runs; configured via env vars
- **No brokerage integration:** Wealthsimple is not connected; Sentient is
  monitoring and advisory only
- **Cost target:** ~$6–10/month total including Codex API, Twilio, and hosting
