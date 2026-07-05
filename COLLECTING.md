# Zbieranie danych — instrukcja operatora

Cel: nagrać zsynchronizowane dane (waga 80 Hz + wideo) z **oznaczeniami
prawdy** przez **cały cykl hodowlany** (dzień 0–~42), żeby zbudować i
zwalidować algorytm ważenia ptaków działający na ESP32.

> **Co jest produktem danych:** nie „jak najwięcej godzin", tylko dość
> **czystych pojedynczych ważeń** + **niezależnych mas referencyjnych** +
> **trudnych przypadków**, rozłożonych **równomiernie po całym cyklu**. Model
> dostrojony tylko na ciężkich, spokojnych ptakach padnie na lekkich pisklętach
> (i odwrotnie).

---

## 0. Zanim zaczniesz (raz na instalację / dzień)

- [ ] ESP z firmware **rig** (`RIG_MODE`) podłączony, `/dev/esp-scale` istnieje.
- [ ] Kamera działa: `rpicam-hello --list-cameras`.
- [ ] Dysk `/data` zamontowany, jest miejsce: `drobit-rig status` (po starcie) lub `df -h /data`.
- [ ] Platforma sztywno zamocowana, wypoziomowana, wolna od śmieci; nic o nią nie uderza (wentylatory, paszociąg).
- [ ] Znasz **rasę** i **datę wstawienia** stada (dzień 0).
- [ ] Masz **zaufaną wagę ręczną** do mas referencyjnych i **znaną masę wzorcową** do kalibracji (np. odważnik).

---

## 1. Start sesji z metadanymi

Podaj metadane od razu — bez nich strona analizy nie policzy dnia cyklu ani
oczekiwanej masy:

```bash
drobit-rig start \
  --breed "Ross 308" \
  --placement-date 2026-06-01 \        # dzień 0 stada (YYYY-MM-DD)
  --house H1 --pen P2 \
  --bird-count 20000 \                 # liczba ptaków w sektorze (kontekst zagęszczenia)
  --note "poranne żerowanie"
```

`cycle_day` policzy się automatycznie z daty wstawienia. Jedna sesja naraz.

## 2. Kalibracja (na starcie każdej sesji)

Ważymy z **różnic**, więc offset zera się skraca — ale wzmocnienie (span)
dryfuje z temperaturą przez 42 dni. Zrób dwa pomiary raz na sesję:

```bash
# platforma pusta, nic na niej — przytrzymaj 5 s
drobit-rig calibrate --grams 0 --dwell 5 --note "pusto"

# połóż znaną masę (np. 2000 g), przytrzymaj stabilnie 5 s
drobit-rig calibrate --grams 2000 --dwell 5 --note "wzorzec 2 kg"
```

Zapisuje interwały do `calibration.jsonl`; offline `compute_calibration`
zamienia je na przelicznik raw→gramy i śledzi dryf wzmocnienia.

## 3. Nagrywanie + masy referencyjne (prawda)

W trakcie sesji, **przy każdym ważeniu referencyjnym**, zważ ptaka na wadze
ręcznej i zaloguj to — to jest prawda, względem której korygujemy bias:

```bash
drobit-rig mark-weight --grams 2450 --bird-id B7 --note "ważenie ręczne"
```

Rób to możliwie często i przy różnych ptakach. Bez tego zmierzysz tylko
**detekcję**, ale nie **dokładność wagi** — a to jest cała przewaga nad
konkurencją.

`status` w dowolnej chwili:

```bash
drobit-rig status     # samples, gaps, temp_frames, disk free, segmenty
```

## 4. Stop

```bash
drobit-rig stop       # flush, znaczniki końca, skan zdarzeń nawigacyjnych
```

---

## Co nagrać przez cały cykl (stratyfikacja)

Rozłóż sesje po całym cyklu i po warunkach — to ważniejsze niż wolumen:

