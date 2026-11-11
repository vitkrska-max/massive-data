"""Test update_tickers + parseru SIC offline (fake massive, žádná síť).

Nesahá na síť: `massive` je podstrčený fake modul a `sic_segments` se předplní,
takže se `update_sic_segments()` (stahování ze SEC) vůbec nezavolá.

Spuštění: python _test_tickers.py
"""
import shutil
import sqlite3
import sys
import time
import types
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pyarrow.parquet as pq

import massive_data as md

DAY = date(2026, 1, 15)
ROOT = Path("_test_tickers_root")
LISTS = Path("_test_tickers_lists")          # sem patří tickers_{market}.txt
TICKERS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG"]

REAL_SLEEP = time.sleep
REAL_MONOTONIC = time.monotonic
sleeps = []
# Virtuální hodiny: uspaný čas se přičte, takže limiter vidí, že okno uběhlo.
# Bez toho by test s patchovaným sleep nikdy neopustil klouzavé okno.
clock = [0.0]


def fake_sleep(seconds):
    sleeps.append(seconds)
    clock[0] += seconds


def details(ticker, name=None):
    """Fake TickerDetails — všechna pole modelu včetně vnořených address/branding."""
    return SimpleNamespace(
        ticker=ticker, name=name or f"{ticker} Inc", active=True, market="stocks",
        locale="us", primary_exchange="XNAS", type="CS",
        currency_name="usd", currency_symbol="USD",
        base_currency_name=None, base_currency_symbol=None,
        cik="0000000001", composite_figi="BBG000000001", share_class_figi="BBG000000002",
        sic_code="0100", sic_description="Agricultural Production-Crops",
        market_cap=1_000_000.0, share_class_shares_outstanding=1000,
        weighted_shares_outstanding=1000, total_employees=10,
        list_date="2020-01-01", delisted_utc=None, description="Test Inc.",
        homepage_url="https://example.com", phone_number="123",
        ticker_root=ticker, ticker_suffix=None,
        address=SimpleNamespace(address1="1 Main St", address2=None, city="New York",
                                state="NY", country="US", postal_code="10001"),
        branding=SimpleNamespace(logo_url="https://logo", icon_url="https://icon",
                                 accent_color="000000", light_color="ffffff",
                                 dark_color="111111"),
    )


class FakeClient:
    """Fake RESTClient — jen dvě metody, které update_tickers používá."""

    def __init__(self, names=None):
        self.names = TICKERS if names is None else names
        self.detail_calls = []
        self.list_calls = []

    def list_tickers(self, **kwargs):
        self.list_calls.append(kwargs)
        return [SimpleNamespace(ticker=name) for name in self.names]

    def get_ticker_details(self, ticker):
        self.detail_calls.append(ticker)
        return details(ticker)


class BoomClient(FakeClient):
    """Simuluje pád uprostřed běhu (KeyboardInterrupt není _RestError)."""

    def __init__(self, boom_at, names=None):
        super().__init__(names)
        self.boom_at = boom_at

    def get_ticker_details(self, ticker):
        if len(self.detail_calls) == self.boom_at:
            raise KeyboardInterrupt
        return super().get_ticker_details(ticker)


def install_client(client):
    mod = types.ModuleType("massive")
    mod.RESTClient = lambda key: client
    sys.modules["massive"] = mod


def cfg_for(root, subscription=False):
    return md.AppConfig(subscription=subscription, markets=["stocks"], root_folder=root,
                        api_key="test-key", retries=2,
                        tickers_info=str(LISTS / "tickers_{market}.txt"))


def db_path(root):
    return root / "stocks" / "tickers" / "reference.sqlite"


def snapshot(root, day):
    return (root / "stocks" / "tickers" / f"{day.year}" / f"{day.month:02d}"
            / f"{day.isoformat()}.parquet")


def tmp_dir(root, day):
    return root / "stocks" / "tickers" / "_tmp" / day.isoformat()


