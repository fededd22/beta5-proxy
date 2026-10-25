"""
Rotation automatique de proxys pour la connexion du bot à l'API Telegram.

Fonctionnement :
  - une liste de proxys gratuits est téléchargée depuis des sources publiques ;
  - chaque proxy est testé (connexion réelle vers api.telegram.org) avant usage ;
  - toutes les PROXY_ROTATE_INTERVAL secondes (défaut : 60) un nouveau proxy
    validé est choisi ; si le proxy courant tombe en panne, on change tout de suite ;
  - si aucun proxy ne fonctionne : connexion directe (PROXY_FALLBACK_DIRECT=1,
    défaut) ou arrêt des requêtes (PROXY_FALLBACK_DIRECT=0, mode strict).

Variables d'environnement :
  PROXY_ENABLED=1            activer (défaut : 1). 0 = connexion directe.
  PROXY_ROTATE_INTERVAL=60   secondes entre deux changements de proxy.
  PROXY_FALLBACK_DIRECT=1    1 = connexion directe si aucun proxy ne marche.
  PROXY_LIST=                proxys fixes, séparés par des virgules
                             (ex: socks5://1.2.3.4:1080,http://5.6.7.8:8080).
                             Si défini, remplace les sources publiques.
  PROXY_SOURCES=             URLs de listes (une proxy ip:port par ligne),
                             séparées par des virgules. Remplace les sources par défaut.
  PROXY_TEST_TIMEOUT=6       délai (s) du test d'un proxy.

SÉCURITÉ : un proxy gratuit voit votre IP source et la destination (api.telegram.org).
Le trafic reste chiffré en TLS de bout en bout (la vérification des certificats
n'est JAMAIS désactivée), donc le token n'est pas lisible par le proxy, mais un proxy
gratuit est lent, instable et non fiable. Ce n'est PAS une protection contre le piratage.
"""

import asyncio
import os
import random
import re
import time
from typing import List, Optional

import aiohttp

from keepalive import add_log

TELEGRAM_HOST_URL = "https://api.telegram.org/"

DEFAULT_SOURCES = [
    # (url, schéma appliqué aux lignes "ip:port")
    ("https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt", "http"),
    ("https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt", "http"),
]

_PROXY_RE = re.compile(r"^(?:(socks5|socks4|http|https)://)?(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})$")


def _env_bool(name: str, default: bool) -> bool:
    val = (os.environ.get(name) or "").strip().lower()
    if not val:
        return default
    return val not in ("0", "false", "no", "off")


def _env_int(name: str, default: int) -> int:
    try:
        return int((os.environ.get(name) or "").strip())
    except ValueError:
        return default


ENABLED = _env_bool("PROXY_ENABLED", True)
ROTATE_INTERVAL_S = max(5, _env_int("PROXY_ROTATE_INTERVAL", 60))
FALLBACK_DIRECT = _env_bool("PROXY_FALLBACK_DIRECT", True)
TEST_TIMEOUT_S = max(2, _env_int("PROXY_TEST_TIMEOUT", 6))
MAX_CANDIDATES_TESTED = 60      # nombre max de proxys testés par recherche
TEST_CONCURRENCY = 20
POOL_REFRESH_S = 15 * 60        # re-télécharger les listes toutes les 15 min
BAD_TTL_S = 30 * 60             # un proxy défaillant est ignoré pendant 30 min


def parse_proxy(line: str, default_scheme: str = "http") -> Optional[str]:
    """Normalise 'ip:port' ou 'scheme://ip:port' ; retourne None si invalide."""
    m = _PROXY_RE.match((line or "").strip())
    if not m:
        return None
    scheme, ip, port = m.group(1) or default_scheme, m.group(2), int(m.group(3))
    if not (1 <= port <= 65535) or any(int(p) > 255 for p in ip.split(".")):
        return None
    return f"{scheme}://{ip}:{port}"


