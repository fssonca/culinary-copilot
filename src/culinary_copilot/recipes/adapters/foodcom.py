"""Food.com adapter: thin wrapper preserving existing behavior."""

from typing import Any

from culinary_copilot.recipes import normalize as foodcom_normalize


def normalize_foodcom(raw: dict[str, str], vocabulary: set[str]) -> dict[str, Any]:
    """Delegate to the authoritative Food.com normalizer (VERSION-tracked)."""
    return foodcom_normalize.normalize(raw, vocabulary)


ADAPTER = "foodcom"
ADAPTER_VERSION = foodcom_normalize.VERSION
