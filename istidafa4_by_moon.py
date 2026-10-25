import subprocess
import os
import sys
import asyncio
import ast
import html
import signal
import time
import importlib.util
from collections import deque
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest

# Configuration du token / ID admin via l'interface web "/" (voir botconfig.py)
import botconfig
# Serveur web + keep-alive automatique (voir keepalive.py)
from keepalive import (DATA_DIR, PYTHON_VERSION, add_log, start_web_server, keep_alive_task,
                       get_status, get_public_domain, PORT as WEB_PORT)
# Pays / mémoire / espace disque (voir serverinfo.py)
import serverinfo
# Rotation automatique de proxys pour la connexion à Telegram (voir proxymanager.py)
import proxymanager

# ==================== CONFIGURATION DU BOT ====================
# Le token et l'ID admin sont saisis via la page "/" (ou TELEGRAM_BOT_TOKEN /
# ADMIN_IDS en variables d'environnement) : plus aucun token dans le code.
# Les scripts des utilisateurs sont stockés dans DATA_DIR (volume persistant).
BASE_DIR = os.path.join(DATA_DIR, "scripts")
processes = {}          # (user_id, nom_fichier) -> process asyncio en cours
user_states = {}
banned_users = set()
# Liste modifiée en place quand l'assistant enregistre un nouvel ID admin
ADMINS = botconfig.get_admin_ids()
BOT_MODE = "public"  # Peut être "public" ou "private"
START_TIME = time.time()

# 📂 Créer le dossier des scripts s'il n'existe pas
os.makedirs(BASE_DIR, exist_ok=True)

bot = None  # créé dans main() dès que le token est disponible
dp = Dispatcher()

# ==================== INTERFACE UTILISATEUR ====================
# 🎛️ Clavier principal pour les utilisateurs normaux
main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Ajouter un script")],
        [KeyboardButton(text="▶️ Exécuter un script")],
        [KeyboardButton(text="📜 Liste des scripts")],
        [KeyboardButton(text="❌ Arrêter et supprimer un script")],
    ],
    resize_keyboard=True
)

# 🎛️ Clavier principal pour les admins
admin_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Ajouter un script")],
        [KeyboardButton(text="▶️ Exécuter un script")],
        [KeyboardButton(text="📜 Liste des scripts")],
        [KeyboardButton(text="❌ Arrêter et supprimer un script")],
        [KeyboardButton(text="👑 Panel admin")],
    ],
    resize_keyboard=True
)

# 🎛️ Panel admin
admin_panel_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🔒 Bannir un utilisateur"), KeyboardButton(text="🔓 Débannir un utilisateur")],
        [KeyboardButton(text="💾 Sauvegarder scripts utilisateur"), KeyboardButton(text="📋 Liste de tous les scripts")],
        [KeyboardButton(text="🛑 Arrêter/supprimer script utilisateur")],
        [KeyboardButton(text="🔐 Mode privé"), KeyboardButton(text="🌍 Mode public")],
        [KeyboardButton(text="💻 Envoyer commande terminal")],
        [KeyboardButton(text="📊 État du serveur"), KeyboardButton(text="🔄 État Keep-Alive")],
        [KeyboardButton(text="⬅️ Retour au menu principal")],
    ],
    resize_keyboard=True
)

# 🎛️ Clavier pour la saisie manuelle des commandes
command_input_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="saisir la commande manuellement")],
        [KeyboardButton(text="⬅️ Annuler")],
    ],
    resize_keyboard=True
)

# ==================== COMMANDES DU BOT ====================
@dp.message(Command("start"))
async def start(message: types.Message):
    if BOT_MODE == "private" and message.from_user.id not in ADMINS:
        await message.reply("🚫 Le bot est en mode privé, seuls les admins peuvent l'utiliser.")
        return
    
    if message.from_user.id in banned_users:
        await message.reply("🚫 Vous êtes banni de ce bot.")
        return
    
    keyboard = admin_keyboard if message.from_user.id in ADMINS else main_keyboard
    await message.reply("👋 Bonjour ! Choisissez une action :", reply_markup=keyboard)

# ==================== COMMANDES ADMIN ====================
@dp.message(lambda message: message.text == "👑 Panel admin" and message.from_user.id in ADMINS)
async def admin_panel(message: types.Message):
    await message.reply("👑 Panel d'administration", reply_markup=admin_panel_keyboard)

# ==================== ÉTAT KEEP-ALIVE (lecture seule) ====================
# Le keep-alive est entièrement automatique (voir keepalive.py) : plus de
# configuration manuelle, seulement un affichage de l'état.
@dp.message(lambda message: message.text == "🔄 État Keep-Alive" and message.from_user.id in ADMINS)
async def keep_alive_status(message: types.Message):
    st = get_status()
    lp = st["last_ping"]
    if lp["at"] is None:
        last = "aucun ping pour l'instant"
    elif lp["ok"]:
        last = f"✅ HTTP {lp['status']} ({lp['ms']} ms)"
    else:
        last = f"❌ {lp['status'] or lp['error']}"
    await message.reply(
        f"📊 Keep-Alive automatique:\n"
        f"Domaine détecté: {st['domain'] or 'pas encore détecté'}\n"
        f"Intervalle: {st['interval_s']} secondes\n"
        f"Port: {st['port']}\n"
        f"Dernier ping: {last}",
        reply_markup=admin_panel_keyboard
    )

