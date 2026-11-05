"""massive_data.py — Konfigurace projektu (config.yaml + secrets.yaml).

Skript aktualizuje lokální data z Massive pro stocks, crypto a forex.
Data se aktualizují dvěma způsoby:
1. subscription=True → flat soubory (csv.gz) se stahují z S3 a převádějí do parquet (viz update_flat_files()).
2. subscription=False → denní agregace se stahují přes REST API (viz update_daily()).

Rozdělení konfigurace:
- config.yaml  — ne-tajná konfigurace, bezpečná pro verzování v gitu.
- secrets.yaml — přístupové klíče, NIKDY neverzovat (viz .gitignore).

"""

import logging
import sys
import time
from dataclasses import dataclass, field, fields
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml
from dateutil.relativedelta import relativedelta

VALID_MARKETS = ("stocks", "crypto", "fx")
VALID_TIMEFRAME = ("1D", "1m")

REPO_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = REPO_ROOT / "config.yaml"
SECRETS_PATH = REPO_ROOT / "secrets.yaml"

# Klíče v secrets.yaml mají tento prefix (např. MASSIVE_API_KEY);
# pole v AppConfig se jmenují bez něj (api_key).
_SECRETS_PREFIX = "MASSIVE_"


def _is_placeholder(value) -> bool:
    """True, pokud je hodnota nevyplněný placeholder (např. '<FLATFILES_ACCESS_KEY>')."""
    return isinstance(value, str) and value.startswith("<") and value.endswith(">")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
# Název log souboru v adresáři, který dostane setup_logging(log_dir).
_LOG_FILE_NAME = "massive_data.log"


def setup_logging(log_dir: Path | None = None) -> logging.Logger:
    """Nastaví logging a vrátí logger pro volající modul.

    Zprávy jdou vždy na stdout. Pokud je zadán `log_dir`, přidá se navíc
    FileHandler do {log_dir}/{_LOG_FILE_NAME} (UTF-8) — volá se po načtení
    konfigurace, protože teprve tehdy je známý root_folder. Opakované volání
    se stejným adresářem handler neduplikuje.

    Log soubor není rotovaný: při týdenním spouštění naroste pomalu, a kdyby
    to vadilo, stačí ho smazat — nový se vytvoří při dalším běhu.
    """
    logging.basicConfig(
        level=logging.INFO,
        format=_LOG_FORMAT,
        datefmt=_LOG_DATEFMT,
    )
    logger = logging.getLogger(__name__)

    if log_dir is not None:
        root = logging.getLogger()
        path = (Path(log_dir) / _LOG_FILE_NAME).resolve()
        already = any(
            isinstance(handler, logging.FileHandler)
            and Path(handler.baseFilename) == path
            for handler in root.handlers
        )
        if not already:
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(path, encoding="utf-8")
            handler.setFormatter(logging.Formatter(_LOG_FORMAT, _LOG_DATEFMT))
            root.addHandler(handler)
            logger.info("Logy se zapisují také do %s", path)

    return logger


log = setup_logging()


# ---------------------------------------------------------------------------
# AppConfig
# ---------------------------------------------------------------------------

@dataclass
class AppConfig:
    """Typovaná konfigurace + secrets pro skripty projektu.

    Načtení ze souborů: load_config().
    """

    # --- config.yaml ---
    subscription: bool = False
    history_months: int = 120
    markets: list[str] = field(default_factory=lambda: ["stocks", "crypto", "fx"])
    retries: int = 3
    root_folder: Path = Path("~/trading_data/massive/").expanduser()
    sec_contact_email: str = "your@email.com"

    # --- secrets.yaml ---
    # Optional, protože ne každý skript všechny potřebuje — konkrétní klíč
    # se ověřuje až v místě použití, viz require_secret().
    api_key: str | None = None
    flatfiles_access_key: str | None = None
    flatfiles_secret_key: str | None = None

    def require_secret(self, field_name: str) -> str:
        """Vrátí hodnotu secret pole, nebo skončí se srozumitelnou chybou.

        Volejte až v místě, kde je konkrétní klíč skutečně potřeba — ne
        každý skript potřebuje každý secret (viz docstring modulu).
        """
        value = getattr(self, field_name)
        if not value or _is_placeholder(value):
            yaml_key = f"{_SECRETS_PREFIX}{field_name.upper()}"
            log.error(
                "Chybí secret '%s' — nastav ho v %s. "
                "Tento soubor NENÍ verzován (viz .gitignore).",
                yaml_key, SECRETS_PATH.name,
            )
            sys.exit(1)
        return value

    def flatfiles_dest(self, market: str, timeframe: str) -> Path:
        """Kořen cesty pro flat soubory: root_folder/flat_files/{market}/{timeframe}/.

        Struktura podle config.yaml:
        [root_folder]/flat_files/{market}/{timeframe}/{year}/{month}/{date}.csv.gz
        """
        return self.root_folder / "flat_files" / market / timeframe


