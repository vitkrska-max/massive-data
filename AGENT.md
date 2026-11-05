# AGENT.md

This file provides guidance to coding agents (Claude Code, Codex, Hermes) when working with code in this repository.

## Projekt

`massive_data.py` stahuje OHLCV data z Massive (dříve Polygon.io) do lokálního parquet úložiště. Design spec je `MASSIVE.md`, konfigurace `config.yaml` + `secrets.yaml`.

**Vše v projektu je česky** — komentáře, docstringy, logy i názvy funkcí jsou české, výjimkou jsou jen názvy převzaté z API (`window_start`, `get_grouped_daily_aggs`). Drž se toho, včetně diakritiky v komentářích.

## Příkazy

```bash
python massive_data.py                           # flat files (1D + 1m) + update_daily
python _test_flat_files.py                       # offline, fake boto3
python _test_daily.py                            # offline, fake massive
python _test_minute.py                           # offline, fake massive
python _test_real_client.py                      # offline, PRAVÁ knihovna massive
python _smoke_real_api.py [both|daily|minute]    # ŽIVĚ — spotřebuje API kvótu
git push                                         # záloha na GitHub (origin)
```

Testy nemají runner ani framework — každý soubor je samostatný skript a „spustit jeden test" znamená spustit ten soubor. Úspěch = výpis `VŠE OK` a exit 0; selhání = `AssertionError`. Testy si samy vytvářejí a uklízejí dočasné adresáře (`_test_*_root*`, `_test_minute_dir`).

Jen `_smoke_real_api.py` sahá na síť: `daily` = 1 dotaz, `minute` = 1 dotaz. Bez předplatného je limit 5 dotazů/minutu — **před spuštěním se zeptej**. Píše do `_smoke_root`, nikdy do skutečného `root_folder`.

## Architektura

Vše je v jediném modulu `massive_data.py` (~800 řádků), žádný balíček. Dvě nezávislé cesty stahování, přepíná je `cfg.subscription`:

| `subscription` | funkce | zdroj dat |
|---|---|---|
| `True` | `update_flat_files(cfg, timeframe)` | S3 flat files, csv.gz → parquet |
| `False` | `update_daily(cfg)` | REST `get_grouped_daily_aggs` — 1 dotaz = celý trh za den |
| `False` | `update_minute(cfg)` | REST `list_aggs` — 1 dotaz = 1 ticker za úsek |

Konfigurace: `load_config()` čte `config.yaml` (povinný) + `secrets.yaml` (nepovinný) a vrací `@dataclass AppConfig`. Klíče v `secrets.yaml` mají prefix `MASSIVE_` (`MASSIVE_API_KEY` → `api_key`); hodnota `<...>` znamená nevyplněno → `None`. Neznámý klíč v YAML je chyba (`sys.exit(1)`) — při přidání pole do `AppConfig` funguje kontrola překlepů automaticky.

Načtení secretu se děje **až v místě použití** přes `cfg.require_secret("api_key")`, ne při načtení konfigurace.

**Navazování na předchozí běh** se odvozuje z názvů souborů, žádný progress soubor neexistuje:
- `update_daily` / `update_minute` → `last_downloaded_date(cfg, market, timeframe)` (modulová funkce)
- `update_flat_files` → vlastní vnořená `_last_downloaded_date(dest_dir)`, čte názvy csv.gz

**Schéma parquet je u obou cest stejné** — `ticker, window_start, open, high, low, close, volume, transactions, vwap`. `window_start` je Unix **nanosekundy** (flat files je tak mají; REST vrací milisekundy, proto `bar.timestamp * 1_000_000`). Sortuje se podle `ticker` a uvnitř podle `window_start`, aby se row groups shlukly po tickerech.

## Záměrné rozdíly proti MASSIVE.md

Nesjednocovat bez dotazu — jsou to vědomá rozhodnutí uživatele:

