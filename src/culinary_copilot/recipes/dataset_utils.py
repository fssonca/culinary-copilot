"""Shared dataset helpers: checksum verification and deterministic sampling."""

import hashlib
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 64), b""):
            digest.update(chunk)
    return digest.hexdigest()


def systematic_sample(total: int, size: int, seed: int) -> list[int]:
    """Deterministic stride sample covering the full file (1-indexed rows)."""
    if size > total:
        raise ValueError("sample larger than population")
    stride = total // size
    picked = sorted({((seed + i * stride) % total) + 1 for i in range(size)})
    # Fill (extremely unlikely collisions) deterministically.
    candidate = seed
    while len(picked) < size:
        row = (candidate % total) + 1
        if row not in picked:
            picked.append(row)
        candidate += 1
    return sorted(picked)