# ---------------------------------------------------------------------------
# Načtení z YAML
# ---------------------------------------------------------------------------

def load_config(
    config_path: Path = CONFIG_PATH,
    secrets_path: Path = SECRETS_PATH,
) -> AppConfig:
    """Načte config.yaml + secrets.yaml a vrátí typovaný AppConfig.

    Chybějící config.yaml je chyba. Chybějící secrets.yaml neznamená
    chybu — secret klíče jen nebudou dostupné (viz require_secret()).
    """

    def _read_yaml(path: Path, required: bool) -> dict:
        """Načte YAML soubor a vrátí dict; prázdný soubor vrátí {}."""
        if not path.is_file():
            if required:
                log.error("Konfigurační soubor nenalezen: %s", path)
                sys.exit(1)
            log.warning(
                "%s nenalezen — secret klíče nebudou dostupné.", path.name
            )
            return {}
        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}

    config = _read_yaml(config_path, required=True)
    secrets = _read_yaml(secrets_path, required=False)

    # secrets.yaml používá klíče s prefixem MASSIVE_ (např. MASSIVE_API_KEY),
    # pole AppConfig se jmenují bez něj (api_key). Placeholder "<...>"
    # znamená nevyplněný secret a ukládá se jako None.
    secrets_norm = {}
    for key, value in secrets.items():
        if not isinstance(key, str) or not key.startswith(_SECRETS_PREFIX):
            log.warning(
                "Ignoruji klíč %r v %s — očekáván prefix %r.",
                key, secrets_path.name, _SECRETS_PREFIX,
            )
            continue
        field_name = key[len(_SECRETS_PREFIX):].lower()
        secrets_norm[field_name] = None if _is_placeholder(value) else value

    merged = config | secrets_norm

    # Kontrola překlepů: neznámý klíč = chyba.
    unknown = set(merged) - {f.name for f in fields(AppConfig)}
    if unknown:
        log.error(
            "Neznámé klíče v %s / %s: %s",
            config_path.name, secrets_path.name, ", ".join(map(str, unknown)),
        )
        sys.exit(1)

    # Převody a validace hodnot, které dataclass sám neudělá.
    if "root_folder" in merged:
        merged["root_folder"] = Path(merged["root_folder"]).expanduser()

    unknown_markets = set(merged.get("markets", [])) - set(VALID_MARKETS)
    if unknown_markets:
        log.error(
            "Neznámé trhy %s; povolené: %s",
            ", ".join(map(str, unknown_markets)), list(VALID_MARKETS),
        )
        sys.exit(1)

    return AppConfig(**merged)


# ---------------------------------------------------------------------------
# Flat files (předplatné subscription=True)
# ---------------------------------------------------------------------------

# Cesty na S3 podle dokumentace Massive (https://massive.com/docs/flat-files/).
# Před prvním ostrým spuštěním ověřte název bucketu v quickstartu.
FLAT_FILES_BUCKET = "files.massive.com"  # TODO: potvrdit podle dokumentace
# Trh v config.yaml → adresář na S3 (fx je v dokumentaci Massive jako "forex").
_FLAT_MARKET_DIR = {"stocks": "stocks", "crypto": "crypto", "fx": "forex"}
# Timeframe → adresář s agregacemi na S3.
_FLAT_AGGS_DIR = {"1D": "day-aggregates", "1m": "minute-aggregates"}
# Velikost row group při zápisu parquet.
_ROW_GROUP_SIZE = 1_000_000


class _NotOnS3(Exception):
    """Soubor (zatím) není na S3 — např. data pro dnešní den ještě nezveřejněna."""


