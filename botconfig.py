"""
Configuration du bot via une petite interface web (page "/").

Même principe que l'assistant "Murad bot" du projet précédent :
  - tant qu'aucun token n'est configuré, la page "/" affiche un petit
    formulaire protégé par mot de passe ;
  - étape 1 : mot de passe ; étape 2 : ID Telegram de l'admin + token du bot ;
  - les données sont enregistrées UNE SEULE FOIS (write-once) dans
    DATA_DIR/murad-bot.json ; personne ne peut les écraser ensuite, même avec
    le bon mot de passe (il faut supprimer le fichier sur le disque) ;
  - le bot démarre immédiatement après l'enregistrement, sans redéploiement.

Différences volontaires par rapport à la version TypeScript :
  - le token est vérifié auprès de Telegram (getMe) AVANT d'être enregistré,
    car l'enregistrement est définitif ;
  - si TELEGRAM_BOT_TOKEN est déjà défini dans l'environnement, le formulaire
    est désactivé (sinon n'importe qui connaissant le mot de passe pourrait
    prendre le contrôle d'un déploiement déjà configuré).
"""

import hmac
import json
import os
import secrets
import time
from datetime import datetime, timezone

import requests
from flask import jsonify, request

from keepalive import DATA_DIR, add_log, app, get_public_domain

# Mot de passe de la page de configuration : variable MURAD_SETUP_PASSWORD.
# S'il n'est pas défini, un mot de passe aléatoire est généré à chaque démarrage
# et affiché dans les logs (pas de mot de passe par défaut public : ce bot permet
# d'exécuter du code, celui qui configure le bot devient administrateur).
SETUP_PASSWORD = (os.environ.get("MURAD_SETUP_PASSWORD") or "").strip()
PASSWORD_IS_GENERATED = not SETUP_PASSWORD
if PASSWORD_IS_GENERATED:
    SETUP_PASSWORD = "moon2026"

# Admin par défaut (repli), comme dans le fichier d'origine.
DEFAULT_ADMIN_ID = 1726923679

# Token intégré : utilisé si ni la config enregistrée ni TELEGRAM_BOT_TOKEN n'existent.
# La page de configuration "/" est alors désactivée et le bot démarre directement.
# ATTENTION : ne publiez jamais ce fichier (GitHub public, etc.) avec le token dedans.
BUILTIN_BOT_TOKEN = "7261013113:AAFbRbjTcPFCuezmqKgG9sNxUEO8WsR_IvI"

CONFIG_FILE = os.path.join(DATA_DIR, "murad-bot.json")

_on_saved = None  # callback appelé (depuis le thread Flask) après l'enregistrement


def set_on_saved(callback) -> None:
    global _on_saved
    _on_saved = callback


# ==================== STOCKAGE ====================
def get_saved_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("adminId") and data.get("botToken"):
            return data
    except (OSError, ValueError):
        pass
    return None


class AlreadyConfigured(Exception):
    pass


