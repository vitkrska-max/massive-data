"""Ověření proti SKUTEČNÉ knihovně `massive` (bez sítě a bez API klíče).

Na rozdíl od _test_daily.py a _test_minute.py se tady nepodstrkuje fake modul
`massive`, ale pravá knihovna. Podstrčí se jen transportní vrstva — metody
`_get` a `_paginate` třídy RESTClient, které jako jediné sahají na síť.
Uvnitř nich se ale zavolá PRAVÝ deserializer modelu (Agg.from_dict,
GroupedDailyAgg.from_dict), takže se ověří i to, že kód přistupuje ke
skutečným polím modelů a že URL a parametry odpovídají realitě.

Spuštění: python _test_real_client.py
"""
import shutil
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pyarrow.parquet as pq

import massive
import massive_data as md

TODAY = date.today()
END = TODAY - timedelta(days=1)
DAY = TODAY - timedelta(days=2)          # den, na který umístíme minutové bary

ROOT = Path("_test_real_root")
TICKER_DIR = Path("_test_real_dir")
for r in (ROOT, TICKER_DIR):
    shutil.rmtree(r, ignore_errors=True)
TICKER_DIR.mkdir()

md.REPO_ROOT = TICKER_DIR
md._reset_rate_limit()
time.sleep = lambda s: None

CALLS = []


def _ms(day, hour=0, minute=0):
    """Unix ms — tak posílá timestampy Polygon/Massive. POZOR: ms, ne ns."""
    return int(datetime(day.year, day.month, day.day, hour, minute,
                        tzinfo=timezone.utc).timestamp() * 1000)


class RecordingClient(massive.RESTClient):
    """Pravý RESTClient, jen transport vrací připravená data."""

    def _get(self, path, params=None, result_key=None, deserializer=None,
             raw=False, options=None):
        CALLS.append(("grouped", path, dict(params or {})))
        # Den bereme z URL — jinak by všechny dotazy vracely bary jednoho dne
        # a v měsíčním souboru by vznikly duplicity.
        requested = date.fromisoformat(path.rsplit("/", 1)[-1])
        rows = [
            {"T": "AAPL", "o": 10.0, "h": 11.0, "l": 9.0, "c": 10.5,
             "v": 1000, "vw": 10.4, "t": _ms(requested), "n": 42},
            {"T": "MSFT", "o": 20.0, "h": 21.0, "l": 19.0, "c": 20.5,
             "v": 2000, "vw": 20.4, "t": _ms(requested), "n": 84},
        ]
        return [deserializer(r) for r in rows]

    def _paginate(self, path, params=None, raw=False, deserializer=None,
                  result_key="results", options=None):
        CALLS.append(("aggs", path, dict(params or {})))
        rows = [
            {"o": 1.0, "h": 1.1, "l": 0.9, "c": 1.05, "v": 100,
             "vw": 1.02, "t": _ms(DAY, 14, 30), "n": 7},
            {"o": 2.0, "h": 2.1, "l": 1.9, "c": 2.05, "v": 200,
             "vw": 2.02, "t": _ms(DAY, 14, 31), "n": 8},
        ]
        for r in rows:
            yield deserializer(r)


massive.RESTClient = RecordingClient


def cfg_for(root):
    return md.AppConfig(subscription=False, markets=["stocks"],
                        root_folder=root, api_key="test-key", retries=3)


def seed_last_day(root, market, day):
    """Předstírá, že poslední stažený den je `day` — zkrátí rozsah stahování.

    `update_daily` odvozuje poslední den z max(window_start) měsíčního
    souboru, takže se seeduje opravdový řádek, ne prázdný soubor.
    """
    import pyarrow as pa

    path = (root / f"market={market}" / "timeframe=1D"
            / f"year={day.year}" / f"month={day.month:02d}.parquet")
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist([{
        "ticker": "SEED",
        "window_start": _ms(day) * 1_000_000,
        "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
        "volume": 1, "transactions": 1, "vwap": 1.0,
    }])
    pq.write_table(table, path, compression="zstd")


def all_rows(root):
    """Všechny řádky ze všech parquet souborů trhu."""
    rows = []
    for path in sorted((root / "market=stocks" / "timeframe=1D").rglob("*.parquet")):
        rows.extend(pq.read_table(path).to_pylist())
    return rows


SEED_DAY = DAY - timedelta(days=1)
while SEED_DAY.weekday() >= 5:        # ať je seed pracovní den
    SEED_DAY -= timedelta(days=1)
