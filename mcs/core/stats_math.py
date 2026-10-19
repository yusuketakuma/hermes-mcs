"""Small statistics helpers shared by the runtime views and offline evaluation."""
import math


def finite_number(value) -> bool:
    """A real int/float that is finite (bool, str, NaN, ±inf and huge ints are not)."""
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def wilson_interval(successes: int, denominator: int,
                    z: float = 1.96) -> dict[str, float | None]:
    """Return a bounded 95% Wilson interval without a statistics package."""
    if denominator <= 0:
        return {"low": None, "high": None}
    p = successes / denominator
    z2 = z * z
    center = (p + z2 / (2 * denominator)) / (1 + z2 / denominator)
    half = z / (1 + z2 / denominator) * math.sqrt(
        p * (1 - p) / denominator + z2 / (4 * denominator ** 2))
    return {"low": max(0.0, center - half),
            "high": min(1.0, center + half)}


def percentile(values: list[float], p: float) -> float | None:
    """Interpolate a percentile over numeric observations validated by the caller."""
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * p
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return values[low]
    return values[low] + (values[high] - values[low]) * (position - low)
