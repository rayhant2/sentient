from datetime import datetime, timedelta, timezone
import unittest

from core.event_bus import EventBus
from core.monitor import MonitorError, SharpMoveMonitor
from models.schemas import (
    AgentContext,
    EventType,
    Motive,
    OHLCVPoint,
    Subscription,
    UpdateInterval,
)


START = datetime(2026, 7, 20, 14, 0, tzinfo=timezone.utc)


def subscription(
    user_id: str,
    *,
    threshold: float = 0.01,
    avg_price: float = 100.5,
) -> Subscription:
    return Subscription(
        user_id=user_id,
        ticker="NVDA",
        avg_price=avg_price,
        shares=2.0,
        motive=Motive.HOLDING,
        update_interval=UpdateInterval.DAILY,
        sharp_move_threshold=threshold,
    )


def points_from_returns(returns: list[float], start_price: float = 100.0):
    prices = [start_price]
    for move in returns:
        prices.append(prices[-1] * (1 + move))

    return [
        OHLCVPoint(
            ticker="NVDA",
            timestamp=START + timedelta(minutes=15 * index),
            open=price,
            high=price * 1.001,
            low=price * 0.999,
            close=price,
            volume=1000.0 + index,
        )
        for index, price in enumerate(prices)
    ]


def configured_monitor(
    subscriptions: list[Subscription],
    datapoints: list[OHLCVPoint],
):
    bus = EventBus(datapoint_loader=lambda _ticker, _limit: datapoints)
    for item in subscriptions:
        bus.refresh_subscription(item)
    received: list[AgentContext] = []
    bus.register_handler(EventType.SHARP_MOVE, received.append)
    return SharpMoveMonitor(bus), received


