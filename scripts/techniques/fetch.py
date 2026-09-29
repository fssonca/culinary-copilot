#!/usr/bin/env python3
"""Fetch the 40 owner-approved technique documents (Milestone 3, Phase 4).

Allowlist: ``evals/technique_corpus/approved_sources.json`` is the only
input. Any ``--only`` id or URL not in that file is refused (and a test
enforces it). Nothing else is fetched: no reserves, substitutes, linked
pages, or media.

- Wikimedia (Action API ``action=parse`` only, serial, ``maxlag``,
  gzip): records the ``revid`` as ``revision_id`` (oldid).
- FDA / FSIS: direct HTTPS at a low rate after a ``robots.txt`` check.
- Retries: transient 5xx / timeouts only (3 attempts, backoff). 404,
  blocks, off-topic redirects, or <150 normalized words drop the doc
  with a recorded reason. Blocks are never worked around.

Outputs (``data/technique-corpus/``, git-ignored, verified before write):
``<doc_id>.raw.*`` (fetched bytes), ``<doc_id>.txt`` (normalized text
with ``##`` section headings kept), ``<doc_id>.meta.json`` (provenance).
Plus the committed ``evals/technique_corpus/manifest.json`` (provenance
only, no document text).

``--dry-run`` prints the plan and performs zero network calls and zero
writes. No embedding or model calls anywhere in this script.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html as html_module
import io
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_APPROVED = REPO_ROOT / "evals" / "technique_corpus" / "approved_sources.json"
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "technique-corpus"
DEFAULT_MANIFEST = REPO_ROOT / "evals" / "technique_corpus" / "manifest.json"

USER_AGENT = "culinary-copilot-technique-corpus/0.1 (https://github.com/fssonca/culinary-copilot)"
WIKIMEDIA_PAUSE_S = 1.0
GOV_PAUSE_S = 2.0
MIN_WORDS = 150

#: Normalizer version, recorded per document in meta/manifest. v2 keeps
#: data tables (row-per-line with " | " joins, captions kept, degree
#: markup normalized to "°") and drops th-less layout/navigation
#: tables. Must match TECHNIQUE_RENDER_VERSION's expectation that table
#: rows are part of the chunk text.
NORMALIZER_VERSION = "2"

#: Normalized section headings dropped (case-insensitive exact match).
DROP_SECTIONS = frozenset(
    {
        "references",
        "see also",
        "external links",
        "further reading",
        "notes",
        "footnotes",
        "sources",
        "bibliography",
        "citations",
        "works cited",
        "related pages",
        "navigation",
        "categories",
    }
)

#: HTML class tokens whose subtree is chrome, not content.
SKIP_CLASS_TOKENS = frozenset(
    {
        "references",
        "reference-list",
        "reflist",
        "navbox",
        "infobox",
        "toc",
        "toctitle",
        "metadata",
        "catlinks",
        "printfooter",
        "noprint",
        "mw-editsection",
        "sistersitebox",
        "ambox",
        "hatnote",
        "sup",
        "breadcrumb",
        "usa-banner",
    }
)

# Note: "header" is deliberately NOT skipped: FDA wraps the article
# title/body in <header class="row content-header">. Site chrome still
# drops via nav/aside/footer skips below.
SKIP_ELEMENTS = frozenset({"script", "style", "noscript", "nav", "footer", "aside"})

#: Void elements never open a subtree: they must not touch the skip
#: counter (an <img> inside a skipped banner otherwise inflates it
#: forever and swallows the rest of the page).
VOID_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


class TransientFetchError(Exception):
    """Retryable network/5xx failure."""


class PermanentFetchError(Exception):
    """Non-retryable failure (carries a drop reason)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _SectionExtractor(HTMLParser):
    """Extract (heading, paragraphs) keeping h1-h4 as section markers.

    Table rule (documented, deterministic):
    - keep DATA tables: a <table> is kept iff its own markup contains
      at least one <th> (header cell). Kept tables render one row per
      line with header/cells joined by " | "; the <caption> is kept as
      a paragraph and the preceding heading stays the section.
    - drop LAYOUT/NAVIGATION tables: th-less tables are discarded
      wholesale (observed in-corpus: Wikibooks shelf navbars like
      "Cookbook | Recipes | ..."), as are tables inside skipped chrome
      subtrees (nav, aside, footer, infobox, navbox, metadata boxes).
    Header presence is the discriminator because navigation/layout
    tables on these sources never carry <th>. Verified on the approved
    set: every " | " row previously kept from a th-less table was
    navbar chrome, and every data row (including the FDA temperature
    table) traces to a th table — so the rule removes chrome and keeps
    data, with no data loss observed.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        # Implicit lead section: content before the first heading belongs
        # to the "" section (many Wikibooks modules have no body headings).
        self.sections: list[tuple[str, list[str]]] = [("", [])]
        self._heading = ""
        self._blocks: list[str] = self.sections[0][1]
        self._buf: list[str] = []
        self._capture: str | None = None  # "heading" | "block" | "cell" | None
        self._heading_level = 0
        self._row: list[str] = []
        self._skip_depth = 0
        self._dropped_section = False
        self._table_stack: list[dict[str, object]] = []
        self._sup_depth = 0
        self._sup_buf: list[str] = []
        self.page_title = ""

    # -- helpers ---------------------------------------------------------
    def _in_skip(self) -> bool:
        return self._skip_depth > 0

    def _flush_buf(self) -> str:
        text = " ".join("".join(self._buf).split())
        self._buf = []
        return text

    def _close_capture(self) -> None:
        kind, self._capture = (self._capture, None)
        if kind == "heading":
            heading = self._flush_buf()
            self.sections.append((heading, []))
            self._dropped_section = heading.strip().lower() in DROP_SECTIONS
            if self._dropped_section:
                self.sections.pop()
                self._blocks = self.sections[-1][1] if self.sections else []
            else:
                self._blocks = self.sections[-1][1]
        elif kind == "block":
            text = self._flush_buf()
            if text and not self._dropped_section:
                self._blocks.append(text)
        elif kind == "cell":
            self._row.append(self._flush_buf())

    # -- parser hooks ----------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in VOID_ELEMENTS:
            if tag == "br" and self._capture in ("block", "cell") and not self._in_skip():
                self._buf.append(" ")
            return
        if self._in_skip():
            self._skip_depth += 1
            return
        attrs_dict = dict(attrs)
        classes = str(attrs_dict.get("class") or "").lower()
        ident = str(attrs_dict.get("id") or "").lower()
        if tag in SKIP_ELEMENTS:
            self._skip_depth = 1
            return
        if any(tok in classes or tok in ident for tok in SKIP_CLASS_TOKENS):
            self._skip_depth = 1
            return
        if tag == "sup":
            # Degree markup (<sup>o</sup>, <sup>°</sup>) normalizes to
            # "°" at close; all other sup content passes through as-is.
            self._sup_depth += 1
            self._sup_buf.append("")
            return
        if tag == "table":
            self._table_stack.append({"has_th": False, "parent": self._blocks, "rows": []})
            self._blocks = self._table_stack[-1]["rows"]  # type: ignore[assignment]
            return
        if tag == "th" and self._table_stack:
            self._table_stack[-1]["has_th"] = True
        if tag in ("h1", "h2", "h3", "h4"):
            if self._capture in ("block", "cell"):
                self._close_capture()
            elif self._capture == "heading":
                self._close_capture()
            self._capture = "heading"
            self._heading_level = int(tag[1])
            self._buf = []
        elif tag in ("p", "li", "dd", "dt", "figcaption", "caption"):
            if self._capture == "heading":
                self._close_capture()
            if self._capture != "cell":
                self._capture = "block"
                self._buf = []
        elif tag == "tr":
            if self._capture == "heading":
                self._close_capture()
            self._row = []
        elif tag in ("td", "th"):
            if self._capture == "block":
                self._close_capture()
            self._capture = "cell"
            self._buf = []

    def handle_endtag(self, tag: str) -> None:
        if self._in_skip():
            self._skip_depth -= 1
            return
        if tag == "sup" and self._sup_depth:
            self._sup_depth -= 1
            sup_text = self._sup_buf.pop()
            if self._capture in ("heading", "block", "cell"):
                if sup_text.strip() in ("o", "O", "°"):
                    if self._buf and not "".join(self._buf).endswith((" ", "\n")):
                        self._buf.append(" ")
                    self._buf.append("°")
                else:
                    self._buf.append(sup_text)
            return
        if tag == "table" and self._table_stack:
            frame = self._table_stack.pop()
            parent = frame["parent"]
            assert isinstance(parent, list)
            if frame["has_th"] and not self._dropped_section:
                parent.extend(frame["rows"])  # type: ignore[arg-type]
            self._blocks = parent
            return
        if tag in ("h1", "h2", "h3", "h4"):
            if self._capture == "heading":
                self._close_capture()
        elif tag in ("p", "li", "dd", "dt", "figcaption", "caption"):
            if self._capture == "block":
                self._close_capture()
        elif tag in ("td", "th"):
            if self._capture == "cell":
                self._close_capture()
        elif tag == "tr":
            if self._row and not self._dropped_section:
                self._blocks.append(" | ".join(c for c in self._row if c))
            self._row = []
        elif tag == "table":
            if not self._dropped_section:
                self._blocks.append("")

    def handle_data(self, data: str) -> None:
        if self._in_skip():
            return
        if self._sup_depth:
            self._sup_buf[-1] += data
            return
        if self._capture in ("heading", "block", "cell"):
            self._buf.append(data)

    def handle_comment(self, data: str) -> None:
        _ = data


def html_to_text(html: str) -> tuple[str, str]:
    """Normalize page HTML to ``(title, text)`` with ``##`` headings."""
    import re as _re

    parser = _SectionExtractor()
    parser.feed(html)
    parser.close()
    title = ""
    chunks: list[str] = []
    for heading, blocks in parser.sections:
        clean_heading = " ".join(heading.split())
        if not title and clean_heading:
            title = clean_heading
        if clean_heading:
            chunks.append(f"## {clean_heading}")
        chunks.extend(blocks)
    text = "\n\n".join(c for c in chunks if c).strip()
    # Degree marks glued to a number ("160°F", "&deg;" forms) read and
    # retrieve as "160 °F" (the <sup>o</sup> path already spaces itself).
    text = _re.sub(r"(?<=\d)°", " °", text)
    return title, html_module.unescape(text)


