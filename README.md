# istidafa4-by-moon

Bot Telegram (aiogram) pour héberger/exécuter des scripts Python.

## Fichiers
| Fichier | Rôle |
|---|---|
| `istidafa4_by_moon.py` | Le bot Telegram |
| `botconfig.py` | Page "/" : saisie de l'ID admin + token (write-once) |
| `keepalive.py` | Serveur web (`/health`) + détection du domaine + keep-alive automatique |
| `serverinfo.py` | Pays, mémoire, espace disque |
| `Dockerfile`, `docker-entrypoint.sh`, `docker-compose.yml` | Déploiement Docker |

## Première configuration (ID admin + token)
1. Déployez sans définir `TELEGRAM_BOT_TOKEN`.
2. Ouvrez l'URL du site : une petite page demande le **mot de passe**
   (`MURAD_SETUP_PASSWORD`), puis l'**ID Telegram de l'admin** et le **token**.
3. Le token est vérifié auprès de Telegram, enregistré une seule fois dans
   `DATA_DIR/murad-bot.json`, et le bot démarre immédiatement.
Si `TELEGRAM_BOT_TOKEN` est défini en variable d'environnement, la page est
désactivée. Pour changer de token : supprimez `murad-bot.json` du volume.

## Fonctions du bot
- **📊 État du serveur** (admin, ou `/status`) : pays + drapeau, IP/ville/opérateur,
  mémoire du bot et du serveur (limite du conteneur), espace disque, taille des
  scripts, scripts en cours, uptime, domaine.
- **Installation des bibliothèques en direct** : les imports du script sont
  analysés, puis la sortie de `pip` s'affiche en temps réel dans un message
  Telegram mis à jour.
- **Notifications d'arrêt** : le propriétaire est prévenu quand son script se
  termine, plante (code de sortie), ou est arrêté (par lui ou un admin). Les
  admins sont prévenus au démarrage et à l'arrêt du bot ; au redémarrage, un
  arrêt inattendu (crash/coupure) est signalé.
- **Keep-alive automatique** : ping de `https://<domaine>/health` toutes les 30 s.

## Variables d'environnement
`TELEGRAM_BOT_TOKEN`, `ADMIN_IDS`, `MURAD_SETUP_PASSWORD`, `APP_URL`, `PORT`
(auto), `KEEP_ALIVE_INTERVAL`, `DATA_DIR` (défaut `/app/data`, à monter en
volume persistant).

## Déploiement
```bash
docker compose up -d --build
```

## Déploiement sur Choreo (Python 3.11)
Le projet est compatible avec Python 3.9+ ; il a été vérifié pour la syntaxe
Python 3.11 (version de Choreo, 3.11.x) et testé en 3.12. Aucune configuration de
version n'est nécessaire : le code s'adapte à celle de la plateforme
(`Dockerfile` : `ARG PYTHON_VERSION=3.11`, `.python-version` : 3.11).

**Méthode A – Docker (recommandée)**
1. Console Choreo → *Create* → *Service* → dépôt GitHub (le `Dockerfile` est à la
   racine).
2. Build preset **Docker**, Dockerfile `/Dockerfile`, contexte `/`.
   Le `Dockerfile` utilise déjà l'utilisateur numérique `10014` (obligatoire) et
   `.choreo/component.yaml` déclare l'endpoint sur le port **3000**.
3. *Build* puis *Deploy*.

**Méthode B – Buildpack Python**
Preset **Python**, *Language Version* **3.11.x**. Le `Procfile` lance le bot et
`requirements.txt` installe les dépendances. `.choreo/component.yaml` est lu aussi.

**Configuration (Deploy → Configs & Secrets)** :
- `TELEGRAM_BOT_TOKEN` et `ADMIN_IDS` : à définir ici. Le disque de Choreo est
  éphémère, donc la saisie via la page "/" ne survivrait pas à un redémarrage.
- `MURAD_SETUP_PASSWORD` si vous utilisez malgré tout la page de configuration.

**Endormissement (scale-to-zero)** : Choreo endort les services HTTP sans trafic
au bout d'environ 5 minutes, mais seulement sur certains ports (5000, 6000, 7000,
8000, 9000, 7070-7079, 8080-8089, 9090-9099, 8290). Le port 3000 n'en fait pas
partie : le bot reste actif. Le keep-alive reste utile ailleurs ; sur Choreo, si le
log affiche `HTTP 401/404`, définissez `APP_URL` avec l'URL publique complète
(chemin inclus) ou `KEEP_ALIVE_INTERVAL=0`.

Le stockage étant éphémère, les scripts uploadés et les bibliothèques installées
disparaissent à chaque redémarrage/redéploiement (voir *Configure Storage* dans
Choreo pour un volume persistant, monté sur `DATA_DIR`).

## Proxys à rotation automatique
La connexion du bot à l'API Telegram passe par un proxy gratuit validé, changé toutes
les 60 s (et immédiatement en cas de panne). Voir `proxymanager.py` et les variables
`PROXY_*` dans `.env.example`. Ce n'est pas une protection contre le piratage : un proxy
gratuit est instable et non fiable (TLS reste vérifié, donc le token n'est pas lisible).