expected_days = [
    d for d in (SEED_DAY + timedelta(days=i)
                for i in range(1, (END - SEED_DAY).days + 1))
    if d.weekday() < 5
]

# --- 1) update_daily: URL a parametry dotazu na celý trh ------------------
seed_last_day(ROOT, "stocks", SEED_DAY)
md.update_daily(cfg_for(ROOT))

grouped = [c for c in CALLS if c[0] == "grouped"]
assert len(grouped) == len(expected_days), \
    f"mělo být {len(expected_days)} dotazů, bylo {len(grouped)}"
kind, path, params = grouped[0]
assert path == f"/v2/aggs/grouped/locale/us/market/stocks/{expected_days[0].isoformat()}", path
assert params.get("adjusted") == "true", params
assert "from" not in params and "to" not in params, params
print(f"update_daily -> {path} {params}")

# --- 2) update_daily: data se dostala do měsíčního parquet se správnými hodnotami
dest = ROOT / "market=stocks" / "timeframe=1D" / f"year={DAY.year}" \
    / f"month={DAY.month:02d}.parquet"
assert dest.exists(), f"chybí {dest}"
with pq.ParquetFile(dest) as pf:
    assert pf.metadata.row_group(0).column(0).compression == "ZSTD"

rows = [r for r in pq.read_table(dest).to_pylist()
        if r["window_start"] == _ms(DAY) * 1_000_000]
assert [r["ticker"] for r in rows] == ["AAPL", "MSFT"], rows
assert [r["close"] for r in rows] == [10.5, 20.5], rows
assert [r["transactions"] for r in rows] == [42, 84], rows
assert [r["vwap"] for r in rows] == [10.4, 20.4], rows
# bar.timestamp je v ms, do parquet jde v ns (jako flat soubory)
assert all(r["window_start"] % 1_000_000_000 == 0 for r in rows), rows
# merge nesmí zahodit dřív uložený den
assert any(r["ticker"] == "SEED" for r in all_rows(ROOT)), "seed řádek zmizel"
print("update_daily: hodnoty, zstd, ns window_start a merge: OK")

# --- 3) update_minute: URL a parametry dotazu na ticker -------------------
TICKER_DIR.joinpath("1m_stocks.txt").write_text("# test\nAAPL\n", encoding="utf-8")
ROOT_M = Path("_test_real_root_m")
shutil.rmtree(ROOT_M, ignore_errors=True)
CALLS.clear()
md.update_minute(cfg_for(ROOT_M))

aggs = [c for c in CALLS if c[0] == "aggs"]
assert len(aggs) == 1, f"jeden dotaz na ticker: {aggs}"
kind, path, params = aggs[0]
expected_prefix = (
    f"/v2/aggs/ticker/AAPL/range/1/minute/"
    f"{(TODAY - timedelta(days=md._MINUTE_CHUNK_DAYS)).isoformat()}/{END.isoformat()}"
)
assert path == expected_prefix, f"\n  je: {path}\n  má: {expected_prefix}"
assert params.get("sort") == "asc" and params.get("limit") == md._REST_MAX_RECORDS, params
# multiplier ani timespan se neposílají jako parametry — jsou v cestě
assert "timespan" not in params and "multiplier" not in params, params
print(f"update_minute -> {path} {params}")

# --- 4) update_minute: den, schéma a ns window_start ----------------------
dest_m = (ROOT_M / "market=stocks" / "timeframe=1m" / f"year={DAY.year}"
          / f"month={DAY.month:02d}" / f"{DAY.isoformat()}.parquet")
assert dest_m.exists(), f"chybí {dest_m} — bary se zařadily do jiného dne"
with pq.ParquetFile(dest_m) as pf:
    t = pf.read()
assert t.column("ticker").to_pylist() == ["AAPL", "AAPL"], t.column("ticker").to_pylist()
assert t.column("window_start").to_pylist() == [
    _ms(DAY, 14, 30) * 1_000_000, _ms(DAY, 14, 31) * 1_000_000], \
    t.column("window_start").to_pylist()
assert t.column("volume").to_pylist() == [100, 200], t.column("volume").to_pylist()
assert t.column("transactions").to_pylist() == [7, 8], t.column("transactions").to_pylist()
print("update_minute: přiřazení dne a ns window_start: OK")

shutil.rmtree(ROOT, ignore_errors=True)
shutil.rmtree(ROOT_M, ignore_errors=True)
shutil.rmtree(TICKER_DIR, ignore_errors=True)
print("VŠE OK")
