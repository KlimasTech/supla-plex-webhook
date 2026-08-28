# Supla-Plex Webhook

Aplikacja webowa (Flask + Docker) łącząca webhooki z **Plex Media Server** ze **scenami/kanałami SUPLA**. Gdy w Plexie ktoś naciśnie play/pauzę/wznów/stop na wybranym odtwarzaczu, aplikacja wysyła odpowiednią akcję na skonfigurowany link bezpośredni SUPLA (np. gaśnie/zapala się światło przy oglądaniu filmu). Obsługuje wiele odtwarzaczy jednocześnie, każdy z własną, niezależną konfiguracją.

## Funkcje

- Obsługa 4 zdarzeń Plex: `media.play`, `media.pause`, `media.resume`, `media.stop`.
- **Wiele odtwarzaczy** (do 4) — liczba ustawiana z listy rozwijanej, każdy w osobnej zakładce panelu z własną:
  - nazwą (wyświetlaną potem w logach zamiast surowego UUID),
  - UUID odtwarzacza Plex,
  - kompletem 4 linków SUPLA,
  - godzinami działania webhooka (niezależnie od pozostałych odtwarzaczy).
- Panel www zabezpieczony logowaniem, z modalem do zmiany hasła dostępnym z każdej podstrony.
- Podgląd logów z automatycznym odświeżaniem (co 5s), przyciskiem **zapisu logów do pliku** i przyciskiem **czyszczenia logów** (z potwierdzeniem w oknie modalnym).
- Automatyczne wykrywanie UUID odtwarzaczy z przychodzących webhooków (tabela „Ostatnio wykryci playerzy”) — tylko dla zdarzeń play/pause/resume/stop, z przyciskiem kopiowania UUID do schowka i możliwością odrzucenia wpisu.
- Interfejs w pełni responsywny (telefon/tablet/desktop).
- Konfiguracja zapisywana w pliku JSON na wolumenie Docker — przeżywa restart kontenera. Stary format (jeden odtwarzacz) migruje się automatycznie do nowego przy pierwszym uruchomieniu po aktualizacji.

## Wymagania

- Docker + Docker Compose.
- Serwer Plex Media Server z aktywnym **Plex Pass** (webhooki w Plexie są dostępne tylko z Plex Pass).
- Konto SUPLA z co najmniej jednym skonfigurowanym linkiem bezpośrednim (kanał lub scena) na odtwarzacz.

## Uruchomienie

```bash
git clone https://github.com/KlimasTech/supla-plex-webhook.git
cd supla-plex-webhook
docker compose up -d --build
```

Aplikacja wystartuje na porcie `3000`. Panel logowania: `http://<adres-serwera>:3000/login`

Domyślne dane logowania: **login** `admin`, **hasło** `supla` — zmień je od razu po pierwszym zalogowaniu (przycisk „Zmień hasło” w nagłówku panelu).

Strefa czasowa kontenera jest ustawiona na `Europe/Warsaw` (zmienna `TZ` w `docker-compose.yml`) — wpływa to na godziny w logach oraz na działanie ograniczenia godzinowego webhooka.

## Konfiguracja webhooka w Plex

W Plex: **Ustawienia → Konto → Webhooks → Add Webhook**, wpisz:

```
http://<adres-serwera>:3000/webhook
```

Pamiętaj o końcówce `/webhook` — sam adres bez tej ścieżki nie zadziała. Ten sam, jeden webhook obsługuje wszystkich skonfigurowanych odtwarzaczy — aplikacja sama dopasowuje przychodzące zdarzenie po UUID do właściwej zakładki/konfiguracji.

## Dodawanie kolejnych odtwarzaczy

1. W panelu, w sekcji „Liczba odtwarzaczy”, wybierz z listy żądaną liczbę (1–4) i kliknij „Zastosuj”.
2. Dla każdego odtwarzacza pojawi się osobna zakładka — przełączasz się między nimi bez przeładowania strony.
3. Zmniejszenie liczby odtwarzaczy poprosi o potwierdzenie (usuwa konfigurację zakładek z końca listy).

## Konfiguracja SUPLA (dla każdego odtwarzacza)

Każde z czterech zdarzeń (PLAY / RESUME / PAUSE / STOP) wymaga jednego pola — pełnego linku bezpośredniego w formacie:

```
https://<serwer-supla>/direct/<id>/<kod>/<akcja>
```

np. `https://svr11.supla.org/direct/1571/XuojuqMMD9S8RuPw/execute`

Link bezpośredni (z kodem i identyfikatorem kanału/sceny) znajdziesz w [SUPLA Cloud](https://cloud.supla.org/) przy danym kanale lub scenie. Aplikacja sama wyciąga z niego adres serwera, kod i akcję, i wywołuje poprawne żądanie `PATCH` do API SUPLA.

## Ustawienie UUID odtwarzacza

Nie trzeba szukać UUID ręcznie w ustawieniach Plexa:

1. Uruchom dowolną akcję w Plexie na docelowym urządzeniu (np. wciśnij play).
2. Wejdź w panelu w zakładkę **Logi** — urządzenie pojawi się w tabeli „Ostatnio wykryci playerzy” (tylko dla zdarzeń play/pause/resume/stop).
3. Kliknij **„Kopiuj”**, żeby skopiować UUID do schowka, i wklej je w polu UUID właściwej zakładki odtwarzacza w panelu konfiguracji.
4. Jeśli wpis w tabeli nie jest już potrzebny, usuń go przyciskiem ✕ (wróci automatycznie, gdy nadejdzie kolejne zdarzenie z tym UUID).

Jeśli w konfiguracji odtwarzacza podasz też nazwę, w logach i w tej tabeli będzie się ona wyświetlać obok UUID (np. `NVIDIA Shield (62724f1a...-com-plexapp-android)`).

## Ograniczenie godzinowe

Każdy odtwarzacz ma własne, niezależne ustawienie „Godziny działania webhooka” — po włączeniu, poza wskazanym przedziałem (obsługuje zakres przez północ, np. 20:00–07:00) zdarzenia z Plexa są nadal wykrywane i widoczne w logach, ale akcje SUPLA nie są wysyłane.

## Struktura projektu

```
app.py                   # aplikacja Flask (webhook + panel administracyjny)
templates/               # szablony HTML i wspólny arkusz stylów (style.css)
supla-plex-webhook       # Dockerfile
docker-compose.yml
requirements.txt
data/                    # (tworzone automatycznie) config.json + logi, wolumen Docker
```

## Bezpieczeństwo

- Konfiguracja (`data/config.json`) zawiera hash hasła oraz kody dostępu do Supli — katalog `data/` jest wykluczony z repozytorium przez `.gitignore` i nie powinien być nigdzie publikowany.
- Panel administracyjny nie ma dodatkowych zabezpieczeń poza logowaniem (brak CSRF, rate-limitingu) — jeśli wystawiasz port `3000` poza sieć lokalną, zabezpiecz go dodatkowo (reverse proxy z HTTPS, VPN, itp.).