def update_flat_files(cfg: AppConfig, timeframe: str = "1D") -> None:
    """Aktualizuje flat soubory pro daný timeframe ('1D' nebo '1m').

    Pro každý trh z cfg.markets:
    1. Určí start_date: dnešní datum minus cfg.history_months měsíců.
       Pokud už lokálně existují stažené soubory, start_date =
       poslední stažené datum + 1 den.
    2. Stáhne chybějící soubory z Massive (viz
       https://massive.com/docs/flat-files/quickstart) a uloží je do
       [root_folder]/flat_files/{market}/{timeframe}/{year}/{month}/{date}.csv.gz
    3. Každý stažený soubor převede do parquet a uloží do HIVE struktury:
       [root_folder]/{market}/timefram=?/year=?/month=?/{date}.parquet
       Zápis: zstd komprese, row groups, řádky seřazené podle *ticker*
       a uvnitř podle *timestamp* — row groups se tak shlukují po tickerech.
    """

    def _each_day(start: date, end: date):
        """Generátor dnů od start do end (včetně)."""
        day = start
        while day <= end:
            yield day
            day += timedelta(days=1)

    def _last_downloaded_date(dest_dir: Path) -> date | None:
        """Nejnovější datum z lokálních souborů {year}/{month}/{date}.csv.gz."""
        latest = None
        if not dest_dir.is_dir():
            return None
        for f in dest_dir.rglob("*.csv.gz"):
            try:
                day = date.fromisoformat(f.name.split(".")[0])
            except ValueError:
                continue
            if latest is None or day > latest:
                latest = day
        return latest

    def _s3_client(access_key: str, secret_key: str):
        """Vytvoří boto3 S3 klienta (lazy import kvůli závislosti)."""
        try:
            import boto3
        except ImportError:
            log.error("Chybí boto3 — nainstaluj: pip install boto3")
            sys.exit(1)
        session = boto3.Session(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
        return session.client("s3")

    def _download_with_retries(s3, key: str, dest: Path, retries: int) -> None:
        """Stáhne jeden soubor z S3; při chybě opakuje (až `retries` pokusů).

        Chyba 404/NoSuchKey znamená, že soubor zatím nebyl zveřejněn —
        vyvolá _NotOnS3 (dnešní data bývají dostupná až druhý den).
        """
        for attempt in range(1, retries + 1):
            try:
                s3.download_file(FLAT_FILES_BUCKET, key, str(dest))
                log.info("Staženo %s -> %s", key, dest)
                return
            except Exception as exc:
                dest.unlink(missing_ok=True)  # odstraní případný nedokončený soubor
                if _is_missing_on_s3(exc):
                    raise _NotOnS3(key) from exc
                if attempt >= retries:
                    log.error(
                        "Soubor %s se nepodařilo stáhnout po %d pokusech: %s",
                        key, retries, exc,
                    )
                    sys.exit(1)
                log.warning(
                    "Pokus %d/%d selhal pro %s: %s — zkouším znovu.",
                    attempt, retries, key, exc,
                )
                time.sleep(2 ** attempt)

    def _is_missing_on_s3(exc: Exception) -> bool:
        """True, pokud chyba znamená 'soubor na S3 neexistuje' (404/NoSuchKey)."""
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        return code in ("404", "NoSuchKey", "NotFound")

    def _csv_gz_to_parquet(src: Path, dest: Path) -> None:
        """Převede csv.gz na parquet: zstd, row groups, řádky dle tickeru a timestamp.

        Seřazení zajistí, že se row groups shlukují po tickerech (viz MASSIVE.md).
        """
        try:
            import pyarrow.csv as pv
            import pyarrow.parquet as pq
        except ImportError:
            log.error("Chybí pyarrow — nainstaluj: pip install pyarrow")
            sys.exit(1)

        table = pv.read_csv(src)  # gzip detekuje z přípony .csv.gz
        ts_col = "timestamp" if "timestamp" in table.column_names else "window_start"
        table = table.sort_by([("ticker", "ascending"), (ts_col, "ascending")])
        dest.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, dest, compression="zstd", row_group_size=_ROW_GROUP_SIZE)
        log.info("Převedeno %s -> %s", src, dest)

    if timeframe not in VALID_TIMEFRAME:
        log.error(
            "Nepodporovaný timeframe %r; povolené: %s",
            timeframe, list(VALID_TIMEFRAME),
        )
        sys.exit(1)

    if not cfg.subscription:
        log.info("subscription=False — flat soubory se nestahují.")
        return

    s3 = _s3_client(
        cfg.require_secret("flatfiles_access_key"),
        cfg.require_secret("flatfiles_secret_key"),
    )

    start_date = date.today() - relativedelta(months=cfg.history_months)
    today = date.today()

    for market in cfg.markets:
        dest_dir = cfg.flatfiles_dest(market, timeframe)

        # Poslední stažené datum → navázat hned po něm.
        last = _last_downloaded_date(dest_dir)
        start = last + timedelta(days=1) if last else start_date
        if start > today:
            log.info("Trh %s je aktuální — není co stahovat.", market)
            continue

        for day in _each_day(start, today):
            s3_key = (
                f"{_FLAT_MARKET_DIR[market]}/{_FLAT_AGGS_DIR[timeframe]}"
                f"/{day.year}/{day.month:02d}/{day.isoformat()}.csv.gz"
            )
            csv_path = (
                dest_dir / f"{day.year}" / f"{day.month:02d}"
                / f"{day.isoformat()}.csv.gz"
            )
            parquet_path = (
                cfg.root_folder / market / f"timeframe={timeframe}"
                / f"year={day.year}" / f"month={day.month:02d}" / f"{day.isoformat()}.parquet"
            )

            if not csv_path.exists():
                try:
                    _download_with_retries(s3, s3_key, csv_path, cfg.retries)
                except _NotOnS3:
                    log.info(
                        "%s zatím není na S3 — novější dny neexistují, končím.",
                        s3_key,
                    )
                    break

            _csv_gz_to_parquet(csv_path, parquet_path)


