import asyncio
import contextlib
import re
import threading
import time
import uuid

from google.cloud import monitoring_v3
from ypl.backend.config import settings
from ypl.structured_logger import get_logger

logger = get_logger()

"""
Metric counters. All counters are exported to GCP every EXPORT_INTERVAL_SECS seconds.

1. create and keep using, good for static counters.

    from ypl.backend.utils.monitoring import CounterMetric, ValueMetric

    NUM_REQUESTS = mon.CounterMetric("num_requests")
    NUM_REQUESTS.inc()
    NUM_REQUESTS.inc_by(10)

    SOME_LATENCY_MS = mon.ValueMetric("some_latency_ms")
    SOME_LATENCY_MS.record_value(249)

2. create one the fly, good for counter with dynamic names. be careful not to blow up the namespace!

    from ypl.backend.utils.monitoring import metric_inc, metric_inc_by, metric_record

    metric_inc(f"num_chosen_{model_name}")            # auto-create CounterMetric
    metric_inc_by(f"num_chosen_{model_name}", 10)     # auto-create CounterMetric

    metric_record(f"latency_routing_ms", 834)  # auto-create ValueMetric

All counters will be exported to GCP periodically every EXPORT_INTERVAL_SECS seconds.
"""
GCM_CLIENT: monitoring_v3.MetricServiceAsyncClient | None = None
GCP_EXPORT_INTERVAL_SECS = 60
GCP_EXPORT_BATCH_SIZE = 50
MAX_VALUES_TO_KEEP_BEFORE_FLUSHING = 128

# According to GCP requirements,for a given timeseries, two points must be at least 5 seconds apart.
# See https://cloud.google.com/monitoring/quotas
MIN_GAP_BETWEEN_TS_DATA_POINTS_SECS = 5


class BaseMetric:
    """Base class for metrics with thread-safe operations."""

    def __init__(self, name: str, labels: dict[str, str] | None = None):
        self.name = name
        self.labels = labels or {}
        self._lock = threading.Lock()

    def flush_values(self) -> list[tuple[float, int]]:
        """Get and clear the current values, return the as a list of tuples of (timestamp, value)."""
        raise NotImplementedError


class CounterMetric(BaseMetric):
    """
    A single thread-safe metric that stores an integer counter. Only one value is stored and flushed.
    """

    def __init__(self, name: str, labels: dict[str, str] | None = None):
        super().__init__(name, labels)
        self._value = 0
        self._last_export_time = 0.0

    def inc(self) -> None:
        with self._lock:
            self._value += 1

    def inc_by(self, amount: int) -> None:
        with self._lock:
            self._value += amount

    def flush_values(self) -> list[tuple[float, int]]:
        with self._lock:
            now = time.time()
            # Only export if enough time has passed since last export
            if now - self._last_export_time < MIN_GAP_BETWEEN_TS_DATA_POINTS_SECS:
                return []
            self._last_export_time = now
            return [(now, self._value)]


class ValueMetric(BaseMetric):
    """
    A thread-safe metric that stores individual values with timestamps.
    This class remembers all values written to it with timestamps.
    """

    # TODO(tian): locking might be improved with the read/write lock split.

    def __init__(self, name: str, labels: dict[str, str] | None = None):
        super().__init__(name, labels)
        # List of (timestamp in seconds since epoch, value) tuples
        self._values: list[tuple[float, int]] = []

    def record_value(self, value: int) -> None:
        with self._lock:
            # Note that here we remember every data point.
            self._values.append((time.time(), value))

            # If we have too many values, remove every other one to reduce memory usage
            if len(self._values) > MAX_VALUES_TO_KEEP_BEFORE_FLUSHING:
                self._values = self._values[::2]  # Keep every other value

    def flush_values(self) -> list[tuple[float, int]]:
        with self._lock:
            values = self._values
            self._values = []
            return values