# ==================== ÉTAT DU SERVEUR (pays, mémoire, espace) ====================
async def send_server_status(message: types.Message):
    loc, mem, disk, (scripts_bytes, scripts_count) = await asyncio.gather(
        asyncio.to_thread(serverinfo.get_location),
        asyncio.to_thread(serverinfo.get_memory),
        asyncio.to_thread(serverinfo.get_disk, DATA_DIR),
        asyncio.to_thread(serverinfo.dir_size, BASE_DIR),
    )
    running = sum(1 for p in processes.values() if p.returncode is None)

    if loc:
        details = []
        if loc["ip"]:
            details.append(f"🌐 IP: <code>{html.escape(loc['ip'])}</code>")
        if loc["city"]:
            details.append(f"🏙️ {html.escape(loc['city'])}")
        if loc["isp"]:
            details.append(f"🛰️ {html.escape(loc['isp'])}")
        country_ar = serverinfo.country_name_ar(loc["code"])
        country = f"{loc['flag']} {html.escape(country_ar)} ({html.escape(loc['country'])})" if country_ar \
            else f"{loc['flag']} {html.escape(loc['country'])}"
        country_detail = "  |  ".join(details)
    else:
        country, country_detail = "🌍 Inconnu", ""

    lines = ["📊 <b>État du serveur</b>", "", f"🗺️ <b>Pays :</b> {country}"]
    if country_detail:
        lines.append(country_detail)

    lines.append(f"💾 <b>Mémoire du bot :</b> {serverinfo.fmt_bytes(mem['process'])}")
    if mem["total"] and mem["used"] is not None:
        pct = mem["used"] * 100 / mem["total"]
        lines.append(
            f"🖥️ <b>Mémoire serveur :</b> {serverinfo.fmt_bytes(mem['used'])} / "
            f"{serverinfo.fmt_bytes(mem['total'])} ({pct:.0f}%)\n<code>{serverinfo.bar(pct)}</code>"
        )
    if disk["total"]:
        pct = (disk["total"] - disk["free"]) * 100 / disk["total"]
        lines.append(
            f"🗄️ <b>Espace disque :</b> {serverinfo.fmt_bytes(disk['free'])} libres sur "
            f"{serverinfo.fmt_bytes(disk['total'])} ({pct:.0f}% utilisé)\n<code>{serverinfo.bar(pct)}</code>"
        )
    lines.append(f"📁 <b>Scripts des utilisateurs :</b> {serverinfo.fmt_bytes(scripts_bytes)} ({scripts_count} fichier(s))")
    lines.append(f"⚙️ <b>Scripts en cours :</b> {running}")
    lines.append(f"🐍 <b>Python :</b> {PYTHON_VERSION}")
    lines.append(f"⏱️ <b>Uptime :</b> {serverinfo.fmt_duration(time.time() - START_TIME)}")
    lines.append(f"🌐 <b>Domaine :</b> <code>{html.escape(get_public_domain() or 'non détecté')}</code>")
    await message.reply("\n".join(lines), parse_mode="HTML", reply_markup=admin_panel_keyboard)


@dp.message(lambda message: message.text == "📊 État du serveur" and message.from_user.id in ADMINS)
async def server_status_button(message: types.Message):
    await send_server_status(message)


@dp.message(Command("status"), lambda message: message.from_user.id in ADMINS)
async def server_status_command(message: types.Message):
    await send_server_status(message)

# ==================== AUTRES COMMANDES ADMIN ====================
@dp.message(lambda message: message.text == "💻 Envoyer commande terminal" and message.from_user.id in ADMINS)
async def send_terminal_command(message: types.Message):
    user_states[message.from_user.id] = "terminal_command"
    await message.reply("📝 Entrez la commande à exécuter dans le terminal:", reply_markup=command_input_keyboard)

# Gestion des commandes terminal
@dp.message(lambda message: user_states.get(message.from_user.id) == "terminal_command" and message.from_user.id in ADMINS)
async def handle_terminal_command(message: types.Message):
    if message.text == "⬅️ Annuler":
        user_states.pop(message.from_user.id, None)
        await message.reply("👑 Panel d'administration", reply_markup=admin_panel_keyboard)
        return
    
    if message.text == "saisir la commande manuellement":
        user_states[message.from_user.id] = "manual_command_input"
        await message.reply("📝 Entrez maintenant la commande à exécuter:")
        return
    
    command = message.text
    await message.reply(f"⚙️ Exécution de la commande: `{command}`", parse_mode="Markdown")
    
    try:
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()
        
        if stdout:
            output = stdout.decode()
            if len(output) > 4000:
                output = output[:4000] + "\n... (sortie tronquée)"
            await message.reply(f"📤 Sortie:\n```\n{output}\n```", parse_mode="Markdown")
        
        if stderr:
            error = stderr.decode()
            if len(error) > 4000:
                error = error[:4000] + "\n... (erreur tronquée)"
            await message.reply(f"⚠️ Erreur:\n```\n{error}\n```", parse_mode="Markdown")
        
        if process.returncode == 0:
            await message.reply("✅ Commande exécutée avec succès!")
        else:
            await message.reply(f"❌ Commande terminée avec le code de sortie: {process.returncode}")
    
    except Exception as e:
        await message.reply(f"⚠️ Erreur lors de l'exécution de la commande: {str(e)}")
    
    user_states.pop(message.from_user.id, None)
    await message.reply("👑 Panel d'administration", reply_markup=admin_panel_keyboard)