- **Dvě různé adresářové struktury.** `update_flat_files` píše `root_folder/{market}/timeframe={tf}/…` (market **bez** `=`), REST cesty píší `root_folder/market={market}/timeframe={tf}/…` (market **s** `=`). Není to bug.
- **`timeframe` je `1m`, ne `1min`** jak říká spec.
- **Seznam tickerů je napevno** `1m_{market}.txt` v `REPO_ROOT`, ne z konfiguračního klíče `tikers_file`. Soubory už v adresáři existují (stocks 12 615, crypto 625, fx 1 208).
- **Hloubka historie pro REST** je 23 měsíců (`_REST_HISTORY_MONTHS`) u `update_daily`, ale jen 31 dní (`_MINUTE_CHUNK_DAYS`) u `update_minute`. Spec zmiňuje 1 měsíc; `history_months` z config.yaml platí **jen pro flat files**.
- **`reference.db` a `tickers.sqlite` z `MASSIVE.md` nejsou implementované.** SQLite část specu je zatím jen návrh. Pozůstatek staré implementace je `me_ticker_info.py` — importuje neexistující moduly (`config`, `me_common`) a čte pole, která v `AppConfig` nejsou (např. `cfg.db_path`), takže se nespustí; slouží jen jako zdroj pro budoucí SQLite část a je v `.gitignore`.
- `FLAT_FILES_BUCKET = "files.massive.com"` je **neověřený odhad** — v kódu označený `# TODO`. Knihovna `massive` flat files vůbec neobsahuje (je čistě REST), takže to z ní ověřit nelze.

## Zrádná místa

**Pomocné funkce patří do těla volající funkce**, ne na úroveň modulu — uživatel to tak chce a opakovaně to vynucoval (`update_flat_files`, `update_daily` i `update_minute` mají všechny helpery vnořené). Na úrovni modulu zůstávají jen konstanty a výjimkové třídy (`_NotOnS3`, `_RestError`). Důsledek: vnořené funkce **nelze monkeypatchovat z testů**, proto testy podstrkují fake moduly do `sys.modules` (`boto3`, `massive`) — lazy importy jsou také uvnitř funkcí.

**Testy nesmí sahat na síť.** `_test_*.py` podstrkují fake modul; `_test_real_client.py` používá pravou knihovnu, ale vyměňuje jen `RESTClient._get` a `_paginate` (jediná dvě místa, kudy teče síť) a uvnitř volá pravý deserializer. Když se helper přesune z těla funkce ven, tyhle záměny přestanou fungovat.

**`Agg` nemá pole `ticker`** (na rozdíl od `GroupedDailyAgg`, kde je klíč `T`) — u minutových barů se ticker bere z dotazu. `bar.timestamp` je v milisekundách.

**`pagination=True` je default knihovny `massive`.** Jedno volání `list_aggs` tak může udělat víc HTTP dotazů (přes `next_url`), což rozbíjí počítání rate limitu. Drží to pod kontrolou až dělení na 31denní úseky (`_MINUTE_CHUNK_DAYS`) — jeden úsek zůstane pod limitem 50 000 záznamů na stránku.

**Logy jdou na stdout a do souboru.** `setup_logging()` na importu nastaví jen konzoli; `__main__` ho volá znovu s `cfg.root_folder / "logs"`, což přidá `FileHandler` do `[root_folder]/logs/massive_data.log` (UTF-8, bez rotace, opakované volání handler neduplikuje). Běží na Linuxu s UTF-8 — diakritika ani `—` v log zprávách nevadí, žádné ASCII omezení tu není.

**`update_minute` není v `__main__`** — je pomalá (12,5 s na ticker) a běží se ručně. `_REST_DELAY_SECONDS = 12.5` je napevno v kódu, v `config.yaml` klíč pro prodlevu není.

**Repozitář je pod gitem** (od 2026-09-12), `origin` míří na https://github.com/vitkrska-max/massive-data, který je **veřejný**. `secrets.yaml` a `.claude/settings.local.json` proto musí zůstat v `.gitignore` — před každým commitem zkontroluj `git status`, že se tam nedostaly. Commit identita je nastavená jen lokálně pro tento repozitář (`vitkrska-max` + GitHub noreply adresa), globální `~/.gitconfig` zůstal nedotčený.
