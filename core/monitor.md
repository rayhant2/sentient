# Sharp-Move Monitor

`monitor.py` decides whether a newly completed 15-minute candle deserves an
investigation. It performs deterministic market calculations only. It does not
explain the move, search the web, make a recommendation, or send a notification;
those responsibilities belong to the sharp-move agent and notification layer.

## Inputs

For each successfully refreshed ticker, the monitor receives up to 150 completed
OHLCV candles and the ticker's subscriptions from the event-bus registry. Candles
are sorted chronologically before any calculation.

## Latest return

The latest 15-minute close-to-close return is:

```text
r_t = (close_t - close_(t-1)) / close_(t-1)
```

Both directions are treated equally by using `abs(r_t)` for trigger checks.

## Trigger conditions

A user is targeted when either condition is true:

### 1. Personal absolute threshold

```text
abs(r_t) >= subscription.sharp_move_threshold
```

The default threshold is 1% (`0.01`), and each subscription can override it.

### 2. Volatility anomaly

The monitor uses up to the previous 50 fifteen-minute returns, excluding the
current return. At least 20 prior returns are required. Their sample mean and
sample standard deviation are:

```text
mu = mean(prior returns)
sigma = sample_standard_deviation(prior returns)
z = abs(r_t - mu) / sigma
```

The volatility branch triggers when:

```text
abs(r_t) >= 0.5% and z >= 3.0
```

The 0.5% floor prevents tiny movements in very quiet data from producing alerts
solely because their statistical score is high. If there are fewer than 20 prior
returns, only the personal absolute threshold can trigger. If prior volatility is
zero and the new return differs from the baseline, the z-score is treated as
infinite.

## Context calculations

Average prices provide significance, not the definition of a sharp market move.

The rolling VWAP uses the latest 26 candles, approximately one regular trading
session:

```text
VWAP = sum(close_i * volume_i) / sum(volume_i)
distance = (current_price - VWAP) / VWAP
```

If every volume is zero, the monitor falls back to the arithmetic mean close.

For each subscriber, the monitor also records whether the candle crossed that
user's average purchase price:

```text
previous_close < average_price <= current_price
or
current_price <= average_price < previous_close
```

VWAP distance and cost-basis crossing help the future agent explain why a move is
personally meaningful. They do not independently trigger an investigation.

## Duplicate suppression

After a successful dispatch, the same user and candle cannot alert twice. Further
alerts are suppressed for 60 minutes unless either:

- The move reverses direction.
- Its absolute size is at least 0.5 percentage points larger than the last alert.

Cooldown state is currently in memory and resets when the process restarts. A
future multi-instance deployment should move this state to shared storage.

## Dispatch

The monitor collects the users whose conditions passed and emits one targeted
event:

```python
event_bus.emit(
    EventType.SHARP_MOVE,
    ticker,
    target_user_ids=triggered_user_ids,
)
```

The event bus loads shared history once, creates one personal `AgentContext` per
target, and hands it to the registered sharp-move handler. Handler failures do
not mark failed users as alerted, allowing a later retry.