class MetricsRegistry:
    metric: BaseMetric
    series: monitoring_v3.TimeSeries

    def __init__(self, metric: BaseMetric):
        self.metric = metric

    def get_series(self) -> list[monitoring_v3.TimeSeries]:
        all_series: list[monitoring_v3.TimeSeries] = []
        timestamped_values = self.metric.flush_values()
        # Sort values by timestamp first
        timestamped_values.sort()

        if not timestamped_values:
            return all_series

        # trim timestamped_values to ensure minimum gap between timestamps,
        # if there are more than 1 value in a MIN_GAP_BETWEEN_TS_DATA_POINTS_MS, keep the average of this window
        # with the starting timestamp.
        trimmed_values = []
        current_window_start = timestamped_values[0][0]
        current_window_values = []

        for timestamp, value in timestamped_values:
            # If this timestamp is within the minimum gap of the window start
            if timestamp - current_window_start < MIN_GAP_BETWEEN_TS_DATA_POINTS_SECS:
                current_window_values.append(value)
            else:
                # Window is complete, calculate average and add to trimmed values
                if current_window_values:
                    avg_value = sum(current_window_values) // len(current_window_values)
                    trimmed_values.append((current_window_start, avg_value))

                # Start a new window
                current_window_start = timestamp
                current_window_values = [value]

        # Don't forget the last window
        if current_window_values:
            avg_value = sum(current_window_values) // len(current_window_values)
            trimmed_values.append((current_window_start, avg_value))

        # Use the trimmed values instead of original timestamped_values
        for timestamp, value in trimmed_values:
            series = self._create_series()
            series.points = [
                monitoring_v3.Point(
                    {
                        "interval": self._create_interval(timestamp),
                        "value": {"int64_value": value},
                    }
                )
            ]
            all_series.append(series)

        return all_series

    def _create_series(self) -> monitoring_v3.TimeSeries:
        series = monitoring_v3.TimeSeries()
        series.metric.type = "custom.googleapis.com/" + self.metric.name
        # Add instance_id label to distinguish between Cloud Run instances
        series.metric.labels["instance_id"] = METRICS_MANAGER.instance_id
        # Add custom labels if provided (labels should already be sanitized at ingestion time)
        for key, value in self.metric.labels.items():
            series.metric.labels[key] = value
        # right now it's all the same, but we can change this later
        match settings.ENVIRONMENT:
            case "local":
                series.resource.type = "global"
            case "staging" | "production":
                # TODO(tian): update this to store more instance/replica information in labels if necessary
                series.resource.type = "global"
            case _:
                series.resource.type = "global"
        return series

    def _create_interval(self, now: float) -> monitoring_v3.TimeInterval:
        seconds = int(now)
        nanos = int((now - seconds) * 10**9)
        return monitoring_v3.TimeInterval({"end_time": {"seconds": seconds, "nanos": nanos}})


