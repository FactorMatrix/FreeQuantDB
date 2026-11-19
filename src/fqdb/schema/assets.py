"""Registered markets and their asset, symbol, unit and availability rules."""

from dataclasses import dataclass, field
import re

from ..util import FQDBError

DEFAULT_MARKET = "cn"
DEFAULT_DATASET = f"{DEFAULT_MARKET}/stock/base"
FREQUENCIES = ("1d", "1m")
_SEGMENT = r"[a-z][a-z0-9_-]*"
_DATASET_ID = re.compile(rf"{_SEGMENT}/{_SEGMENT}/{_SEGMENT}")


@dataclass(frozen=True)
class AssetContract:
    exchanges: tuple[str, ...]
    symbol_pattern: re.Pattern[str]
    volume_units: tuple[str, ...]
    price_units: tuple[str, ...]
    data_types: tuple[str, ...] = ("base",)
    exchange_patterns: dict[str, re.Pattern[str]] = field(default_factory=dict)
    frequencies: tuple[str, ...] = FREQUENCIES
    nullable_fields: tuple[str, ...] = ()
    positive_fields: tuple[str, ...] = ()
    amount_units: tuple[str, ...] = ()

    @property
    def volume_unit(self):
        return self.volume_units[0]

    def accepts_symbol(self, value):
        symbol, separator, exchange = value.rpartition(".")
        pattern = self.exchange_patterns.get(exchange, self.symbol_pattern)
        return bool(separator and exchange in self.exchanges and pattern.fullmatch(symbol))


@dataclass(frozen=True)
class MarketContract:
    timezone: str
    amount_units: tuple[str, ...]
    assets: dict[str, AssetContract]


_SECURITY_CODE = re.compile(r"[0-9]{6}")
_INDEX_CODE = re.compile(r"[A-Z0-9]{5,12}")
_SH_INDEX_CODE = re.compile(r"[A-Z0-9]{6}")
_DELIVERY_MONTH = r"[0-9]{2}(?:0[1-9]|1[0-2])"
_FUTURE_CODE = re.compile(rf"[A-Z]+{_DELIVERY_MONTH}")
_DCE_FUTURE_CODE = re.compile(rf"{_FUTURE_CODE.pattern}|(?:L|PP|V){_DELIVERY_MONTH}F")
_SHARE_UNITS = ("shares", "股")
_CNY = ("CNY", "元")
_POINTS = ("points", "点")
_REPORTED = ("reported",)
MARKET_CONTRACTS = {
    "cn": MarketContract(
        "Asia/Shanghai",
        _CNY,
        {
            "stock": AssetContract(("SZ", "SH", "BJ"), _SECURITY_CODE, _SHARE_UNITS, _CNY),
            "etf": AssetContract(("SZ", "SH"), _SECURITY_CODE, _SHARE_UNITS, _CNY),
            "lof": AssetContract(("SZ", "SH"), _SECURITY_CODE, _SHARE_UNITS, _CNY),
            "index": AssetContract(
                ("SH", "SZ", "BJ", "INDX"),
                _SECURITY_CODE,
                _REPORTED,
                _POINTS,
                exchange_patterns={"SH": _SH_INDEX_CODE, "INDX": _INDEX_CODE},
                frequencies=("1d",),
                nullable_fields=("open", "high", "low", "volume", "amount", "prev_close"),
                positive_fields=("open", "high", "low", "close", "prev_close"),
                amount_units=_REPORTED,
            ),
            "future": AssetContract(
                ("SHFE", "DCE", "CZCE", "CFFEX", "INE", "GFEX"),
                _FUTURE_CODE,
                ("contracts", "lots", "手", "张"),
                (*_CNY, *_POINTS),
                exchange_patterns={"DCE": _DCE_FUTURE_CODE},
            ),
        },
    ),
}


def dataset_parts(dataset: str) -> tuple[str, str, str]:
    if not isinstance(dataset, str) or not _DATASET_ID.fullmatch(dataset):
        raise FQDBError("Dataset id must be market/asset/data_type")
    market, asset, data_type = dataset.split("/")
    return market, asset, data_type


def market_contract(market):
    try:
        return MARKET_CONTRACTS[market]
    except KeyError as exc:
        raise FQDBError(f"Unsupported market: {market!r}") from exc


def asset_contract(asset, *, market=DEFAULT_MARKET):
    try:
        return market_contract(market).assets[asset]
    except KeyError as exc:
        raise FQDBError(f"Unsupported asset {asset!r} for market {market!r}") from exc


def dataset_contract(dataset):
    market, asset, data_type = dataset_parts(dataset)
    contract = asset_contract(asset, market=market)
    if data_type not in contract.data_types:
        raise FQDBError(f"Unsupported market dataset: {dataset!r}")
    return contract