class ProxyManager:
    def __init__(self) -> None:
        self.current: Optional[str] = None      # None = connexion directe
        self.rotated_at = 0.0
        self.rotations = 0
        self._pool: List[str] = []
        self._pool_at = 0.0
        self._bad = {}                          # proxy -> instant de blocage
        self._lock = asyncio.Lock()
        self._listeners = []                    # callbacks appelés à chaque changement

    # ---------- sources ----------
    def _fixed_list(self) -> List[str]:
        out = []
        for item in (os.environ.get("PROXY_LIST") or "").split(","):
            p = parse_proxy(item)
            if p:
                out.append(p)
        return out

    def _sources(self):
        custom = [u.strip() for u in (os.environ.get("PROXY_SOURCES") or "").split(",") if u.strip()]
        if custom:
            return [(u, "http") for u in custom]
        return DEFAULT_SOURCES

    async def _download_pool(self) -> List[str]:
        fixed = self._fixed_list()
        if fixed:
            return fixed
        found: List[str] = []
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            for url, scheme in self._sources():
                try:
                    async with s.get(url) as r:
                        if r.status != 200:
                            continue
                        text = await r.text()
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    continue
                for line in text.splitlines():
                    p = parse_proxy(line, scheme)
                    if p:
                        found.append(p)
        random.shuffle(found)
        add_log(f"[Proxy] {len(found)} proxys candidats téléchargés.")
        return list(dict.fromkeys(found))

    # ---------- test ----------
    async def _works(self, proxy: str) -> bool:
        """Vrai si le proxy atteint api.telegram.org avec certificat TLS valide."""
        try:
            timeout = aiohttp.ClientTimeout(total=TEST_TIMEOUT_S)
            if proxy.startswith("socks"):
                from aiohttp_socks import ProxyConnector
                connector = ProxyConnector.from_url(proxy)
                async with aiohttp.ClientSession(connector=connector, timeout=timeout) as s:
                    async with s.get(TELEGRAM_HOST_URL) as r:
                        return r.status < 500
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(TELEGRAM_HOST_URL, proxy=proxy) as r:
                    return r.status < 500
        except Exception:
            return False

    async def _find_working(self) -> Optional[str]:
        now = time.time()
        self._bad = {p: t for p, t in self._bad.items() if now - t < BAD_TTL_S}
        if not self._pool or now - self._pool_at > POOL_REFRESH_S:
            self._pool = await self._download_pool()
            self._pool_at = now
        candidates = [p for p in self._pool if p not in self._bad and p != self.current]
        random.shuffle(candidates)
        candidates = candidates[:MAX_CANDIDATES_TESTED]
        if not candidates:
            return None

        sem = asyncio.Semaphore(TEST_CONCURRENCY)
        winner: "asyncio.Future[str]" = asyncio.get_running_loop().create_future()

        async def check(p: str) -> None:
            async with sem:
                if winner.done():
                    return
                if await self._works(p):
                    if not winner.done():
                        winner.set_result(p)
                else:
                    self._bad[p] = time.time()

        tasks = [asyncio.create_task(check(p)) for p in candidates]
        try:
            await asyncio.wait_for(asyncio.shield(winner), timeout=45)
        except asyncio.TimeoutError:
            pass
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return winner.result() if winner.done() and not winner.cancelled() else None

    # ---------- API publique ----------
    def on_change(self, callback) -> None:
        self._listeners.append(callback)

    async def rotate(self, reason: str = "planifiée") -> Optional[str]:
        """Choisit un nouveau proxy validé. Retourne le proxy (None = direct)."""
        async with self._lock:
            new = await self._find_working()
            if new is None and not FALLBACK_DIRECT:
                new = self.current   # mode strict : on garde l'ancien, jamais de direct
            self.current = new
            self.rotated_at = time.time()
            self.rotations += 1
            add_log(f"[Proxy] Rotation ({reason}) -> {self.describe()}")
            for cb in list(self._listeners):
                try:
                    await cb(new)
                except Exception as e:  # noqa: BLE001
                    add_log(f"[Proxy] Erreur listener : {e}")
            return new

    async def report_failure(self) -> None:
        """Appelé quand une requête échoue : bloque le proxy courant et en prend un autre."""
        if self.current:
            self._bad[self.current] = time.time()
        await self.rotate("panne du proxy")

    def describe(self) -> str:
        if not self.current:
            return "connexion directe"
        # masque l'IP : n'afficher que le schéma et les derniers chiffres
        scheme, rest = self.current.split("://", 1)
        host = rest.rsplit(":", 1)[0].split(".")
        return f"{scheme}://*.*.*.{host[-1]}"

    async def run(self, skip_first: bool = False) -> None:
        """Boucle : change de proxy toutes les ROTATE_INTERVAL_S secondes."""
        if not ENABLED:
            add_log("[Proxy] Désactivé (PROXY_ENABLED=0) : connexion directe.")
            return
        add_log(f"[Proxy] Rotation automatique toutes les {ROTATE_INTERVAL_S}s "
                f"(repli direct : {'oui' if FALLBACK_DIRECT else 'non'}).")
        if skip_first:
            await asyncio.sleep(ROTATE_INTERVAL_S)
        while True:
            try:
                await self.rotate()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                add_log(f"[Proxy] Erreur de rotation : {e}")
            await asyncio.sleep(ROTATE_INTERVAL_S)


manager = ProxyManager()


# ==================== INTÉGRATION AIOGRAM ====================
def make_session(proxy: Optional[str]):
    """Crée une session aiogram qui passe par `proxy` (None = direct) et signale les pannes."""
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.exceptions import TelegramNetworkError

    class RotatingSession(AiohttpSession):
        _failure_pending = False

        async def make_request(self, bot, method, *args, **kwargs):
            try:
                return await super().make_request(bot, method, *args, **kwargs)
            except TelegramNetworkError:
                # panne réseau : on change de proxy sans attendre la minute suivante
                if proxy and proxy == manager.current and not RotatingSession._failure_pending:
                    RotatingSession._failure_pending = True

                    async def _rotate():
                        try:
                            await manager.report_failure()
                        finally:
                            RotatingSession._failure_pending = False

                    asyncio.create_task(_rotate())
                raise

    return RotatingSession(proxy=proxy) if proxy else RotatingSession()


async def _close_later(session, delay: float = 75.0) -> None:
    """Ferme l'ancienne session après la fin des requêtes de polling en cours."""
    await asyncio.sleep(delay)
    try:
        await session.close()
    except Exception:  # noqa: BLE001
        pass


def bind_bot(bot) -> None:
    """À chaque changement de proxy, remplace bot.session par une session sur le nouveau proxy."""
    async def _swap(new_proxy):
        old = bot.session
        bot.session = make_session(new_proxy)
        asyncio.create_task(_close_later(old))

    manager.on_change(_swap)
