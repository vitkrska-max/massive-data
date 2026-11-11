"""Test update_daily s falešným REST klientem (bez sítě a API klíče).

Denní data se ukládají do měsíčních souborů:
[root_folder]/market=?/timeframe=1D/year=?/month=??.parquet

Spuštění: python _test_daily.py
"""
import shutil
import sys
import time
import types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq

import massive_data as md

TODAY = date.today()
END = TODAY - timedelta(days=1)               # update_daily končí včerejškem
MONDAY = END - timedelta(days=END.weekday())
HOLIDAY = MONDAY + timedelta(days=1)          # simulovaný svátek (prázdná odpověď)
START = TODAY - md.relativedelta(months=23)   # _REST_HISTORY_MONTHS

ROOTS = [Path("_test_daily_root_a"), Path("_test_daily_root_b")]
ROOT = ROOTS[0]

md._reset_rate_limit()
sleeps = []
time.sleep = lambda s: sleeps.append(s)       # test nesmí reálně čekat


def fake_bars(day):
    """Dva tickery, záměrně v opačném pořadí než abecedně.

    Volume střídavě int a double: tak vypadají reálná adjusted data a dřív to
    shodilo merge měsíce ("Field volume has incompatible types: int64 vs
    double"), protože si pyarrow odvodil typ z hodnot.
    """
    base = (day - date(1970, 1, 1)).days * 86_400_000
    volume = 200 if day.toordinal() % 2 == 0 else 200.6
    return [
        SimpleNamespace(ticker="MSFT", timestamp=base, open=2.0, high=2.1,
                        low=1.9, close=2.05, volume=volume, transactions=20, vwap=2.0),
        SimpleNamespace(ticker="AAPL", timestamp=base, open=1.0, high=1.1,
                        low=0.9, close=1.05, volume=100, transactions=10, vwap=1.0),
    ]


class FakeClient:
    def __init__(self, fail_days=(), error="429 Too Many Requests"):
        self.fail_days = set(fail_days)
        self.error = error
        self.attempts = []

    def get_grouped_daily_aggs(self, **kwargs):
        # parametry bereme přes **kwargs, aby klíč `date` nezastínil třídu date
        day = date.fromisoformat(kwargs["date"])
        self.attempts.append(day)
        if day in self.fail_days:
            raise RuntimeError(self.error)
        if day == HOLIDAY:
            return []          # svátek — prázdná odpověď, není to chyba
        return fake_bars(day)


def install_client(client):
    """Podstrčí falešný `massive` modul místo skutečné knihovny."""
    mod = types.ModuleType("massive")
    mod.RESTClient = lambda key: client
    sys.modules["massive"] = mod


def cfg_for(root):
    return md.AppConfig(subscription=False, markets=["stocks"], root_folder=root,
                        api_key="test-key", retries=3)


def parquets(root):
    return sorted((root / "market=stocks" / "timeframe=1D").rglob("*.parquet"))


def days_in(path):
    """Množina dnů (UTC) uložených v jednom parquet souboru."""
    column = pq.read_table(path, columns=["window_start"]).column("window_start")
    return {
        datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc).date()
        for ns in column.to_pylist()
    }


def stored_days(root):
    """Množina dnů (UTC) uložených ve všech parquet souborech trhu."""
    days = set()
    for path in parquets(root):
        days |= days_in(path)
    return days


for r in ROOTS:
    shutil.rmtree(r, ignore_errors=True)

weekdays = [
    d for d in (START + timedelta(days=i) for i in range((END - START).days + 1))
    if d.weekday() < 5
]
expected = [d for d in weekdays if d != HOLIDAY]
months = sorted({(d.year, d.month) for d in expected})

# --- Test 1: první naplnění — od START, bez víkendů a svátků ---------------
cfg = cfg_for(ROOT)
client = FakeClient()
install_client(client)
md.update_daily(cfg)

files = parquets(ROOT)
print(f"dotazy: {len(client.attempts)} | měsíčních souborů: {len(files)} "
      f"| očekáváno: {len(expected)} dnů v {len(months)} měsících")
assert client.attempts == weekdays, "dotazy neodpovídají pracovním dnům"
assert len(files) == len(months), f"měsíčních souborů {len(files)}, čekáno {len(months)}"
assert stored_days(ROOT) == set(expected), "svátek se nemá ukládat nebo chybí dny"
assert not any(d.weekday() >= 5 for d in client.attempts), "dotaz na víkend"
print("první naplnění + přeskočení víkendů/svátku + 1 soubor na měsíc: OK")

# --- Test 2: hive cesta měsíce, zstd, řazení dle tickeru ------------------
dest = files[-1]
fname = dest.name                                     # month=11.parquet
rel = dest.relative_to(ROOT)
assert rel.parts[:2] == ("market=stocks", "timeframe=1D"), rel.parts
assert fname.startswith("month=") and fname.endswith(".parquet"), fname
year = int(rel.parts[2].removeprefix("year="))
month = int(fname.removeprefix("month=").removesuffix(".parquet"))
assert rel.parts[2] == f"year={year}", rel.parts
assert all((d.year, d.month) == (year, month) for d in days_in(dest)), \
    "v měsíčním souboru jsou dny z jiného měsíce"
with pq.ParquetFile(dest) as pf:
    assert pf.metadata.row_group(0).column(0).compression == "ZSTD"
    t = pf.read()
print(f"hive cesta měsíce + zstd: OK ({rel})")

