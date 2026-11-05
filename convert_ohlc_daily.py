#!/usr/bin/env python3
"""convert_ohlc_daily.py — jednorázová konverze denních OHLCV do měsíčních parquet.

Zdroj: <source>/symbol=<SYM>/data2020s.parquet — jeden soubor na ticker, sloupce
`symbol, timestamp, open, high, low, close, volume, vwap, trades` (timestamp
v ms/us/ns, volume a trades jako double).

Cíl: <root>/market={market}/timeframe=1D/year={YYYY}/month={MM}.parquet —
stejný tvar, jaký produkuje update_daily() v massive_data.py:
    ticker, window_start (Unix ns, int64), open, high, low, close,
    volume (int64), transactions (int64), vwap
zstd, row groups po 5 000 řádcích, řádky setříděné podle tickeru a uvnitř
podle window_start, zápis přes *.tmp + Path.replace().

Market se pozná z názvu adresáře: "C_" -> crypto, "X_" -> fx, jinak stocks.
Ticker se bere z dat (v datech je např. "C:AEDEUR"), ne z názvu adresáře.

Skript je offline (žádné API) a je určen pro jednorázový převod; cíl ovšem
odmítne přepsat, dokud se nepustí s --force (aby data nezdvojil).

Použití:
    python convert_ohlc_daily.py --limit 20 --root /tmp/prevod   # zkouška
    python convert_ohlc_daily.py                                  # ostrý převod
    python convert_ohlc_daily.py --force                          # od nuly znovu
"""
import argparse
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

import massive_data as md

# Cílové schéma a parametry zápisu — stejné jako u update_daily().
_COLUMNS = (
    "ticker", "window_start", "open", "high", "low", "close",
    "volume", "transactions", "vwap",
)
_ROW_GROUP_SIZE = 5_000
_DEFAULT_SOURCE = Path("/home/vitek/trading/ohlc/timeframe=1D")
_PREFIX_MARKET = (("C_", "crypto"), ("X_", "fx"))
_DEFAULT_MARKET = "stocks"
_FILENAME = "data2020s.parquet"


def _market_of(dir_name: str) -> str:
    """"symbol=C_AEDEUR" -> "crypto", "symbol=X_EURUSD" -> "fx", jinak stocks."""
    symbol = dir_name.removeprefix("symbol=")
    for prefix, market in _PREFIX_MARKET:
        if symbol.startswith(prefix):
            return market
    return _DEFAULT_MARKET


def _month_path(root: Path, market: str, year: int, month: int) -> Path:
    """Cesta k měsíčnímu parquet souboru v hive struktuře."""
    return (
        root / f"market={market}" / "timeframe=1D"
        / f"year={year}" / f"month={month:02d}.parquet"
    )


def _to_int64(column: pa.ChunkedArray) -> pa.ChunkedArray:
    """Převede sloupec na int64; u double zaokrouhlí (adjusted objemy nejsou celá čísla)."""
    if pa.types.is_integer(column.type):
        return pc.cast(column, pa.int64())
    return pc.cast(pc.round(pc.cast(column, pa.float64())), pa.int64())


def _split_by_month(table: pa.Table) -> dict[tuple[int, int], pa.Table]:
    """Rozdělí tabulku jednoho tickeru po měsících (UTC), vrátí kanonické schéma."""
    names = set(table.column_names)
    ticker_col = "ticker" if "ticker" in names else "symbol"
    trades_col = "transactions" if "transactions" in names else "trades"
    missing = {"timestamp", "open", "high", "low", "close", "volume", "vwap"} - names
    if missing:
        raise ValueError(f"chybí sloupce: {sorted(missing)}")
    if trades_col not in names:
        raise ValueError(f"chybí sloupec trades/transactions")

    # Timestamp má v různých souborech ms / us / ns — sjednotí se na ns (int64).
    ts_ns = pc.cast(
        pc.cast(table.column("timestamp"), pa.timestamp("ns", tz="UTC")), pa.int64()
    )
    canonical = pa.table({
        "ticker": pc.cast(table.column(ticker_col), pa.string()),
        "window_start": ts_ns,
        "open": pc.cast(table.column("open"), pa.float64()),
        "high": pc.cast(table.column("high"), pa.float64()),
        "low": pc.cast(table.column("low"), pa.float64()),
        "close": pc.cast(table.column("close"), pa.float64()),
        "volume": _to_int64(table.column("volume")),
        "transactions": _to_int64(table.column(trades_col)),
        "vwap": pc.cast(table.column("vwap"), pa.float64()),
    })

    ts_utc = pc.cast(ts_ns, pa.timestamp("ns", tz="UTC"))
    keys = list(zip(pc.year(ts_utc).to_pylist(), pc.month(ts_utc).to_pylist()))
    buckets: dict[tuple[int, int], list[int]] = {}
    for index, key in enumerate(keys):
        buckets.setdefault(key, []).append(index)
    return {
        key: canonical.take(pa.array(indices, pa.int64()))
        for key, indices in buckets.items()
    }


