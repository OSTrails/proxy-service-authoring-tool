"""
Reachability checks for the assessment-component URLs in the rendered
FAIRsharing JSON.

Scope: by default only the FAC links - `metric_url`, `benchmark_url`,
`test_url` - wherever they appear in the payload. Other URL-bearing sections
can be switched on with `extra_sections=...`.

Anything that is not a 2xx/3xx counts as a failure - 401, 403, 404, 429, 5xx
all produce a warning. Warnings are returned, not raised, so a submission is
never silently blocked by a flaky host; pass `raise_on_warnings=True` (or use
`ensure_fac_urls_resolvable`) if you want a hard 400 instead.

The payload may be given as a dict or as the raw JSON string that the Jinja
template emits (leading whitespace and all) - both are accepted.

Usage:

    from services.url_validation import check_fac_urls
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Iterable, NamedTuple, Sequence
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException

# ─────────────────────────────────────────────────────────────
# Where the URLs live
# ─────────────────────────────────────────────────────────────

FAC_SECTION = "valuesFACMap"
FAC_URL_FIELDS: tuple[str, ...] = ("metric_url", "benchmark_url", "test_url")

# Optional wider scope: section name -> URL-valued fields, for
# {uuid: {field: value}} shaped sections of the rendered payload.
OPTIONAL_DICT_SECTIONS: dict[str, tuple[str, ...]] = {
    "valuesToolMap": ("url",),
    "valuesOrganisationMap": ("url", "homepage"),
    "valuesKeywordMap": ("iri",),
    "valuesThemeMap": ("iri",),
    "valuesThemeManualMap": ("iri",),
    "valuesPositiveMap": ("iri",),
    "valuesNegativeMap": ("iri",),
}

TOP_LEVEL_URL_KEYS: tuple[str, ...] = (
    "Homepage",
    "EndpointURL",
    "CodeRepository",
    "LandingPage",
    "Specification",
)

EMPTY_SENTINELS: frozenset[str] = frozenset({"", "none", "null", "n/a", "-"})

# Human-readable hints attached to each warning, so whoever reads the response
# knows whether to fix the record or just retry.
STATUS_NOTES: dict[int, str] = {
    401: "requires authentication, so the link is not openly resolvable",
    403: "forbidden, host refused the request (may be bot filtering)",
    404: "not found, check the identifier or whether the file was published",
    405: "method not allowed, even for GET",
    410: "gone, the resource was removed",
    429: "rate limited, may be a false alarm - retry before editing the record",
    500: "server error on the target host",
    502: "bad gateway on the target host",
    503: "service unavailable, host may be temporarily down",
    504: "gateway timeout on the target host",
}

DEFAULT_USER_AGENT = "OSTrails-proxy-service/1.2.1 (+link-check)"

RETRYABLE_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})

EMPTY_PAYLOAD_NOTE = (
    "No metric_url / benchmark_url / test_url fields were found. If this record "
    "does have FAC entries, check that the FAIR Wizard JSON (input_json) was passed in. "
)


class UrlRef(NamedTuple):
    """A URL found in the payload, with a dotted path describing where."""

    path: str
    url: str


class UrlCheck(NamedTuple):
    path: str
    url: str
    ok: bool
    status: int | None
    reason: str
    final_url: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "field": self.path,
            "url": self.url,
            "status": self.status,
            "reason": self.reason,
        }
        if self.final_url and self.final_url != self.url:
            out["redirected_to"] = self.final_url
        return out


# ─────────────────────────────────────────────────────────────
# Payload handling
# ─────────────────────────────────────────────────────────────


def coerce_payload(payload: Any) -> dict[str, Any]:
    """Accept a dict, or the JSON string the Jinja template renders."""
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, (str, bytes, bytearray)):
        text = payload.decode() if isinstance(payload, (bytes, bytearray)) else payload
        try:
            parsed = json.loads(text.strip())
        except json.JSONDecodeError as exc:
            raise HTTPException(400, f"Payload is not valid JSON: {exc}")
        if not isinstance(parsed, dict):
            raise HTTPException(400, "Payload JSON is not an object.")
        return parsed
    raise HTTPException(400, f"Unsupported payload type: {type(payload).__name__}")


def _is_blank(value: Any) -> bool:
    return not isinstance(value, str) or value.strip().lower() in EMPTY_SENTINELS


def _walk(node: Any, path: str, field_names: frozenset[str], out: list[UrlRef]) -> None:
    """Depth-first scan collecting values of the named fields, wherever they sit."""
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            if key in field_names:
                if not _is_blank(value):
                    out.append(UrlRef(child, value.strip()))
            else:
                _walk(value, child, field_names, out)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _walk(value, f"{path}[{index}]", field_names, out)


def _find_section(node: Any, name: str) -> tuple[str, dict[str, Any]] | None:
    """Locate a values*Map section at any depth (breadth-first)."""
    queue: list[tuple[str, Any]] = [("", node)]
    while queue:
        path, current = queue.pop(0)
        if isinstance(current, dict):
            for key, value in current.items():
                child = f"{path}.{key}" if path else str(key)
                if key == name and isinstance(value, dict):
                    return child, value
                queue.append((child, value))
        elif isinstance(current, list):
            for index, value in enumerate(current):
                queue.append((f"{path}[{index}]", value))
    return None


def collect_fac_urls(
    payload: Any,
    *,
    extra_sections: Sequence[str] = (),
    include_top_level: bool = False,
) -> list[UrlRef]:
    """
    Return every FAC URL in the payload, in document order.

    The scan is structural rather than path-based: `metric_url`,
    `benchmark_url` and `test_url` are picked up wherever they occur, so a
    payload nested under an extra wrapper key still works. Duplicated URLs are
    kept - each field gets its own entry so repeated links stay visible.
    """
    data = coerce_payload(payload)
    found: list[UrlRef] = []

    _walk(data, "", frozenset(FAC_URL_FIELDS), found)

    for name in extra_sections:
        fields = OPTIONAL_DICT_SECTIONS.get(name)
        if not fields:
            continue
        located = _find_section(data, name)
        if not located:
            continue
        section_path, entries = located
        for entry_key, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            for field in fields:
                value = entry.get(field)
                if not _is_blank(value):
                    found.append(
                        UrlRef(f"{section_path}.{entry_key}.{field}", value.strip())
                    )

    if include_top_level:
        for key in TOP_LEVEL_URL_KEYS:
            value = data.get(key)
            if not _is_blank(value):
                found.append(UrlRef(key, value.strip()))

    return found


# ─────────────────────────────────────────────────────────────
# Probing
# ─────────────────────────────────────────────────────────────


def _syntax_error(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "not an absolute http(s) URL"
    if not parsed.netloc:
        return "missing host"
    if any(c.isspace() for c in url):
        return "contains whitespace"
    return None


async def _probe(
    client: httpx.AsyncClient, url: str
) -> tuple[int | None, str | None, str | None]:
    """Return (status, final_url, transport_error). HEAD first, GET as fallback."""
    try:
        response = await client.head(url)
        status, final_url = response.status_code, str(response.url)
        # Many servers mishandle HEAD; confirm with a real GET before judging.
        if status >= 400:
            async with client.stream("GET", url) as streamed:
                status, final_url = streamed.status_code, str(streamed.url)
        return status, final_url, None
    except httpx.TooManyRedirects:
        return None, None, "redirect loop"
    except httpx.TimeoutException:
        return None, None, "timed out"
    except httpx.HTTPError as exc:
        return None, None, f"unreachable: {type(exc).__name__}: {exc}"


async def _check_url(
    client: httpx.AsyncClient,
    url: str,
    semaphore: asyncio.Semaphore,
    retries: int,
    retry_delay: float,
) -> tuple[bool, int | None, str, str | None]:
    """Return (ok, status, reason, final_url) for a single distinct URL."""
    attempt = 0
    while True:
        async with semaphore:
            status, final_url, error = await _probe(client, url)

        transient = error is not None or status in RETRYABLE_STATUSES
        if transient and attempt < retries:
            attempt += 1
            await asyncio.sleep(retry_delay * attempt)
            continue

        if error is not None:
            return False, None, error, None
        if 200 <= status < 400:
            return True, status, "ok", final_url

        note = STATUS_NOTES.get(status)
        return False, status, f"HTTP {status}" + (f" - {note}" if note else ""), final_url


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────


async def check_fac_urls(
    payload: Any,
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float = 10.0,
    max_concurrency: int = 6,
    retries: int = 1,
    retry_delay: float = 1.0,
    extra_sections: Sequence[str] = (),
    include_top_level: bool = False,
    ignore_paths: Iterable[str] = (),
    raise_on_warnings: bool = False,
) -> dict[str, Any]:
    """
    Check every FAC URL in the payload and return a report:

        {
          "checked": 15,          # fields checked
          "distinct_urls": 14,    # network requests actually made
          "ok": 13,
          "warnings": [ {...}, ... ],
          "duplicate_urls": [ {"url": "...", "fields": [...]} ]
        }

    Only 2xx and 3xx count as reachable. With `raise_on_warnings=True` the
    report is turned into an HTTPException(400) instead.
    """
    refs = collect_fac_urls(
        payload,
        extra_sections=extra_sections,
        include_top_level=include_top_level,
    )
    ignored = set(ignore_paths)
    refs = [r for r in refs if r.path not in ignored]

    if not refs:
        return {
            "checked": 0,
            "distinct_urls": 0,
            "ok": 0,
            "warnings": [],
            "duplicate_urls": [],
            "note": EMPTY_PAYLOAD_NOTE,
        }

    # One request per distinct URL, one result row per field.
    by_url: dict[str, list[str]] = {}
    for ref in refs:
        by_url.setdefault(ref.url, []).append(ref.path)

    outcomes: dict[str, tuple[bool, int | None, str, str | None]] = {}
    to_probe: list[str] = []
    for url in by_url:
        syntax = _syntax_error(url)
        if syntax:
            outcomes[url] = (False, None, syntax, None)
        else:
            to_probe.append(url)

    if to_probe:
        semaphore = asyncio.Semaphore(max_concurrency)
        owns_client = client is None
        if owns_client:
            client = httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "*/*"},
            )
        try:
            results = await asyncio.gather(
                *(
                    _check_url(client, url, semaphore, retries, retry_delay)
                    for url in to_probe
                )
            )
        finally:
            if owns_client:
                await client.aclose()
        outcomes.update(zip(to_probe, results))

    checks: list[UrlCheck] = []
    for ref in refs:
        ok, status, reason, final_url = outcomes[ref.url]
        checks.append(UrlCheck(ref.path, ref.url, ok, status, reason, final_url))

    warnings = sorted((c for c in checks if not c.ok), key=lambda c: c.path)
    duplicates = [
        {"url": url, "fields": paths} for url, paths in by_url.items() if len(paths) > 1
    ]

    report: dict[str, Any] = {
        "checked": len(checks),
        "distinct_urls": len(by_url),
        "ok": len(checks) - len(warnings),
        "warnings": [w.as_dict() for w in warnings],
        "duplicate_urls": duplicates,
    }

    if warnings and raise_on_warnings:
        raise HTTPException(
            status_code=400,
            detail={
                "message": f"{len(warnings)} of {len(checks)} URL(s) did not resolve.",
                "invalid_urls": report["warnings"],
            },
        )
    return report


async def ensure_fac_urls_resolvable(payload: Any, **kwargs: Any) -> dict[str, Any]:
    """Strict variant: raises HTTPException(400) if anything fails to resolve."""
    return await check_fac_urls(payload, raise_on_warnings=True, **kwargs)
