from dataclasses import asdict, dataclass, field
from datetime import date
import calendar
import re

from ..schema.assets import MARKET_CONTRACTS, dataset_contract, dataset_parts
from ..util import FQDBError, day, safe_path

RICH_ENTRY_VERSION = 1
MAGNET_ENTRY_VERSION = 2


def period_bounds(kind, period):
    lengths = {"day": 8, "month": 6, "year": 4}
    if kind not in lengths or not re.fullmatch(rf"[0-9]{{{lengths[kind]}}}", str(period)):
        raise FQDBError("Invalid market package period")
    try:
        year = int(period[:4])
        if kind == "year":
            return date(year, 1, 1).isoformat(), date(year, 12, 31).isoformat()
        month = int(period[4:6])
        if kind == "month":
            return date(year, month, 1).isoformat(), date(
                year, month, calendar.monthrange(year, month)[1]
            ).isoformat()
        d = date(year, month, int(period[6:8])).isoformat()
        return d, d
    except (ValueError, OverflowError) as exc:
        raise FQDBError("Invalid market package date") from exc


def market_relative(dataset, frequency, kind, period, suffix="parquet"):
    dataset_parts(dataset)
    period_bounds(kind, period)
    prefix = f"{dataset}/{frequency}/{period[:4]}"
    if kind != "year":
        prefix += "/" + period[4:6]
    return prefix + "/" + period + "." + suffix


