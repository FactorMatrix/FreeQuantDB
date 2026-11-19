from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import re
from zoneinfo import ZoneInfo

from ..util import FQDBError, day
from .assets import dataset_contract, dataset_parts, market_contract


@dataclass
class Dataset:
    id: str
    frequencies: list[str]
    fields: dict[str, str]
    primary_key: list[str]
    timezone: str
    source: str
    license: str
    synthetic: bool
    units: dict[str, str] = field(default_factory=dict)
    timestamp_meaning: str = "bar end"
    trading_date_rule: str = "explicit source trading_date; never inferred from trading_time"
    rules: list[str] = field(default_factory=list)
    rule_version: str = "1"
    # frequency -> expected sessions; None means unknown, including holidays.
    calendars: dict[str, list[str]] = field(default_factory=dict)
    # day -> symbol -> {status: trading|suspended|not_listed|expired, expected_rows: int|null}
    instruments: dict = field(default_factory=dict)
    sessions: dict = field(default_factory=dict)
    format_version: int = 2
    frequency_fields: dict[str, dict[str, str]] = field(default_factory=dict)
    active_frequency: str = ""
    units_confirmed: bool = False

    def __post_init__(self):
        if self.format_version != 2:
            raise FQDBError("Unsupported market format version")
        if not self.frequencies or any(not re.fullmatch(r"[a-z0-9_-]+", f) for f in self.frequencies):
            raise FQDBError("Invalid frequency")
        ZoneInfo(self.timezone)
        self._validate_market()
        if not self.source or not self.license:
            raise FQDBError("Source and redistribution license must be explicit")
        for dates in self.calendars.values():
            for d in dates:
                day(d)

    def _validate_market(self):
        from .market import market_fields, market_primary_key

        contract = dataset_contract(self.id)
        timezone = market_contract(self.market).timezone
        if self.timezone != timezone:
            raise FQDBError(f"Market dataset {self.id} requires timezone {timezone}")
        if any(f not in contract.frequencies for f in self.frequencies):
            raise FQDBError(f"Market dataset {self.id} supports frequencies {', '.join(contract.frequencies)}")
        current = self.active_frequency or ("1d" if "trading_date" in self.fields else "1m")
        if current not in self.frequencies:
            raise FQDBError("Concrete schema frequency is not declared")
        asset = self.asset
        expected = market_fields(current, asset=asset)
        if self.fields != expected:
            raise FQDBError(f"Market {current} schema fields, order and types must match the strict contract")
        self.fields = expected
        if self.primary_key != market_primary_key(current):
            raise FQDBError("Market primary key must contain instrument and the declared time column")
        for frequency, fields in self.frequency_fields.items():
            if frequency not in self.frequencies or fields != market_fields(frequency, asset=asset):
                raise FQDBError(f"Invalid frequency-specific market contract: {frequency}")
            self.frequency_fields[frequency] = market_fields(frequency, asset=asset)
        if not isinstance(self.units_confirmed, bool):
            raise FQDBError("units_confirmed must be a boolean declaration")
        if not self.timestamp_meaning.strip() or not self.trading_date_rule.strip():
            raise FQDBError("Timestamp semantics and reliable trading-date basis must be declared")

    @property
    def market(self):
        return dataset_parts(self.id)[0]

    @property
    def asset(self):
        return dataset_parts(self.id)[1]

    @property
    def data_type(self):
        return dataset_parts(self.id)[2]

    def fields_for(self, frequency):
        if frequency not in self.frequencies:
            raise FQDBError(f"Frequency not declared in dataset: {frequency}")
        from .market import market_fields

        return dict(self.frequency_fields.get(frequency) or market_fields(frequency, asset=self.asset))

    def for_frequency(self, frequency):
        fields = self.fields_for(frequency)
        from .market import market_primary_key

        return replace(
            self, fields=fields, primary_key=market_primary_key(frequency), active_frequency=frequency
        )

    def to_dict(self):
        result = asdict(self)
        if not self.active_frequency:
            result.pop("active_frequency")
        return result
