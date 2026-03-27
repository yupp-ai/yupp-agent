import json
import logging
from pathlib import Path
from typing import Any, TypedDict

import yaml
from cachetools.func import ttl_cache
from tenacity import RetryCallState, retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from ypl.db.redis import get_redis_client
from ypl.structured_logger import get_logger
from ypl.utils import async_timed_cache

REDIS_FEATURE_FLAG_PREFIX = "feature_flag:"  # for redis only

FLAG_NAME_USE_BUDGET_DECAY = "use_budget_decay"
FLAG_NAME_ENABLE_WINDDOWN = "enable_winddown"


def get_feature_flags_path() -> Path:
    """This is just to make testing easier"""
    return Path("data/feature_flags.yml")


"""
Feature flags are stored in two parts
- defaults values in a local .yml file
- current values in redis, always serialized as json and automatically parsed

See test_feature_flags_data.yml for examples of different types.

When you try to get the feature flag, we will first check Redis, if it's not found there, fallback to the default values
in the .yml file. If still not found, return None. When setting the feature flag, it's always set in Redis.

Use following getters and setters

- get_feature_value()    # returns a value of Any type
- is_feature_enabled()   # returns a value of boolean type, a convenient getter for boolean flags

- set_feature_value()    # sets a value of Any type
"""


class FeatureFlag(TypedDict):
    name: str
    description: str
    default_state: str
    critical: bool


def _maybe_add_prefix(name: str) -> str:
    if not name.startswith(REDIS_FEATURE_FLAG_PREFIX):
        return REDIS_FEATURE_FLAG_PREFIX + name
    return name


def _log_retry_attempt(retry_state: RetryCallState) -> None:
    get_logger().warning(
        "Retrying getting feature flag from Redis",
        attempt=retry_state.attempt_number,
        error=str(retry_state.outcome.exception()) if retry_state.outcome else None,
        exc_info=True,
    )


@retry(
    stop=stop_after_attempt(3),
    wait=wait_fixed(0.1),
    after=_log_retry_attempt,
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
async def get_feature_value_from_redis_no_cache(name: str) -> Any | None:
    """Get a feature value as is, if it doesn't exist or there's error, return None"""
    name = _maybe_add_prefix(name)
    redis_client = await get_redis_client()
    value_str = await redis_client.get(name)
    if value_str is None:
        return None
    return json.loads(value_str)


@async_timed_cache(seconds=30)
async def get_feature_value(name: str, default: Any | None = None) -> Any | None:
    """
    Get the string value of a feature flag, if it's not set in redis, fall back to the default value in the YAML file.
    If not found either, return None.
    """
    try:
        value = await get_feature_value_from_redis_no_cache(name)
    except Exception as e:
        logging.error(f"Error getting feature flag {name}", exc_info=e)
        value = None
    if value is None:
        # fallback to default values
        flag_values: dict[str, Any] = get_default_feature_flags()
        return flag_values.get(name, default)
    return value


def strtobool(s: str) -> bool:
    s = s.strip().lower()
    if s in {"y", "yes", "yupp", "t", "true", "on", "1"}:
        return True
    if s in {"n", "no", "nopef", "false", "off", "0"}:
        return False
    raise ValueError(f"invalid truth value '{s}'")


async def is_feature_enabled(name: str) -> bool:
    """
    Get the boolean state of a feature flag, if can be parsed so. If the stored value is not a boolean, throw ValueError
    """
    value = await get_feature_value(name)
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    # this is only for some backward compatibility
    try:
        return bool(strtobool(value))
    except ValueError:
        logging.error(
            f"The feature flag [{name}] does not have a valid bool value in Redis (value: {value}), assuming False."
        )
        return False


async def set_feature_value(name: str, value: Any) -> None:
    """
    Set a string value for a feature flag in Redis, you need to serialize the value yourself
    """
    serialized_value = json.dumps(value)
    await set_serialized_feature_value(name, serialized_value)


async def set_serialized_feature_value(name: str, serialized_value: str) -> None:
    """
    Set a string value for a feature flag in Redis, you need to serialize the value yourself
    """
    name = _maybe_add_prefix(name)
    try:
        redis_client = await get_redis_client()
        await redis_client.set(name, serialized_value)  # always store in json format in redis
    except Exception as e:
        logging.error(f"Error setting feature flag {name}", exc_info=e)


def get_feature_flags_from_yml() -> list[FeatureFlag]:
    """Read feature flags from the YAML file."""

    path = get_feature_flags_path()
    try:
        if not path.exists():
            logging.warning(f"Feature flags file not found at {path}")
            return []

        with open(path) as f:
            try:
                data = yaml.safe_load(f)
            except yaml.YAMLError as e:
                logging.error("Error parsing feature flags YAML", exc_info=e)
                return []

            if not isinstance(data, dict) or "feature_flags" not in data:
                logging.error("Invalid feature flags file format: missing 'feature_flags' key")
                return []

            flags = data["feature_flags"]
            if not isinstance(flags, list):
                logging.error("Invalid feature flags file format: 'feature_flags' must be a list")
                return []

            # Validate each flag has required fields
            valid_flags = []
            for flag in flags:
                if not all(key in flag for key in ["name", "description", "default_state"]):
                    logging.warning(f"Skipping invalid feature flag: missing required fields. Flag data: {flag}")
                    continue
                # Set default critical to False if not specified
                if "critical" not in flag:
                    flag["critical"] = False
                valid_flags.append(flag)

            return valid_flags

    except Exception as e:
        logging.error("Unexpected error reading feature flags", exc_info=e)
        return []


@ttl_cache(ttl=3600 * 3)  # it's good forever actually since it's a local file
def get_default_feature_flags() -> dict[str, Any]:
    """Get the default feature flags from the YAML file."""
    feature_flags = get_feature_flags_from_yml()
    return {flag["name"]: flag["default_state"] for flag in feature_flags}


def is_feature_flag_critical(flag_name: str) -> bool:
    """Check if a feature flag is marked as critical."""
    feature_flags = get_feature_flags_from_yml()
    for flag in feature_flags:
        if flag["name"] == flag_name:
            return flag.get("critical", False)
    return False