| Etap | Dni (przykład) | Na co uważać |
|------|----------------|--------------|
| Wczesny | ~3, 7, 10 | pisklęta lekkie (dziesiątki g), **bardzo ruchliwe**, dużo krótkich/partial wejść, mały sygnał (SNR) |
| Środek | ~17, 24, 30 | rosnąca masa i zagęszczenie, coraz częściej **wiele ptaków naraz** |
| Późny | ~35, 40, 42 | ciężkie (>3 kg), **spokojne**, platforma rzadko pusta, silny **tłok** i przestępowanie |

Dodatkowo różnicuj: pory dnia (żerowanie vs odpoczynek), i jeśli się da różne sektory.

## Świadomie łap trudne przypadki (negatywy do bramki)

Model potrzebuje też przykładów tego, czego **nie** wolno liczyć jako czyste
ważenie. Nagraj sceny z:

- **zeskok → natychmiastowy wskok kolejnego** (krok w dół i w górę bez osadzenia),
- **kilka ptaków wchodzi/schodzi jednocześnie** (sygnał wielopoziomowy),
- **częściowe wejście** (ptak wpół, oparty), przestępowanie na platformie,
- uderzenia/wibracje (wentylator, paszociąg) — artefakty.

W aplikacji do etykietowania oznaczysz je flagami `multiple_simultaneous`,
`partial_entry`, `artifact`, `unclear` — to są etykiety treningowe bramki
ważności.

---

## Co powstaje (katalog sesji)

```
/data/sessions/<YYYYMMDD_HHMMSS>_<id>/
├── meta.json                 metadane: deployment (rasa, cycle_day, ...) + config urządzenia
├── scale.parquet             surowy sygnał 80 Hz (rpi_mono_ns, raw)
├── temperature.jsonl         temperatura ESP32 (dryf) — wymaga firmware z ramką TEMP
├── reference_weights.jsonl   masy referencyjne z `mark-weight`
├── calibration.jsonl         interwały kalibracji z `calibrate`
├── video/…, video_index.jsonl  wideo zsynchronizowane po rpi_mono_ns
├── gaps.jsonl, status.json, events.jsonl, logs/, done
```

Wszystkie znaczniki czasu są w `rpi_mono_ns` — masy referencyjne, kalibracja,
temperatura i wideo **automatycznie wyrównują się** ze `scale.parquet`.

## Dalej: etykietowanie i analiza

Skopiuj katalogi sesji do `drobit-rig-analysis/data/sessions/`, uruchom
aplikację do anotacji, wybierz detektor **`steps`** → `re-detect`, oznacz
zdarzenia (liczba ptaków + flagi), a trudne dobierz ręcznie (`+ event` / `a`).
Eksport: `analysis dataset <dir…> --out dataset.parquet`.

---

## Pułapki / czego nie robić

- **Nie przenoś ani nie dotykaj rigu w trakcie sesji** — zmienia tarę/kalibrację.
- **Temperatura** pojawi się w `temperature.jsonl` dopiero po wgraniu firmware z ramką TEMP; sprawdź, że `temp_frames` w `status` rośnie (~1 co 2 s).
- **Masy referencyjne**: waż na tej samej zaufanej wadze, notuj `--bird-id` gdy wiesz który ptak.
- **Jedna sesja naraz** (lock `current.json`); jeśli `start` mówi „already running", zrób `stop`.
- **Nie ufaj `sample_rate_hz` z nagłówka aplikacji** do niczego poza wyświetlaniem — jest zawyżony przez bursty timing.

---

## Szybka checklista sesji

1. [ ] `start` z `--breed --placement-date --house --pen --bird-count`
2. [ ] `calibrate --grams 0` + `calibrate --grams <wzorzec>`
3. [ ] nagrywaj; `mark-weight` przy każdym ważeniu referencyjnym
4. [ ] łap trudne sceny (tłok, zeskok+wskok, partial)
5. [ ] `stop`
6. [ ] skopiuj do analizy, oznacz (`steps` → `re-detect`), eksportuj dataset
