"""
Informations serveur : pays (avec drapeau), mémoire et espace disque.

Le pays est détecté à partir de l'IP de sortie du serveur avec plusieurs
fournisseurs gratuits essayés dans l'ordre (comme dans le projet précédent :
ip-api.com, ipinfo.io, ipapi.co), puis mis en cache 6 h.

La mémoire est lue depuis les cgroups quand le bot tourne dans un conteneur
(limite réelle du conteneur), sinon depuis /proc/meminfo.
"""

import os
import shutil
import time

import requests

from countries_ar import COUNTRIES_AR

_LOCATION_CACHE_S = 6 * 60 * 60
_location = None
_location_at = 0.0


# ==================== FORMATAGE ====================
def fmt_bytes(n) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}j {h}h {m}min"
    if h:
        return f"{h}h {m}min"
    if m:
        return f"{m}min {s}s"
    return f"{s}s"


def bar(pct: float, width: int = 10) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = round(width * pct / 100)
    return "█" * filled + "░" * (width - filled)


def flag_emoji(country_code: str) -> str:
    if not country_code or len(country_code) != 2:
        return "🌍"
    return "".join(chr(127397 + ord(c)) for c in country_code.upper())


# ==================== PAYS ====================
def country_name_ar(code: str) -> str:
    """Nom du pays en arabe (données ICU, comme le projet précédent) ou chaîne vide."""
    return COUNTRIES_AR.get((code or "").upper(), "")


def _from_ip_api():
    r = requests.get(
        "http://ip-api.com/json/",
        params={"fields": "status,country,countryCode,city,isp,query", "lang": "fr"},
        timeout=5,
    )
    j = r.json()
    if j.get("status") == "success" and j.get("countryCode"):
        return {"country": j.get("country") or j["countryCode"], "code": j["countryCode"],
                "city": j.get("city") or "", "isp": j.get("isp") or "", "ip": j.get("query") or ""}


def _from_ipinfo():
    j = requests.get("https://ipinfo.io/json", timeout=5).json()
    if j.get("country"):
        return {"country": j["country"], "code": j["country"], "city": j.get("city") or "",
                "isp": j.get("org") or "", "ip": j.get("ip") or ""}


def _from_ipapi_co():
    j = requests.get("https://ipapi.co/json/", timeout=5).json()
    if j.get("country_code") and j.get("country_name"):
        return {"country": j["country_name"], "code": j["country_code"], "city": j.get("city") or "",
                "isp": j.get("org") or "", "ip": j.get("ip") or ""}


def get_location():
    """Pays/IP/ville/opérateur du serveur, ou None si tous les fournisseurs échouent."""
    global _location, _location_at
    now = time.time()
    if _location and now - _location_at < _LOCATION_CACHE_S:
        return _location
    for provider in (_from_ip_api, _from_ipinfo, _from_ipapi_co):
        try:
            result = provider()
        except (requests.RequestException, ValueError):
            result = None
        if result:
            result["flag"] = flag_emoji(result["code"])
            _location, _location_at = result, now
            return result
    return _location  # dernière valeur connue (peut être None)


# ==================== MÉMOIRE ====================
def _read(path):
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except OSError:
        return None


def _meminfo():
    out = {}
    raw = _read("/proc/meminfo")
    if raw:
        for line in raw.splitlines():
            k, _, v = line.partition(":")
            parts = v.split()
            if parts:
                out[k] = int(parts[0]) * 1024
    return out


def _stat_value(path, key):
    raw = _read(path)
    if raw:
        for line in raw.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == key:
                return int(parts[1])
    return 0


def _process_rss():
    raw = _read("/proc/self/status")
    if raw:
        for line in raw.splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    except Exception:
        return None


def get_memory() -> dict:
    """{'process': RSS du bot, 'total': .., 'used': .., 'free': ..} en octets."""
    mi = _meminfo()
    host_total = mi.get("MemTotal")
    host_avail = mi.get("MemAvailable")
    total = host_total
    used = (host_total - host_avail) if host_total and host_avail is not None else None

    # cgroup v2
    limit_raw = _read("/sys/fs/cgroup/memory.max")
    current_raw = _read("/sys/fs/cgroup/memory.current")
    if current_raw and current_raw.isdigit():
        current = int(current_raw) - _stat_value("/sys/fs/cgroup/memory.stat", "inactive_file")
        used = max(current, 0)
        if limit_raw and limit_raw.isdigit() and (not host_total or int(limit_raw) < host_total):
            total = int(limit_raw)
    else:
        # cgroup v1
        limit_raw = _read("/sys/fs/cgroup/memory/memory.limit_in_bytes")
        usage_raw = _read("/sys/fs/cgroup/memory/memory.usage_in_bytes")
        if usage_raw and usage_raw.isdigit():
            used = max(int(usage_raw) - _stat_value("/sys/fs/cgroup/memory/memory.stat",
                                                    "total_inactive_file"), 0)
            if limit_raw and limit_raw.isdigit() and (not host_total or int(limit_raw) < host_total):
                total = int(limit_raw)

    free = (total - used) if total is not None and used is not None else None
    return {"process": _process_rss(), "total": total, "used": used, "free": free}


# ==================== DISQUE ====================
def get_disk(path: str) -> dict:
    try:
        du = shutil.disk_usage(path)
        return {"total": du.total, "used": du.used, "free": du.free}
    except OSError:
        return {"total": None, "used": None, "free": None}


def dir_size(path: str) -> tuple:
    """(taille totale en octets, nombre de fichiers) d'un dossier."""
    total, count = 0, 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
                count += 1
            except OSError:
                pass
    return total, count
