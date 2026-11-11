"""Živý smoke-test: SKUTEČNÉ dotazy na Massive REST API.

Pozor — spotřebuje API kvótu (free tier = 5 dotazů/minutu):
  * denní agregace ... 1 dotaz (celý trh za jeden den)
  * minutové bary ... 1 dotaz (1 ticker za ~31 dní)
Nezapisuje do skutečného root_folder z config.yaml, pracuje v _smoke_root.

Spuštění: python _smoke_real_api.py [both|daily|minute]
"""
import shutil
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pyarrow.parquet as pq

import massive_data as md

MODE = sys.argv[1] if len(sys.argv) > 1 else "both"
assert MODE in ("both", "daily", "minute"), f"neznámý režim: {MODE}"

# Burzovní čas — kvůli výpisu session. Když chybí tzdata, použije se EDT
# (New York je v létě UTC-4, což pro září platí).
try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:
    ET = timezone(timedelta(hours=-4))

ROOT = Path("_smoke_root")
TICKER_DIR = Path("_smoke_dir")
for r in (ROOT, TICKER_DIR):
    shutil.rmtree(r, ignore_errors=True)
TICKER_DIR.mkdir()

cfg = md.load_config()
print(f"API klíč: {cfg.require_secret('api_key')[:6]}...{cfg.require_secret('api_key')[-4:]}"
      f" | markets={cfg.markets} | subscription={cfg.subscription}")
cfg.root_folder = ROOT          # nikdy nepíšeme do skutečného root_folder
md.REPO_ROOT = TICKER_DIR
md._reset_rate_limit()
time.sleep = lambda s: None     # ať smoke test nečeká na rate limit

# Poslední uzavřený obchodní den (o víkendu se neobchoduje).
day = date.today() - timedelta(days=1)
while day.weekday() >= 5:
    day -= timedelta(days=1)
print(f"poslední obchodní den: {day} ({day.strftime('%A')})\n")

ok = True


def et(ns):
    """Unix ns -> čas v New Yorku."""
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).astimezone(ET)


# --- 1) denní agregace: jeden dotaz za celý trh ---------------------------
if MODE in ("daily", "both"):
    seed_day = day - timedelta(days=1)
    seed = (ROOT / "market=stocks" / "timeframe=1D" / f"year={seed_day.year}"
            / f"month={seed_day.month:02d}" / f"{seed_day.isoformat()}.parquet")
    seed.parent.mkdir(parents=True, exist_ok=True)
    seed.write_bytes(b"")       # ať update_daily stahuje jen jeden den

    try:
        t0 = time.time()
        md.update_daily(cfg)
        elapsed = time.time() - t0
    except SystemExit as exc:
        print(f"DENNÍ AGREGACE: skončilo přes sys.exit({exc.code})")
        ok = False
    else:
        dest = (ROOT / "market=stocks" / "timeframe=1D" / f"year={day.year}"
                / f"month={day.month:02d}" / f"{day.isoformat()}.parquet")
        if not dest.exists():
            print(f"DENNÍ AGREGACE: soubor {dest} nevznikl")
            ok = False
        else:
            with pq.ParquetFile(dest) as pf:
                n_rows = pf.metadata.num_rows
                n_groups = pf.metadata.num_row_groups
                t = pf.read()
            tickers = t.column("ticker").to_pylist()
            closes = t.column("close").to_pylist()
            print(f"DENNÍ AGREGACE: OK za {elapsed:.1f} s")
            print(f"  soubor    : {dest.name} ({dest.stat().st_size / 1024:.0f} kB)")
            print(f"  tickerů   : {n_rows} v {n_groups} row groups")
            print(f"  ukázka    : {tickers[0]} close={closes[0]} "
                  f"| {tickers[n_rows // 2]} close={closes[n_rows // 2]}")
            print(f"  sloupce   : {t.column_names}")
            assert None not in tickers, "nějaký ticker je None"
            assert all(c is not None for c in closes), "nějaké close je None"

# --- 2) minutové bary: jeden dotaz na jeden ticker ------------------------
if MODE in ("minute", "both"):
    TICKER_DIR.joinpath(md._MINUTE_TICKER_FILE.format(market="stocks")).write_text(
        "# smoke-test\nAAPL\n", encoding="utf-8")
    try:
        t0 = time.time()
        md.update_minute(cfg)
        elapsed_m = time.time() - t0
    except SystemExit as exc:
        print(f"MINUTOVÉ BARY: skončilo přes sys.exit({exc.code})")
        ok = False
    else:
        files = sorted((ROOT / "market=stocks" / "timeframe=1m").rglob("*.parquet"))
        total = 0
        print(f"\nMINUTOVÉ BARY: OK za {elapsed_m:.1f} s")
        print(f"  souborů   : {len(files)} denních parquet")
        print("\n  hranice session podle dne (čas v New Yorku):")
        spans = []
        for f in files:
            with pq.ParquetFile(f) as pf:
                n = pf.metadata.num_rows
            t = pq.read_table(f, columns=["window_start"])
            ws = t.column("window_start").to_pylist()
            total += n
            first, last = et(min(ws)), et(max(ws))
            spans.append((first, last))
            print(f"    {f.stem}  {n:4d} barů  {first:%H:%M} - {last:%H:%M}"
                  f"  ({first:%Z})")

        # Rozložení podle hodin — z něj je přímo vidět tvar session.
        print(f"  celkem    : {total} minutových barů")
        starts = {f"{s:%H}" for s, _ in spans}
        ends = {f"{e:%H}" for _, e in spans}
        print(f"\n  začátky session: {sorted(starts)}")
        print(f"  konce session  : {sorted(ends)}")

        with pq.ParquetFile(files[0]) as pf:
            t = pf.read()
        print(f"\n  rozložení podle hodiny NY ({files[0].stem}):")
        per_hour = {}
        for ns in t.column("window_start").to_pylist():
            per_hour.setdefault(et(ns).hour, 0)
            per_hour[et(ns).hour] += 1
        for h in range(24):
            c = per_hour.get(h, 0)
            if c:
                print(f"    {h:02d}:00  {'#' * (c // 4):<16} {c:4d}")
        print(f"  ticker v souboru: "
              f"{sorted(set(t.column('ticker').to_pylist()))}")
        print(f"  sloupce         : {t.column_names}")

shutil.rmtree(ROOT, ignore_errors=True)
shutil.rmtree(TICKER_DIR, ignore_errors=True)
print("\nSMOKE-TEST OK" if ok else "\nSMOKE-TEST SELHAL")
sys.exit(0 if ok else 1)