# ---------------------------------------------------------------------------
# REST API (subscription=False)
# ---------------------------------------------------------------------------

# Prodleva mezi dotazy: bez předplatného je limit 5 dotazů za minutu,
# 12.5s je bezpečná hodnota proti chybě 429 (viz config.yaml).
_REST_DELAY_SECONDS = 12.5
# Hloubka historie při prvním naplnění přes REST (v měsících).
_REST_HISTORY_MONTHS = 23
# Trh → locale pro grouped daily endpoint.
_REST_LOCALE = {"stocks": "us", "crypto": "global", "fx": "global"}
# Trhy, které mají o víkendu zavřeno (crypto obchoduje 7/7).
_REST_WEEKEND_CLOSED = ("stocks", "fx")
# Velikost row group u denních souborů. Řádky jsou seřazené podle tickeru,
# takže row group obsahuje souvislý rozsah tickerů a čtení jednoho tickeru
# přeskočí ostatní skupiny.
_DAY_ROW_GROUP_SIZE = 5_000


class _RestError(Exception):
    """Den se nepodařilo stáhnout ani po cfg.retries pokusech."""


def _bars_to_table(rows: list[dict]):
    """Převede stažené bary na tabulku s pevným schématem.

    Bez explicitního schématu si pyarrow odvodí typy z hodnot: adjusted volume
    bývá jednou int64, jednou double — a `concat_tables(promote)` int64 vs
    double nesjednotí, takže merge měsíce spadne na
    "Field volume has incompatible types". Volume a transactions proto
    zaokrouhlujeme na int64; tím je schéma stejné jako u flat files i u
    konvertovaných dat (convert_ohlc_daily.py).
    """
    import pyarrow as pa
    import pyarrow.compute as pc

    def _floats(name: str):
        return pa.array([row.get(name) for row in rows], pa.float64())

    def _ints(name: str):
        return pc.cast(pc.round(_floats(name)), pa.int64())

    return pa.table({
        "ticker": pa.array([row.get("ticker") for row in rows], pa.string()),
        "window_start": pa.array([row.get("window_start") for row in rows], pa.int64()),
        "open": _floats("open"),
        "high": _floats("high"),
        "low": _floats("low"),
        "close": _floats("close"),
        "volume": _ints("volume"),
        "transactions": _ints("transactions"),
        "vwap": _floats("vwap"),
    })


def last_downloaded_date(cfg: AppConfig, market: str, timeframe: str) -> date | None:
    """Nejnovější datum z lokálních parquet souborů daného trhu."""
    market_dir = cfg.root_folder / f"market={market}" / f"timeframe={timeframe}"
    latest = None
    if not market_dir.is_dir():
        return None
    for f in market_dir.rglob("*.parquet"):
        try:
            day = date.fromisoformat(f.stem)
        except ValueError:
            continue
        if latest is None or day > latest:
            latest = day
    return latest

