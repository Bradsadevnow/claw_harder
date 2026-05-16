from __future__ import annotations

import json
from math import isfinite
from typing import Any


def to_jsonable(value: Any) -> Any:
    if isinstance(value, float):
        if isfinite(value):
            return value
        if value > 0:
            return "Infinity"
        if value < 0:
            return "-Infinity"
        return "NaN"
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    return value


def dumps_json(value: Any, **kwargs: Any) -> str:
    kwargs.setdefault("ensure_ascii", True)
    kwargs.setdefault("allow_nan", False)
    return json.dumps(to_jsonable(value), **kwargs)
