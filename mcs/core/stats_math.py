"""Small statistics helpers shared by the runtime views and offline evaluation."""
import math


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
