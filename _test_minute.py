"""Test update_minute s falešným REST klientem (bez sítě a API klíče).

Spuštění: python _test_minute.py
"""
import shutil
import sys
import time
import types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pyarrow.parquet as pq

import massive_data as md

TODAY = date.today()
END = TODAY - timedelta(days=1)                 # update_minute končí včerejškem
START = TODAY - timedelta(days=md._MINUTE_CHUNK_DAYS)

# Adresář se seznamem tickerů — podstrčíme místo REPO_ROOT.
TICKER_DIR = Path("_test_minute_dir")
ROOTS = [Path("_test_minute_root_a"), Path("_test_minute_root_b")]

md.REPO_ROOT = TICKER_DIR
md._reset_rate_limit()
time.sleep = lambda s: None                      # test nesmí reálně čekat

for r in [TICKER_DIR] + ROOTS:
    shutil.rmtree(r, ignore_errors=True)
TICKER_DIR.mkdir()


def write_tickers(market, tickers):
    path = TICKER_DIR / md._MINUTE_TICKER_FILE.format(market=market)
    path.write_text(
        "# komentar\n" + "\n".join(tickers) + "\n", encoding="utf-8"
    )


class FakeClient:
    def __init__(self, bars_per_day=3, max_day=None, fail_tickers=()):
        self.bars_per_day = bars_per_day
        self.max_day = max_day        # po tomto dni už data nejsou
        self.fail_tickers = set(fail_tickers)
        self.calls = []               # (ticker, from_, to)

    def list_aggs(self, **kw):
        f = date.fromisoformat(kw["from_"])
        t = date.fromisoformat(kw["to"])
        self.calls.append((kw["ticker"], f, t))
        if kw["ticker"] in self.fail_tickers:
            raise RuntimeError("500 Internal Server Error")
        out = []
        day = f
        while day <= t:
            if self.max_day is None or day <= self.max_day:
                # bary začínají o půlnoci UTC, takže zůstanou v rámci dne
                base = int(datetime(day.year, day.month, day.day,
                                    tzinfo=timezone.utc).timestamp() * 1000)
                for i in range(self.bars_per_day):
                    out.append(SimpleNamespace(
                        timestamp=base + i * 60_000,
                        open=1.0 + i, high=1.1 + i, low=0.9 + i,
                        close=1.05 + i, volume=100 + i,
                        transactions=10 + i, vwap=1.0 + i,
                    ))
            day += timedelta(days=1)
        return out


def install_client(client):
    """Podstrčí falešný `massive` modul místo skutečné knihovny."""
    mod = types.ModuleType("massive")
    mod.RESTClient = lambda key: client
    sys.modules["massive"] = mod


def cfg_for(root, markets=("stocks",)):
    return md.AppConfig(subscription=False, markets=list(markets),
                        root_folder=root, api_key="test-key", retries=3)


def minute_files(root, market="stocks"):
    return sorted((root / f"market={market}" / "timeframe=1m").rglob("*.parquet"))


# --- Test 1: první naplnění ------------------------------------------------
write_tickers("stocks", ["ZZZ", "AAPL", "MSFT"])
ROOT = ROOTS[0]
cfg = cfg_for(ROOT)
client = FakeClient(max_day=END - timedelta(days=2))
install_client(client)
md.update_minute(cfg)

files = minute_files(ROOT)
days_with_data = (END - timedelta(days=2)) - START + timedelta(days=1)
print(f"dotazů: {len(client.calls)} | souborů: {len(files)} | očekáváno: {days_with_data.days}")
assert len(files) == days_with_data.days, "počet denních souborů"
assert len(client.calls) == 3, f"jeden dotaz na ticker: {client.calls}"
assert all(f == START and t == END for _, f, t in client.calls), client.calls
print("první naplnění + jeden dotaz na ticker: OK")

# --- Test 2: hive cesta, zstd, řazení, schéma -----------------------------
dest = files[0]
rel = dest.relative_to(ROOT)
assert rel.parts == ("market=stocks", "timeframe=1m", f"year={dest.stem[:4]}",
                     f"month={dest.stem[5:7]}", f"{dest.stem}.parquet"), rel.parts
with pq.ParquetFile(dest) as pf:
    assert pf.metadata.row_group(0).column(0).compression == "ZSTD"
    t = pf.read()
# vstupní pořadí tickerů bylo ZZZ, AAPL, MSFT — výstup dle tickeru
assert t.column("ticker").to_pylist() == ["AAPL", "AAPL", "AAPL",
                                          "MSFT", "MSFT", "MSFT",
                                          "ZZZ", "ZZZ", "ZZZ"], \
    t.column("ticker").to_pylist()
assert set(t.column_names) == {"ticker", "window_start", "open", "high", "low",
                               "close", "volume", "transactions", "vwap"}
ws = t.column("window_start").to_pylist()
assert ws[:3] == sorted(ws[:3]), "window_start není v rámci tickeru seřazený"
print(f"hive cesta + zstd + řazení + schéma: OK ({dest.name})")

