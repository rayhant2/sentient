from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import math
import statistics

from core.event_bus import DispatchResult, EventBus
from models.schemas import EventType, OHLCVPoint, Subscription


DEFAULT_VOLATILITY_WINDOW = 50
DEFAULT_MIN_VOLATILITY_OBSERVATIONS = 20
DEFAULT_VOLATILITY_Z_SCORE = 3.0
DEFAULT_ANOMALY_MOVE_FLOOR = 0.005
DEFAULT_VWAP_WINDOW = 26
DEFAULT_COOLDOWN_MINUTES = 60
DEFAULT_ESCALATION_DELTA = 0.005


class MonitorError(RuntimeError):
    """Raised when sharp-move monitoring cannot evaluate valid market data."""


@dataclass(frozen=True)
class MoveMetrics:
    ticker: str
    candle_timestamp: datetime
    previous_close: float
    current_price: float
    move_pct: float
    baseline_return: float | None
    volatility: float | None
    volatility_z_score: float | None
    rolling_vwap: float
    distance_from_vwap_pct: float


@dataclass(frozen=True)
class UserMoveDecision:
    user_id: str
    threshold: float
    absolute_trigger: bool
    volatility_trigger: bool
    crossed_cost_basis: bool
    suppressed: bool = False
    suppression_reason: str | None = None

    @property
    def triggered(self) -> bool:
        return self.absolute_trigger or self.volatility_trigger


@dataclass(frozen=True)
class MonitorResult:
    ticker: str
    metrics: MoveMetrics | None
    evaluated_users: int
    target_user_ids: frozenset[str] = frozenset()
    decisions: list[UserMoveDecision] = field(default_factory=list)
    dispatch_result: DispatchResult | None = None
    reason: str | None = None

    @property
    def triggered(self) -> bool:
        return bool(self.target_user_ids)


@dataclass(frozen=True)
class _AlertState:
    alerted_at: datetime
    candle_timestamp: datetime
    move_pct: float


