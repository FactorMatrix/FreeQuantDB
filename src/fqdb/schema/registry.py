from .base import Dataset
from .assets import DEFAULT_MARKET, MARKET_CONTRACTS, asset_contract, market_contract
from .market import FREQUENCIES, market_fields, market_primary_key


def base_dataset(asset="stock", *, market=DEFAULT_MARKET):
    market_info = market_contract(market)
    contract = asset_contract(asset, market=market)
    return Dataset(
        id=f"{market}/{asset}/base",
        frequencies=list(FREQUENCIES),
        fields=market_fields("1d"),
        frequency_fields={f: market_fields(f) for f in FREQUENCIES},
        primary_key=market_primary_key("1d"),
        timezone=market_info.timezone,
        source="No real provider configured",
        license="No real data redistribution authorized",
        synthetic=False,
        format_version=2,
        units={
            "price": market_info.amount_units[0],
            "volume": contract.volume_unit,
            "amount": market_info.amount_units[0],
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
            dataset = base_dataset(asset, market=market)
            if store.dataset(dataset.id, required=False) is None:
                store.register(dataset)
