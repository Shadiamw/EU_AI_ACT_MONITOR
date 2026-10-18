"""JSON responses that write Decimal values as exact JSON numbers (spec 1.6).

The standard encoder has no exact form for a Decimal; going through float() rounds long values
(0.1234567890123456789 becomes 0.12345678901234568). Here a Decimal is written as its plain
decimal text, so a stored NUMERIC reaches the client digit for digit (decision 124).
"""

import json
from decimal import Decimal
from typing import Any

from fastapi.responses import Response


def dumps(value: Any) -> str:
    """Compact JSON, with each Decimal written as a plain decimal number."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("a JSON number must be finite")
        return format(value, "f")
    if isinstance(value, dict):
        items = (f"{json.dumps(str(k), ensure_ascii=False)}:{dumps(v)}" for k, v in value.items())
        return "{" + ",".join(items) + "}"
    if isinstance(value, list | tuple):
        return "[" + ",".join(dumps(item) for item in value) + "]"
    if isinstance(value, float):
        raise TypeError("floats are not written; use Decimal")
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class ExactJSONResponse(Response):
    media_type = "application/json"

    def render(self, content: Any) -> bytes:
        return dumps(content).encode("utf-8")
