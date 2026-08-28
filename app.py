import glob
import json
import logging
import logging.handlers
import os
import re
import secrets
from datetime import datetime
from functools import wraps

import requests
from flask import Flask, Response, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/data/config.json")
LOG_PATH = os.environ.get("LOG_PATH", "/data/supla-plex-webhook.log")

# (klucz, etykieta w panelu, nazwa zdarzenia Plex)
ACTIONS = [
    ("play", "PLAY", "media.play"),
    ("resume", "RESUME", "media.resume"),
    ("pause", "PAUSE", "media.pause"),
    ("stop", "STOP", "media.stop"),
]

MAX_PLAYERS = 4


def default_player():
    return {
        "name": "",
        "uuid": "",
        "links": {key: "" for key, _, _ in ACTIONS},
        "active_hours": {"enabled": False, "start": "20:00", "end": "07:00"},
    }


def default_config():
    return {
        "secret_key": secrets.token_hex(32),
        "auth": {"username": "admin", "password_hash": generate_password_hash("supla")},
        "players": [default_player()],
        "dismissed_uuids": [],
    }


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp_path = CONFIG_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, CONFIG_PATH)


def load_config():
    if not os.path.exists(CONFIG_PATH):
        cfg = default_config()
        save_config(cfg)
        return cfg

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    changed = False
    if not cfg.get("secret_key"):
        cfg["secret_key"] = secrets.token_hex(32)
        changed = True
    cfg.setdefault("auth", {})
    cfg["auth"].setdefault("username", "admin")
    if not cfg["auth"].get("password_hash"):
        cfg["auth"]["password_hash"] = generate_password_hash("supla")
        changed = True
    cfg.setdefault("dismissed_uuids", [])

    if "players" not in cfg:
        cfg["players"] = [
            {
                "name": cfg.pop("player_name", ""),
                "uuid": cfg.pop("player_uuid", ""),
                "links": cfg.pop("links", {key: "" for key, _, _ in ACTIONS}),
                "active_hours": cfg.pop("active_hours", {"enabled": False, "start": "20:00", "end": "07:00"}),
            }
        ]
        changed = True

    if not cfg["players"]:
        cfg["players"] = [default_player()]
        changed = True

    for player in cfg["players"]:
        player.setdefault("name", "")
        player.setdefault("uuid", "")
        player.setdefault("links", {})
        for key, _, _ in ACTIONS:
            player["links"].setdefault(key, "")
        player.setdefault("active_hours", {})
        player["active_hours"].setdefault("enabled", False)
        player["active_hours"].setdefault("start", "20:00")
        player["active_hours"].setdefault("end", "07:00")

    if changed:
        save_config(cfg)
    return cfg


app = Flask(__name__)
app.secret_key = load_config()["secret_key"]


@app.after_request
def add_no_cache_headers(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@app.route("/style.css")
def style_css():
    css = render_template("style.css", style_version=datetime.now().isoformat())
    return Response(css, mimetype="text/css")

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
file_handler = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3)
file_handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))

logger = logging.getLogger("supla-plex-webhook")
logger.setLevel(logging.INFO)
logger.addHandler(file_handler)
logger.addHandler(logging.StreamHandler())

logger.info("🚀 Startuję Supla-Plex Webhook Listener")

PLEX_EVENT_RE = re.compile(
    r"^\[(?P<ts>[^\]]+)\] \w+ Zdarzenie Plex: (?:(?P<name>.+) )?uuid=(?P<uuid>\S+) event=(?P<event>\S+)"
)
DIRECT_LINK_RE = re.compile(r"^(?P<base>https?://[^/]+/direct/[^/]+)/(?P<code>[^/]+)/(?P<action>[^/]+)/?$")


def parse_direct_link(link):
    m = DIRECT_LINK_RE.match(link.strip())
    if not m:
        return None
    return m.group("base"), m.group("code"), m.group("action")


def is_within_active_hours(ah):
    if not ah.get("enabled"):
        return True
    try:
        start = datetime.strptime(ah["start"], "%H:%M").time()
        end = datetime.strptime(ah["end"], "%H:%M").time()
    except (KeyError, ValueError, TypeError):
        return True
    now = datetime.now().time()
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end