# --- Test 3: navázání na poslední stažené datum ---------------------------
before = len(client.calls)
client.max_day = END                      # nově jsou dostupná i poslední 2 dny
md.update_minute(cfg)
new = client.calls[before:]
assert len(new) == 3, f"má se dotazovat znovu jen 3x (jednou na ticker): {new}"
assert all(f == END - timedelta(days=1) and t == END for _, f, t in new), new
assert len(minute_files(ROOT)) == days_with_data.days + 2, "nedotáhly se 2 dny"
print("navázání na poslední stažené datum: OK")

# --- Test 4: dělení na úseky podle limitu API -----------------------------
# Nasimulujeme starší poslední soubor, aby období přesáhlo _MINUTE_CHUNK_DAYS.
ROOT_B = ROOTS[1]
cfg_b = cfg_for(ROOT_B)
old_day = END - timedelta(days=40)
seed = (ROOT_B / "market=stocks" / "timeframe=1m" / f"year={old_day.year}"
        / f"month={old_day.month:02d}" / f"{old_day.isoformat()}.parquet")
seed.parent.mkdir(parents=True, exist_ok=True)
seed.write_bytes(b"")                     # last_downloaded_date čte jen název
client_b = FakeClient(max_day=END)
install_client(client_b)
md.update_minute(cfg_b)

assert len(client_b.calls) == 6, f"2 úseky x 3 tickery: {len(client_b.calls)}"
spans = sorted({(f, t) for _, f, t in client_b.calls})
assert len(spans) == 2, f"mají vzniknout 2 úseky: {spans}"
first, second = spans
assert first[0] == old_day + timedelta(days=1)
assert (first[1] - first[0]).days + 1 == md._MINUTE_CHUNK_DAYS, first
assert second[0] == first[1] + timedelta(days=1) and second[1] == END, second
print(f"dělení na úseky (limit API): OK ({first[0]}..{first[1]}, {second[0]}..{second[1]})")

# --- Test 5: row groups po menších blocích --------------------------------
ROOT_C = Path("_test_minute_root_c")
shutil.rmtree(ROOT_C, ignore_errors=True)
cfg_c = cfg_for(ROOT_C)
client_c = FakeClient(bars_per_day=1440, max_day=END)   # celý den minut
install_client(client_c)
md.update_minute(cfg_c)
with pq.ParquetFile(minute_files(ROOT_C)[0]) as pf:
    rg = pf.metadata.num_row_groups
    rows = pf.metadata.num_rows
# 3 tickery x 1440 minut = 4320 řádků / 1500 = 3 row groups
assert (rg, rows) == (3, 4320), f"row groups={rg}, rows={rows}"
print(f"row groups: OK ({rg} skupiny pro {rows} řádků)")

# --- Test 6: subscription=True → REST se nepoužije ------------------------
client2 = FakeClient()
install_client(client2)
md.update_minute(md.AppConfig(subscription=True, markets=["stocks"],
                              root_folder=ROOT))
assert not client2.calls, "subscription=True nesmí volat REST"
print("subscription=True: OK")

# --- Test 7: vadný ticker se přeskočí a nezablokuje trh -------------------
ROOT_D = Path("_test_minute_root_d")
shutil.rmtree(ROOT_D, ignore_errors=True)
write_tickers("stocks", ["ZZZ", "AAPL", "MSFT"])
cfg_d = cfg_for(ROOT_D)
client_d = FakeClient(max_day=END, fail_tickers={"AAPL"})
install_client(client_d)
md.update_minute(cfg_d)

files_d = minute_files(ROOT_D)
# max_day=END → data za celý rozsah START..END
assert len(files_d) == (END - START).days + 1, \
    f"vadný ticker nesmí zastavit zápis ostatních: {len(files_d)}"
with pq.ParquetFile(files_d[0]) as pf:
    tickers_in_file = set(pf.read().column("ticker").to_pylist())
assert tickers_in_file == {"MSFT", "ZZZ"}, tickers_in_file
# AAPL se zkoušel cfg.retries krát, ostatní jednou
assert client_d.calls.count(("AAPL", START, END)) == cfg_d.retries, \
    client_d.calls.count(("AAPL", START, END))
assert len(files_d) > 0, "trh musí doběhnout do konce"
print(f"vadný ticker přeskočen, trh doběhl: OK ({len(files_d)} souborů bez AAPL)")

# --- Test 8: chybějící seznam tickerů → trh se přeskočí, nic nespadne ----
TICKER_DIR.joinpath(md._MINUTE_TICKER_FILE.format(market="stocks")).unlink()
client3 = FakeClient()
install_client(client3)
md.update_minute(cfg)
assert not client3.calls, "bez seznamu tickerů se nesmí dotazovat"
print("chybějící seznam tickerů: OK")

for r in [TICKER_DIR, ROOT_C, ROOT_D] + ROOTS:
    shutil.rmtree(r, ignore_errors=True)
print("VŠE OK")
