"""PipelineBatchStalled and PipelineBatchDeliveryLost: when they fire, and when they stay silent.

The thresholds are read from the provisioned file. The timing fixtures model the rule
against the loader's real constants: a 300s stall bound (POSTGRES_BATCH_STALL_S default),
a gauge re-set every 15s, and the rule's 1m `for`. This is a model of Grafana's
evaluation, not a Prometheus run, so the wiring is covered by test_dashboards.py.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Callable


REPO_ROOT = Path(__file__).resolve().parents[2]
RULES = (REPO_ROOT / "config/grafana/provisioning/alerting/groovemap.yaml").read_text(encoding="utf-8")
STALLED = RULES[RULES.index("uid: groovemap-pipeline-batch-stalled") : RULES.index("uid: groovemap-batch-delivery-lost")]
DELIVERY_LOST = RULES[RULES.index("uid: groovemap-batch-delivery-lost") : RULES.index("uid: groovemap-insights-stale")]

STALL_BOUND_S = 300.0
REPORT_INTERVAL_S = 15.0
FOR_S = 60.0
HORIZON_S = 2000.0


def _threshold(block: str) -> tuple[str, float]:
    match = re.search(r"evaluator: \{type: (\w+), params: \[([\d.]+)\]\}", block)
    assert match is not None
    return match.group(1), float(match.group(2))


def _above(value: float, block: str) -> bool:
    kind, bound = _threshold(block)
    assert kind == "gt"
    return value > bound


def _first_fire(last_ack_at: Callable[[float], float]) -> float | None:
    """First report time at which the stalled gauge has held 1 for the rule's `for`.

    The loader sets the gauge to 1 when now - last ack > STALL_BOUND_S, reporting every
    REPORT_INTERVAL_S. Grafana fires once the condition has held for FOR_S.
    """
    held_since: float | None = None
    t = 0.0
    while t <= HORIZON_S:
        stalled = (t - last_ack_at(t)) > STALL_BOUND_S
        if stalled:
            if held_since is None:
                held_since = t
            if t - held_since >= FOR_S:
                return t
        else:
            held_since = None
        t += REPORT_INTERVAL_S
    return None


class TestStalledThresholdAndTiming:
    def test_the_gauge_fires_at_one_and_not_at_zero(self) -> None:
        assert _above(0, STALLED) is False
        assert _above(1, STALLED) is True

    def test_the_rule_groups_by_loader_and_entity(self) -> None:
        assert "max by (loader, entity) (groovemap_pipeline_batch_stalled" in STALLED

    def test_a_stalled_batch_alerts_within_the_stated_bound(self) -> None:
        # The last ack was at t=0 and nothing settles after it.
        fired = _first_fire(lambda _t: 0.0)
        assert fired is not None, "a stalled batch must alert"
        assert STALL_BOUND_S < fired <= 390.0, f"fired at {fired}s, expected about 375s"

    def test_the_description_states_the_bound_and_the_latency(self) -> None:
        assert "Threshold: groovemap_pipeline_batch_stalled above 0 for 1m" in STALLED
        assert "about 6 minutes" in STALLED


class TestNormalLongFlushStaysSilent:
    def test_a_deep_queue_that_keeps_settling_never_fires(self) -> None:
        # The oldest record has waited 900s, but a flush acks every 60s, so the last ack is always recent.
        def last_ack(t: float) -> float:
            return float(int(t // 60) * 60)

        assert _first_fire(last_ack) is None

    def test_a_batch_that_recovers_before_the_for_window_does_not_fire(self) -> None:
        # Last ack at t=0; the gauge is 1 from t=315 (first report past the bound) until the
        # ack at t=360, after which acks continue every 60s. That is 30s of held stall at the
        # reports, shorter than the 1m `for`.
        def last_ack(t: float) -> float:
            return 0.0 if t < 360 else float(int(t // 60) * 60)

        assert _first_fire(last_ack) is None


class TestDeliveryLostRule:
    def test_one_delivery_lost_fires_and_zero_does_not(self) -> None:
        assert _above(0, DELIVERY_LOST) is False
        assert _above(1, DELIVERY_LOST) is True

    def test_only_delivery_lost_is_counted_not_held_timeout(self) -> None:
        assert 'reason="delivery_lost"' in DELIVERY_LOST
        assert "held_timeout" not in DELIVERY_LOST