class SharpMoveMonitor:
    def __init__(
        self,
        event_bus: EventBus,
        *,
        volatility_window: int = DEFAULT_VOLATILITY_WINDOW,
        min_volatility_observations: int = DEFAULT_MIN_VOLATILITY_OBSERVATIONS,
        volatility_z_score: float = DEFAULT_VOLATILITY_Z_SCORE,
        anomaly_move_floor: float = DEFAULT_ANOMALY_MOVE_FLOOR,
        vwap_window: int = DEFAULT_VWAP_WINDOW,
        cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES,
        escalation_delta: float = DEFAULT_ESCALATION_DELTA,
    ) -> None:
        if volatility_window < 2:
            raise ValueError("volatility_window must be at least two")
        if not 2 <= min_volatility_observations <= volatility_window:
            raise ValueError(
                "min_volatility_observations must be between two and volatility_window"
            )
        if volatility_z_score <= 0:
            raise ValueError("volatility_z_score must be greater than zero")
        if not 0 <= anomaly_move_floor <= 0.5:
            raise ValueError("anomaly_move_floor must be between zero and 50%")
        if vwap_window <= 0:
            raise ValueError("vwap_window must be greater than zero")
        if cooldown_minutes < 0:
            raise ValueError("cooldown_minutes must be non-negative")
        if not 0 <= escalation_delta <= 0.5:
            raise ValueError("escalation_delta must be between zero and 50%")

        self.event_bus = event_bus
        self.volatility_window = volatility_window
        self.min_volatility_observations = min_volatility_observations
        self.volatility_z_score = volatility_z_score
        self.anomaly_move_floor = anomaly_move_floor
        self.vwap_window = vwap_window
        self.cooldown = timedelta(minutes=cooldown_minutes)
        self.escalation_delta = escalation_delta
        self._alert_states: dict[tuple[str, str], _AlertState] = {}

    @staticmethod
    def _ticker_symbol(ticker: str) -> str:
        return ticker.upper()

    @staticmethod
    def _returns(datapoints: list[OHLCVPoint]) -> list[float]:
        returns: list[float] = []
        for previous, current in zip(datapoints, datapoints[1:]):
            if previous.close <= 0:
                raise MonitorError(
                    f"Cannot calculate a return from non-positive close for {previous.ticker}."
                )
            returns.append((current.close - previous.close) / previous.close)
        return returns

    def _volatility_metrics(
        self,
        returns: list[float],
    ) -> tuple[float | None, float | None, float | None]:
        prior_returns = returns[-(self.volatility_window + 1):-1]
        if len(prior_returns) < self.min_volatility_observations:
            return None, None, None

        baseline_return = statistics.fmean(prior_returns)
        volatility = statistics.stdev(prior_returns)
        difference = abs(returns[-1] - baseline_return)
        if math.isclose(volatility, 0.0, abs_tol=1e-12):
            z_score = 0.0 if math.isclose(difference, 0.0, abs_tol=1e-12) else math.inf
        else:
            z_score = difference / volatility
        return baseline_return, volatility, z_score

    def _rolling_vwap(self, datapoints: list[OHLCVPoint]) -> float:
        window = datapoints[-self.vwap_window:]
        total_volume = sum(point.volume for point in window)
        if total_volume <= 0:
            return statistics.fmean(point.close for point in window)
        return sum(point.close * point.volume for point in window) / total_volume

    @staticmethod
    def _crossed_cost_basis(
        subscription: Subscription,
        previous_close: float,
        current_price: float,
    ) -> bool:
        cost_basis = subscription.avg_price
        return (
            previous_close < cost_basis <= current_price
            or current_price <= cost_basis < previous_close
        )

    def _suppression_reason(
        self,
        user_id: str,
        ticker: str,
        metrics: MoveMetrics,
        evaluated_at: datetime,
    ) -> str | None:
        state = self._alert_states.get((user_id, ticker))
        if state is None:
            return None
        if state.candle_timestamp == metrics.candle_timestamp:
            return "already alerted for this candle"
        if evaluated_at - state.alerted_at >= self.cooldown:
            return None

        direction_reversed = (state.move_pct < 0 < metrics.move_pct) or (
            metrics.move_pct < 0 < state.move_pct
        )
        escalated = abs(metrics.move_pct) >= (
            abs(state.move_pct) + self.escalation_delta
        )
        if direction_reversed or escalated:
            return None
        return "within cooldown without reversal or escalation"

    def evaluate_ticker(
        self,
        ticker: str,
        datapoints: list[OHLCVPoint],
        *,
        now: datetime | None = None,
    ) -> MonitorResult:
        symbol = self._ticker_symbol(ticker)
        ticker_registry = self.event_bus.get_registry(symbol)
        ordered = sorted(datapoints, key=lambda point: point.timestamp)
        if len(ordered) < 2:
            return MonitorResult(
                ticker=symbol,
                metrics=None,
                evaluated_users=ticker_registry.subscriber_count,
                reason="at least two completed candles are required",
            )

        returns = self._returns(ordered)
        move_pct = returns[-1]
        baseline_return, volatility, z_score = self._volatility_metrics(returns)
        rolling_vwap = self._rolling_vwap(ordered)
        current_price = ordered[-1].close
        distance_from_vwap_pct = (
            (current_price - rolling_vwap) / rolling_vwap
            if rolling_vwap > 0
            else 0.0
        )
        metrics = MoveMetrics(
            ticker=symbol,
            candle_timestamp=ordered[-1].timestamp,
            previous_close=ordered[-2].close,
            current_price=current_price,
            move_pct=move_pct,
            baseline_return=baseline_return,
            volatility=volatility,
            volatility_z_score=z_score,
            rolling_vwap=rolling_vwap,
            distance_from_vwap_pct=distance_from_vwap_pct,
        )

        evaluated_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        decisions: list[UserMoveDecision] = []
        targets: set[str] = set()
        for subscription in ticker_registry.subscribers:
            absolute_trigger = abs(move_pct) >= subscription.sharp_move_threshold
            volatility_trigger = (
                z_score is not None
                and abs(move_pct) >= self.anomaly_move_floor
                and z_score >= self.volatility_z_score
            )
            suppression_reason = None
            if absolute_trigger or volatility_trigger:
                suppression_reason = self._suppression_reason(
                    subscription.user_id,
                    symbol,
                    metrics,
                    evaluated_at,
                )
                if suppression_reason is None:
                    targets.add(subscription.user_id)

            decisions.append(
                UserMoveDecision(
                    user_id=subscription.user_id,
                    threshold=subscription.sharp_move_threshold,
                    absolute_trigger=absolute_trigger,
                    volatility_trigger=volatility_trigger,
                    crossed_cost_basis=self._crossed_cost_basis(
                        subscription,
                        metrics.previous_close,
                        metrics.current_price,
                    ),
                    suppressed=suppression_reason is not None,
                    suppression_reason=suppression_reason,
                )
            )

        dispatch_result = None
        if targets:
            dispatch_result = self.event_bus.emit(
                EventType.SHARP_MOVE,
                symbol,
                target_user_ids=targets,
            )
            failed_users = {failure.user_id for failure in dispatch_result.failures}
            for user_id in targets - failed_users:
                self._alert_states[(user_id, symbol)] = _AlertState(
                    alerted_at=evaluated_at,
                    candle_timestamp=metrics.candle_timestamp,
                    move_pct=metrics.move_pct,
                )

        return MonitorResult(
            ticker=symbol,
            metrics=metrics,
            evaluated_users=ticker_registry.subscriber_count,
            target_user_ids=frozenset(targets),
            decisions=decisions,
            dispatch_result=dispatch_result,
        )
