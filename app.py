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


def default_config():
    return {
        "secret_key": secrets.token_hex(32),
        "auth": {"username": "admin", "password_hash": generate_password_hash("supla")},
        "player_uuid": "",
        "links": {key: "" for key, _, _ in ACTIONS},
        "dismissed_uuids": [],
        "active_hours": {"enabled": False, "start": "20:00", "end": "07:00"},
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
    cfg.setdefault("player_uuid", "")
    cfg.setdefault("links", {})
    for key, _, _ in ACTIONS:
        cfg["links"].setdefault(key, "")
    cfg.setdefault("dismissed_uuids", [])
    cfg.setdefault("active_hours", {})
    cfg["active_hours"].setdefault("enabled", False)
    cfg["active_hours"].setdefault("start", "20:00")
    cfg["active_hours"].setdefault("end", "07:00")

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

PLEX_EVENT_RE = re.compile(r"^\[(?P<ts>[^\]]+)\] \w+ Zdarzenie Plex: uuid=(?P<uuid>\S+) event=(?P<event>\S+)")
DIRECT_LINK_RE = re.compile(r"^(?P<base>https?://[^/]+/direct/[^/]+)/(?P<code>[^/]+)/(?P<action>[^/]+)/?$")


def parse_direct_link(link):
    m = DIRECT_LINK_RE.match(link.strip())
    if not m:
        return None
    return m.group("base"), m.group("code"), m.group("action")


def is_within_active_hours(cfg):
    ah = cfg.get("active_hours", {})
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


def extract_recent_players(lines_newest_first, dismissed_uuids, limit=10):
    seen = {}
    for line in lines_newest_first:
        m = PLEX_EVENT_RE.match(line)
        if not m or m.group("uuid") in seen or m.group("uuid") in dismissed_uuids:
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
        cfg["player_uuid"] = request.form.get("player_uuid", "").strip()
        for key, _, _ in ACTIONS:
            cfg["links"][key] = request.form.get(f"link_{key}", "").strip()
        cfg["active_hours"] = {
            "enabled": request.form.get("active_hours_enabled") == "on",
            "start": request.form.get("active_hours_start", "").strip() or "20:00",
            "end": request.form.get("active_hours_end", "").strip() or "07:00",
        }
        save_config(cfg)
        flash("Zapisano konfigurację.", "success")
        return redirect(url_for("admin"))
    webhook_url = request.host_url.rstrip("/") + "/webhook"
    return render_template("admin.html", config=cfg, actions=ACTIONS, webhook_url=webhook_url)


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
    return render_template("logs.html", lines=last_lines, recent_players=recent_players)


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


@app.route("/admin/set-uuid", methods=["POST"])
@login_required
def set_uuid():
    uuid = request.form.get("uuid", "").strip()
    if uuid:
        cfg = load_config()
        cfg["player_uuid"] = uuid
        save_config(cfg)
        flash(f"Ustawiono UUID playera: {uuid}", "success")
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
    logger.info(f"Zdarzenie Plex: uuid={player_uuid} event={event}")

    if not cfg["player_uuid"] or player_uuid != cfg["player_uuid"]:
        return "🚀 Webhook received (inny player, ignoruję)"

    if not is_within_active_hours(cfg):
        logger.info(
            f"Poza godzinami aktywności ({cfg['active_hours']['start']}–{cfg['active_hours']['end']}) — pomijam akcję"
        )
        return "🚀 Webhook received (poza godzinami aktywności)"

    for key, label, plex_event in ACTIONS:
        if event != plex_event:
            continue
        link = cfg["links"].get(key)
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