@dp.message(lambda message: message.text == "🔐 Mode privé" and message.from_user.id in ADMINS)
async def set_private_mode(message: types.Message):
    global BOT_MODE
    BOT_MODE = "private"
    await message.reply("✅ Le bot est maintenant en mode privé (admins seulement)")
    await message.reply("👑 Panel d'administration", reply_markup=admin_panel_keyboard)

@dp.message(lambda message: message.text == "🌍 Mode public" and message.from_user.id in ADMINS)
async def set_public_mode(message: types.Message):
    global BOT_MODE
    BOT_MODE = "public"
    await message.reply("✅ Le bot est maintenant en mode public (tout le monde)")
    await message.reply("👑 Panel d'administration", reply_markup=admin_panel_keyboard)

@dp.message(lambda message: message.text == "📋 Liste de tous les scripts" and message.from_user.id in ADMINS)
async def list_all_scripts(message: types.Message):
    MAX_MESSAGE_LENGTH = 4000
    
    text = "📜 Liste des scripts de tous les utilisateurs:\n"
    messages = [text]
    current_length = len(text)
    
    for user_id in os.listdir(BASE_DIR):
        user_folder = os.path.join(BASE_DIR, str(user_id))
        if os.path.isdir(user_folder):
            scripts = os.listdir(user_folder)
            user_text = f"\n👤 {user_id}:\n"
            
            for script in scripts:
                status = "🟢 Actif" if (int(user_id), script) in processes and processes[(int(user_id), script)].returncode is None else "🔴 Arrêté"
                script_line = f" - {script}: {status}\n"
                
                if current_length + len(user_text) + len(script_line) > MAX_MESSAGE_LENGTH:
                    messages.append("📜 Liste des scripts (suite):\n")
                    current_length = len(messages[-1])
                
                messages[-1] += user_text + script_line
                current_length += len(user_text) + len(script_line)
                user_text = ""
    
    for msg in messages:
        await message.reply(msg)
    
    await message.reply("👑 Panel d'administration", reply_markup=admin_panel_keyboard)

@dp.message(lambda message: message.text == "🔒 Bannir un utilisateur" and message.from_user.id in ADMINS)
async def ban_user_prompt(message: types.Message):
    user_states[message.from_user.id] = "ban_user"
    await message.reply("📝 Envoyez l'ID de l'utilisateur à bannir:")

@dp.message(lambda message: message.text == "🔓 Débannir un utilisateur" and message.from_user.id in ADMINS)
async def unban_user_prompt(message: types.Message):
    user_states[message.from_user.id] = "unban_user"
    await message.reply("📝 Envoyez l'ID de l'utilisateur à débannir:")

@dp.message(lambda message: message.text == "💾 Sauvegarder scripts utilisateur" and message.from_user.id in ADMINS)
async def save_user_prompt(message: types.Message):
    user_states[message.from_user.id] = "save_user"
    await message.reply("📝 Envoyez l'ID de l'utilisateur dont vous voulez sauvegarder les scripts:")

@dp.message(lambda message: message.text == "🛑 Arrêter/supprimer script utilisateur" and message.from_user.id in ADMINS)
async def admin_stop_script_prompt(message: types.Message):
    user_states[message.from_user.id] = "admin_stop_script_user"
    await message.reply("📝 Envoyez l'ID de l'utilisateur dont vous voulez arrêter/supprimer les scripts:")

# ==================== COMMANDES UTILISATEUR ====================
@dp.message(lambda message: message.text == "➕ Ajouter un script")
async def prompt_add_script(message: types.Message):
    if BOT_MODE == "private" and message.from_user.id not in ADMINS:
        await message.reply("🚫 Le bot est en mode privé, seuls les admins peuvent l'utiliser.")
        return
    
    if message.from_user.id in banned_users:
        await message.reply("🚫 Vous êtes banni de ce bot.")
        return
    
    user_states[message.from_user.id] = "ajout_script"
    await message.reply("📤 Envoyez-moi un fichier **.py** à ajouter.")

@dp.message(lambda message: message.document and user_states.get(message.from_user.id) == "ajout_script")
async def handle_script_upload(message: types.Message):
    user_id = message.from_user.id

    if user_id in banned_users:
        await message.reply("🚫 Vous êtes banni de ce bot.")
        return

    document = message.document
    if not document.file_name.endswith(".py"):
        await message.reply("⚠️ Seuls les fichiers `.py` sont acceptés.")
        return

    user_folder = os.path.join(BASE_DIR, str(user_id))
    os.makedirs(user_folder, exist_ok=True)

    if user_id not in ADMINS:
        existing_files = os.listdir(user_folder)
        if len(existing_files) >= 4:
            await message.reply("⚠️ Vous ne pouvez pas avoir plus de 4 scripts. Supprimez-en un d'abord.")
            return

    file_path = os.path.join(user_folder, document.file_name)
    file = await bot.get_file(document.file_id)
    await bot.download_file(file.file_path, file_path)

    await message.reply(f"✅ Le script **{document.file_name}** a été ajouté avec succès!")
    user_states.pop(user_id, None)

# ==================== INSTALLATION DES BIBLIOTHÈQUES (affichage en direct) ====================
# Commande pip (modifiable, utile pour les tests)
PIP_CMD = [sys.executable, "-m", "pip"]
PIP_TIMEOUT_S = 300

