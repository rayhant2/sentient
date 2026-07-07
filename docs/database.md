# Database Schema

Sentient uses Supabase/Postgres as the durable store. The schema lives in
[`supabase/schema.sql`](../supabase/schema.sql) and should be run in the Supabase
SQL editor for a new project.

The core design is: ticker market data is shared, user context is personal.

## Tables

### `users`

One row per Sentient user.

Important fields:

- `user_id`: app-level primary key.
- `whatsapp_number`: unique number used for Twilio delivery.
- `email`: optional unique email.
- `preferences`: JSONB for user-level settings that do not deserve columns yet.

### `tickers`

One row per unique stock symbol across all users.

Important fields:

- `ticker`: primary key, stored uppercase.
- `last_fetched`: last successful Polygon fetch.
- `next_fetch_time`: next scheduled fetch time.
- `current_price`: latest known shared price.

This table is shared infrastructure. If 50 users watch NVDA, NVDA appears here
once.

### `ticker_data`

Shared OHLCV candles for each ticker.

Important fields:

- `ticker`: foreign key to `tickers`.
- `timestamp`: candle timestamp.
- `open`, `high`, `low`, `close`, `volume`: validated non-negative OHLCV data.

The primary key is `(ticker, timestamp)` so the same candle cannot be inserted
twice. The app layer will enforce the rolling 150-row limit per ticker in
`data/database.py`.

### `subscriptions`

The personalization layer. This is the heart of the multi-user design.

Important fields:

- `user_id`: foreign key to `users`.
- `ticker`: foreign key to `tickers`.
- `avg_price`: user's average entry price.
- `shares`: user's position size.
- `motive`: one of `holding`, `short-term`, or `watching`.
- `update_interval`: one of `daily` or `weekly`.
- `sharp_move_threshold`: user-specific move threshold, default `0.025`.

The primary key is `(user_id, ticker)`, which means each user can have one
subscription per ticker while many users can watch the same ticker.

### `updates`

History of agent outputs.

Important fields:

- `user_id`: owner of the update.
- `ticker`: ticker being discussed, nullable for future portfolio-level outputs.
- `event_type`: one of the `EventType` values from `models/schemas.py`.
- `summary`: agent-written summary.
- `recommendation`: agent-written recommendation or next-step framing.
- `confidence`: one of `high`, `medium`, or `low`.
- `price_at_update`: optional price snapshot.
- `searched_web`: whether the agent used web search.
- `metadata`: JSONB escape hatch for future agent-specific fields.

### `alerts`

History of WhatsApp notifications.

Important fields:

- `user_id`: notification recipient.
- `ticker`: nullable because cross-portfolio alerts are not about one ticker.
- `alert_type`: one of the `AlertType` values from `models/schemas.py`.
- `message`: exact notification body or rendered alert text.
- `trigger_details`: JSONB with structured context about why the alert fired.

## Indexes

The schema adds indexes for the app's expected query patterns:

- `subscriptions(ticker)`: event bus fan-out by ticker.
- `subscriptions(user_id)`: dashboard/user portfolio lookups.
- `ticker_data(ticker, timestamp desc)`: latest candles for charts and agent context.
- `tickers(next_fetch_time)`: scheduler lookup for due ticker fetches.
- `updates(user_id, timestamp desc)`: latest user updates.
- `updates(user_id, ticker, timestamp desc)`: latest update for one user/ticker.
- `alerts(user_id, timestamp desc)`: alert history.
- `alerts(user_id, ticker, timestamp desc)`: alert history for one user/ticker.

## Constraint Strategy

The schema uses `text check (...)` constraints instead of Postgres enum types.
That keeps early development flexible while still preventing invalid values from
entering the database.

The checked values mirror `models/schemas.py`:

- `motive`: `holding`, `short-term`, `watching`
- `update_interval`: `daily`, `weekly`
- `event_type`: `scheduled_update`, `sharp_move`, `motive_check`, `hypothesis_scan`
- `confidence`: `high`, `medium`, `low`
- `alert_type`: `sharp_move`, `motive_flag`, `hypothesis`, `cross_portfolio`, `scheduled`

## Next Step

After this schema is created in Supabase, Step 4 is `data/database.py`: a single
Python interface for all database reads and writes. No other module should talk
to Supabase directly.
