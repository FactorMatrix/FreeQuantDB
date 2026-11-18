from ..catalog.index import select
from ..validation.coverage import dates
from ..validation.files import inspect_file
from ..util import FQDBError


def package_dates(entry, start, end):
    """Use nominal dates for sparse package selection, not coverage assertions."""
    if entry.sparse:
        return set(dates(max(entry.start, start or entry.start), min(entry.end, end or entry.end)))
    return {d for d in entry.days if (not start or start <= d) and (not end or d <= end)}


def _automatic_items(candidates, preferred_kind):
    """Cover the requested dates with existing files first, then matching seeds."""
    covered = set()
    selected = []
    for item, nominal_dates in sorted(
        candidates,
        key=lambda candidate: (
            candidate[0]["action"] != "ready",
            -len(candidate[1]),
            candidate[0]["entry"]["period_kind"] != preferred_kind,
            candidate[0]["entry"].get("size") is None,
            candidate[0]["entry"].get("size") or 0,
            -len(candidate[0]["entry"]["period"]),
        ),
    ):
        if nominal_dates - covered:
            selected.append(item)
            covered.update(nominal_dates)
    return sorted(selected, key=lambda item: item["entry"]["start_date"])


def plan(
    store,
    dataset,
    frequency,
    start_date=None,
    end_date=None,
    year=None,
    month=None,
    *,
    granularity=None,
    allow_daily_fallback=False,
    public_periods=None,
):
    entries, start, end = select(store, dataset, frequency, start_date, end_date, year, month)
    if public_periods is not None:
        entries = [e for e in entries if not e.sparse or (e.dataset, e.frequency, e.period) in public_periods]
    schema = store.dataset(dataset)
    preferred_kind = "month" if month else "year" if year else "day"
    expected_kind = (
        granularity if granularity is not None else (preferred_kind if allow_daily_fallback else "auto")
    )
    if expected_kind not in ("auto", "day", "month", "year") or type(allow_daily_fallback) is not bool:
        raise FQDBError("Invalid download granularity or daily fallback option")
    ranges = {e.info_hash: package_dates(e, start, end) for e in entries}
    entries = [e for e in entries if ranges[e.info_hash]]
    available = sorted({e.period_kind for e in entries})
    possibly_published = {d for e in entries for d in ranges[e.info_hash]}
    choices = [e for e in entries if expected_kind in ("auto", e.period_kind)]
    stale_packages = []
    eligible = []
    for e in choices:
        if store.aggregate_stale(e):
            stale_packages.append(e.to_dict())
        else:
            eligible.append(e)
    fallback_required = expected_kind in ("month", "year") and not eligible
    if fallback_required and allow_daily_fallback:
        eligible = [e for e in entries if e.period_kind == "day"]
    active = {e.key: (e, cache) for e, cache in store.active(dataset, frequency, start, end)}
    candidates = []
    for e in eligible:
        old, cache = active.get(e.key, (None, None))
        status = inspect_file(store.root, e, schema, deep=False, cached=cache if old == e else None)
        action = (
            "ready"
            if status["file"] == "verified_cached" and old == e
            else ("revision" if old and old.revision_order < e.revision_order else "verify_or_download")
        )
        candidates.append(
            ({"entry": e.to_dict(), "action": action, "local": status["file"]}, ranges[e.info_hash])
        )
    items = (
        _automatic_items(candidates, preferred_kind)
        if expected_kind == "auto"
        else [item for item, _ in candidates]
    )
    calendar = schema.calendars.get(frequency)
    unpublished = [
        d
        for d in (calendar or [])
        if (not start or d >= start) and (not end or d <= end) and d not in possibly_published
    ]
    sizes = [i["entry"].get("size") for i in items if i["action"] != "ready"]
    return {
        "dataset": dataset,
        "frequency": frequency,
        "start_date": start,
        "end_date": end,
        "items": items,
        "bytes": None if None in sizes else sum(sizes),
        "source_unpublished": unpublished,
        "catalog_empty": not items,
        "calendar": "known_within_declared_range" if calendar else "pending",
        "expected_kind": expected_kind,
        "available_granularities": available,
        "fallback_required": fallback_required,
        "daily_fallback_selected": fallback_required and allow_daily_fallback,
        "stale_packages": stale_packages,
        "fallback_reason": "Corresponding independent package unavailable or stale; choose daily torrents explicitly"
        if fallback_required
        else None,
    }