TRACKED_EVENTS = {plex_event for _, _, plex_event in ACTIONS}


def extract_recent_players(lines_newest_first, dismissed_uuids, limit=10):
    seen = {}
    for line in lines_newest_first:
        m = PLEX_EVENT_RE.match(line)
        if not m or m.group("event") not in TRACKED_EVENTS:
            continue
        if m.group("uuid") in seen or m.group("uuid") in dismissed_uuids:
            continue
        seen[m.group("uuid")] = {
            "uuid": m.group("uuid"),
            "event": m.group("event"),
            "ts": m.group("ts"),
        }
        if len(seen) >= limit:
            break
    return list(seen.values())


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


@app.route("/")
def index():
    return '🚀 Supla-Plex Webhook Listener 🔐 <a href="' + url_for("login") + '">Panel</a>'


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        cfg = load_config()
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == cfg["auth"]["username"] and check_password_hash(cfg["auth"]["password_hash"], password):
            session["logged_in"] = True
            logger.info(f"Zalogowano do panelu jako '{username}' z {request.remote_addr}")
            return redirect(url_for("admin"))
        logger.warning(f"Nieudana próba logowania jako '{username}' z {request.remote_addr}")
        flash("Nieprawidłowy login lub hasło.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/admin", methods=["GET", "POST"])
@login_required
def admin():
    cfg = load_config()
    if request.method == "POST":
        players = []
        for i in range(len(cfg["players"])):
            players.append(
                {
                    "name": request.form.get(f"player_{i}_name", "").strip(),
                    "uuid": request.form.get(f"player_{i}_uuid", "").strip(),
                    "links": {
                        key: request.form.get(f"link_{i}_{key}", "").strip() for key, _, _ in ACTIONS
                    },
                    "active_hours": {
                        "enabled": request.form.get(f"active_hours_{i}_enabled") == "on",
                        "start": request.form.get(f"active_hours_{i}_start", "").strip() or "20:00",
                        "end": request.form.get(f"active_hours_{i}_end", "").strip() or "07:00",
                    },
                }
            )
        cfg["players"] = players
        save_config(cfg)
        flash("Zapisano konfigurację.", "success")
        return redirect(url_for("admin"))
    webhook_url = request.host_url.rstrip("/") + "/webhook"
    return render_template(
        "admin.html", config=cfg, actions=ACTIONS, webhook_url=webhook_url, max_players=MAX_PLAYERS
    )


@app.route("/admin/players/count", methods=["POST"])
@login_required
def set_player_count():
    cfg = load_config()
    try:
        count = int(request.form.get("count", "1"))
    except ValueError:
        count = 1
    count = max(1, min(MAX_PLAYERS, count))
    players = cfg["players"]
    if count > len(players):
        players.extend(default_player() for _ in range(count - len(players)))
    else:
        players = players[:count]
    cfg["players"] = players
    save_config(cfg)
    flash(f"Ustawiono liczbę odtwarzaczy: {count}", "success")
    return redirect(url_for("admin"))


@app.route("/admin/password", methods=["POST"])
@login_required
def change_password():
    cfg = load_config()
    current = request.form.get("current_password", "")
    new = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")

    if not check_password_hash(cfg["auth"]["password_hash"], current):
        flash("Aktualne hasło jest nieprawidłowe.", "error")
    elif not new or new != confirm:
        flash("Nowe hasło i potwierdzenie muszą być identyczne i niepuste.", "error")
    else:
        cfg["auth"]["password_hash"] = generate_password_hash(new)
        save_config(cfg)
        flash("Hasło zostało zmienione.", "success")
    next_page = request.form.get("next", "admin")
    if next_page not in ("admin", "logs"):
        next_page = "admin"
    return redirect(url_for(next_page))