# Nom du module à importer -> nom du paquet pip quand ils diffèrent
IMPORT_TO_PIP = {
    "cv2": "opencv-python-headless", "PIL": "pillow", "bs4": "beautifulsoup4",
    "yaml": "pyyaml", "sklearn": "scikit-learn", "skimage": "scikit-image",
    "telebot": "pyTelegramBotAPI", "telegram": "python-telegram-bot",
    "dotenv": "python-dotenv", "Crypto": "pycryptodome", "dateutil": "python-dateutil",
    "serial": "pyserial", "jwt": "pyjwt", "OpenSSL": "pyopenssl", "socks": "pysocks",
    "docx": "python-docx", "fitz": "pymupdf", "attr": "attrs",
}


# Bibliothèques installées à la demande : dans DATA_DIR/libs via `pip --target`.
# Fonctionne même si site-packages est en lecture seule (Choreo, conteneurs
# non-root) ou dans un virtualenv ; les scripts la voient grâce à PYTHONPATH.
LIBS_DIR = os.path.join(DATA_DIR, "libs")
try:
    os.makedirs(LIBS_DIR, exist_ok=True)
except OSError:
    pass


def _ensure_libs_on_path():
    """Rend visibles, dans le bot lui-même, les paquets installés après son démarrage."""
    if LIBS_DIR not in sys.path:
        sys.path.append(LIBS_DIR)
    importlib.invalidate_caches()


def _stdlib_names():
    """Noms des modules de la bibliothèque standard (compatible Python 3.9+)."""
    names = getattr(sys, "stdlib_module_names", None)  # Python 3.10+
    if names:
        return set(names)
    import sysconfig
    found = set(sys.builtin_module_names)
    stdlib = sysconfig.get_paths().get("stdlib")
    if stdlib and os.path.isdir(stdlib):
        for entry in os.listdir(stdlib):
            found.add(entry[:-3] if entry.endswith(".py") else entry.split(".")[0])
    return found


STDLIB_NAMES = _stdlib_names()


def find_missing_libraries(file_path):
    """Analyse les imports du script (module ast) et retourne les paquets pip manquants."""
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        source = f.read()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []  # l'erreur de syntaxe sera affichée à l'exécution

    _ensure_libs_on_path()
    local_dir = os.path.dirname(file_path)
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module.split(".")[0])

    missing = []
    for mod in sorted(modules):
        if mod in STDLIB_NAMES:
            continue
        if os.path.exists(os.path.join(local_dir, mod + ".py")) or os.path.isdir(os.path.join(local_dir, mod)):
            continue  # module local du script
        try:
            if importlib.util.find_spec(mod) is not None:
                continue
        except (ImportError, ValueError, AttributeError):
            pass
        missing.append(IMPORT_TO_PIP.get(mod, mod))
    return missing


class LiveMessage:
    """Message Telegram mis à jour en direct (au plus une édition toutes les ~1,2 s)."""

    MIN_INTERVAL_S = 1.2

    def __init__(self, target: types.Message):
        self.target = target
        self.msg = None
        self._last_edit = 0.0
        self._last_text = ""

    async def start(self, text: str):
        self.msg = await self.target.reply(text, parse_mode="HTML")
        self._last_text, self._last_edit = text, time.monotonic()

    async def update(self, text: str, force: bool = False):
        now = time.monotonic()
        if text == self._last_text or (not force and now - self._last_edit < self.MIN_INTERVAL_S):
            return
        try:
            await self.msg.edit_text(text, parse_mode="HTML")
            self._last_text, self._last_edit = text, now
        except TelegramBadRequest:
            pass  # "message is not modified"
        except Exception as e:  # limite de débit, réseau...
            add_log(f"[Live] edit failed: {e}")