# vstup fake_bars je [MSFT, AAPL] — výstup musí být seřazený podle tickeru
tickers = t.column("ticker").to_pylist()
assert tickers == sorted(tickers), "řádky nejsou seřazené podle tickeru"
ws = t.column("window_start").to_pylist()
# uvnitř tickeru musí jít window_start vzestupně a v nanosekundách
for ticker in ("AAPL", "MSFT"):
    own = [w for tk, w in zip(tickers, ws) if tk == ticker]
    assert own == sorted(own), f"{ticker}: window_start není vzestupně"
    assert len(own) > 1, "měsíční soubor má mít víc dnů jednoho tickeru"
assert ws[0] % 1_000_000_000 == 0, "window_start není v nanosekundách"
assert ws[0] // 1_000_000_000 == (
    (date.fromtimestamp(ws[0] // 1_000_000_000) - date(1970, 1, 1)).days
    * 86_400
), "window_start neodpovídá půlnoci UTC daného dne"
assert set(t.column_names) == {"ticker", "window_start", "open", "high", "low",
                               "close", "volume", "transactions", "vwap"}
# pevné schéma: bez něj si pyarrow odvodí volume jednou int64, jednou double
assert t.schema.field("window_start").type == pa.int64()
assert t.schema.field("volume").type == pa.int64()
assert t.schema.field("transactions").type == pa.int64()
assert t.schema.field("close").type == pa.float64()
assert t.schema.field("ticker").type == pa.string()
volumes = {tk: set() for tk in ("AAPL", "MSFT")}
for tk, vol in zip(tickers, t.column("volume").to_pylist()):
    assert isinstance(vol, int), (tk, vol)
    volumes[tk].add(vol)
assert volumes["AAPL"] == {100}, volumes
assert volumes["MSFT"] <= {200, 201} and volumes["MSFT"], volumes
assert not list(ROOT.rglob("*.tmp")), "zůstal dočasný soubor po zápisu"
print(f"řazení ticker/window_start + ns + schéma: OK ({dest.name})")

# --- Test 3: idempotence — druhý běh nestahuje znovu ----------------------
before = len(client.attempts)
md.update_daily(cfg)
new = client.attempts[before:]
# případně se dotáhne jen poslední (sváteční) den, který se neukládá
assert len(new) <= 1, f"druhý běh stahoval znovu: {len(new)} dotazů"
assert stored_days(ROOT) == set(expected), "druhý běh změnil uložená data"
print("idempotence: OK")

# --- Test 4: subscription=True → REST se nepoužije ------------------------
client2 = FakeClient()
install_client(client2)
md.update_daily(md.AppConfig(subscription=True, markets=["stocks"], root_folder=ROOT))
assert not client2.attempts, "subscription=True nesmí volat REST"
print("subscription=True: OK")

# --- Test 5: selhání po cfg.retries → konec, žádné díry v datech ----------
# Vlastní adresář: Windows drží otevřené parquet handly, takže nelze
# spolehlivě mazat pod běžícím testem.
ROOT_B = ROOTS[1]
cfg_b = cfg_for(ROOT_B)
fail_day = END - timedelta(days=5)
while fail_day.weekday() >= 5:
    fail_day -= timedelta(days=1)

sleeps.clear()
client3 = FakeClient(fail_days={fail_day})
install_client(client3)
md.update_daily(cfg_b)

saved = stored_days(ROOT_B)
assert fail_day not in saved, "selhaný den se uložil"
assert not [d for d in saved if d > fail_day], "po selhání se uložily další dny"
assert client3.attempts.count(fail_day) == cfg_b.retries, client3.attempts.count(fail_day)
assert 60.0 in sleeps, f"u 429 se má čekat 60s, čekalo se {sorted(set(sleeps))}"
# co se stihlo stáhnout před selháním, musí být uložené (jinak se stahuje znovu)
before_fail = {d for d in expected if d < fail_day}
assert before_fail <= saved, f"chybí dny před selháním: {sorted(before_fail - saved)}"
print(f"selhání po {cfg_b.retries} pokusech (429 -> 60s), zbytek nedotažen: OK")

# --- Test 6: navázání po selhání — merge do existujícího měsíce -----------
files_before = parquets(ROOT_B)
client4 = FakeClient()
install_client(client4)
md.update_daily(cfg_b)

saved2 = stored_days(ROOT_B)
assert saved2 == set(expected), f"po navázání chybí {sorted(set(expected) - saved2)}"
assert fail_day in saved2, "selhaný den se po opakování neuložil"
# merge nesmí staré dny zahodit ani zdvojit řádky
dupes = 0
rows_per_day = {}
for path in parquets(ROOT_B):
    table = pq.read_table(path, columns=["ticker", "window_start"])
    pairs = list(zip(table.column("ticker").to_pylist(),
                     table.column("window_start").to_pylist()))
    dupes += len(pairs) - len(set(pairs))
    for _, ns in pairs:
        d = datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc).date()
        rows_per_day[d] = rows_per_day.get(d, 0) + 1
assert not dupes, f"v měsíčních souborech je {dupes} duplicitních řádků"
assert set(rows_per_day.values()) == {2}, \
    f"den má mít 2 tickery, našel jsem {sorted(set(rows_per_day.values()))}"
assert set(files_before) <= set(parquets(ROOT_B)), "merge zahodil existující soubor"
print("navázání po selhání + merge měsíce bez duplicit: OK")

for r in ROOTS:
    shutil.rmtree(r, ignore_errors=True)
print("VŠE OK")