@dataclass(frozen=True)
class Entry:
    dataset: str
    frequency: str
    trading_date: str
    version: int
    file: str
    size: int | None
    sha256: str | None
    info_hash: str
    timezone: str | None
    source: str | None
    license: str | None
    synthetic: bool | None
    published_at: str | None
    torrent: str = ""  # Accepted from explicitly imported native releases.
    magnet: str = ""
    schema_version: int = RICH_ENTRY_VERSION
    torrent_format: str = "v1"
    rows: int | None = 0
    symbols: tuple | None = ()
    schema_hash: str = ""
    format_version: int = 2
    period_kind: str = "day"
    period: str = ""
    start_date: str = ""
    end_date: str = ""
    trading_days: tuple = ()
    day_groups: dict = field(default_factory=dict)
    daily_sources: dict = field(default_factory=dict)
    complete: bool = False
    as_of: str = ""
    published_version: int | None = None

    def __post_init__(self):
        if self.format_version != 2:
            raise FQDBError("Unsupported market format version")
        if not self.period:
            object.__setattr__(self, "period", self.trading_date.replace("-", ""))

    @classmethod
    def from_magnet(cls, dataset, frequency, period, magnet, *, source=None, synthetic=None):
        from ..bt.torrents import magnet_hash
        from .magnet_manifest import package_path, scope_identity

        kind = {4: "year", 6: "month", 8: "day"}.get(len(period) if isinstance(period, str) else None)
        first, last = period_bounds(kind, period)
        scope = scope_identity(f"{dataset}/{frequency}")
        contract = MARKET_CONTRACTS.get(scope["market"])
        return cls(
            dataset=dataset,
            frequency=frequency,
            trading_date=first,
            version=1,
            file="data/" + package_path(scope["scope"], period).removesuffix(".json"),
            size=None,
            sha256=None,
            info_hash=magnet_hash(magnet),
            timezone=contract.timezone if contract else None,
            source=source,
            license=None,
            synthetic=synthetic,
            published_at=None,
            magnet=magnet,
            schema_version=MAGNET_ENTRY_VERSION,
            rows=None,
            symbols=None,
            period_kind=kind,
            period=period,
            start_date=first,
            end_date=last,
        )

    @property
    def sparse(self):
        return self.schema_version == MAGNET_ENTRY_VERSION

    @property
    def revision_order(self):
        return self.version, self.sparse or self.published_version is not None

    def _validate_rich(self, first, last):
        days = tuple(self.trading_days)
        if (
            not days
            or list(days) != sorted(set(days))
            or any(day(x) != x for x in days)
            or self.start_date != days[0]
            or self.end_date != days[-1]
            or self.trading_date != days[0]
            or not first <= days[0] <= days[-1] <= last
        ):
            raise FQDBError("Invalid market package trading-day coverage")
        groups = [g for values in self.day_groups.values() for g in values]
        if (
            set(self.day_groups) != set(days)
            or any(not isinstance(v, list) or not v for v in self.day_groups.values())
            or any(type(g) is not int or g < 0 for g in groups)
            or sorted(groups) != list(range(len(groups)))
            or set(self.daily_sources) != set(days)
            or any(not re.fullmatch(r"[a-f0-9]{64}", str(h)) for h in self.daily_sources.values())
        ):
            raise FQDBError("Invalid file-bound trading-day row-group index or source digest")
        if type(self.size) is not int or self.size < 1:
            raise FQDBError("Invalid immutable file identity or manifest version")
        if not isinstance(self.sha256, str) or not re.fullmatch("[a-f0-9]{64}", self.sha256):
            raise FQDBError("Invalid SHA-256")
        if not self.source or not self.license:
            raise FQDBError("Missing provenance")

    def _validate_sparse(self, first, last):
        if (self.trading_date, self.start_date, self.end_date) != (first, first, last):
            raise FQDBError("Magnet package dates must match its nominal period")
        if (
            self.sha256 is not None
            or self.rows is not None
            or self.symbols is not None
            or self.trading_days
            or self.day_groups
            or self.daily_sources
            or self.schema_hash
            or self.complete
            or self.as_of
            or not self.magnet
            or (self.size is not None and (type(self.size) is not int or self.size < 1))
        ):
            raise FQDBError("Invalid magnet package metadata")

    def validate(self, root):
        first, last = period_bounds(self.period_kind, self.period)
        if self.sparse:
            from .magnet_manifest import package_path

            expected = "data/" + package_path(f"{self.dataset}/{self.frequency}", self.period).removesuffix(
                ".json"
            )
        else:
            contract = dataset_contract(self.dataset)
            if self.frequency not in contract.frequencies:
                raise FQDBError(f"Unsupported market package frequency for {self.dataset}: {self.frequency}")
            expected = "data/" + market_relative(self.dataset, self.frequency, self.period_kind, self.period)
        if self.file != expected or type(self.version) is not int or self.version < 1:
            raise FQDBError("Invalid immutable file identity or manifest version")
        if self.published_version is not None and (
            self.sparse or type(self.published_version) is not int or self.published_version < 1
        ):
            raise FQDBError("Invalid source catalog revision")
        if self.schema_version == RICH_ENTRY_VERSION:
            self._validate_rich(first, last)
        elif self.sparse:
            self._validate_sparse(first, last)
        else:
            raise FQDBError("Unsupported manifest entry version")
        if type(self.complete) is not bool or (self.as_of and not self.start_date <= day(self.as_of) <= last):
            raise FQDBError("Invalid market package completion metadata")
        if self.torrent_format != "v1" or not re.fullmatch("[a-f0-9]{40}", self.info_hash):
            raise FQDBError("Unsupported torrent format/hash")
        safe_path(root, self.file)
        if self.magnet:
            from ..bt.torrents import magnet_hash

            if magnet_hash(self.magnet) != self.info_hash:
                raise FQDBError("Magnet info-hash does not match trusted manifest")
        return self

    def to_dict(self):
        result = asdict(self)
        if not self.magnet:
            result.pop("magnet")
        if not self.torrent:
            result.pop("torrent")
        if self.published_version is None:
            result.pop("published_version")
        return result

    @property
    def key(self):
        partition = f"{self.period_kind}:{self.period}" if self.period_kind != "day" else self.trading_date
        return self.dataset, self.frequency, partition

    @property
    def days(self):
        return tuple(self.trading_days)

    @property
    def start(self):
        return self.start_date

    @property
    def end(self):
        return self.end_date