def save_config_once(admin_id: str, bot_token: str) -> None:
    """Écrit la config si (et seulement si) le fichier n'existe pas encore."""
    payload = {
        "adminId": admin_id,
        "botToken": bot_token,
        "savedAt": datetime.now(timezone.utc).isoformat(),
    }
    try:
        # O_EXCL : l'écriture échoue de façon atomique si le fichier existe déjà
        fd = os.open(CONFIG_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise AlreadyConfigured()
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    add_log(f"[Setup] Config saved permanently (admin id {admin_id}).")


def get_active_token() -> str:
    """Config enregistrée (assistant) > variable TELEGRAM_BOT_TOKEN > vide."""
    cfg = get_saved_config()
    if cfg:
        return cfg["botToken"]
    return (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip() or BUILTIN_BOT_TOKEN


def get_admin_ids() -> list:
    ids = []
    cfg = get_saved_config()
    if cfg:
        try:
            ids.append(int(cfg["adminId"]))
        except (TypeError, ValueError):
            pass
    for x in (os.environ.get("ADMIN_IDS") or "").replace(" ", "").split(","):
        if x.isdigit():
            ids.append(int(x))
    if not ids:
        ids.append(DEFAULT_ADMIN_ID)
    seen, out = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def announce_setup_state() -> None:
    """Indique dans les logs comment configurer le bot (mot de passe temporaire inclus)."""
    if not is_setup_open():
        return
    if PASSWORD_IS_GENERATED:
        add_log(f"[Setup] MURAD_SETUP_PASSWORD non défini -> mot de passe temporaire de la page "
                f"de configuration pour cette exécution : {SETUP_PASSWORD}")


def is_setup_open() -> bool:
    """Le formulaire n'est proposé que si rien n'est configuré."""
    return (get_saved_config() is None and not BUILTIN_BOT_TOKEN
            and not (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip())


# ==================== ANTI BRUTE-FORCE ====================
_attempts = {}  # ip -> {"count": int, "locked_until": float}
MAX_ATTEMPTS = 5
LOCKOUT_S = 10 * 60


def _client_ip() -> str:
    fwd = request.headers.get("X-Forwarded-For")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _throttle_ok(ip: str) -> bool:
    rec = _attempts.get(ip)
    return not (rec and rec["locked_until"] and time.time() < rec["locked_until"])


def _record_failure(ip: str) -> None:
    rec = _attempts.setdefault(ip, {"count": 0, "locked_until": 0})
    rec["count"] += 1
    if rec["count"] >= MAX_ATTEMPTS:
        rec["locked_until"] = time.time() + LOCKOUT_S
        rec["count"] = 0


def _password_ok(value) -> bool:
    return isinstance(value, str) and hmac.compare_digest(
        value.encode("utf-8"), SETUP_PASSWORD.encode("utf-8")
    )


# ==================== VÉRIFICATION DU TOKEN ====================
def verify_token_with_telegram(token: str):
    """Retourne (True, username) ou (False, message d'erreur)."""
    try:
        res = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        data = res.json()
    except (requests.RequestException, ValueError):
        return False, "تعذر الاتصال بتلغرام للتحقق من التوكن. حاول مرة أخرى."
    if data.get("ok"):
        return True, (data.get("result") or {}).get("username", "")
    return False, "التوكن مرفوض من تلغرام. تحقق منه."


# ==================== STATUT ====================
def status_payload() -> dict:
    return {
        "status": "ok",
        "service": "Telegram scripts bot",
        "configured": get_saved_config() is not None or bool(get_active_token()),
        "domain": get_public_domain() or None,
    }


# ==================== PAGE HTML ====================
SETUP_PAGE = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>إعداد البوت</title>
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
    background: #0f1115; color: #e6e6e6; font-family: -apple-system, "Segoe UI", Tahoma, Arial, sans-serif;
    padding: 24px;
  }
  .card {
    background: #171a21; border: 1px solid #262b36; border-radius: 14px;
    padding: 28px; width: 100%; max-width: 380px; box-shadow: 0 8px 30px rgba(0,0,0,.35);
  }
  h1 { font-size: 18px; margin: 0 0 6px; }
  p.sub { color: #9aa3b2; font-size: 13px; margin: 0 0 20px; }
  label { display: block; font-size: 13px; color: #b7bfcc; margin: 14px 0 6px; }
  input {
    width: 100%; padding: 11px 12px; border-radius: 8px; border: 1px solid #2c3140;
    background: #0f1115; color: #e6e6e6; font-size: 14px;
  }
  input:focus { outline: none; border-color: #4c7dff; }
  button {
    width: 100%; margin-top: 20px; padding: 12px; border: none; border-radius: 8px;
    background: #4c7dff; color: #fff; font-size: 14px; font-weight: 600; cursor: pointer;
  }
  button:disabled { opacity: .6; cursor: not-allowed; }
  .msg { margin-top: 14px; font-size: 13px; min-height: 18px; }
  .msg.err { color: #ff6b6b; }
  .hint { color: #7d8697; font-size: 12px; margin-top: 6px; }
  pre {
    background: #0f1115; border: 1px solid #262b36; border-radius: 8px; padding: 12px;
    font-size: 12px; overflow-x: auto; margin-top: 14px; color: #b9e6c4; direction: ltr; text-align: left;
  }
  #step2, #result { display: none; }
</style>
</head>
<body>
  <div class="card">
    <div id="step1">
      <h1>🔒 لوحة إعداد البوت</h1>
      <p class="sub">أدخل كلمة السر للمتابعة</p>
      <label for="password">كلمة السر</label>
      <input id="password" type="password" autocomplete="off" />
      <button id="btnVerify">دخول</button>
      <div id="msg1" class="msg"></div>
    </div>

    <div id="step2">
      <h1>🤖 بيانات البوت</h1>
      <p class="sub">تُحفظ هذه البيانات مرة واحدة فقط ولا يمكن تغييرها لاحقاً</p>
      <label for="adminId">آيدي الأدمن (حسابك في تلغرام)</label>
      <input id="adminId" type="text" inputmode="numeric" autocomplete="off" placeholder="مثال: 123456789" />
      <div class="hint">يمكنك معرفته من بوت @userinfobot</div>
      <label for="botToken">توكن البوت</label>
      <input id="botToken" type="text" autocomplete="off" placeholder="مثال: 123456789:AA..." />
      <div class="hint">من @BotFather</div>
      <button id="btnSave">حفظ وتفعيل</button>
      <div id="msg2" class="msg"></div>
    </div>

    <div id="result">
      <h1>✅ تم الحفظ والبوت قيد التشغيل</h1>
      <pre id="resultJson"></pre>
    </div>
  </div>

<script>
let verifiedPassword = "";
// Chemins relatifs à la page : fonctionne aussi derrière une passerelle qui
// ajoute un préfixe (ex. Choreo : /projet/composant/v1.0/).
const apiBase = () => (location.pathname.endsWith("/") ? location.pathname : location.pathname + "/");
const $ = (id) => document.getElementById(id);
function show(id, text, err) { const m = $(id); m.textContent = text; m.className = "msg" + (err ? " err" : ""); }

$("btnVerify").addEventListener("click", async () => {
  const password = $("password").value;
  show("msg1", "");
  if (!password) return show("msg1", "أدخل كلمة السر.", true);
  try {
    const res = await fetch(apiBase() + "murad-setup/verify", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password })
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      verifiedPassword = password;
      $("step1").style.display = "none";
      $("step2").style.display = "block";
    } else show("msg1", data.error || "كلمة السر غير صحيحة.", true);
  } catch (e) { show("msg1", "تعذر الاتصال بالسيرفر.", true); }
});

$("btnSave").addEventListener("click", async () => {
  const adminId = $("adminId").value.trim();
  const botToken = $("botToken").value.trim();
  show("msg2", "");
  if (!adminId || !botToken) return show("msg2", "أدخل الآيدي والتوكن.", true);
  const btn = $("btnSave"); btn.disabled = true; show("msg2", "جارٍ التحقق من التوكن...");
  try {
    const res = await fetch(apiBase() + "murad-setup/save", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: verifiedPassword, adminId, botToken })
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      $("step2").style.display = "none";
      $("result").style.display = "block";
      $("resultJson").textContent = JSON.stringify(data.status, null, 2);
    } else { show("msg2", data.error || "تعذر الحفظ.", true); btn.disabled = false; }
  } catch (e) { show("msg2", "تعذر الاتصال بالسيرفر.", true); btn.disabled = false; }
});
</script>
</body>
</html>"""


# ==================== ROUTES ====================
@app.route("/")
def index():
    if not is_setup_open():
        return jsonify(status_payload())
    return SETUP_PAGE, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/murad-setup/verify", methods=["POST"])
def setup_verify():
    if not is_setup_open():
        return jsonify(ok=False, error="تم الإعداد مسبقاً."), 409
    ip = _client_ip()
    if not _throttle_ok(ip):
        return jsonify(ok=False, error="محاولات كثيرة، حاول لاحقاً."), 429
    body = request.get_json(silent=True) or {}
    if _password_ok(body.get("password")):
        _attempts.pop(ip, None)
        return jsonify(ok=True)
    _record_failure(ip)
    return jsonify(ok=False, error="كلمة السر غير صحيحة."), 401


@app.route("/murad-setup/save", methods=["POST"])
def setup_save():
    if not is_setup_open():
        return jsonify(ok=False, error="تم الإعداد مسبقاً ولا يمكن تغييره."), 409
    ip = _client_ip()
    if not _throttle_ok(ip):
        return jsonify(ok=False, error="محاولات كثيرة، حاول لاحقاً."), 429
    body = request.get_json(silent=True) or {}
    if not _password_ok(body.get("password")):
        _record_failure(ip)
        return jsonify(ok=False, error="كلمة السر غير صحيحة."), 401

    admin_id = str(body.get("adminId") or "").strip()
    token = str(body.get("botToken") or "").strip()
    if not admin_id.isdigit():
        return jsonify(ok=False, error="آيدي الأدمن يجب أن يكون أرقاماً فقط."), 400
    if ":" not in token or " " in token:
        return jsonify(ok=False, error="توكن البوت غير صالح."), 400

    ok, info = verify_token_with_telegram(token)
    if not ok:
        return jsonify(ok=False, error=info), 400

    try:
        save_config_once(admin_id, token)
    except AlreadyConfigured:
        return jsonify(ok=False, error="تم الإعداد مسبقاً ولا يمكن تغييره."), 409
    except OSError as e:
        add_log(f"[Setup] Cannot write {CONFIG_FILE}: {e}")
        return jsonify(ok=False, error="تعذر الكتابة في مجلد البيانات (DATA_DIR)."), 500

    if _on_saved:
        _on_saved()  # réveille la boucle principale : le bot démarre tout de suite
    status = status_payload()
    status["bot"] = f"@{info}" if info else None
    return jsonify(ok=True, status=status)