def _http_get(url: str, *, timeout_s: float = 30.0) -> tuple[bytes, dict[str, str], str]:
    """GET with the corpus User-Agent; returns (body, headers, final_url)."""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            body = response.read()
            headers = {k.lower(): v for k, v in response.headers.items()}
            final_url = response.geturl()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise PermanentFetchError("http-404-missing") from exc
        if exc.code in (403, 429):
            raise PermanentFetchError(f"http-{exc.code}-blocked") from exc
        if 500 <= exc.code <= 599:
            raise TransientFetchError(f"http-{exc.code}") from exc
        raise PermanentFetchError(f"http-{exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise TransientFetchError(f"network: {exc}") from exc
    if headers.get("content-encoding") == "gzip" and body[:2] == b"\x1f\x8b":
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as gz:
            body = gz.read()
    return body, headers, final_url


def _get_with_retries(url: str) -> tuple[bytes, dict[str, str], str]:
    last: Exception | None = None
    for attempt in range(3):
        try:
            return _http_get(url)
        except TransientFetchError as exc:
            last = exc
            time.sleep(2.0 * (attempt + 1))
    raise last or TransientFetchError("unknown")


def _load_approved(path: Path) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = {str(e["id"]): dict(e) for e in payload["sources"]}
    return entries, {k: str(v) for k, v in payload.get("attribution_templates", {}).items()}


def _check_robots(host: str, path: str) -> None:
    """Refuse paths disallowed by the host robots.txt (crude prefix match)."""
    body, _, _ = _get_with_retries(f"https://{host}/robots.txt")
    disallows: list[str] = []
    applies = False
    for line in body.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "user-agent":
            applies = value in ("*", USER_AGENT)
        elif key == "disallow" and applies and value:
            disallows.append(value)
    for rule in disallows:
        if path.startswith(rule):
            raise PermanentFetchError("robots-denied")


def _same_path(a: str, b: str) -> bool:
    pa = urllib.parse.urlparse(a)
    pb = urllib.parse.urlparse(b)
    return pa.netloc.lower() == pb.netloc.lower() and pa.path.rstrip("/") == pb.path.rstrip("/")


def _base_title(title: str) -> str:
    """Lowercased title with parenthetical disambiguators stripped."""
    import re as _re

    base = _re.sub(r"\s*\([^)]*\)\s*", " ", title.replace("_", " ")).strip().lower()
    return _re.sub(r"\s+", " ", base)


def _same_topic(want: str, got: str) -> bool:
    """Accept same-topic redirect variants (plural, disambiguator).

    ``Egg substitute`` -> ``Egg substitutes`` and ``Garnish (food)`` ->
    ``Garnish (cooking)`` are the same topic; a genuinely different
    article title still drops.
    """
    want_base, got_base = _base_title(want), _base_title(got)
    if want_base == got_base:
        return True
    # Plural-only difference, word by word.
    want_words, got_words = want_base.split(), got_base.split()
    if len(want_words) == len(got_words) and want_words:
        pairs = [(w, g) for w, g in zip(want_words, got_words) if w != g]
        if pairs and all(w + "s" == g or g + "s" == w for w, g in pairs):
            return True
    return False


def _fetch_wikimedia(entry: dict[str, str]) -> tuple[bytes, str, dict[str, str], str]:
    """Fetch via Action API parse; returns (raw, html, meta_headers, html)."""
    parts = urllib.parse.urlparse(entry["url"])
    title = urllib.parse.unquote(parts.path.rpartition("/")[2])
    api = (
        f"https://{parts.netloc}/w/api.php?action=parse&page="
        f"{urllib.parse.quote(title)}&prop=text%7Crevid%7Ctitle"
        f"&format=json&formatversion=2&maxlag=5&redirects=1"
    )
    raw, _, _ = _get_with_retries(api)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise PermanentFetchError("api-bad-json") from exc
    if "error" in payload:
        code = str(payload["error"].get("code", "unknown"))
        if code in ("missingtitle", "invalidtitle"):
            raise PermanentFetchError("missingtitle")
        raise PermanentFetchError(f"api-{code}")
    parse = payload.get("parse") or {}
    html = str(parse.get("text") or "")
    revid = str(parse.get("revid") or "")
    returned_title = str(parse.get("title") or "")
    if not html or not revid:
        raise PermanentFetchError("api-empty")
    want = title.replace("_", " ").strip()
    got = returned_title.replace("_", " ").strip()
    if not _same_topic(want, got):
        raise PermanentFetchError(f"redirect-different-topic: {returned_title!r}")
    return raw, html, {"final_title": returned_title}, revid


def _fetch_gov(entry: dict[str, str]) -> tuple[bytes, str, dict[str, str], str]:
    parts = urllib.parse.urlparse(entry["url"])
    _check_robots(parts.netloc, parts.path)
    raw, headers, final_url = _get_with_retries(entry["url"])
    if not _same_path(final_url, entry["url"]):
        raise PermanentFetchError(f"redirect-different-topic: {final_url}")
    html = raw.decode("utf-8", "replace")
    revision = str(headers.get("etag") or headers.get("last-modified") or "none")
    return raw, html, headers, revision


def _ensure_gitignored(path: Path) -> None:
    proc = subprocess.run(
        ["git", "check-ignore", "-q", str(path)],
        cwd=str(REPO_ROOT),
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"refusing to write outside git-ignored storage: {path}")


def _resolve_attribution(
    entry: dict[str, str], templates: dict[str, str]
) -> Callable[[str, str], str]:
    template = templates[entry["attribution_template"]]

    def _render(revision: str, date: str) -> str:
        return template.format(title=entry["title"], url=entry["url"], revision=revision, date=date)

    return _render


def fetch_one(
    doc_id: str,
    entries: dict[str, dict[str, str]],
    templates: dict[str, str],
    *,
    out_dir: Path,
    pause_s: float = 0.0,
) -> dict[str, object]:
    """Fetch + normalize one allowlisted doc; returns its manifest record."""
    if doc_id not in entries:
        raise ValueError(f"refusing id not in approved sources: {doc_id!r}")
    entry = entries[doc_id]
    retrieved = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    record: dict[str, object] = {
        "doc_id": doc_id,
        "title": entry["title"],
        "topic": entry.get("topic", ""),
        "url": entry["url"],
        "publisher": entry["publisher"],
        "licence": entry["licence"],
        "licence_url": entry["licence_url"],
    }
    try:
        host = urllib.parse.urlparse(entry["url"]).netloc
        if host.endswith("wikipedia.org") or host.endswith("wikibooks.org"):
            raw, html, extra, revision = _fetch_wikimedia(entry)
        elif host.endswith(".gov") or host.endswith(".gov/"):
            raw, html, _, revision = _fetch_gov(entry)
            extra = {}
        else:  # pragma: no cover - allowlist is .gov + wikimedia only
            raise PermanentFetchError("unsupported-host")
        _, text = html_to_text(html)
        words = len(text.split())
        if words < MIN_WORDS:
            raise PermanentFetchError(f"too-short: {words} words")
        normalized = f"# {entry['title']}\n\n{text}\n"
        raw_bytes = bytes(raw)
        norm_bytes = normalized.encode("utf-8")
        for path, data in (
            (out_dir / f"{doc_id}.raw.{'json' if host.endswith('.org') else 'html'}", raw_bytes),
            (out_dir / f"{doc_id}.txt", norm_bytes),
        ):
            _ensure_gitignored(path)
            path.write_bytes(data)
        meta = {
            **record,
            "final_title": str(extra.get("final_title") or entry["title"]),
            "normalizer_version": NORMALIZER_VERSION,
            "attribution_text": _resolve_attribution(entry, templates)(revision, retrieved),
            "retrieval_date_utc": retrieved,
            "revision_id": revision,
            "sha256_raw": hashlib.sha256(raw_bytes).hexdigest(),
            "sha256_normalized": hashlib.sha256(norm_bytes).hexdigest(),
            "words": words,
            "bytes": len(norm_bytes),
            "status": "ingested",
        }
        (out_dir / f"{doc_id}.meta.json").write_text(
            json.dumps(meta, indent=2) + "\n", encoding="utf-8"
        )
        if pause_s:
            time.sleep(pause_s)
        return meta
    except PermanentFetchError as exc:
        return {**record, "status": "dropped", "reason": exc.reason}
    except TransientFetchError as exc:
        return {**record, "status": "dropped", "reason": f"transient-failed: {exc}"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approved", default=str(DEFAULT_APPROVED))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    entries, templates = _load_approved(Path(args.approved))
    wanted = list(args.only) if args.only else sorted(entries)
    for doc_id in wanted:
        if doc_id not in entries:
            print(f"error: refusing id not in approved sources: {doc_id!r}", file=sys.stderr)
            return 2
    if args.dry_run:
        print(f"approved docs: {len(wanted)}; zero network calls, zero writes.")
        for doc_id in wanted:
            print(f"  {doc_id}  {entries[doc_id]['url']}")
        return 0

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    docs: dict[str, dict[str, object]] = {}
    for doc_id in wanted:
        host = urllib.parse.urlparse(entries[doc_id]["url"]).netloc
        pause = WIKIMEDIA_PAUSE_S if host.endswith(".org") else GOV_PAUSE_S
        record = fetch_one(doc_id, entries, templates, out_dir=out_dir, pause_s=pause)
        docs[doc_id] = record
        print(f"{doc_id}: {record.get('status')} ({record.get('reason', record.get('words'))})")
    manifest = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "approved_sources_sha256": hashlib.sha256(Path(args.approved).read_bytes()).hexdigest(),
        "count": len(docs),
        "docs": docs,
    }
    Path(args.manifest).parent.mkdir(parents=True, exist_ok=True)
    Path(args.manifest).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    ingested = sum(1 for d in docs.values() if d.get("status") == "ingested")
    print(f"ingested {ingested}/{len(docs)}; manifest: {args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