def update_daily(cfg: AppConfig) -> None:
    """Aktualizuje parquet soubory pro timeframe '1D' (denní agregace) pomocí REST API.

    Používá se při subscription=False — s předplatným se denní data stahují
    jako flat soubory (viz update_flat_files).

    Pro každý trh z cfg.markets:
    1. Zjistí poslední stažené datum z měsíčních souborů
       [root_folder]/market=?/timeframe=1D/year=?/month=??.parquet (maximum
       window_start) a naváže hned po něm. Pokud žádné soubory nejsou, začne
       _REST_HISTORY_MONTHS měsíců zpět.
    2. Postupně stáhne všechny chybějící dny (u stocks/fx mimo víkendy) —
       jeden dotaz vrátí grouped daily aggs za celý trh, cca 15 000 tickerů.
    3. Stažené dny se hromadí v bufferu a do souboru se zapíší najednou,
       jakmile se přejde do dalšího měsíce: existující měsíční soubor se
       načte, nové dny se k němu přidají a soubor se přepíše. V paměti je tak
       nejvýš jeden měsíc dat. Duplicity nevznikají, protože se začíná až za
       posledním uloženým dnem (viz bod 1).
    4. Zápis: zstd komprese, řádky seřazené podle *ticker* a uvnitř podle
       *window_start*, row groups po menších blocích — soubor se tak dá
       číst po tickerech. Zápis jde přes dočasný soubor a Path.replace(),
       aby pád uprostřed nenechal měsíc bez už uložených dnů.

    Sloupce odpovídají flat souborům (window_start, transactions), aby obě
    cesty stahování dávaly stejné schéma; navíc je vwap, který REST vrací.
    """

    def _month_path(market: str, day: date) -> Path:
        """Cesta k měsíčnímu parquet souboru v hive struktuře."""
        return (
            cfg.root_folder / f"market={market}" / "timeframe=1D"
            / f"year={day.year}" / f"month={day.month:02d}.parquet"
        )

    def _last_downloaded_day(market: str) -> date | None:
        """Nejnovější uložený den trhu = maximum window_start v posledním měsíci.

        Čte se jen jeden sloupec nejnovějšího souboru; názvy souborů
        (month=??.parquet) datum neobsahují, takže se musí podívat do dat.
        """
        market_dir = cfg.root_folder / f"market={market}" / "timeframe=1D"
        if not market_dir.is_dir():
            return None
        files = sorted(market_dir.glob("year=*/month=*.parquet"))
        if not files:
            return None

        import pyarrow.parquet as pq

        column = pq.read_table(files[-1], columns=["window_start"]).column(
            "window_start"
        )
        if len(column) == 0:
            return None
        newest_ns = max(column.to_pylist())
        return datetime.fromtimestamp(
            newest_ns / 1_000_000_000, tz=timezone.utc
        ).date()

    def _rest_client():
        """Vytvoří Massive REST klienta (lazy import kvůli závislosti)."""
        try:
            from massive import RESTClient
        except ImportError:
            log.error("Chybí massive — nainstaluj: pip install massive")
            sys.exit(1)
        return RESTClient(cfg.require_secret("api_key"))

    def _fetch_day(client, market: str, day: date) -> list[dict]:
        """Stáhne grouped daily aggs pro celý trh za jeden den.

        Jeden dotaz vrátí cca 15 000 tickerů. Prázdný seznam znamená, že
        trh byl zavřený (svátek) — to není chyba.
        """
        bars = client.get_grouped_daily_aggs(
            date=day.isoformat(),
            locale=_REST_LOCALE[market],
            market_type=market,
            adjusted=True,
        )
        return [
            {
                "ticker": bar.ticker,
                # window_start = Unix ns, stejně jako u flat souborů
                "window_start": bar.timestamp * 1_000_000,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
                "transactions": getattr(bar, "transactions", None),
                "vwap": getattr(bar, "vwap", None),
            }
            for bar in (bars or [])
        ]

    def _fetch_with_retries(client, market: str, day: date) -> list[dict]:
        """Stáhne den s opakováním při chybě (až cfg.retries pokusů).

        U chyby 429 se čeká déle — limit je 5 dotazů za minutu.
        """
        last_exc = None
        for attempt in range(1, cfg.retries + 1):
            try:
                return _fetch_day(client, market, day)
            except Exception as exc:
                last_exc = exc
                if attempt >= cfg.retries:
                    break
                wait = 60.0 if "429" in str(exc) else 2.0 ** attempt
                log.warning(
                    "Pokus %d/%d selhal pro %s (%s): %s — čekám %.0fs.",
                    attempt, cfg.retries, day, market, exc, wait,
                )
                time.sleep(wait)
        raise _RestError(
            f"den {day} ({market}) se nepodařilo stáhnout: {last_exc}"
        )

    def _save_month(market: str, day: date, by_day: dict[date, list[dict]]) -> Path:
        """Přidá stažené dny do měsíčního parquet souboru.

        Existující soubor se načte, nové dny se k němu přičtou a výsledek se
        setřídí podle *ticker* a *window_start*. Zápis jde přes dočasný soubor
        a Path.replace(), takže pád uprostřed nezničí už uložené dny měsíce.
        """
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError:
            log.error("Chybí pyarrow — nainstaluj: pip install pyarrow")
            sys.exit(1)

        dest = _month_path(market, day)
        tables = []
        if dest.is_file():
            tables.append(pq.read_table(dest))
        # Pevné schéma (viz _bars_to_table) — jinak concat měsíce spadne na
        # int64 vs double u adjusted volume.
        tables.append(_bars_to_table(
            [row for day_rows in by_day.values() for row in day_rows]
        ))
        # promote_options: první zápis může mít sloupec celý NULL (typ null),
        # při dalším běhu už int64 — bez povýšení by concat spadl.
        table = (
            pa.concat_tables(tables, promote_options="default")
            if len(tables) > 1 else tables[0]
        )
        table = table.sort_by(
            [("ticker", "ascending"), ("window_start", "ascending")]
        )

        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        pq.write_table(
            table, tmp, compression="zstd",
            row_group_size=_DAY_ROW_GROUP_SIZE,
        )
        tmp.replace(dest)
        return dest

    if cfg.subscription:
        log.info(
            "subscription=True — denní data se stahují jako flat soubory, "
            "REST se nepoužívá."
        )
        return

    client = _rest_client()
    default_start = date.today() - relativedelta(months=_REST_HISTORY_MONTHS)
    # Dnešní agregace ještě nejsou uzavřené — končíme včerejškem.
    end = date.today() - timedelta(days=1)

    # Buffer pro stažené dny právě zpracovávaného měsíce. Jakmile se přejde do
    # dalšího měsíce, buffer se zapíše do měsíčního souboru a uvolní — v paměti
    # tak zůstává nejvýš jeden měsíc dat, i když se doplňuje 23 měsíců historie.
    buffer: dict[date, list[dict]] = {}
    buffer_month: tuple[int, int] | None = None

    def _flush(market: str) -> None:
        """Zapíše nasbírané dny do měsíčního souboru (prázdný buffer nic nedělá)."""
        nonlocal buffer, buffer_month
        if not buffer:
            return
        dest = _save_month(market, min(buffer), buffer)
        log.info(
            "Uloženo %s (%d dní, %d řádků)",
            dest, len(buffer), sum(len(rows) for rows in buffer.values()),
        )
        buffer = {}
        buffer_month = None

    for market in cfg.markets:
        last = _last_downloaded_day(market)
        start = last + timedelta(days=1) if last else default_start
        if start > end:
            log.info("Trh %s je aktuální — není co stahovat.", market)
            continue

        log.info("Trh %s: stahuji od %s do %s.", market, start, end)
        day = start
        while day <= end:
            if market in _REST_WEEKEND_CLOSED and day.weekday() >= 5:
                day += timedelta(days=1)  # zavřeno, dotaz by byl zbytečný
                continue

            # Přechod do dalšího měsíce — předchozí měsíc se uloží najednou.
            if buffer_month is not None and (day.year, day.month) != buffer_month:
                _flush(market)

            try:
                rows = _fetch_with_retries(client, market, day)
            except _RestError as exc:
                log.error(
                    "%s — končím s trhem %s, příští běh naváže odtud.", exc, market
                )
                break

            if rows:
                buffer[day] = rows
                buffer_month = (day.year, day.month)
            else:
                log.info("%s: žádná data (svátek nebo zavřený trh).", day)

            time.sleep(_REST_DELAY_SECONDS)
            day += timedelta(days=1)

        # I po selhání se uloží, co se stihlo stáhnout — jinak by přišla
        # o data a příští běh by je stahoval znovu (a zbytečně pálil kvótu).
        _flush(market)