class SharpMoveMonitorTests(unittest.TestCase):
    def test_absolute_threshold_targets_only_matching_users(self):
        datapoints = points_from_returns([0.012])
        monitor, received = configured_monitor(
            [
                subscription("user-1", threshold=0.01),
                subscription("user-2", threshold=0.02),
            ],
            datapoints,
        )

        result = monitor.evaluate_ticker("nvda", datapoints, now=START)

        self.assertEqual(result.target_user_ids, frozenset({"user-1"}))
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].subscription.user_id, "user-1")
        decisions = {decision.user_id: decision for decision in result.decisions}
        self.assertTrue(decisions["user-1"].absolute_trigger)
        self.assertFalse(decisions["user-2"].triggered)

    def test_volatility_anomaly_can_trigger_below_absolute_threshold(self):
        prior_returns = [0.001 if index % 2 == 0 else -0.001 for index in range(24)]
        datapoints = points_from_returns([*prior_returns, 0.006])
        monitor, received = configured_monitor(
            [subscription("user-1", threshold=0.01)],
            datapoints,
        )

        result = monitor.evaluate_ticker("NVDA", datapoints, now=START)

        decision = result.decisions[0]
        self.assertFalse(decision.absolute_trigger)
        self.assertTrue(decision.volatility_trigger)
        self.assertGreater(result.metrics.volatility_z_score, 3.0)
        self.assertEqual(len(received), 1)

    def test_move_below_anomaly_floor_does_not_trigger(self):
        prior_returns = [0.0001 if index % 2 == 0 else -0.0001 for index in range(24)]
        datapoints = points_from_returns([*prior_returns, 0.004])
        monitor, received = configured_monitor(
            [subscription("user-1", threshold=0.01)],
            datapoints,
        )

        result = monitor.evaluate_ticker("NVDA", datapoints, now=START)

        self.assertGreater(result.metrics.volatility_z_score, 3.0)
        self.assertFalse(result.triggered)
        self.assertEqual(received, [])

    def test_cost_basis_and_vwap_are_context_not_independent_triggers(self):
        datapoints = points_from_returns([0.006])
        monitor, received = configured_monitor(
            [subscription("user-1", threshold=0.01, avg_price=100.5)],
            datapoints,
        )

        result = monitor.evaluate_ticker("NVDA", datapoints, now=START)

        self.assertTrue(result.decisions[0].crossed_cost_basis)
        self.assertGreater(result.metrics.rolling_vwap, 0)
        self.assertFalse(result.triggered)
        self.assertEqual(received, [])

    def test_same_candle_is_dispatched_only_once(self):
        datapoints = points_from_returns([0.012])
        monitor, received = configured_monitor(
            [subscription("user-1")],
            datapoints,
        )

        first = monitor.evaluate_ticker("NVDA", datapoints, now=START)
        second = monitor.evaluate_ticker(
            "NVDA",
            datapoints,
            now=START + timedelta(minutes=5),
        )

        self.assertTrue(first.triggered)
        self.assertFalse(second.triggered)
        self.assertTrue(second.decisions[0].suppressed)
        self.assertEqual(
            second.decisions[0].suppression_reason,
            "already alerted for this candle",
        )
        self.assertEqual(len(received), 1)

    def test_reversal_bypasses_cooldown(self):
        first_points = points_from_returns([0.012])
        monitor, received = configured_monitor(
            [subscription("user-1")],
            first_points,
        )
        monitor.evaluate_ticker("NVDA", first_points, now=START)
        reversal_points = [
            *first_points,
            OHLCVPoint(
                ticker="NVDA",
                timestamp=first_points[-1].timestamp + timedelta(minutes=15),
                open=first_points[-1].close,
                high=first_points[-1].close * 1.001,
                low=first_points[-1].close * 0.98,
                close=first_points[-1].close * 0.988,
                volume=1300.0,
            ),
        ]
        monitor.event_bus._datapoint_loader = lambda _ticker, _limit: reversal_points

        result = monitor.evaluate_ticker(
            "NVDA",
            reversal_points,
            now=START + timedelta(minutes=15),
        )

        self.assertTrue(result.triggered)
        self.assertEqual(len(received), 2)

    def test_larger_same_direction_move_bypasses_cooldown(self):
        first_points = points_from_returns([0.012])
        monitor, received = configured_monitor(
            [subscription("user-1")],
            first_points,
        )
        monitor.evaluate_ticker("NVDA", first_points, now=START)
        escalated_points = [
            *first_points,
            OHLCVPoint(
                ticker="NVDA",
                timestamp=first_points[-1].timestamp + timedelta(minutes=15),
                open=first_points[-1].close,
                high=first_points[-1].close * 1.02,
                low=first_points[-1].close * 0.999,
                close=first_points[-1].close * 1.018,
                volume=1300.0,
            ),
        ]
        monitor.event_bus._datapoint_loader = lambda _ticker, _limit: escalated_points

        result = monitor.evaluate_ticker(
            "NVDA",
            escalated_points,
            now=START + timedelta(minutes=15),
        )

        self.assertTrue(result.triggered)
        self.assertEqual(len(received), 2)

    def test_failed_handler_does_not_start_cooldown(self):
        datapoints = points_from_returns([0.012])
        bus = EventBus(datapoint_loader=lambda _ticker, _limit: datapoints)
        bus.refresh_subscription(subscription("user-1"))

        def failing_handler(_context: AgentContext) -> None:
            raise RuntimeError("queue unavailable")

        bus.register_handler(EventType.SHARP_MOVE, failing_handler)
        monitor = SharpMoveMonitor(bus)
        first = monitor.evaluate_ticker("NVDA", datapoints, now=START)
        received: list[AgentContext] = []
        bus.register_handler(EventType.SHARP_MOVE, received.append)

        second = monitor.evaluate_ticker(
            "NVDA",
            datapoints,
            now=START + timedelta(minutes=5),
        )

        self.assertEqual(first.dispatch_result.failed, 1)
        self.assertTrue(second.triggered)
        self.assertEqual(len(received), 1)

    def test_insufficient_data_returns_reason_without_dispatch(self):
        datapoints = points_from_returns([])
        monitor, received = configured_monitor(
            [subscription("user-1")],
            datapoints,
        )

        result = monitor.evaluate_ticker("NVDA", datapoints, now=START)

        self.assertIsNone(result.metrics)
        self.assertIn("two completed candles", result.reason)
        self.assertEqual(received, [])

    def test_non_positive_previous_close_is_rejected(self):
        datapoints = points_from_returns([0.01])
        datapoints[0] = datapoints[0].model_copy(update={"close": 0.0})
        monitor, _received = configured_monitor(
            [subscription("user-1")],
            datapoints,
        )

        with self.assertRaises(MonitorError):
            monitor.evaluate_ticker("NVDA", datapoints, now=START)


if __name__ == "__main__":
    unittest.main()
