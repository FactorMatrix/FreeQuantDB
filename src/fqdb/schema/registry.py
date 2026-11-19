from .base import Dataset
from .assets import DEFAULT_MARKET, MARKET_CONTRACTS, asset_contract, market_contract
from .market import market_fields, market_primary_key


def base_dataset(asset="stock", *, market=DEFAULT_MARKET):
    market_info = market_contract(market)
    contract = asset_contract(asset, market=market)
    return Dataset(
        id=f"{market}/{asset}/base",
        frequencies=list(contract.frequencies),
        fields=market_fields("1d", asset=asset),
        frequency_fields={f: market_fields(f, asset=asset) for f in contract.frequencies},
        primary_key=market_primary_key("1d"),
        timezone=market_info.timezone,
        source="No real provider configured",
        license="No real data redistribution authorized",
        synthetic=False,
        format_version=2,
        units={
            "price": contract.price_units[0],
            "volume": contract.volume_unit,
            "amount": (contract.amount_units or market_info.amount_units)[0],
            "prev_close": "unadjusted source previous close; basis must be verified",
        },
        units_confirmed=False,
        timestamp_meaning="source bar end; must be confirmed before production",
        trading_date_rule="reliable explicit source trading date; never inferred from trading_time",
        rules=["ohlc", "nonnegative"],
        rule_version="market-2",
    )


def install_defaults(store):
    for market, contract in MARKET_CONTRACTS.items():
        for asset in contract.assets:
            store.register_default(base_dataset(asset, market=market))
