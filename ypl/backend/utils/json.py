import base64
import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID

import numpy as np
import orjson
from pydantic import BaseModel


class CustomJSONEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        # UUID objects
        if isinstance(obj, UUID):
            return str(obj)

        # Date and time objects
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, date):
            return obj.isoformat()
        if isinstance(obj, time):
            return obj.isoformat()
        if isinstance(obj, timedelta):
            return str(obj)

        # Decimal numbers
        if isinstance(obj, Decimal):
            return str(obj)

        # Enum values
        if isinstance(obj, Enum):
            return obj.value

        # Path objects
        if isinstance(obj, Path):
            return str(obj)

        # BaseModel objects, common in our logging
        if isinstance(obj, BaseModel):
            return obj.model_dump(mode="json")

        # Sets
        if isinstance(obj, set):
            return list(obj)

        # Bytes - try UTF-8 first, fall back to base64 for binary data
        if isinstance(obj, bytes):
            try:
                return obj.decode("utf-8")
            except UnicodeDecodeError:
                return f"base64:{base64.b64encode(obj).decode('ascii')}"

        # Numpy types
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.bool_):
            return bool(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()

        # Any objects with a to_json method
        if hasattr(obj, "to_json"):
            return obj.to_json()

        # Any objects with a __dict__ attribute
        if hasattr(obj, "__dict__"):
            return obj.__dict__

        return super().default(obj)


def _convert_dict_keys(obj: Any) -> Any:
    """Convert numpy types in dict keys to strings."""
    if isinstance(obj, dict):
        return {str(k): _convert_dict_keys(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_convert_dict_keys(item) for item in obj]
    return obj


def json_dumps(obj: Any, **kwargs: Any) -> str:
    obj = _convert_dict_keys(obj)
    return json.dumps(obj, cls=CustomJSONEncoder, **kwargs)


def orjson_dumps(obj: Any, **kwargs: Any) -> bytes:
    kwargs.pop("default", None)
    return orjson.dumps(obj, default=CustomJSONEncoder().default, **kwargs)