# Maximální počet záznamů, který REST API vrátí v jednom dotazu.
_REST_MAX_RECORDS = 50_000
# Délka jednoho dotazovaného úseku ve dnech. 31 dní × 24 h × 60 min = 44 640
# záznamů na ticker, tedy i pro trhy obchodující 24/7 pod limitem 50 000.
_MINUTE_CHUNK_DAYS = 31
# Row group u minutových souborů ≈ jeden obchodní den jednoho tickeru
# (stocks 390 minut, crypto/fx 1440), takže skupina zůstane v rozsahu
# několika málo tickerů a čtení po tickerech přeskočí ostatní.
_MINUTE_ROW_GROUP_SIZE = 1_500
# Soubor se seznamem tickerů pro daný trh (jeden na řádek, '#' = komentář).
_MINUTE_TICKER_FILE = "1m_{market}.txt"


def update_minute(cfg: AppConfig) -> None:
    """Aktualizuje parquet soubory pro timeframe '1m' pomocí REST API.

    Používá se při subscription=False — s předplatným se data stahují jako
    flat soubory (viz update_flat_files).

    Použité REST API:
    https://massive.com/docs/rest/stocks/aggregates/custom-bars

    Pro každý trh z cfg.markets:
    1. Zjistí poslední stažené datum z
       [root_folder]/market=?/timeframe=1m/year=?/month=?/{date}.parquet
       a naváže hned po něm (viz last_downloaded_date). Pokud žádné soubory
       neexistují, začne 31 dní zpět. REST API vrátí maximálně 50 000 záznamů
       na dotaz, proto se delší období dělí na úseky (viz _chunks).
    2. Pro každý úsek stáhne minutová data po jednotlivých tickerech. Seznam
       tickerů je v souborech 1m_{market}.txt, jeden ticker na řádek.
       Všechny tickery se drží v jednom bufferu. Víkendy u stocks/fx se
       neřeší zvlášť — v dotazovaném úseku prostě nemají žádné bary.
       Ticker, který se nepodaří stáhnout ani po cfg.retries pokusech, se
       přeskočí a zaloguje (nesmí zablokovat celý trh) — jeho data za daný
       úsek v úložišti chybí, na konci trhu se vypíše jejich seznam.
    3. Zápis: zstd komprese, řádky seřazené podle *ticker* a uvnitř podle
       *window_start*, row groups po menších blocích. Pro každý den vznikne
       jeden soubor se všemi tickery, které pro něj mají data.

    Sloupce odpovídají flat souborům (window_start, transactions), aby obě
    cesty stahování dávaly stejné schéma; navíc je vwap, který REST vrací.
    """

    def _load_tickers(market: str) -> list[str]:
        """Načte seznam tickerů z 1m_{market}.txt (jeden na řádek)."""
        path = REPO_ROOT / _MINUTE_TICKER_FILE.format(market=market)
        if not path.is_file():
            log.error(
                "Soubor se seznamem tickerů nenalezen: %s — trh %s přeskakuji.",
                path, market,
            )
            return []
        tickers = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not tickers:
            log.error("Soubor %s je prázdný — trh %s přeskakuji.", path, market)
        return tickers

    def _chunks(start: date, end: date):
        """Rozdělí období na úseky po _MINUTE_CHUNK_DAYS dnech.

        REST API má omezení 50 000 záznamů na dotaz — delší období by
        u jednoho tickeru vrátilo jen jeho část.
        """
        chunk_start = start
        while chunk_start <= end:
            chunk_end = min(
                chunk_start + timedelta(days=_MINUTE_CHUNK_DAYS - 1), end
            )
            yield chunk_start, chunk_end
            chunk_start = chunk_end + timedelta(days=1)

    def _rest_client():
        """Vytvoří Massive REST klienta (lazy import kvůli závislosti)."""
        try:
            from massive import RESTClient
        except ImportError:
            log.error("Chybí massive — nainstaluj: pip install massive")
            sys.exit(1)
        return RESTClient(cfg.require_secret("api_key"))

    def _fetch_ticker(
        client, ticker: str, from_day: date, to_day: date
    ) -> dict[date, list[dict]]:
        """Stáhne minutové bary jednoho tickeru pro daný úsek.

        Vrací bary rozdělené podle dne — jeden den = jeden budoucí parquet.
        Prázdný dict znamená, že ticker v úseku nemá data (delisted, svátek).
        """
        by_day: dict[date, list[dict]] = {}
        count = 0
        for bar in client.list_aggs(
            ticker=ticker,
            multiplier=1,
            timespan="minute",
            from_=from_day.isoformat(),
            to=to_day.isoformat(),
            limit=_REST_MAX_RECORDS,
            sort="asc",
        ):
            count += 1
            # Den bereme v UTC — denní agregace i flat soubory jsou v UTC.
            day = datetime.fromtimestamp(
                bar.timestamp / 1000, tz=timezone.utc
            ).date()
            by_day.setdefault(day, []).append({
                # Agg ticker nenese, použijeme ten, na který jsme se dotazovali
                "ticker": ticker,
                "window_start": bar.timestamp * 1_000_000,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": getattr(bar, "volume", None),
                "transactions": getattr(bar, "transactions", None),
                "vwap": getattr(bar, "vwap", None),
            })
        if count >= _REST_MAX_RECORDS:
            log.warning(
                "Ticker %s vrátil %d záznamů — narazili jsme na limit API, "
                "část dat úseku %s..%s může chybět.",
                ticker, count, from_day, to_day,
            )
        return by_day

    def _fetch_with_retries(
        client, ticker: str, from_day: date, to_day: date
    ) -> dict[date, list[dict]]:
        """Stáhne ticker s opakováním při chybě (až cfg.retries pokusů).

        U chyby 429 se čeká déle — limit je 5 dotazů za minutu.
        """
        last_exc = None
        for attempt in range(1, cfg.retries + 1):
            try:
                return _fetch_ticker(client, ticker, from_day, to_day)
            except Exception as exc:
                last_exc = exc
                if attempt >= cfg.retries:
                    break
                wait = 60.0 if "429" in str(exc) else 2.0 ** attempt
                log.warning(
                    "Pokus %d/%d selhal pro %s (%s..%s): %s — čekám %.0fs.",
                    attempt, cfg.retries, ticker, from_day, to_day, exc, wait,
                )
                time.sleep(wait)
        raise _RestError(
            f"ticker {ticker} ({from_day}..{to_day}) se nepodařilo stáhnout: "
            f"{last_exc}"
        )

    def _write_day(market: str, day: date, rows: list[dict]) -> Path:
        """Uloží jeden den do parquet: zstd, řazení dle tickeru, row groups."""
        try:
            import pyarrow.parquet as pq
        except ImportError:
            log.error("Chybí pyarrow — nainstaluj: pip install pyarrow")
            sys.exit(1)

        table = _bars_to_table(rows)
        table = table.sort_by(
            [("ticker", "ascending"), ("window_start", "ascending")]
        )
        dest = (
            cfg.root_folder / f"market={market}" / "timeframe=1m"
            / f"year={day.year}" / f"month={day.month:02d}"
            / f"{day.isoformat()}.parquet"
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(
            table, dest, compression="zstd",
            row_group_size=_MINUTE_ROW_GROUP_SIZE,
        )
        return dest

    if cfg.subscription:
        log.info(
            "subscription=True — minutová data se stahují jako flat soubory, "
            "REST se nepoužívá."
        )
        return

    client = _rest_client()
    # Dnešní minuta ještě není uzavřená — končíme včerejškem.
    end = date.today() - timedelta(days=1)

    for market in cfg.markets:
        tickers = _load_tickers(market)
        if not tickers:
            continue

        last = last_downloaded_date(cfg, market, "1m")
        start = (
            last + timedelta(days=1) if last
            else date.today() - timedelta(days=_MINUTE_CHUNK_DAYS)
        )
        if start > end:
            log.info("Trh %s je aktuální — není co stahovat.", market)
            continue

        log.info(
            "Trh %s: %d tickerů, stahuji od %s do %s.",
            market, len(tickers), start, end,
        )
        failed: list[str] = []
        for chunk_start, chunk_end in _chunks(start, end):
            # Buffer pro celý úsek: teprve až jsou hotové všechny tickery,
            # víme, že denní soubor je kompletní.
            buffer: dict[date, list[dict]] = {}
            for i, ticker in enumerate(tickers, 1):
                by_day = None
                try:
                    by_day = _fetch_with_retries(
                        client, ticker, chunk_start, chunk_end
                    )
                except _RestError as exc:
                    # Vadný ticker nesmí zablokovat celý trh — přeskočíme ho
                    # a jedeme dál. Data za tento úsek mu v úložišti chybí.
                    log.error(
                        "%s — ticker přeskakuji, jeho data za %s..%s "
                        "v úložišti nebudou.",
                        exc, chunk_start, chunk_end,
                    )
                    failed.append(ticker)

                if by_day is not None:
                    for day, rows in by_day.items():
                        buffer.setdefault(day, []).extend(rows)

                    log.info(
                        "[%d/%d] %s %s..%s: %d dní",
                        i, len(tickers), ticker, chunk_start, chunk_end,
                        len(by_day),
                    )

                time.sleep(_REST_DELAY_SECONDS)

            # Úsek je hotový — teprve teď se zapisují denní soubory.
            for day in sorted(buffer):
                dest = _write_day(market, day, buffer[day])
                log.info("Uloženo %s (%d řádků)", dest, len(buffer[day]))

        if failed:
            log.error(
                "Trh %s: přeskočeno %d tickerů, jejich data chybí — %s",
                market, len(failed), ", ".join(sorted(set(failed))),
            )


if __name__ == "__main__":
    cfg = load_config()
    # Teprve teď je známý root_folder — odsud se loguje i do [root_folder]/logs.
    setup_logging(cfg.root_folder / "logs")
    # update_flat_files(cfg, timeframe="1D")
    # update_flat_files(cfg, timeframe="1m")
    # Při subscription=False výše uvedené funkce nic nestahují a denní data
    # dotáhne REST; při subscription=True update_daily rovnou skončí.
    update_daily(cfg)
