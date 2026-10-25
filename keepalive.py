"""
Keep-Alive automatique + petit serveur web (Flask).

Fonctionne exactement comme le keep-alive du projet précédent (server.ts) :

  1. Le domaine public est détecté automatiquement à partir des en-têtes
     `X-Forwarded-Host` / `Host` des requêtes reçues (ou fourni via APP_URL).
     Il est mémorisé dans DATA_DIR/detected-host.json pour survivre aux
     redémarrages.
  2. Toutes les 30 secondes, le bot appelle lui-même
     https://<domaine-public>/health afin que la plateforme d'hébergement ne
     le considère pas comme inactif.

Aucune URL n'est codée en dur et aucune configuration manuelle n'est requise.
"""

import asyncio
import json
import logging
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from threading import Thread
from urllib.parse import urlparse

import requests
from flask import Flask, request

# ==================== COMPATIBILITÉ PYTHON ====================
# Le projet fonctionne avec Python 3.9+ (testé pour 3.11, version de Choreo, et
# 3.12). La version réellement utilisée est celle de la plateforme : rien à
# configurer, on vérifie seulement qu'elle est assez récente (aiogram 3 : >= 3.9).
MIN_PYTHON = (3, 9)
if sys.version_info < MIN_PYTHON:
    raise SystemExit(
        f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ est requis "
        f"(version détectée : {sys.version.split()[0]})."
    )
PYTHON_VERSION = ".".join(str(x) for x in sys.version_info[:3])

# ==================== CONFIGURATION ====================
# Port d'écoute : injecté par la plateforme via PORT (voir docker-entrypoint.sh).
# Défaut 3000 : ce port doit correspondre à celui déclaré dans
# .choreo/component.yaml (Choreo n'injecte pas PORT). Il ne fait pas partie des
# ports "scale-to-zero" de Choreo, donc le bot n'est pas endormi automatiquement.
PORT = int(os.environ.get("PORT") or 3000)


