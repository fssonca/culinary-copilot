"""Publisher-signal source labels (Phase 5, part 2, owner decision 7).

Domains are a publisher signal, not an authority verdict. Labels:

- official_guidance: fda.gov, fsis.usda.gov, foodsafety.gov, cdc.gov,
  and who.int food-safety pages (config WEB_OFFICIAL_GUIDANCE_DOMAINS;
  who.int only counts on food-safety paths, same rule as usda.gov);
- usda.gov: official_guidance ONLY on food-safety paths, otherwise
  unclassified (documented rule below);
- research_publication: pubmed.ncbi.nlm.nih.gov and
  pmc.ncbi.nlm.nih.gov (config WEB_RESEARCH_DOMAINS; never official
  guidance — cite the NLM disclaimer that NLM does not endorse content);
- culinary_source: owner-supplied list (config WEB_CULINARY_DOMAINS;
  empty by default);
- unclassified: everything else, rendered neutrally, never as "anecdote".

Hostname matching is exact equality or dot-delimited subdomain
(host == entry or host.endswith("." + entry)); never a raw suffix
("evilfda.gov" must NOT match "fda.gov"). Never label all of nih.gov.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

#: usda.gov / who.int count as official_guidance only on these paths.
USDA_FOOD_SAFETY_PATH_RES = (
    re.compile(r"food[ -]?safety", re.IGNORECASE),
    re.compile(r"foodsafety", re.IGNORECASE),
    re.compile(r"foodborne", re.IGNORECASE),
    re.compile(r"fsis", re.IGNORECASE),
)

_VALID_LABELS = frozenset(
    {"official_guidance", "research_publication", "culinary_source", "unclassified"}
)


def parse_domain_list(raw: str | None) -> list[str]:
    """Lowercased domain entries from a comma-separated setting."""
    out: list[str] = []
    for part in str(raw or "").split(","):
        entry = part.strip().lower().rstrip(".")
        if entry:
            out.append(entry)
    return out


def _host_of(url: str) -> str:
    try:
        return (urlsplit(str(url or "")).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _matches(host: str, entry: str) -> bool:
    entry = entry.lower().rstrip(".")
    if not host or not entry:
        return False
    return host == entry or host.endswith("." + entry)


def _on_food_safety_path(url: str) -> bool:
    try:
        path = urlsplit(str(url or "")).path or ""
    except ValueError:
        return False
    hay = f"{path}"
    return any(rx.search(hay) is not None for rx in USDA_FOOD_SAFETY_PATH_RES)


def classify_source(
    url: str,
    *,
    official: list[str],
    research: list[str],
    culinary: list[str],
) -> str:
    """Deterministic publisher-signal label for one source URL.

    Order: research, then culinary, then exact-or-subdomain matches
    against the official list (so ``fsis.usda.gov`` listed there wins
    without a path check), then the ``usda.gov`` / ``who.int``
    food-safety path gates. Keep bare ``usda.gov`` out of the
    official list or it would bypass the path gate.
    """
    host = _host_of(url)
    if not host:
        return "unclassified"
    for entry in research:
        if _matches(host, entry):
            # NLM hosts are research_publication, never official guidance.
            return "research_publication"
    for entry in culinary:
        if _matches(host, entry):
            return "culinary_source"
    for entry in official:
        norm = entry.lower().rstrip(".")
        if norm in ("who.int", "www.who.int"):
            if host == "who.int" or host.endswith(".who.int"):
                return "official_guidance" if _on_food_safety_path(url) else "unclassified"
            continue
        if _matches(host, norm):
            return "official_guidance"
    if host == "usda.gov" or host.endswith(".usda.gov"):
        return "official_guidance" if _on_food_safety_path(url) else "unclassified"
    return "unclassified"


__all__ = [
    "USDA_FOOD_SAFETY_PATH_RES",
    "classify_source",
    "parse_domain_list",
]