class MetricManager:
    """Register and export counters periodically."""

    def __init__(self, export_interval_secs: int = GCP_EXPORT_INTERVAL_SECS):
        self.export_interval_secs: int = export_interval_secs
        self.registry_map: dict[str, MetricsRegistry] = {}
        self._lock: threading.Lock = threading.Lock()
        self._stop_event: asyncio.Event = asyncio.Event()
        self._export_task: asyncio.Task | None = None
        # Generate a unique instance ID for this Cloud Run instance
        self.instance_id = str(uuid.uuid4())
        logger.info(f"MetricManager initialized with instance_id: {self.instance_id}")

    def set_up_gcp(self, project_id: str, client: monitoring_v3.MetricServiceAsyncClient) -> None:
        """Set up GCP project and client."""
        self.project_name = f"projects/{project_id}"
        self.client = client

    def _get_metric_key(self, name: str, labels: dict[str, str] | None = None) -> str:
        """Generate a unique key for a metric based on name and labels."""
        if not labels:
            return name

        # Sort labels to ensure consistent key generation
        sorted_labels = sorted(labels.items())
        label_str = "_".join(f"{k}={v}" for k, v in sorted_labels)
        return f"{name}_{label_str}"

    def get_metric(self, name: str, metric_type: type[BaseMetric], labels: dict[str, str] | None = None) -> BaseMetric:
        """Get an existing counter or create and register a new one if it doesn't exist."""
        metric_key = self._get_metric_key(name, labels)
        with self._lock:
            if metric_key in self.registry_map:
                return self.registry_map[metric_key].metric
            metric = metric_type(name, labels)
            self.registry_map[metric_key] = MetricsRegistry(metric)

            return metric

    def _get_input_list(self) -> list[MetricsRegistry]:
        """
        Get a list of all metrics registries.
        This is used to avoid waiting for the lock on the main event loop.
        """
        with self._lock:
            return list(self.registry_map.values())

    async def export_to_gcp(self) -> None:
        """Export all registered counters to Google Cloud Monitoring."""
        metric_inc("general/metric_manager_export_attempts")

        input_list = await asyncio.to_thread(self._get_input_list)

        # First get all series from all registries
        all_series = []
        series_to_registry = {}
        for registry in input_list:
            series_list = registry.get_series()
            for series in series_list:
                all_series.append(series)
                # Store the metric name for each series
                series_to_registry[id(series)] = registry.metric.name

        # Create batches with unique metric names
        seen_metrics: set[str] = set()
        current_batch: list[monitoring_v3.TimeSeries] = []
        batches: list[list[monitoring_v3.TimeSeries]] = []

        for series in all_series:
            metric_name = series_to_registry[id(series)]
            if metric_name not in seen_metrics:
                seen_metrics.add(metric_name)
                current_batch.append(series)

                # When we reach the batch size, create a new batch
                if len(current_batch) >= GCP_EXPORT_BATCH_SIZE:
                    batches.append(current_batch)
                    current_batch = []
                    seen_metrics.clear()

        # Add any remaining series in the last batch
        if current_batch:
            batches.append(current_batch)

        all_success = True
        for i, batch in enumerate(batches):
            try:
                assert self.client is not None
                await self.client.create_time_series(name=self.project_name, time_series=batch)
            except Exception as e:
                # we will lost all ValueMetrics in the past time interval if this fails
                all_success = False
                msg = str(e)
                if "RESOURCE_EXHAUSTED" in msg:
                    logger.debug(f"Rate limit hit on batch {i + 1}, skipping")
                else:
                    logger.warning(
                        f"MetricManager error exporting batch {i + 1} of {len(batches)} "
                        f"with {len(batch)} counters to GCP: {e}"
                    )
        if all_success:
            metric_inc("general/metric_manager_export_success")
        else:
            metric_inc("general/metric_manager_export_partial_success")

    async def _periodic_export(self) -> None:
        """Internal method to export metrics periodically."""
        while not self._stop_event.is_set():
            await self.export_to_gcp()
            await asyncio.sleep(self.export_interval_secs)

    def start(self) -> None:
        """Start the periodic exporting thread."""
        if self._export_task is not None and not self._export_task.done():
            # start only once
            return

        self._stop_event.clear()
        self._export_task = asyncio.create_task(self._periodic_export())
        logger.info(f"MetricManager started, GCP Metrics Writing interval: {self.export_interval_secs} s")

    def stop(self) -> None:
        """Stop the periodic exporting thread."""
        if self._export_task is None or self._export_task.done():
            logger.warning("MetricManager is not running, ignoring stop() call")
            return

        self._stop_event.set()
        self._export_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            asyncio.get_event_loop().run_until_complete(self._export_task)
        self._export_task = None


# initialize this early so it can be used in tests but doesn't depend anything GCP.
METRICS_MANAGER = MetricManager()


def start_metrics_manager() -> None:
    if settings.DISABLE_WRITE_GOOGLE_CLOUD_METRICS:
        return

    # only initialize everything now.
    global GCM_CLIENT
    GCM_CLIENT = monitoring_v3.MetricServiceAsyncClient()

    # only setup anything GCP related now, so we have settings initialized
    METRICS_MANAGER.set_up_gcp(settings.GCP_PROJECT_ID, GCM_CLIENT)
    METRICS_MANAGER.start()


def start_metrics_manager_for_cron_job() -> None:
    """Initialize metrics manager for cron jobs without starting periodic export.
    This sets up the GCP client and project but does not start the background
    periodic export task. Use flush_all_metrics_for_cron_job() at the end of
    the job to export metrics.
    """
    if settings.DISABLE_WRITE_GOOGLE_CLOUD_METRICS:
        return

    # only initialize everything now.
    global GCM_CLIENT
    GCM_CLIENT = monitoring_v3.MetricServiceAsyncClient()

    # only setup anything GCP related now, so we have settings initialized
    METRICS_MANAGER.set_up_gcp(settings.GCP_PROJECT_ID, GCM_CLIENT)