async def install_libraries_live(message: types.Message, libs):
    """Installe les paquets avec pip en affichant la sortie en direct. Retourne la liste des échecs."""
    live = LiveMessage(message)
    status = {lib: "⏳" for lib in libs}
    tail = deque(maxlen=12)

    def render(title="📦 <b>Installation des bibliothèques</b>"):
        rows = "\n".join(f"{icon} {html.escape(lib)}" for lib, icon in status.items())
        log = "\n".join(html.escape(line) for line in tail)
        return f"{title}\n\n{rows}" + (f"\n\n<pre>{log}</pre>" if log else "")

    await live.start(render())
    failed = []
    for lib in libs:
        tail.clear()
        tail.append(f"$ pip install {lib}")
        await live.update(render())
        try:
            proc = await asyncio.create_subprocess_exec(
                *PIP_CMD, "install", "--no-input", "--progress-bar", "off", "--target", LIBS_DIR, lib,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
        except Exception as e:
            status[lib] = "❌"
            failed.append(lib)
            tail.append(f"Impossible de lancer pip : {e}")
            continue

        async def pump():
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").rstrip()
                if line:
                    tail.append(line[:160])
                    await live.update(render())

        try:
            await asyncio.wait_for(asyncio.gather(pump(), proc.wait()), timeout=PIP_TIMEOUT_S)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            tail.append(f"⏱️ Délai dépassé ({PIP_TIMEOUT_S}s)")

        if proc.returncode == 0:
            status[lib] = "✅"
        else:
            status[lib] = "❌"
            failed.append(lib)
        await live.update(render(), force=True)

    _ensure_libs_on_path()
    if failed:
        title = f"⚠️ <b>Installation terminée avec {len(failed)} échec(s)</b>"
    else:
        title = "✅ <b>Bibliothèques installées</b>"
    await live.update(render(title), force=True)
    return failed


# ==================== EXÉCUTION / ARRÊT DES SCRIPTS ====================
OUTPUT_TAIL_CHARS = 3000
script_tasks = {}    # (user_id, fichier) -> tâche de surveillance
stop_reasons = {}    # (user_id, fichier) -> "owner" | "admin" | "shutdown"


async def send_safe(chat_id, text):
    """Envoie un message HTML sans jamais lever d'exception."""
    if bot is None:
        return
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception as e:
        add_log(f"[Notify] cannot send to {chat_id}: {e}")


def _signal_group(process, sig):
    try:
        os.killpg(os.getpgid(process.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.send_signal(sig)
        except ProcessLookupError:
            pass


async def stop_process(key, reason, grace=5):
    """Arrête proprement (SIGTERM puis SIGKILL) un script. Retourne True s'il tournait."""
    process = processes.get(key)
    if not process or process.returncode is not None:
        return False
    stop_reasons[key] = reason
    task = script_tasks.get(key)
    _signal_group(process, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=grace)
    except asyncio.TimeoutError:
        _signal_group(process, signal.SIGKILL)
        await process.wait()
    if task:  # laisse la tâche de surveillance envoyer sa notification
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=10)
        except Exception:
            pass
    return True


async def _read_tail(stream, buf: bytearray):
    """Lit un flux en ne gardant que la fin (évite de saturer la mémoire)."""
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > OUTPUT_TAIL_CHARS * 2:
            del buf[: len(buf) - OUTPUT_TAIL_CHARS * 2]


def _describe_exit(code):
    if code is None:
        return "inconnu"
    if code < 0:
        try:
            name = signal.Signals(-code).name
        except ValueError:
            name = f"signal {-code}"
        hint = " — souvent un manque de mémoire" if -code == 9 else ""
        return f"{code} ({name}{hint})"
    return str(code)


async def _supervise_script(chat_id, user_id, filename, process):
    """Attend la fin du script puis prévient son propriétaire (arrêt manuel, fin normale ou crash)."""
    key = (user_id, filename)
    out, err = bytearray(), bytearray()
    started = time.monotonic()
    try:
        await asyncio.gather(_read_tail(process.stdout, out), _read_tail(process.stderr, err), process.wait())
    except Exception as e:
        add_log(f"[Script] supervision error for {filename}: {e}")
    if processes.get(key) is process:
        del processes[key]

    reason = stop_reasons.pop(key, None)
    code = process.returncode
    duration = serverinfo.fmt_duration(time.monotonic() - started)
    name = html.escape(filename)
    if reason == "owner":
        head = f"⛔ <b>Script arrêté</b>\n📄 {name}\n🕒 Durée d'exécution : {duration}"
    elif reason == "admin":
        head = f"⛔ <b>Script arrêté par un administrateur</b>\n📄 {name}\n🕒 Durée d'exécution : {duration}"
    elif reason == "shutdown":
        head = (f"🔴 <b>Le bot s'arrête : votre script a été arrêté</b>\n📄 {name}\n"
                f"🕒 Durée d'exécution : {duration}\nRelancez-le quand le bot sera de nouveau en ligne.")
    elif code == 0:
        head = f"✅ <b>Script terminé normalement</b>\n📄 {name}\n🕒 Durée : {duration}"
    else:
        head = f"💥 <b>Le script s'est arrêté</b>\n📄 {name}\n🔢 Code de sortie : {_describe_exit(code)}\n🕒 Durée : {duration}"
    add_log(f"[Script] {filename} (user {user_id}) stopped: reason={reason or 'exit'} code={code}")
    await send_safe(chat_id, head)

    for title, buf in (("📤 Sortie", out), ("⚠️ Erreurs", err)):
        text = bytes(buf).decode(errors="replace").strip()
        if text:
            if len(text) > OUTPUT_TAIL_CHARS:
                text = "…" + text[-OUTPUT_TAIL_CHARS:]
            await send_safe(chat_id, f"{title} :\n<pre>{html.escape(text)}</pre>")


async def launch_script(message: types.Message, user_id: int, filename: str, file_path: str):
    key = (user_id, filename)
    running = processes.get(key)
    if running and running.returncode is None:
        await message.reply(f"ℹ️ Le script {filename} est déjà en cours d'exécution.")
        return

    try:
        missing = await asyncio.to_thread(find_missing_libraries, file_path)
    except Exception as e:
        add_log(f"[Script] import analysis failed for {filename}: {e}")
        missing = []

    if missing:
        await install_libraries_live(message, missing)
    else:
        await message.reply("✅ Toutes les bibliothèques sont déjà installées.")

    # Le token du bot n'est pas transmis aux scripts des utilisateurs
    env = {k: v for k, v in os.environ.items() if k not in ("TELEGRAM_BOT_TOKEN", "MURAD_SETUP_PASSWORD")}
    # Les bibliothèques installées à la demande (LIBS_DIR) sont visibles par le script
    env["PYTHONPATH"] = os.pathsep.join(p for p in (env.get("PYTHONPATH"), LIBS_DIR) if p)
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-u", file_path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=env, start_new_session=True,
    )
    processes[key] = process
    await message.reply(f"🚀 Script {filename} démarré (PID {process.pid}). Vous serez prévenu quand il s'arrêtera.")
    task = asyncio.create_task(_supervise_script(message.chat.id, user_id, filename, process))
    script_tasks[key] = task
    task.add_done_callback(lambda _t, k=key: script_tasks.pop(k, None))

@dp.message(lambda message: message.text == "❌ Arrêter et supprimer un script")
async def stop_and_delete_script(message: types.Message):
    if BOT_MODE == "private" and message.from_user.id not in ADMINS:
        await message.reply("🚫 Le bot est en mode privé, seuls les admins peuvent l'utiliser.")
        return
    
    user_id = message.from_user.id
    user_states[user_id] = "suppression"

    user_folder = os.path.join(BASE_DIR, str(user_id))
    os.makedirs(user_folder, exist_ok=True)

    files = os.listdir(user_folder)
    if not files:
        await message.reply("🚫 Aucun script trouvé à supprimer.")
        return

    buttons = [KeyboardButton(text=file) for file in files]
    keyboard_layout = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    keyboard_layout.append([KeyboardButton(text="⬅️ Retour au menu principal")])

    keyboard = ReplyKeyboardMarkup(keyboard=keyboard_layout, resize_keyboard=True)
    await message.reply("🔍 Choisissez un script à arrêter et supprimer:", reply_markup=keyboard)

@dp.message(lambda message: message.text == "📜 Liste des scripts")
async def list_codes(message: types.Message):
    if BOT_MODE == "private" and message.from_user.id not in ADMINS:
        await message.reply("🚫 Le bot est en mode privé, seuls les admins peuvent l'utiliser.")
        return
    
    user_id = message.from_user.id
    user_folder = os.path.join(BASE_DIR, str(user_id))
    os.makedirs(user_folder, exist_ok=True)

    files = os.listdir(user_folder)
    if not files:
        await message.reply("🚫 Aucun script trouvé.")
        return

    status = {file: "🟢 Actif" if (user_id, file) in processes and processes[(user_id, file)].returncode is None else "🔴 Arrêté" for file in files}
    response = "\n".join([f"{file}: {state}" for file, state in status.items()])
    await message.reply(f"📂 Vos scripts:\n{response}")

@dp.message(lambda message: message.text == "▶️ Exécuter un script")
async def list_files_for_running(message: types.Message):
    if BOT_MODE == "private" and message.from_user.id not in ADMINS:
        await message.reply("🚫 Le bot est en mode privé, seuls les admins peuvent l'utiliser.")
        return
    
    user_id = message.from_user.id
    user_states[user_id] = "execution"

    user_folder = os.path.join(BASE_DIR, str(user_id))
    os.makedirs(user_folder, exist_ok=True)

    files = os.listdir(user_folder)
    if not files:
        await message.reply("🚫 Aucun script trouvé.")
        return

    buttons = [KeyboardButton(text=file) for file in files]
    keyboard_layout = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    keyboard_layout.append([KeyboardButton(text="⬅️ Retour au menu principal")])

    keyboard = ReplyKeyboardMarkup(keyboard=keyboard_layout, resize_keyboard=True)
    await message.reply("🔍 Choisissez un script à exécuter:", reply_markup=keyboard)

@dp.message(lambda message: message.text == "⬅️ Retour au menu principal")
async def return_to_main_menu(message: types.Message):
    user_id = message.from_user.id
    user_states.pop(user_id, None)
    keyboard = admin_keyboard if user_id in ADMINS else main_keyboard
    await message.reply("🏠 Retour au menu principal.", reply_markup=keyboard)

@dp.message(lambda message: message.text == "⬅️ Retour au panel admin" and message.from_user.id in ADMINS)
async def return_to_admin_panel(message: types.Message):
    await message.reply("👑 Panel d'administration", reply_markup=admin_panel_keyboard)

# ==================== GESTION DES ACTIONS UTILISATEUR ====================
@dp.message()
async def handle_user_action(message: types.Message):
    if not message.text:  # photo, sticker, document hors ajout... : rien à traiter
        return

    # Vérifier d'abord le mode du bot
    if BOT_MODE == "private" and message.from_user.id not in ADMINS:
        await message.reply("🚫 Le bot est en mode privé, seuls les admins peuvent l'utiliser.")
        return
    
    if message.from_user.id in banned_users:
        await message.reply("🚫 Vous êtes banni de ce bot.")
        return

    # Traiter d'abord les commandes admin
    if message.from_user.id in ADMINS:
        if user_states.get(message.from_user.id) == "ban_user":
            try:
                banned_user_id = int(message.text)
                banned_users.add(banned_user_id)
                await message.reply(f"✅ L'utilisateur {banned_user_id} a été banni avec succès.")
            except ValueError:
                await message.reply("⚠️ Veuillez entrer un ID utilisateur valide (chiffres seulement)")
            user_states.pop(message.from_user.id, None)
            await message.reply("👑 Panel d'administration", reply_markup=admin_panel_keyboard)
            return

        elif user_states.get(message.from_user.id) == "unban_user":
            try:
                unbanned_user_id = int(message.text)
                banned_users.discard(unbanned_user_id)
                await message.reply(f"✅ L'utilisateur {unbanned_user_id} a été débanni avec succès.")
            except ValueError:
                await message.reply("⚠️ Veuillez entrer un ID utilisateur valide (chiffres seulement)")
            user_states.pop(message.from_user.id, None)
            await message.reply("👑 Panel d'administration", reply_markup=admin_panel_keyboard)
            return

        elif user_states.get(message.from_user.id) == "save_user":
            try:
                target_user_id = int(message.text)
                user_folder = os.path.join(BASE_DIR, str(target_user_id))
                
                if not os.path.exists(user_folder):
                    await message.reply("🚫 Aucun script trouvé pour cet utilisateur.")
                    user_states.pop(message.from_user.id, None)
                    return
                
                files = os.listdir(user_folder)
                if not files:
                    await message.reply("🚫 Cet utilisateur n'a aucun script.")
                    user_states.pop(message.from_user.id, None)
                    return
                
                for file in files:
                    try:
                        await message.answer_document(
                            types.FSInputFile(os.path.join(user_folder, file)),
                            caption=f"Script {file} de l'utilisateur {target_user_id}"
                        )
                        await asyncio.sleep(1)
                    except Exception as e:
                        await message.reply(f"⚠️ Erreur lors de l'envoi du fichier {file}: {str(e)}")
                
                await message.reply("✅ Tous les scripts ont été envoyés avec succès.")
            except ValueError:
                await message.reply("⚠️ Veuillez entrer un ID utilisateur valide (chiffres seulement)")
            except Exception as e:
                await message.reply(f"⚠️ Une erreur inattendue est survenue: {str(e)}")
            
            user_states.pop(message.from_user.id, None)
            await message.reply("👑 Panel d'administration", reply_markup=admin_panel_keyboard)
            return

        elif user_states.get(message.from_user.id) == "admin_stop_script_user":
            try:
                target_user_id = int(message.text)
                user_folder = os.path.join(BASE_DIR, str(target_user_id))
                if not os.path.exists(user_folder):
                    await message.reply("🚫 Aucun script trouvé pour cet utilisateur.")
                    user_states.pop(message.from_user.id, None)
                    return
                
                files = os.listdir(user_folder)
                if not files:
                    await message.reply("🚫 Aucun script trouvé pour cet utilisateur.")
                    user_states.pop(message.from_user.id, None)
                    return
                
                user_states[message.from_user.id] = ("admin_stop_script_select", target_user_id)
                
                buttons = [KeyboardButton(text=file) for file in files]
                keyboard_layout = [buttons[i:i+2] for i in range(0, len(files), 2)]
                keyboard_layout.append([KeyboardButton(text="⬅️ Retour au menu principal")])

                keyboard = ReplyKeyboardMarkup(keyboard=keyboard_layout, resize_keyboard=True)
                await message.reply(f"🔍 Choisissez un script de l'utilisateur {target_user_id} à arrêter/supprimer:", reply_markup=keyboard)
                return
                
            except ValueError:
                await message.reply("⚠️ Veuillez entrer un ID utilisateur valide (chiffres seulement)")
                user_states.pop(message.from_user.id, None)
                await message.reply("👑 Panel d'administration", reply_markup=admin_panel_keyboard)
                return

        elif isinstance(user_states.get(message.from_user.id), tuple) and user_states[message.from_user.id][0] == "admin_stop_script_select":
            target_user_id = user_states[message.from_user.id][1]
            filename = message.text
            
            if filename == "⬅️ Retour au menu principal":
                user_states.pop(message.from_user.id, None)
                keyboard = admin_keyboard if message.from_user.id in ADMINS else main_keyboard
                await message.reply("🏠 Retour au menu principal.", reply_markup=keyboard)
                return
            
            file_path = os.path.join(BASE_DIR, str(target_user_id), filename)
            
            key = (target_user_id, filename)
            stopper = "owner" if target_user_id == message.from_user.id else "admin"
            process_stopped = await stop_process(key, stopper)
            if process_stopped and stopper == "admin":
                await message.reply(f"⛔ Le script {filename} de l'utilisateur {target_user_id} a été arrêté (l'utilisateur a été prévenu).")

            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                    await message.reply(f"✅ Le script {filename} de l'utilisateur {target_user_id} a été supprimé avec succès.")
                else:
                    if not process_stopped:
                        await message.reply(f"⚠️ Le fichier {filename} n'existe pas pour l'utilisateur {target_user_id}.")
            except Exception as e:
                await message.reply(f"⚠️ Erreur lors de la suppression du script: {str(e)}")

            user_states.pop(message.from_user.id, None)
            await message.reply("👑 Panel d'administration", reply_markup=admin_panel_keyboard)
            return

    # Traiter les commandes normales
    user_folder = os.path.join(BASE_DIR, str(message.from_user.id))
    os.makedirs(user_folder, exist_ok=True)

    user_files = os.listdir(user_folder)
    filename = message.text
    file_path = os.path.join(user_folder, filename)

    if filename not in user_files:
        return

    if user_states.get(message.from_user.id) == "execution":
        user_states.pop(message.from_user.id, None)
        await launch_script(message, message.from_user.id, filename, file_path)

    elif user_states.get(message.from_user.id) == "suppression":
        user_states.pop(message.from_user.id, None)
        # Arrête le script s'il tourne (le propriétaire reçoit le message d'arrêt), puis le supprime
        await stop_process((message.from_user.id, filename), "owner")

        if os.path.exists(file_path):
            os.remove(file_path)
            await message.reply(f"✅ Le script {filename} a été supprimé avec succès.")
        else:
            await message.reply(f"⚠️ Le fichier {filename} n'existe pas.")

# ==================== FONCTION PRINCIPALE ====================
_background_tasks = []  # garde une référence pour éviter le garbage collection
RUNNING_FLAG = os.path.join(DATA_DIR, "bot-running.flag")


async def notify_admins(text: str):
    for admin_id in list(ADMINS):
        await send_safe(admin_id, text)


async def wait_for_token(token_ready: asyncio.Event) -> str:
    """Attend qu'un token soit disponible (variable d'environnement ou assistant web)."""
    cycles = 0
    while True:
        token_ready.clear()  # avant la vérification : un signal reçu ensuite n'est pas perdu
        token = botconfig.get_active_token()
        if token:
            return token
        if cycles % 10 == 0:
            domain = get_public_domain()
            where = f"https://{domain}/" if domain else "la page \"/\" du site"
            add_log(f"[Setup] Aucun token configuré : ouvrez {where} pour saisir l'ID admin et le token du bot.")
        cycles += 1
        try:
            await asyncio.wait_for(token_ready.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass


async def shutdown_bot(reason: str):
    """Arrête les scripts, prévient les admins et libère les ressources."""
    running = [k for k, p in processes.items() if p.returncode is None]
    text = (f"🔴 <b>Le bot s'arrête</b>\n📌 Raison : {html.escape(reason)}\n"
            f"⏱️ Uptime : {serverinfo.fmt_duration(time.time() - START_TIME)}\n"
            f"⚙️ Scripts en cours arrêtés : {len(running)}")
    add_log(f"[Shutdown] {reason} -- stopping {len(running)} script(s)")
    try:
        await asyncio.wait_for(
            asyncio.gather(
                notify_admins(text),
                *[stop_process(k, "shutdown", grace=2) for k in running],
                return_exceptions=True,
            ),
            timeout=15,
        )
    except asyncio.TimeoutError:
        add_log("[Shutdown] timeout while stopping scripts")
    try:
        os.remove(RUNNING_FLAG)  # arrêt propre : le prochain démarrage ne signalera rien d'anormal
    except OSError:
        pass
    if bot is not None:
        try:
            await bot.session.close()
        except Exception:
            pass


async def main():
    global bot
    loop = asyncio.get_running_loop()
    token_ready = asyncio.Event()
    # Quand l'assistant web enregistre le token, la boucle démarre le bot tout de suite
    botconfig.set_on_saved(lambda: loop.call_soon_threadsafe(token_ready.set))

    # Serveur web (/, /health, assistant de configuration) sur le port PORT
    start_web_server()

    # Keep-alive automatique : ping du domaine public toutes les 30 s
    _background_tasks.append(asyncio.create_task(keep_alive_task()))

    botconfig.announce_setup_state()

    token = await wait_for_token(token_ready)
    ADMINS[:] = botconfig.get_admin_ids()
    # Proxy : premier choix avant de créer le bot, puis rotation toutes les 60 s
    initial_proxy = None
    if proxymanager.ENABLED:
        initial_proxy = await proxymanager.manager.rotate("démarrage")
    bot = Bot(token=token, session=proxymanager.make_session(initial_proxy))
    if proxymanager.ENABLED:
        proxymanager.bind_bot(bot)
        _background_tasks.append(asyncio.create_task(proxymanager.manager.run(skip_first=True)))

    # Telegram refuse getUpdates (polling) tant qu'un webhook est actif -- par
    # exemple celui d'un ancien projet qui utilisait le même token.
    try:
        info = await bot.get_webhook_info()
        if info.url:
            add_log(f"[Webhook] Un webhook est actif ({info.url}) : suppression pour utiliser le polling.")
            await bot.delete_webhook(drop_pending_updates=False)
    except Exception as e:
        add_log(f"[Webhook] Impossible de vérifier/supprimer le webhook : {e}")

    unexpected_stop = os.path.exists(RUNNING_FLAG)
    try:
        with open(RUNNING_FLAG, "w") as f:
            f.write(str(int(time.time())))
    except OSError:
        pass

    add_log(f"[Boot] Python {PYTHON_VERSION} | DATA_DIR={DATA_DIR} | PORT={WEB_PORT}")
    print("🤖 Bot démarré avec succès!", flush=True)
    start_text = f"🟢 <b>Le bot est démarré</b>\n🌐 Domaine : <code>{html.escape(get_public_domain() or 'non détecté')}</code>"
    if unexpected_stop:
        start_text += "\n⚠️ Le précédent arrêt était inattendu (crash, redéploiement forcé ou coupure)."
    await notify_admins(start_text)

    reason = "arrêt demandé par la plateforme (SIGTERM/SIGINT)"
    try:
        # Le polling gère SIGINT/SIGTERM et se termine proprement
        await dp.start_polling(bot, handle_signals=True, close_bot_session=False)
    except asyncio.CancelledError:
        reason = "tâche annulée"
        raise
    except BaseException as e:
        reason = f"erreur inattendue : {type(e).__name__}: {e}"
        add_log(f"[Polling] stopped by {reason}")
        raise
    finally:
        await shutdown_bot(reason)

if __name__ == "__main__":
    asyncio.run(main())
