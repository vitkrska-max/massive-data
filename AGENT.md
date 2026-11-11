# AGENT.md

This file provides guidance to coding agents (Claude Code, Codex, Hermes) when working with code in this repository. Obsahuje i **zadání projektu** (dřív samostatný `MASSIVE.md`).

## Projekt

`massive_data.py` stahuje OHLCV data z Massive (dříve Polygon.io) do lokálního parquet úložiště. Konfigurace `config.yaml` + `secrets.yaml`, zadání níže v sekci [Zadání](#zadání-dřív-massivemd).

**Vše v projektu je česky** — komentáře, docstringy, logy i názvy funkcí jsou české, výjimkou jsou jen názvy převzaté z API (`window_start`, `get_grouped_daily_aggs`). Drž se toho, včetně diakritiky v komentářích.

## Příkazy

```bash
python massive_data.py                           # flat files (1D + 1m) + update_daily
python _test_flat_files.py                       # offline, fake boto3
python _test_daily.py                            # offline, fake massive
python _test_minute.py                           # offline, fake massive
python _test_real_client.py                      # offline, PRAVÁ knihovna massive
python _test_tickers.py                          # offline, fake massive (update_tickers)
python _smoke_real_api.py [both|daily|minute]    # ŽIVĚ — spotřebuje API kvótu
python convert_ohlc_daily.py                     # jednorázově: /trading/ohlc -> měsíční parquet (offline)
git push                                         # záloha na GitHub (origin)
```

Testy nemají runner ani framework — každý soubor je samostatný skript a „spustit jeden test" znamená spustit ten soubor. Úspěch = výpis `VŠE OK` a exit 0; selhání = `AssertionError`. Testy si samy vytvářejí a uklízejí dočasné adresáře (`_test_*_root*`, `_test_minute_dir`).

Jen `_smoke_real_api.py` sahá na síť: `daily` = 1 dotaz, `minute` = 1 dotaz. Bez předplatného je limit 5 dotazů/minutu — **před spuštěním se zeptej**. Píše do `_smoke_root`, nikdy do skutečného `root_folder`.

## Architektura

Vše je v jediném modulu `massive_data.py` (~950 řádků), žádný balíček. Tři nezávislé cesty stahování, přepíná je `cfg.subscription`:

| `subscription` | funkce | zdroj dat |
|---|---|---|
| `True` | `update_flat_files(cfg, timeframe)` | S3 flat files, csv.gz → parquet |
| `False` | `update_daily(cfg)` | REST `get_grouped_daily_aggs` — 1 dotaz = celý trh za den |
| `False` | `update_minute(cfg)` | REST `list_aggs` — 1 dotaz = 1 ticker za úsek |
| obojí | `update_tickers(cfg, day, tickers)` | REST `get_ticker_details` — metadata o tickerech do `reference.sqlite` + denní snapshot |
| obojí | `update_sic_segments(cfg)` | SEC — tabulka SIC kódů (volá se z `update_tickers`, když je `sic_segments` prázdná) |

Konfigurace: `load_config()` čte `config.yaml` (povinný) + `secrets.yaml` (nepovinný) a vrací `@dataclass AppConfig`. Klíče v `secrets.yaml` mají prefix `MASSIVE_` (`MASSIVE_API_KEY` → `api_key`); hodnota `<...>` znamená nevyplněno → `None`. Neznámý klíč v YAML je chyba (`sys.exit(1)`) — při přidání pole do `AppConfig` funguje kontrola překlepů automaticky.

Načtení secretu se děje **až v místě použití** přes `cfg.require_secret("api_key")`, ne při načtení konfigurace.

**Po pádu běh navazuje, nestahuje od začátku** — dlouhé běhy (první naplnění, `update_minute`) mohou spadnout a nesmí spálit kvótu znovu. Žádný progress soubor neexistuje, postup se odvozuje z uložených dat:
- `update_minute` → `last_downloaded_date(cfg, market, timeframe)` (modulová funkce, pozná den z ISO názvů denních souborů)
- `update_daily` → vlastní vnořená `_last_downloaded_day(market)`: názvy měsíčních souborů datum neobsahují, takže bere maximum `window_start` z nejnovějšího `month=??.parquet`
- `update_flat_files` → vlastní vnořená `_last_downloaded_date(dest_dir)`, čte názvy csv.gz (csv.gz je zároveň cache — už stažený den se znovu netahá ze S3)
- `update_tickers` → hotové tickery ze snapshotu `{market}/tickers/{year}/{month}/{date}.parquet` **a** z dočasných partů `{market}/tickers/_tmp/{date}/part-*.parquet` (viz Metadata o tickerech níže)

Známá mezera: u `update_minute` se postupuje podle posledního uloženého dne, takže ticker, který v úseku selhal, se při dalším běhu už nedotáhne (jeho data za ten úsek chybí a jen se vypíšou). U `update_flat_files` zůstane bez parquet den, po jehož stažení proces spadl před převodem.

**Schéma parquet je u všech cest stejné** — `ticker, window_start, open, high, low, close, volume, transactions, vwap`. `window_start` je Unix **nanosekundy** (flat files je tak mají; REST vrací milisekundy, proto `bar.timestamp * 1_000_000`), `volume` a `transactions` jsou int64, ceny double. Sortuje se podle `ticker` a uvnitř podle `window_start`, aby se row groups shlukly po tickerech.

### Metadata o tickerech (`update_tickers`)

Pro každý trh z `cfg.markets` udržuje `{market}/tickers/reference.sqlite` (WAL, FK) a denní snapshot `{market}/tickers/{year}/{month}/{date}.parquet`. Zdroj dat je REST `get_ticker_details` (ticker overview), ukládá se **všechno, co endpoint vrací** — vnořené `address`/`branding` se rozbalují do plochých sloupců, takže snapshot má stejné schéma jako tabulka `tickers`.

- **Seznam tickerů** (první neprázdný zdroj vyhrává): parametr `tickers` → soubor z `cfg.tickers_info` (šablona s `{market}`, jeden ticker na řádek, `#` komentář) → tabulka `tickers` → REST `list_tickers(active=True)`.
- **Dávkování**: tempo REST dotazů hlídá `_throttle()` — klouzavé okno **5 dotazů / 60 s** (`_REST_RATE_LIMIT`, `_REST_RATE_WINDOW`, rezerva `_REST_RATE_MARGIN`). Platí pro celý proces, takže se o limit dělí `update_tickers`, `update_daily` i `update_minute`; s předplatným se nečeká. `_TICKERS_BATCH = 5` už určuje jen velikost partu a jednoho commitu, ne tempo.
- **Průběžný zápis**: po dávce upsert do `tickers` (jen když `tickers.last_updated < day`) a part `{market}/tickers/_tmp/{date}/part-<prvni ticker>.parquet`. Na konci se snapshot + party sloučí (zstd, row group 5 000, řazeno podle `ticker`, zápis přes `*.tmp`) a `_tmp` se smaže. Pád uprostřed běhu tak o nic nepřijde a další běh nestahuje hotové tickery znovu.
- **`day`** je `datetime.date` (jen `yyyy-mm-dd`) a ukládá se i jako `last_updated` — TEXT se porovnává lexikograficky, takže `last_updated < day` funguje; stejný tvar bere i REST `?date=`.
- **`sic_segments`** naplní `update_sic_segments()` ze SEC (`_parse_sic_html`, stdlib parser, žádné pandas); když se to nepovede, běh pokračuje s `PRAGMA foreign_keys=OFF`.
- V `__main__` běží `update_tickers(cfg)` **před** `update_daily(cfg)`.

## Zadání (dřív MASSIVE.md)

Cíl je vytvořit skript na tvorbu a aktualizaci lokálního úložiště dat ze zdroje massive. Předpokládané spouštění skriptu je jednou za týden.

- Konfigurace bude v souboru *config.yaml*.
- Přístupové klíče budou v souboru *secrets.yaml*; tento soubor bude vyloučen z gitu.
- Skript musí být schopen obsloužit všechny chybové stavy. Chyby logovat do souboru.
- Opakovat čtení v případě chyby. Počet opakování bude v konfiguračním souboru *retries=3*.
- Historická data budeme stahovat pomocí:
  1. [REST Api](https://github.com/massive-com/client-python) při použití přístupu zdarma.
  2. [Flat files](https://massive.com/docs/flat-files/quickstart) při placeném přístupu.
- Při aktualizaci dat si skript z již uložených dat zjistí, kde má začít. Dlouhý běh může spadnout — po výpadku se musí rychle navázat, **nestahovat data od začátku**.
- Při vytváření úložiště (prvním spuštění) ještě nejsou žádná data, bude v konfiguračním souboru nastaven počet měsíců pro zpětné načtení *history_months=120*. To platí jen pro flat soubory. Pro REST api bude defaultně historie jen jeden měsíc.
- Jaká varianta stahování se použije, bude v konfiguračním souboru *subscription=False*.
- Jestliže nebude použito předplatné *subscription*, jsou API dotazy omezeny na 5 za minutu.
- Stahovat budeme jen 1D a 1min data. Flat soubory jiné nedoporučují.
- U 1min dat se musíme dotazovat zvlášť na každý ticker. Seznam tickerů bude dán jménem souboru *tikers_file* v konfiguračním souboru.
- U 1min dat při použití REST api je nevýhodné stahovat data po dnech. Nejlépe je stáhnout potřebný počet dní najednou a pak je rozdělit po jednotlivých dnech. Z omezení REST api na 50_000 záznamů je možné stáhnout najednou 1 měsíc denních dat.

### Formát OHLCV souborů *Parquet*

- Použít sloupcovou kompresi *zstd*.
- Použít *row groups* a před uložením seřadit řádky podle *ticker* a uvnitř podle *timestamp*. Tím by se měly row groups shluknout po tickerech.
- Po stažení flat souboru a jeho uložení se převede na parquet.

### Adresářová struktura lokálního úložiště

Cesta k lokálnímu úložišti je v konfiguračním souboru, položka *root_folder*.

- market = stocks (, futures, crypto, options)
- timeframe = 1D, 1min
- date = yyyy-mm-dd
- adresář pro logy: `[root_folder]/logs`

Flat files (csv.gz):

```
[root_folder]/{market}/flat_files/{timeframe}/{year}/{month}/{date}.csv.gz
```

Parquet — `update_daily` ukládá **po měsících** (den je jen v `window_start`), `1m` data a flat files po dnech:

| kdo píše | cesta |
|---|---|
| `update_daily` (REST 1D) | `[root_folder]/market={market}/timeframe=1D/year={YYYY}/month={MM}.parquet` |
| `update_minute` (REST 1m) | `[root_folder]/market={market}/timeframe=1m/year={YYYY}/month={MM}/{date}.parquet` |
| `update_flat_files` (parquet) | `[root_folder]/{market}/timeframe={timeframe}/year={YYYY}/month={MM}/{date}.parquet` |

Informace o *tickerech*:

```
[root_folder]/{market}/tickers/{year}/{month}/{date}.parquet   # historické snapshoty
[root_folder]/{market}/tickers/reference.sqlite                # aktuální data
```

### Návrhová rozhodnutí a jejich zdůvodnění

- **Jeden sdílený SQLite soubor** (`reference.sqlite`) pro všechny tabulky níže, ne separátní soubory. Objem dat je malý (řádově 10–100 MB), tabulky jsou vzájemně provázané přes `ticker` a je potřeba mezi nimi dělat JOIN (např. dividendy podle sektoru).
- **SQLite místo Parquet** pro tato data, protože:
  - přístupový vzor je point-lookup podle tickeru (backtest se ptá „dej mi dividendy pro AAPL"), ne bulk analytický scan přes celý trh,
  - data se průběžně mění/doplňují (Massive je aktualizuje denně) – SQLite umí upsert, Parquet je prakticky immutable.
- **`sic_segments` je oddělená lookup tabulka**, ne sloučená do `tickers` – SIC kód→segment mapování je nezávislé na tickeru, malé (~1000 řádků) a mění se prakticky nikdy.

### Struktura databáze (DDL)

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ------------------------------------------------------------
-- 1. tickers – aktuální snapshot metadat, 1 řádek na ticker
-- zdroj: GET /v3/reference/tickers/{ticker}  (Ticker Overview)
-- update: denně, batch přes celé univerzum (~15 000 tickerů)
-- ------------------------------------------------------------
CREATE TABLE tickers (
    ticker                          TEXT PRIMARY KEY,
    name                            TEXT,
    active                          BOOLEAN,
    market                          TEXT,
    locale                          TEXT,
    primary_exchange                TEXT,
    type                            TEXT,
    currency_name                   TEXT,
    currency_symbol                 TEXT,
    base_currency_name              TEXT,
    base_currency_symbol            TEXT,
    cik                             TEXT,
    composite_figi                  TEXT,
    share_class_figi                TEXT,
    sic_code                        TEXT REFERENCES sic_segments(sic_code),
    sic_description                 TEXT,
    market_cap                      REAL,
    share_class_shares_outstanding  INTEGER,
    weighted_shares_outstanding     INTEGER,
    total_employees                 INTEGER,
    list_date                       TEXT,
    delisted_utc                    TEXT,
    description                     TEXT,
    homepage_url                    TEXT,
    phone_number                    TEXT,
    ticker_root                     TEXT,
    ticker_suffix                   TEXT,
    address_line1                   TEXT,      -- z vnořeného address.address1
    address_line2                   TEXT,      -- z vnořeného address.address2
    address_city                    TEXT,
    address_state                   TEXT,
    address_country                 TEXT,
    address_postal_code             TEXT,
    logo_url                        TEXT,      -- z vnořeného branding.logo_url
    icon_url                        TEXT,
    accent_color                    TEXT,
    light_color                     TEXT,
    dark_color                      TEXT,
    last_updated                    TEXT NOT NULL   -- yyyy-mm-dd posledního stažení
);
CREATE INDEX idx_tickers_sic ON tickers(sic_code);
CREATE INDEX idx_tickers_active ON tickers(active);

-- ------------------------------------------------------------
-- 2. sic_segments – SIC kód -> segment/sektor (malá lookup tabulka)
-- zdroj: SEC (tabulka SIC kódů), mění se prakticky nikdy
-- ------------------------------------------------------------
CREATE TABLE sic_segments (
    sic_code    TEXT PRIMARY KEY,
    sec_office  TEXT,
    industry    TEXT,
    segment     TEXT NOT NULL
);

-- ------------------------------------------------------------
-- 3. dividends – korporátní akce: dividendy
-- zdroj: GET /stocks/v1/dividends
-- update: denně
-- ------------------------------------------------------------
CREATE TABLE dividends (
    id                              TEXT PRIMARY KEY,
    ticker                          TEXT NOT NULL REFERENCES tickers(ticker),
    ex_dividend_date                TEXT,
    declaration_date                TEXT,
    record_date                     TEXT,
    pay_date                        TEXT,
    cash_amount                     REAL,
    split_adjusted_cash_amount      REAL,
    currency                        TEXT,
    frequency                       INTEGER,
    distribution_type               TEXT,
    historical_adjustment_factor    REAL
);
CREATE INDEX idx_dividends_ticker_date ON dividends(ticker, ex_dividend_date);
```

### Mapování zdroj → tabulka → frekvence

| Tabulka              | Endpoint                                              | Frekvence update | Upsert klíč                        |
|-----------------------|--------------------------------------------------------|-------------------|-------------------------------------|
| `tickers`              | `GET /v3/reference/tickers/{ticker}`                   | denně              | `ticker`                             |
| `sic_segments`         | SEC (stávající logika projektu)                        | jednorázově/zřídka | `sic_code`                           |
| `dividends`            | `GET /stocks/v1/dividends`                             | denně              | `id`                                 |

## Vědomé rozdíly proti zadání

Nesjednocovat bez dotazu — jsou to vědomá rozhodnutí uživatele:

- **Adresářové struktury nejsou jednotné.** Flat files csv.gz jdou do `[root]/{market}/flat_files/{tf}/…`, ale parquet z flat files do `[root]/{market}/timeframe={tf}/…` (market **bez** `=`), REST cesty do `[root]/market={market}/timeframe={tf}/…` (market **s** `=`). Není to bug. `update_daily` navíc ukládá **po měsících** (`year={YYYY}/month={MM}.parquet`), zatímco `update_minute` a flat files po dnech (`month={MM}/{date}.parquet`).
- **`timeframe` je `1m`, ne `1min`** jak říká zadání.
- **Seznam tickerů je napevno** `1m_{market}.txt` v `REPO_ROOT`, ne z konfiguračního klíče `tikers_file`. Soubory už v adresáři existují (stocks 12 615, crypto 625, fx 1 208).
- **Hloubka historie pro REST** je 23 měsíců (`_REST_HISTORY_MONTHS`) u `update_daily`, ale jen 31 dní (`_MINUTE_CHUNK_DAYS`) u `update_minute`. Zadání zmiňuje 1 měsíc; `history_months` z config.yaml platí **jen pro flat files**.
- **Trhy**: zadání zmiňuje stocks/futures/crypto/options, kód podporuje `VALID_MARKETS = ("stocks", "crypto", "fx")` (fx je na S3 jako `forex`).
- **SQLite část je implementovaná jen pro tickere** — `update_tickers` plní `{market}/tickers/reference.sqlite` (tabulky `tickers`, `sic_segments`) a denní snapshoty `{market}/tickers/{year}/{month}/{date}.parquet`. Tabulka `dividends` ze zadání zatím v kódu není. Legacy `me_ticker_info.py` (píše `tickers.db` s tabulkou `ticker_info`) je pozůstatek staré implementace: importuje neexistující moduly (`config`, `me_common`) a čte pole, která v `AppConfig` nejsou (např. `cfg.db_path`), takže se nespustí; slouží jen jako zdroj pro budoucí práci a je v `.gitignore`.
- `FLAT_FILES_BUCKET = "files.massive.com"` je **neověřený odhad** — v kódu označený `# TODO`. Knihovna `massive` flat files vůbec neobsahuje (je čistě REST), takže to z ní ověřit nelze.

## Zrádná místa

**Pomocné funkce patří do těla volající funkce**, ne na úroveň modulu — uživatel to tak chce a opakovaně to vynucoval (`update_flat_files`, `update_daily` i `update_minute` mají všechny helpery vnořené). Na úrovni modulu zůstávají jen konstanty, výjimkové třídy (`_NotOnS3`, `_RestError`), `_bars_to_table()` — převod stažených barů na tabulku s pevným schématem, který potřebují obě REST cesty — a rate limiter `_throttle()`/`_reset_rate_limit()` se stavem `_request_times`, protože limit 5 dotazů/min platí pro celý proces a dělí se o něj všechny REST cesty. Důsledek vnořených funkcí: **nelze je monkeypatchovat z testů**, proto testy podstrkují fake moduly do `sys.modules` (`boto3`, `massive`) — lazy importy jsou také uvnitř funkcí.

**Testy nesmí sahat na síť.** `_test_*.py` podstrkují fake modul; `_test_real_client.py` používá pravou knihovnu, ale vyměňuje jen `RESTClient._get` a `_paginate` (jediná dvě místa, kudy teče síť) a uvnitř volá pravý deserializer. Když se helper přesune z těla funkce ven, tyhle záměny přestanou fungovat.

**`Agg` nemá pole `ticker`** (na rozdíl od `GroupedDailyAgg`, kde je klíč `T`) — u minutových barů se ticker bere z dotazu. `bar.timestamp` je v milisekundách.

**`pagination=True` je default knihovny `massive`.** Jedno volání `list_aggs` tak může udělat víc HTTP dotazů (přes `next_url`), což rozbíjí počítání rate limitu. Drží to pod kontrolou až dělení na 31denní úseky (`_MINUTE_CHUNK_DAYS`) — jeden úsek zůstane pod limitem 50 000 záznamů na stránku.

**Logy jdou na stdout a do souboru.** `setup_logging()` na importu nastaví jen konzoli; `__main__` ho volá znovu s `cfg.root_folder / "logs"`, což přidá `FileHandler` do `[root_folder]/logs/massive_data.log` (UTF-8, bez rotace, opakované volání handler neduplikuje). Běží na Linuxu s UTF-8 — diakritika ani `—` v log zprávách nevadí, žádné ASCII omezení tu není.

**Měsíční soubor se nikdy neappenduje.** `_save_month` načte existující parquet, přidá nové dny přes `concat_tables`, setřídí a zapíše přes `*.tmp` + `Path.replace()`. Deduplikace tam není — nepotřebná, protože se začíná až za `max(window_start)`; ruční zásah do souboru ji ale obejde. Buffer drží nejvýš jeden měsíc, takže ani 23měsíční naplnění nesežere paměť.

**Bary z REST musí jít přes `_bars_to_table()`.** `pa.Table.from_pylist` si odvodí typy z hodnot a adjusted volume je jednou `int64`, jednou `double`; `concat_tables(promote)` int64 vs double nesjednotí, takže merge měsíce spadne na `Field volume has incompatible types`. Funkce proto volume i transactions zaokrouhluje na int64 — stejné schéma mají flat files i data z `convert_ohlc_daily.py`.

**`update_minute` není v `__main__`** — je pomalá (limit 5 dotazů/min) a běží se ručně. Tempo hlídá `_throttle()` (klouzavé okno 5 dotazů / 60 s, modulová, společná pro všechny REST cesty); `config.yaml` klíč pro prodlevu nemá.

**Repozitář je pod gitem** (od 2026-09-12), `origin` míří na https://github.com/vitkrska-max/massive-data, který je **veřejný**. `secrets.yaml` a `.claude/settings.local.json` proto musí zůstat v `.gitignore` — před každým commitem zkontroluj `git status`, že se tam nedostaly. Commit identita je nastavená jen lokálně pro tento repozitář (`vitkrska-max` + GitHub noreply adresa), globální `~/.gitconfig` zůstal nedotčený.