@app.route("/admin/logs")
@login_required
def logs():
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except FileNotFoundError:
        lines = []
    cfg = load_config()
    last_lines = lines[-300:]
    last_lines.reverse()
    recent_players = extract_recent_players(last_lines, cfg["dismissed_uuids"])
    player_names_by_uuid = {p["uuid"]: p["name"] for p in cfg["players"] if p["uuid"] and p["name"]}
    return render_template(
        "logs.html", lines=last_lines, recent_players=recent_players, player_names_by_uuid=player_names_by_uuid
    )


@app.route("/admin/logs/download")
@login_required
def download_logs():
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except FileNotFoundError:
        content = ""
    filename = f"supla-plex-webhook_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    return Response(
        content,
        mimetype="text/plain",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/admin/clear-logs", methods=["POST"])
@login_required
def clear_logs():
    file_handler.acquire()
    try:
        file_handler.stream.close()
        open(LOG_PATH, "w").close()
        for backup in glob.glob(LOG_PATH + ".*"):
            try:
                os.remove(backup)
            except OSError:
                pass
        file_handler.stream = file_handler._open()
    finally:
        file_handler.release()
    logger.info(f"Logi wyczyszczone przez {request.remote_addr}")
    flash("Logi zostały wyczyszczone.", "success")
    return redirect(url_for("logs"))


@app.route("/admin/dismiss-uuid", methods=["POST"])
@login_required
def dismiss_uuid():
    uuid = request.form.get("uuid", "").strip()
    if uuid:
        cfg = load_config()
        if uuid not in cfg["dismissed_uuids"]:
            cfg["dismissed_uuids"].append(uuid)
            save_config(cfg)
    return redirect(url_for("logs"))


@app.route("/webhook", methods=["POST"])
def webhook():
    logger.info(
        f"Żądanie na /webhook z {request.remote_addr} "
        f"content-type={request.content_type} "
        f"form-keys={list(request.form.keys())} "
        f"body-len={request.content_length}"
    )
    raw_payload = request.form.get("payload")
    if not raw_payload:
        logger.warning(f"Brak pola 'payload' w żądaniu — surowe body: {request.get_data(as_text=True)[:500]!r}")
        return "brak payload", 400
    try:
        data = json.loads(raw_payload)
    except (TypeError, ValueError):
        logger.warning(f"Otrzymano nieprawidłowy JSON w payload: {raw_payload[:500]!r}")
        return "invalid payload", 400

    cfg = load_config()
    player_uuid = (data.get("Player") or {}).get("uuid")
    event = data.get("event")
    matched = next((p for p in cfg["players"] if p["uuid"] and p["uuid"] == player_uuid), None)
    if matched and matched["name"]:
        logger.info(f"Zdarzenie Plex: {matched['name']} uuid={player_uuid} event={event}")
    else:
        logger.info(f"Zdarzenie Plex: uuid={player_uuid} event={event}")

    if not matched:
        return "🚀 Webhook received (inny player, ignoruję)"

    if not is_within_active_hours(matched["active_hours"]):
        logger.info(
            f"Poza godzinami aktywności ({matched['active_hours']['start']}–{matched['active_hours']['end']}) "
            f"— pomijam akcję dla {matched['name'] or player_uuid}"
        )
        return "🚀 Webhook received (poza godzinami aktywności)"

    for key, label, plex_event in ACTIONS:
        if event != plex_event:
            continue
        link = matched["links"].get(key)
        if not link:
            logger.info(f"Zdarzenie {label} pasuje, ale nie skonfigurowano linku — pomijam")
            break
        parsed = parse_direct_link(link)
        if not parsed:
            logger.error(
                f"Nieprawidłowy format linku dla {label}: {link!r} — oczekiwano "
                "https://<serwer>/direct/<id>/<kod>/<akcja>"
            )
            break
        base_url, code, action = parsed
        try:
            res = requests.patch(base_url, json={"code": code, "action": action}, timeout=5)
            logger.info(f"Wysłano PATCH ({label}) na {base_url} (action={action}) -> status {res.status_code}")
        except requests.RequestException as exc:
            logger.error(f"Błąd wysyłki PATCH ({label}) na {base_url}: {exc}")
        break

    return "🚀 Webhook received!"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