def seed_sic(root):
    """Předplní sic_segments, aby se nestahovalo ze SEC."""
    path = db_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(md._DDL_SIC_SEGMENTS)
    con.execute(
        "INSERT OR REPLACE INTO sic_segments VALUES ('0100', 'Office of Technology',"
        " 'Agricultural Production-Crops', 'Technology')"
    )
    con.commit()
    con.close()


def tickers_in(root, day):
    table = pq.read_table(snapshot(root, day), columns=["ticker"])
    return table.column("ticker").to_pylist()


for path in (ROOT, LISTS):
    shutil.rmtree(path, ignore_errors=True)
LISTS.mkdir()

time.sleep = fake_sleep
time.monotonic = lambda: clock[0]

try:
    # --- 1) all-tickers: snapshot + DB + dávkování po pěti -----------------
    seed_sic(ROOT)
    client = FakeClient()
    install_client(client)
    sleeps.clear()
    md.update_tickers(cfg_for(ROOT), day=DAY)

    snap = snapshot(ROOT, DAY)
    assert snap.is_file(), "snapshot se nevytvořil"
    table = pq.read_table(snap)
    assert table.column("ticker").to_pylist() == TICKERS, table.column("ticker").to_pylist()
    assert set(table.column("last_updated").to_pylist()) == {DAY.isoformat()}
    assert table.column("address_city").to_pylist()[0] == "New York"
    assert table.column("logo_url").to_pylist()[0] == "https://logo"
    assert table.column("sic_code").to_pylist()[0] == "0100"
    assert len(table.column_names) == len(md._TICKERS_COLUMNS), len(table.column_names)
    with pq.ParquetFile(snap) as pf:
        assert pf.metadata.row_group(0).column(0).compression == "ZSTD"
    assert client.list_calls and client.list_calls[0]["active"] is True
    assert client.detail_calls == TICKERS, client.detail_calls
    # 7 tickerů = prvních pět hned, pak jedno čekání do konce okna
    assert len(sleeps) == 1, sleeps
    assert 59.0 < sleeps[0] <= md._REST_RATE_WINDOW + md._REST_RATE_MARGIN + 1, sleeps
    assert not tmp_dir(ROOT, DAY).exists(), "dočasný adresář měl zmizet"

    con = sqlite3.connect(db_path(ROOT))
    rows = con.execute("SELECT ticker, name, last_updated FROM tickers ORDER BY ticker").fetchall()
    con.close()
    assert [row[0] for row in rows] == TICKERS, rows
    assert {row[2] for row in rows} == {DAY.isoformat()}
    print("all-tickers -> snapshot + reference.sqlite + dávka po pěti: OK")

    # --- 2) idempotence: stejný den se nestahuje znovu ---------------------
    before = len(client.detail_calls)
    md._reset_rate_limit()
    md.update_tickers(cfg_for(ROOT), day=DAY)
    assert client.detail_calls[before:] == [], client.detail_calls[before:]
    print("idempotence stejného dne: OK")

    # --- 3) priorita: soubor z tickers_info vyhraje nad DB i all-tickers ---
    (LISTS / "tickers_stocks.txt").write_text("# test\nZZZ\nYYY\n", encoding="utf-8")
    client2 = FakeClient()
    install_client(client2)
    md._reset_rate_limit()
    md.update_tickers(cfg_for(ROOT), day=DAY)
    assert client2.detail_calls == ["ZZZ", "YYY"], client2.detail_calls
    assert not client2.list_calls, client2.list_calls
    assert set(tickers_in(ROOT, DAY)) == set(TICKERS) | {"ZZZ", "YYY"}
    print("priorita soubor -> DB -> all-tickers: OK")

    # --- 4) bez souboru se bere tabulka tickers z reference.sqlite ---------
    (LISTS / "tickers_stocks.txt").unlink()
    day_db = DAY.replace(day=16)
    client3 = FakeClient()
    install_client(client3)
    md._reset_rate_limit()
    md.update_tickers(cfg_for(ROOT), day=day_db)
    assert sorted(client3.detail_calls) == sorted(TICKERS + ["ZZZ", "YYY"]), client3.detail_calls
    assert not client3.list_calls
    assert sorted(tickers_in(ROOT, day_db)) == sorted(TICKERS + ["ZZZ", "YYY"])
    print("seznam z tabulky tickers: OK")

    # --- 5) pád uprostřed běhu -> dočasné party -> navázání ---------------
    day_crash = DAY.replace(day=17)
    boomer = BoomClient(boom_at=6)          # spadne v druhé dávce
    install_client(boomer)
    try:
        md.update_tickers(cfg_for(ROOT), day=day_crash)
        raise AssertionError("běh měl spadnout")
    except KeyboardInterrupt:
        pass

    parts = sorted(tmp_dir(ROOT, day_crash).glob("part-*.parquet"))
    assert len(parts) == 1, parts
    done = pq.read_table(parts[0], columns=["ticker"]).column("ticker").to_pylist()
    assert done == TICKERS[:5], done
    assert not snapshot(ROOT, day_crash).exists(), "při pádu nesmí vzniknout snapshot"

    client5 = FakeClient()
    install_client(client5)
    md._reset_rate_limit()
    md.update_tickers(cfg_for(ROOT), day=day_crash)
    # zbytek abecedy po partu: FFF, GGG + dřív přidané YYY, ZZZ
    assert client5.detail_calls == ["FFF", "GGG", "YYY", "ZZZ"], client5.detail_calls
    assert sorted(tickers_in(ROOT, day_crash)) == sorted(TICKERS + ["ZZZ", "YYY"])
    assert not tmp_dir(ROOT, day_crash).exists(), "po úspěšném běhu má tmp zmizet"
    print("navázání z dočasných partů po pádu: OK")

    # --- 6) upsert jen když last_updated < day -----------------------------
    day_upsert = DAY.replace(day=18)
    con = sqlite3.connect(db_path(ROOT))
    con.execute(
        "INSERT INTO tickers (ticker, name, last_updated) VALUES ('AAA', 'STARÝ', ?) "
        "ON CONFLICT(ticker) DO UPDATE SET name='STARÝ', last_updated=excluded.last_updated",
        (day_upsert.isoformat(),),
    )
    con.commit()
    client6 = FakeClient()
    install_client(client6)
    md.update_tickers(cfg_for(ROOT), day=day_upsert)
    row = con.execute("SELECT name, last_updated FROM tickers WHERE ticker='AAA'").fetchone()
    con.close()
    assert row == ("STARÝ", day_upsert.isoformat()), row
    print("upsert přeskočen, když last_updated >= day: OK")

    # --- 7) subscription=True -> žádné čekání ------------------------------
    day_sub = DAY.replace(day=19)
    sleeps.clear()
    client7 = FakeClient()
    install_client(client7)
    md.update_tickers(cfg_for(ROOT, subscription=True), day=day_sub)
    assert sleeps == [], sleeps
    print("subscription=True bez čekání: OK")

    # --- 8) parser SIC tabulky ze SEC (stdlib) ----------------------------
    html = """
    <table>
      <tr><th>SIC Code</th><th>Office</th><th>Industry Title</th></tr>
      <tr><td>0100</td><td>Office of Technology</td>
          <td>Agricultural Production-Crops</td></tr>
      <tr><td>200</td><td>Neznámý úřad</td><td>Nějaká industry</td></tr>
      <tr><td>&nbsp;</td><td></td><td>řádek bez kódu</td></tr>
    </table>
    """
    parsed = md._parse_sic_html(html)
    assert parsed == [
        ("0100", "Office of Technology", "Agricultural Production-Crops", "Technology"),
        ("0200", "Neznámý úřad", "Nějaká industry", "Other"),
    ], parsed
    print("parser SIC tabulky: OK")

finally:
    time.sleep = REAL_SLEEP
    time.monotonic = REAL_MONOTONIC
    sys.modules.pop("massive", None)
    for path in (ROOT, LISTS):
        shutil.rmtree(path, ignore_errors=True)

print("VŠE OK")