async def flush_all_metrics_for_cron_job() -> None:
    """Flush all pending metrics to Google Cloud Monitoring for cron jobs.
    This function should be called at the end of Cloud Run cron jobs to ensure
    all metrics are exported before the job terminates.
    """
    if settings.DISABLE_WRITE_GOOGLE_CLOUD_METRICS:
        return

    if (
        not hasattr(METRICS_MANAGER, "client")
        or METRICS_MANAGER.client is None
        or not hasattr(METRICS_MANAGER, "project_name")
    ):
        logger.warning("MetricManager not initialized, cannot flush metrics")
        return

    await METRICS_MANAGER.export_to_gcp()


# Precompile the regex pattern for better performance
SANITIZE_PATTERN = re.compile(r"[^a-zA-Z0-9_/]")


def sanitize_name(name: str) -> str:
    """Sanitize a name to be used as a metric name."""
    return SANITIZE_PATTERN.sub("_", name)


def sanitize_labels(labels: dict[str, str]) -> dict[str, str]:
    """Sanitize label values to avoid GCP errors.

    GCP metric label values have restrictions on allowed characters.
    Invalid characters (like <, >, etc. in model names) are replaced with underscores.
    This must be done at ingestion time to ensure registry keys match exported labels.
    """
    return {key: SANITIZE_PATTERN.sub("_", value) for key, value in labels.items()}


# Convenience functions
def metric_inc(name: str) -> None:
    """increment a named counter by 1"""
    if settings.DISABLE_WRITE_GOOGLE_CLOUD_METRICS:
        return

    name = sanitize_name(name)
    m = METRICS_MANAGER.get_metric(name, CounterMetric)
    if isinstance(m, CounterMetric):
        m.inc()
    else:
        raise ValueError(f"Metric {name} is not a CounterMetric")


def metric_inc_by(name: str, amount: int) -> None:
    """increment a named counter by amount"""
    if settings.DISABLE_WRITE_GOOGLE_CLOUD_METRICS:
        return

    name = sanitize_name(name)
    m = METRICS_MANAGER.get_metric(name, CounterMetric)
    if isinstance(m, CounterMetric):
        m.inc_by(amount)
    else:
        raise ValueError(f"Metric {name} is not a CounterMetric")


def metric_record(name: str, value: int) -> None:
    """export a specific value for a counter"""
    if settings.DISABLE_WRITE_GOOGLE_CLOUD_METRICS:
        return

    name = sanitize_name(name)
    m = METRICS_MANAGER.get_metric(name, ValueMetric)
    if isinstance(m, ValueMetric):
        m.record_value(value)
    else:
        raise ValueError(f"Metric {name} is not a ValueMetric")


# Labeled convenience functions
def metric_inc_with_labels(name: str, labels: dict[str, str]) -> None:
    """increment a named counter by 1 with labels"""
    if settings.DISABLE_WRITE_GOOGLE_CLOUD_METRICS:
        return

    name = sanitize_name(name)
    labels = sanitize_labels(labels)
    m = METRICS_MANAGER.get_metric(name, CounterMetric, labels)
    if isinstance(m, CounterMetric):
        m.inc()
    else:
        raise ValueError(f"Metric {name} is not a CounterMetric")


def metric_inc_by_with_labels(name: str, amount: int, labels: dict[str, str]) -> None:
    """increment a named counter by amount with labels"""
    if settings.DISABLE_WRITE_GOOGLE_CLOUD_METRICS:
        return

    name = sanitize_name(name)
    labels = sanitize_labels(labels)
    m = METRICS_MANAGER.get_metric(name, CounterMetric, labels)
    if isinstance(m, CounterMetric):
        m.inc_by(amount)
    else:
        raise ValueError(f"Metric {name} is not a CounterMetric")


def metric_record_with_labels(name: str, value: int, labels: dict[str, str]) -> None:
    """export a specific value for a counter with labels"""
    if settings.DISABLE_WRITE_GOOGLE_CLOUD_METRICS:
        return

    name = sanitize_name(name)
    labels = sanitize_labels(labels)
    m = METRICS_MANAGER.get_metric(name, ValueMetric, labels)
    if isinstance(m, ValueMetric):
        m.record_value(value)
    else:
        raise ValueError(f"Metric {name} is not a ValueMetric")
