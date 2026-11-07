"""PurgeVetoedConsecutive: fires at two consecutive vetoed extractions, not at one.

The rule's threshold is read from the provisioned file, so a changed evaluator fails
here, and the fixtures are the gauge values a real extraction sequence produces:
a clean purge sets 0, one veto sets 1, a second consecutive veto sets 2.
"""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
RULES = (REPO_ROOT / "config/grafana/provisioning/alerting/groovemap.yaml").read_text(encoding="utf-8")
BLOCK = RULES[RULES.index("uid: groovemap-purge-vetoed-consecutive") : RULES.index("uid: groovemap-insights-stale")]


def _threshold() -> tuple[str, float]:
    match = re.search(r"evaluator: \{type: (\w+), params: \[([\d.]+)\]\}", BLOCK)
    assert match is not None, "the rule needs a threshold evaluator"
    return match.group(1), float(match.group(2))


def _fires(value: float) -> bool:
    """Evaluate the rule's threshold node on one gauge value, as Grafana does."""
    kind, bound = _threshold()
    if kind == "gt":
        return value > bound
    if kind == "lt":
        return value < bound
    raise AssertionError(f"unexpected evaluator {kind}")


class TestPurgeVetoAlertFiresAtTwo:
    def test_a_clean_purge_does_not_fire(self) -> None:
        assert _fires(0) is False

    def test_one_veto_followed_by_a_clean_purge_stays_silent(self) -> None:
        assert _fires(1) is False

    def test_two_consecutive_vetoes_fire(self) -> None:
        assert _fires(2) is True

    def test_a_longer_run_keeps_firing(self) -> None:
        assert _fires(5) is True

    def test_the_rule_groups_by_both_loader_label_shapes(self) -> None:
        assert "max by (loader, source, entity)" in BLOCK

    def test_the_rule_reads_the_catalogued_gauge(self) -> None:
        assert "groovemap_pipeline_purge_consecutive_vetoes" in BLOCK

    def test_the_description_states_the_threshold(self) -> None:
        assert "Threshold: groovemap_pipeline_purge_consecutive_vetoes above 1" in BLOCK