def _write_month(path: Path, tables: list[pa.Table]) -> int:
    """Přidá tabulky do měsíčního souboru (read -> concat -> sort -> tmp -> replace)."""
    if path.is_file():
        tables = [pq.read_table(path), *tables]
    table = (
        pa.concat_tables(tables, promote_options="default")
        if len(tables) > 1 else tables[0]
    )
    table = table.sort_by(
        [("ticker", "ascending"), ("window_start", "ascending")]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    pq.write_table(
        table, tmp, compression="zstd", row_group_size=_ROW_GROUP_SIZE,
    )
    tmp.replace(path)
    return table.num_rows


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Převede denní OHLCV po tickerech na měsíční parquet "
                    "(formát update_daily z massive_data.py).",
    )
    parser.add_argument("--source", type=Path, default=_DEFAULT_SOURCE,
                        help=f"zdrojový adresář (default {_DEFAULT_SOURCE})")
    parser.add_argument("--root", type=Path, default=None,
                        help="cílový root_folder (default z config.yaml)")
    parser.add_argument("--chunk", type=int, default=2000,
                        help="kolik tickerů zpracovat najednou (default 2000)")
    parser.add_argument("--limit", type=int, default=None,
                        help="jen prvních N tickerů z každého trhu (zkouška)")
    parser.add_argument("--markets", nargs="+", default=None,
                        help="jen tyto trhy (default všechny nalezené)")
    parser.add_argument("--force", action="store_true",
                        help="smazat existující měsíční soubory a začít od nuly")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    cfg = md.load_config()
    root = (args.root or cfg.root_folder).expanduser().resolve()
    log = md.setup_logging(root / "logs")

    if not args.source.is_dir():
        log.error("Zdrojový adresář neexistuje: %s", args.source)
        return 1

    dirs = sorted(p for p in args.source.glob("symbol=*") if p.is_dir())
    if not dirs:
        log.error("Ve %s nejsou žádné adresáře symbol=*", args.source)
        return 1

    by_market: dict[str, list[Path]] = {}
    for directory in dirs:
        by_market.setdefault(_market_of(directory.name), []).append(directory)

    markets = sorted(by_market)
    if args.markets:
        wanted = set(args.markets)
        markets = [m for m in markets if m in wanted]
        if not markets:
            log.error("Nic k převodu — zadané trhy %s v datech nejsou.", args.markets)
            return 1

    log.info(
        "Zdroj %s: %d tickerů (%s) -> cíl %s",
        args.source, len(dirs),
        ", ".join(f"{m}: {len(by_market[m])}" for m in markets), root,
    )

    # Výstup musí být prázdný — jinak by merge data zdvojil.
    existing = sorted(
        path
        for market in markets
        for path in (root / f"market={market}" / "timeframe=1D").glob("year=*/month=*.parquet")
    )
    if existing and not args.force:
        log.error(
            "V cíli už je %d měsíčních souborů (např. %s) — převod by data zdvojil. "
            "Spusť znovu s --force, pokud je má přepsat.",
            len(existing), existing[0],
        )
        return 1
    for path in existing:
        path.unlink()
    if existing:
        log.warning("Smazáno %d existujících měsíčních souborů (--force).", len(existing))

    source_rows = 0
    for market in markets:
        market_dirs = by_market[market]
        if args.limit:
            market_dirs = market_dirs[:args.limit]
        log.info("Trh %s: %d tickerů, chunk %d", market, len(market_dirs), args.chunk)

        for offset in range(0, len(market_dirs), args.chunk):
            chunk = market_dirs[offset:offset + args.chunk]
            by_month: dict[tuple[int, int], list[pa.Table]] = {}

            for directory in chunk:
                path = directory / _FILENAME
                if not path.is_file():
                    log.warning("Chybí %s — přeskakuji", path)
                    continue
                try:
                    split = _split_by_month(pq.read_table(path))
                except Exception as exc:
                    log.error("Soubor %s se nepodařilo převést: %s — přeskakuji", path, exc)
                    continue
                for key, piece in split.items():
                    by_month.setdefault(key, []).append(piece)
                    source_rows += piece.num_rows

            for (year, month), tables in sorted(by_month.items()):
                dest = _month_path(root, market, year, month)
                rows = _write_month(dest, tables)
                log.info("Uloženo %s (%d řádků v měsíci)", dest, rows)

            log.info(
                "[%s] %d/%d tickerů hotovo, %d řádků",
                market, offset + len(chunk), len(market_dirs), source_rows,
            )

    # --- kontrola: počet řádků musí sedět a nikde nesmí být duplicity -------
    written_rows = 0
    duplicates = 0
    for market in markets:
        for path in sorted(
            (root / f"market={market}" / "timeframe=1D").glob("year=*/month=*.parquet")
        ):
            table = pq.read_table(path, columns=list(_COLUMNS))
            pairs = table.select(["ticker", "window_start"])
            unique = pairs.group_by(["ticker", "window_start"]).aggregate([]).num_rows
            dupes = table.num_rows - unique
            duplicates += dupes
            written_rows += table.num_rows
            log.info(
                "Kontrola %s: %d řádků, %d duplicit", path, table.num_rows, dupes,
            )

    if written_rows != source_rows:
        log.error(
            "Nesouhlasí počet řádků: zdroj %d, výstup %d", source_rows, written_rows,
        )
        return 1
    if duplicates:
        log.error("Ve výstupu je %d duplicitních (ticker, window_start).", duplicates)
        return 1

    log.info(
        "Hotovo: %d tickerů, %d řádků, %d měsíčních souborů, 0 duplicit.",
        sum(len(by_market[m][:args.limit] if args.limit else by_market[m]) for m in markets),
        written_rows,
        sum(len(list((root / f"market={m}" / "timeframe=1D").glob("year=*/month=*.parquet")))
            for m in markets),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
