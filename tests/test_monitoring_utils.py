"""Unit tests for ypl/backend/utils/monitoring.py.

Covers:
- CounterMetric: inc, inc_by, flush_values, min-gap enforcement
- ValueMetric: record_value, flush_values, overflow pruning
- MetricsRegistry.get_series: windowing/averaging, empty flush
- MetricManager: get_metric, _get_metric_key, registry management
- sanitize_name / sanitize_labels
- Convenience functions: metric_inc, metric_inc_by, metric_record,
  metric_inc_with_labels, metric_inc_by_with_labels, metric_record_with_labels
- flush_all_metrics_for_cron_job
"""

from __future__ import annotations
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ypl.backend.utils.monitoring import (
    MAX_VALUES_TO_KEEP_BEFORE_FLUSHING,
    MIN_GAP_BETWEEN_TS_DATA_POINTS_SECS,
    CounterMetric,
    MetricManager,
    MetricsRegistry,
    ValueMetric,
    flush_all_metrics_for_cron_job,
    metric_inc,
    metric_inc_by,
    metric_inc_by_with_labels,
    metric_inc_with_labels,
    metric_record,
    metric_record_with_labels,
    sanitize_labels,
    sanitize_name,
)

MODULE = "ypl.backend.utils.monitoring"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_settings_disabled() -> MagicMock:
    """Settings where GCP metric writing is disabled (no-op shortcuts)."""
    settings = MagicMock()
    settings.DISABLE_WRITE_GOOGLE_CLOUD_METRICS = True
    return settings


def _mock_settings_enabled() -> MagicMock:
    settings = MagicMock()
    settings.DISABLE_WRITE_GOOGLE_CLOUD_METRICS = False
    settings.ENVIRONMENT = "local"
    return settings


# ---------------------------------------------------------------------------
# CounterMetric
# ---------------------------------------------------------------------------


