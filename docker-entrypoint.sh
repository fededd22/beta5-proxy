#!/bin/sh
set -e

# --- Détection automatique du port d'écoute -------------------------------
# Ordre de priorité :
#   1. Variable d'environnement PORT fournie par la plateforme d'hébergement
#      (Cloud Run, Render, Railway, Fly.io, bata.bliz/blitz.cloud, etc.)
#   2. Valeur par défaut : 3000 (= port déclaré dans .choreo/component.yaml)
# ---------------------------------------------------------------------------
if [ -z "$PORT" ]; then
  export PORT="3000"
  echo "[entrypoint] Aucune variable PORT définie -> port par défaut : $PORT"
else
  echo "[entrypoint] Variable d'environnement PORT détectée : $PORT"
fi
echo "[entrypoint] L'application va écouter sur le port $PORT"

# --- Dossier de données inscriptible --------------------------------------
# Certaines plateformes lancent le conteneur avec un utilisateur non-root
# arbitraire. Si DATA_DIR n'est pas inscriptible, on bascule sur /tmp/data
# plutôt que de planter (les données ne seront alors pas persistantes).
DATA_DIR="${DATA_DIR:-/app/data}"
mkdir -p "$DATA_DIR" 2>/dev/null || true
if [ ! -w "$DATA_DIR" ]; then
  echo "[entrypoint] ATTENTION : $DATA_DIR n'est pas inscriptible -> repli sur /tmp/data (non persistant)"
  DATA_DIR="/tmp/data"
  mkdir -p "$DATA_DIR"
  export HOME="$DATA_DIR/home"
fi
export DATA_DIR
mkdir -p "$DATA_DIR/scripts" "$DATA_DIR/libs" "${HOME:-$DATA_DIR/home}" 2>/dev/null || true
echo "[entrypoint] Données stockées dans : $DATA_DIR"

# --- Augmentation de la limite de descripteurs de fichiers ----------------
ulimit -n 65536 2>/dev/null || echo "[entrypoint] Impossible d'augmenter ulimit -n (droits insuffisants), valeur actuelle : $(ulimit -n)"

exec "$@"
