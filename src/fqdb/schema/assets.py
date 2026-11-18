"""Registered markets and their asset, exchange, symbol and unit rules."""

from dataclasses import dataclass
import re

from ..util import FQDBError

DEFAULT_MARKET = "cn"
DEFAULT_DATASET = f"{DEFAULT_MARKET}/stock/base"
_SEGMENT = r"[a-z][a-z0-9_-]*"
_DATASET_ID = re.compile(rf"{_SEGMENT}/{_SEGMENT}/{_SEGMENT}")


@dataclass(frozen=True)
class AssetContract:
    exchanges: tuple[str, ...]
    symbol_pattern: re.Pattern[str]
    volume_units: tuple[str, ...]
    price_units: tuple[str, ...]
    data_types: tuple[str, ...] = ("base",)

    @property
    def volume_unit(self):
        return self.volume_units[0]

    def accepts_symbol(self, value):
        symbol, separator, exchange = value.rpartition(".")
        return bool(separator and exchange in self.exchanges and self.symbol_pattern.fullmatch(symbol))


@dataclass(frozen=True)
class MarketContract:
    timezone: str
    amount_units: tuple[str, ...]
    assets: dict[str, AssetContract]


_SECURITY_CODE = re.compile(r"[0-9]{6}")
_SHARE_UNITS = ("shares", "股")
_CNY = ("CNY", "元")
MARKET_CONTRACTS = {
    "cn": MarketContract(
        "Asia/Shanghai",
        _CNY,
        {
            "stock": AssetContract(("SZ", "SH", "BJ"), _SECURITY_CODE, _SHARE_UNITS, _CNY),
            "etf": AssetContract(("SZ", "SH"), _SECURITY_CODE, _SHARE_UNITS, _CNY),
            "future": AssetContract(
                ("SHFE", "DCE", "CZCE", "CFFEX", "INE", "GFEX"),
                re.compile(r"[A-Z]+[0-9]{2}(?:0[1-9]|1[0-2])"),
                ("contracts", "lots", "手", "张"),
                (*_CNY, "points", "点"),
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