def _is_writable_dir(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        return os.access(path, os.W_OK | os.X_OK)
    except OSError:
        return False


def _pick_data_dir() -> str:
    """DATA_DIR > ./data > dossier temporaire : le premier qui est inscriptible.

    Sur les plateformes à système de fichiers en lecture seule (ex. Choreo avec
    buildpack), on retombe sur un dossier temporaire (données non persistantes).
    """
    candidates = []
    if os.environ.get("DATA_DIR"):
        candidates.append(os.environ["DATA_DIR"])
    candidates.append(os.path.join(os.getcwd(), "data"))
    candidates.append(os.path.join(tempfile.gettempdir(), "bot-data"))
    for path in candidates:
        if _is_writable_dir(path):
            if path != candidates[0]:
                print(f"[Data] {candidates[0]} n'est pas inscriptible -> repli sur {path} "
                      f"(données non persistantes).", flush=True)
            return path
    return candidates[-1]


# Dossier des données (detected-host.json, murad-bot.json, scripts, bibliothèques...).
DATA_DIR = _pick_data_dir()
os.environ["DATA_DIR"] = DATA_DIR  # cohérent pour les sous-processus

# KEEP_ALIVE_INTERVAL=0 désactive le keep-alive (utile derrière une passerelle
# qui exige une authentification, ou sur une plateforme qui ne s'endort pas).
KEEP_ALIVE_INTERVAL_S = int(os.environ.get("KEEP_ALIVE_INTERVAL") or 30)
KEEP_ALIVE_TIMEOUT_S = 10

DETECTED_HOST_FILE = os.path.join(DATA_DIR, "detected-host.json")

_cached_public_host = None
_last_ping = {"ok": None, "status": None, "ms": None, "at": None, "error": None}
_hint_shown = {"done": False}


def add_log(message: str) -> None:
    """Log horodaté (même format que le projet précédent)."""
    ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    print(f"[{ts}] {message}", flush=True)


# ==================== DÉTECTION DU DOMAINE PUBLIC ====================
def _is_likely_public_host(host: str) -> bool:
    host = (host or "").split(":")[0].strip().lower()
    if not host or host in ("localhost", "0.0.0.0"):
        return False
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", host):
        return False
    return "." in host


def _remember_public_host(host: str) -> None:
    global _cached_public_host
    host = (host or "").split(":")[0].strip().lower()
    if not _is_likely_public_host(host) or host == _cached_public_host:
        return
    _cached_public_host = host
    try:
        with open(DETECTED_HOST_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {"host": host, "updatedAt": datetime.now(timezone.utc).isoformat()},
                f, indent=2,
            )
    except OSError:
        pass  # best-effort uniquement
    add_log(f"Auto-detected public domain: {host}")


def _load_persisted_host() -> None:
    global _cached_public_host
    try:
        if os.path.exists(DETECTED_HOST_FILE):
            with open(DETECTED_HOST_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("host") and _is_likely_public_host(data["host"]):
                _cached_public_host = data["host"]
    except (OSError, ValueError):
        pass


_load_persisted_host()


def get_public_domain() -> str:
    """APP_URL (si défini) > domaine auto-détecté > chaîne vide."""
    app_url = os.environ.get("APP_URL", "").strip()
    if app_url:
        parsed = urlparse(app_url if "://" in app_url else f"https://{app_url}")
        if parsed.hostname:
            return parsed.hostname
    return _cached_public_host or ""


def get_ping_url() -> str:
    """URL complète à pinger.

    Si APP_URL contient un chemin (ex. l'URL de la passerelle Choreo
    https://xxx.choreoapis.dev/projet/composant/v1.0), on le conserve ;
    sinon on utilise https://<domaine détecté>/health.
    """
    app_url = os.environ.get("APP_URL", "").strip()
    if app_url:
        if "://" not in app_url:
            app_url = f"https://{app_url}"
        return app_url.rstrip("/") + "/health"
    domain = get_public_domain()
    return f"https://{domain}/health" if domain else ""


# ==================== SERVEUR WEB (FLASK) ====================
app = Flask(__name__)
# /health est appelé toutes les 30 s : on évite de polluer les logs.
logging.getLogger("werkzeug").setLevel(logging.WARNING)


@app.before_request
def _detect_host_from_request():
    forwarded = request.headers.get("X-Forwarded-Host") or request.headers.get("Host")
    if forwarded:
        _remember_public_host(forwarded.split(",")[0].strip())


# La route "/" (assistant de configuration, puis statut JSON) est définie dans
# botconfig.py.
@app.route("/health")
def health():
    return "OK", 200


def _extra_ports() -> list:
    """Ports d'écoute supplémentaires (variable EXTRA_PORTS, séparés par des virgules).

    Le port de l'endpoint est fixé dans la console de la plateforme (Choreo :
    Deploy > Endpoint, ou .choreo/component.yaml). S'il diffère de PORT, la
    passerelle affiche « delayed connect error: 111 » (connexion refusée). En
    écoutant aussi sur quelques ports courants, le bot répond quel que soit celui
    qui a été déclaré. Mettre EXTRA_PORTS="" pour désactiver.
    """
    raw = os.environ.get("EXTRA_PORTS")
    if raw is None:
        raw = "8080,8000,5000,9090"
    ports = []
    for x in raw.replace(" ", "").split(","):
        if x.isdigit() and 0 < int(x) < 65536 and int(x) != PORT and int(x) not in ports:
            ports.append(int(x))
    return ports


def _serve(port: int, primary: bool) -> bool:
    import socket
    from werkzeug.serving import make_server
    try:
        # Vérification préalable : make_server() quitte le processus (sys.exit)
        # au lieu de lever une exception quand le port est déjà pris.
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("0.0.0.0", port))
        finally:
            probe.close()
        server = make_server("0.0.0.0", port, app, threaded=True)
    except (OSError, SystemExit) as e:
        add_log(f"[Web] Impossible d'écouter sur le port {port} : {e}"
                + ("" if primary else " (ignoré)"))
        return False
    Thread(target=server.serve_forever, daemon=True).start()
    return True


def start_web_server() -> None:
    """Démarre le serveur web (Flask/Werkzeug) sur PORT et sur les ports supplémentaires."""
    listening = []
    if _serve(PORT, primary=True):
        listening.append(PORT)
    for port in _extra_ports():
        if _serve(port, primary=False):
            listening.append(port)
    if not listening:
        raise SystemExit(f"Aucun port disponible (PORT={PORT}).")
    add_log("Web server (Flask) listening on " + ", ".join(f"http://0.0.0.0:{x}" for x in listening))


# ==================== KEEP-ALIVE AUTOMATIQUE ====================
def _ping_self_blocking() -> None:
    url = get_ping_url()
    if not url:
        return
    start = time.monotonic()
    try:
        res = requests.get(
            url,
            timeout=KEEP_ALIVE_TIMEOUT_S,
            headers={"User-Agent": "KeepAlive/1.0"},
        )
        ms = int((time.monotonic() - start) * 1000)
        icon = "✅" if res.status_code == 200 else f"⚠️{res.status_code}"
        add_log(f"[Keep-Alive] {icon} {url} ({ms}ms)")
        if res.status_code in (401, 403, 404) and not _hint_shown["done"]:
            _hint_shown["done"] = True
            add_log(f"[Keep-Alive] HTTP {res.status_code} : l'URL publique n'atteint pas /health "
                    "(chemin de passerelle manquant ou authentification requise). Définissez "
                    "APP_URL avec l'URL publique COMPLÈTE (chemin inclus) ou KEEP_ALIVE_INTERVAL=0 "
                    "pour désactiver le keep-alive.")
        _last_ping.update(ok=res.status_code == 200, status=res.status_code,
                          ms=ms, at=datetime.now(timezone.utc).isoformat(), error=None)
    except requests.RequestException as e:
        add_log(f"[Keep-Alive] ❌ {url} -- {e}")
        _last_ping.update(ok=False, status=None, ms=None,
                          at=datetime.now(timezone.utc).isoformat(), error=str(e))


async def keep_alive_task() -> None:
    """Boucle infinie : ping du domaine public toutes les KEEP_ALIVE_INTERVAL_S s."""
    if KEEP_ALIVE_INTERVAL_S <= 0:
        add_log("[Keep-Alive] Disabled (KEEP_ALIVE_INTERVAL=0).")
        return
    add_log(f"[Keep-Alive] Started -- self-pinging every {KEEP_ALIVE_INTERVAL_S}s.")
    waiting_logs = 0
    while True:
        try:
            if get_ping_url():
                waiting_logs = 0
                # requests est bloquant -> thread séparé pour ne pas figer le bot
                await asyncio.to_thread(_ping_self_blocking)
            else:
                waiting_logs += 1
                if waiting_logs == 1 or waiting_logs % 10 == 0:
                    add_log("[Keep-Alive] Public domain not detected yet -- waiting for the "
                            "first incoming request (or set APP_URL).")
        except Exception as e:  # ne jamais tuer la boucle
            add_log(f"[Keep-Alive] Unexpected error: {e}")
        await asyncio.sleep(KEEP_ALIVE_INTERVAL_S)


def get_status() -> dict:
    return {
        "domain": get_public_domain() or None,
        "interval_s": KEEP_ALIVE_INTERVAL_S,
        "port": PORT,
        "python": PYTHON_VERSION,
        "last_ping": dict(_last_ping),
    }