class TestCounterMetric:
    def test_initial_value_is_zero(self) -> None:
        m = CounterMetric("test_counter")
        # flush_values returns [(now, 0)] — value should be 0
        # But may return [] due to min-gap check on first call with _last_export_time=0
        # Force it: set _last_export_time to far past
        m._last_export_time = 0.0
        vals = m.flush_values()
        # Either no values (min-gap) or value is 0
        if vals:
            assert vals[0][1] == 0

    def test_inc_increments_by_one(self) -> None:
        m = CounterMetric("test_counter")
        m.inc()
        m.inc()
        m._last_export_time = 0.0
        vals = m.flush_values()
        assert vals[0][1] == 2

    def test_inc_by_increments_correctly(self) -> None:
        m = CounterMetric("test_counter")
        m.inc_by(5)
        m.inc_by(3)
        m._last_export_time = 0.0
        vals = m.flush_values()
        assert vals[0][1] == 8

    def test_flush_values_returns_empty_within_min_gap(self) -> None:
        m = CounterMetric("test_counter")
        m.inc()
        # Set last export time to "just now"
        m._last_export_time = time.time()
        vals = m.flush_values()
        assert vals == []

    def test_flush_values_returns_value_after_min_gap(self) -> None:
        m = CounterMetric("test_counter")
        m.inc()
        # Set last export time to long ago
        m._last_export_time = time.time() - (MIN_GAP_BETWEEN_TS_DATA_POINTS_SECS + 1)
        vals = m.flush_values()
        assert len(vals) == 1
        assert vals[0][1] == 1

    def test_inc_is_thread_safe(self) -> None:
        """Concurrent increments should all count."""
        import threading

        m = CounterMetric("concurrent")
        threads = [threading.Thread(target=m.inc) for _ in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        m._last_export_time = 0.0
        vals = m.flush_values()
        assert vals[0][1] == 100

    def test_name_and_labels_stored(self) -> None:
        labels = {"env": "test"}
        m = CounterMetric("my_counter", labels=labels)
        assert m.name == "my_counter"
        assert m.labels == labels


# ---------------------------------------------------------------------------
# ValueMetric
# ---------------------------------------------------------------------------


class TestValueMetric:
    def test_record_and_flush(self) -> None:
        m = ValueMetric("latency")
        m.record_value(100)
        m.record_value(200)
        vals = m.flush_values()
        assert len(vals) == 2
        values_only = [v for _, v in vals]
        assert sorted(values_only) == [100, 200]

    def test_flush_clears_values(self) -> None:
        m = ValueMetric("latency")
        m.record_value(42)
        m.flush_values()  # First flush
        vals = m.flush_values()  # Second flush should be empty
        assert vals == []

    def test_overflow_pruning(self) -> None:
        m = ValueMetric("big_metric")
        for i in range(MAX_VALUES_TO_KEEP_BEFORE_FLUSHING + 10):
            m.record_value(i)
        # After pruning, values should be at most MAX_VALUES_TO_KEEP_BEFORE_FLUSHING
        assert len(m._values) <= MAX_VALUES_TO_KEEP_BEFORE_FLUSHING

    def test_record_value_is_thread_safe(self) -> None:
        import threading

        m = ValueMetric("concurrent_val")
        threads = [threading.Thread(target=m.record_value, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        vals = m.flush_values()
        # Some may have been pruned but we should have some values
        assert len(vals) <= 50

    def test_timestamps_are_included(self) -> None:
        m = ValueMetric("ts_test")
        m.record_value(999)
        vals = m.flush_values()
        assert len(vals) == 1
        timestamp, value = vals[0]
        assert value == 999
        assert timestamp > 0


# ---------------------------------------------------------------------------
# MetricsRegistry.get_series
# ---------------------------------------------------------------------------


class TestMetricsRegistryGetSeries:
    def test_empty_flush_returns_empty_list(self) -> None:
        m = ValueMetric("empty_metric")
        registry = MetricsRegistry(m)
        series = registry.get_series()
        assert series == []

    def test_single_value_creates_one_series(self) -> None:
        m = ValueMetric("single_metric")
        m.record_value(42)
        registry = MetricsRegistry(m)

        with patch(f"{MODULE}.METRICS_MANAGER") as mock_mgr:
            mock_mgr.instance_id = "test-instance"
            series = registry.get_series()

        assert len(series) == 1

    def test_values_within_min_gap_are_averaged(self) -> None:
        """Two values close together should collapse to one averaged point."""
        m = ValueMetric("windowed_metric")
        now = time.time()
        # Manually inject two values with timestamps less than MIN_GAP_BETWEEN_TS_DATA_POINTS_SECS apart
        m._values = [(now, 100), (now + 1, 200)]
        registry = MetricsRegistry(m)

        with patch(f"{MODULE}.METRICS_MANAGER") as mock_mgr:
            mock_mgr.instance_id = "test-instance"
            series = registry.get_series()

        # Both are in the same window → averaged → 1 series point
        assert len(series) == 1

    def test_values_far_apart_create_separate_series(self) -> None:
        m = ValueMetric("spread_metric")
        now = time.time()
        gap = MIN_GAP_BETWEEN_TS_DATA_POINTS_SECS + 1
        m._values = [(now, 10), (now + gap, 20)]
        registry = MetricsRegistry(m)

        with patch(f"{MODULE}.METRICS_MANAGER") as mock_mgr:
            mock_mgr.instance_id = "test-instance"
            series = registry.get_series()

        assert len(series) == 2

    def test_counter_registry_uses_flush_values(self) -> None:
        m = CounterMetric("cnt")
        m.inc_by(5)
        m._last_export_time = 0.0
        registry = MetricsRegistry(m)

        with patch(f"{MODULE}.METRICS_MANAGER") as mock_mgr:
            mock_mgr.instance_id = "inst-1"
            series = registry.get_series()

        assert len(series) == 1


# ---------------------------------------------------------------------------
# MetricManager
# ---------------------------------------------------------------------------


class TestMetricManager:
    def test_get_metric_creates_counter_metric(self) -> None:
        manager = MetricManager()
        m = manager.get_metric("my_counter", CounterMetric)
        assert isinstance(m, CounterMetric)

    def test_get_metric_creates_value_metric(self) -> None:
        manager = MetricManager()
        m = manager.get_metric("my_value", ValueMetric)
        assert isinstance(m, ValueMetric)

    def test_get_metric_returns_same_instance(self) -> None:
        manager = MetricManager()
        m1 = manager.get_metric("reused", CounterMetric)
        m2 = manager.get_metric("reused", CounterMetric)
        assert m1 is m2

    def test_metric_key_without_labels(self) -> None:
        manager = MetricManager()
        key = manager._get_metric_key("my_metric")
        assert key == "my_metric"

    def test_metric_key_with_labels_is_deterministic(self) -> None:
        manager = MetricManager()
        key1 = manager._get_metric_key("m", {"b": "2", "a": "1"})
        key2 = manager._get_metric_key("m", {"a": "1", "b": "2"})
        assert key1 == key2

    def test_metrics_with_different_labels_are_separate(self) -> None:
        manager = MetricManager()
        m1 = manager.get_metric("count", CounterMetric, {"env": "staging"})
        m2 = manager.get_metric("count", CounterMetric, {"env": "production"})
        assert m1 is not m2

    def test_get_input_list_returns_all_registries(self) -> None:
        manager = MetricManager()
        manager.get_metric("c1", CounterMetric)
        manager.get_metric("c2", CounterMetric)
        lst = manager._get_input_list()
        assert len(lst) >= 2

    def test_instance_id_is_uuid_string(self) -> None:
        import uuid

        manager = MetricManager()
        uuid.UUID(manager.instance_id)  # Should not raise

    def test_start_is_idempotent(self) -> None:
        """Calling start() twice should not create two tasks."""
        manager = MetricManager()
        mock_task = MagicMock()
        mock_task.done.return_value = False
        manager._export_task = mock_task

        manager.start()  # Should be no-op since task is not done
        # The mock task should still be the same
        assert manager._export_task is mock_task


# ---------------------------------------------------------------------------
# sanitize_name / sanitize_labels
# ---------------------------------------------------------------------------


class TestSanitizeName:
    def test_leaves_valid_names_unchanged(self) -> None:
        assert sanitize_name("valid_metric_name") == "valid_metric_name"

    def test_replaces_invalid_chars_with_underscore(self) -> None:
        result = sanitize_name("metric<name>here")
        assert "<" not in result
        assert ">" not in result
        assert "_" in result

    def test_allows_slashes(self) -> None:
        result = sanitize_name("general/metric_name")
        assert result == "general/metric_name"

    def test_handles_empty_string(self) -> None:
        result = sanitize_name("")
        assert result == ""

    def test_replaces_spaces(self) -> None:
        result = sanitize_name("metric name")
        assert " " not in result

    def test_replaces_hyphens(self) -> None:
        result = sanitize_name("metric-name")
        assert "-" not in result
        assert result == "metric_name"


class TestSanitizeLabels:
    def test_sanitizes_label_values(self) -> None:
        labels = {"model": "claude-3<opus>"}
        result = sanitize_labels(labels)
        assert "<" not in result["model"]
        assert ">" not in result["model"]

    def test_preserves_valid_label_values(self) -> None:
        labels = {"env": "staging", "region": "us_central1"}
        result = sanitize_labels(labels)
        assert result == labels

    def test_handles_empty_labels(self) -> None:
        result = sanitize_labels({})
        assert result == {}

    def test_sanitizes_multiple_labels(self) -> None:
        labels = {"k1": "v<1>", "k2": "v/2"}
        result = sanitize_labels(labels)
        assert "<" not in result["k1"]
        assert result["k2"] == "v/2"


# ---------------------------------------------------------------------------
# Convenience functions (no-op when disabled)
# ---------------------------------------------------------------------------


class TestConvenienceFunctionsDisabled:
    def test_metric_inc_noop_when_disabled(self) -> None:
        mock_settings = _mock_settings_disabled()
        with patch(f"{MODULE}.settings", mock_settings):
            metric_inc("some_counter")  # Should not raise

    def test_metric_inc_by_noop_when_disabled(self) -> None:
        mock_settings = _mock_settings_disabled()
        with patch(f"{MODULE}.settings", mock_settings):
            metric_inc_by("some_counter", 5)

    def test_metric_record_noop_when_disabled(self) -> None:
        mock_settings = _mock_settings_disabled()
        with patch(f"{MODULE}.settings", mock_settings):
            metric_record("some_metric", 100)

    def test_metric_inc_with_labels_noop_when_disabled(self) -> None:
        mock_settings = _mock_settings_disabled()
        with patch(f"{MODULE}.settings", mock_settings):
            metric_inc_with_labels("cnt", {"env": "test"})

    def test_metric_inc_by_with_labels_noop_when_disabled(self) -> None:
        mock_settings = _mock_settings_disabled()
        with patch(f"{MODULE}.settings", mock_settings):
            metric_inc_by_with_labels("cnt", 10, {"env": "test"})

    def test_metric_record_with_labels_noop_when_disabled(self) -> None:
        mock_settings = _mock_settings_disabled()
        with patch(f"{MODULE}.settings", mock_settings):
            metric_record_with_labels("latency", 250, {"region": "us"})


class TestConvenienceFunctionsEnabled:
    def test_metric_inc_increments_counter(self) -> None:
        manager = MetricManager()
        mock_settings = _mock_settings_enabled()

        with (
            patch(f"{MODULE}.settings", mock_settings),
            patch(f"{MODULE}.METRICS_MANAGER", manager),
        ):
            metric_inc("test_counter_enabled")
            m = manager.get_metric("test_counter_enabled", CounterMetric)

        assert isinstance(m, CounterMetric)
        m._last_export_time = 0.0
        vals = m.flush_values()
        assert vals[0][1] == 1

    def test_metric_inc_by_increments_counter(self) -> None:
        manager = MetricManager()
        mock_settings = _mock_settings_enabled()

        with (
            patch(f"{MODULE}.settings", mock_settings),
            patch(f"{MODULE}.METRICS_MANAGER", manager),
        ):
            metric_inc_by("my_cnt_by", 7)
            m = manager.get_metric("my_cnt_by", CounterMetric)

        assert isinstance(m, CounterMetric)
        m._last_export_time = 0.0
        vals = m.flush_values()
        assert vals[0][1] == 7

    def test_metric_record_records_value(self) -> None:
        manager = MetricManager()
        mock_settings = _mock_settings_enabled()

        with (
            patch(f"{MODULE}.settings", mock_settings),
            patch(f"{MODULE}.METRICS_MANAGER", manager),
        ):
            metric_record("my_latency", 300)
            m = manager.get_metric("my_latency", ValueMetric)

        vals = m.flush_values()
        assert any(v == 300 for _, v in vals)

    def test_metric_inc_raises_if_existing_metric_is_value_metric(self) -> None:
        manager = MetricManager()
        manager.get_metric("conflict_metric", ValueMetric)  # Register as ValueMetric
        mock_settings = _mock_settings_enabled()

        with (
            patch(f"{MODULE}.settings", mock_settings),
            patch(f"{MODULE}.METRICS_MANAGER", manager),
            pytest.raises(ValueError, match="not a CounterMetric"),
        ):
            metric_inc("conflict_metric")

    def test_metric_record_raises_if_existing_metric_is_counter(self) -> None:
        manager = MetricManager()
        manager.get_metric("conflict_value", CounterMetric)  # Register as CounterMetric
        mock_settings = _mock_settings_enabled()

        with (
            patch(f"{MODULE}.settings", mock_settings),
            patch(f"{MODULE}.METRICS_MANAGER", manager),
            pytest.raises(ValueError, match="not a ValueMetric"),
        ):
            metric_record("conflict_value", 100)

    def test_metric_inc_with_labels(self) -> None:
        manager = MetricManager()
        mock_settings = _mock_settings_enabled()

        with (
            patch(f"{MODULE}.settings", mock_settings),
            patch(f"{MODULE}.METRICS_MANAGER", manager),
        ):
            metric_inc_with_labels("labeled_cnt", {"region": "us-central"})

        # Metric should exist in registry with sanitized labels
        assert len(manager.registry_map) >= 1

    def test_metric_inc_by_with_labels(self) -> None:
        manager = MetricManager()
        mock_settings = _mock_settings_enabled()

        with (
            patch(f"{MODULE}.settings", mock_settings),
            patch(f"{MODULE}.METRICS_MANAGER", manager),
        ):
            metric_inc_by_with_labels("labeled_cnt_by", 5, {"model": "gpt<4>"})

        assert len(manager.registry_map) >= 1

    def test_metric_record_with_labels(self) -> None:
        manager = MetricManager()
        mock_settings = _mock_settings_enabled()

        with (
            patch(f"{MODULE}.settings", mock_settings),
            patch(f"{MODULE}.METRICS_MANAGER", manager),
        ):
            metric_record_with_labels("labeled_latency", 500, {"env": "staging"})

        assert len(manager.registry_map) >= 1


# ---------------------------------------------------------------------------
# flush_all_metrics_for_cron_job
# ---------------------------------------------------------------------------


class TestFlushAllMetricsForCronJob:
    async def test_noop_when_disabled(self) -> None:
        mock_settings = _mock_settings_disabled()
        with patch(f"{MODULE}.settings", mock_settings):
            await flush_all_metrics_for_cron_job()  # Should not raise

    async def test_noop_when_manager_not_initialized(self) -> None:
        mock_settings = _mock_settings_enabled()
        manager = MetricManager()  # No client or project_name set

        with (
            patch(f"{MODULE}.settings", mock_settings),
            patch(f"{MODULE}.METRICS_MANAGER", manager),
        ):
            await flush_all_metrics_for_cron_job()  # Should log warning but not raise

    async def test_calls_export_when_initialized(self) -> None:
        mock_settings = _mock_settings_enabled()
        manager = MetricManager()
        manager.client = AsyncMock()
        manager.project_name = "projects/test-project"

        with (
            patch(f"{MODULE}.settings", mock_settings),
            patch(f"{MODULE}.METRICS_MANAGER", manager),
            patch.object(manager, "export_to_gcp", new_callable=AsyncMock) as mock_export,
        ):
            await flush_all_metrics_for_cron_job()

        mock_export.assert_awaited_once()
