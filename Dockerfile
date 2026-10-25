# syntax=docker/dockerfile:1

# =============================================================================
# Image d'exécution du bot Telegram (Python / aiogram + Flask + keep-alive)
# -----------------------------------------------------------------------------
# Compatible Choreo (WSO2) : utilisateur non-root avec UID numérique entre
# 10000 et 20000 (obligatoire), port fixe déclaré dans .choreo/component.yaml.
# Compatible aussi avec les autres plateformes (Cloud Run, Render, blitz.cloud...)
# qui injectent PORT et/ou lancent le conteneur avec un utilisateur arbitraire.
#
# Version de Python : 3.11 par défaut (celle de Choreo, 3.11.x). Le code
# s'adapte à toute version >= 3.9 ; pour en changer :
#   docker build --build-arg PYTHON_VERSION=3.12 .
# =============================================================================
ARG PYTHON_VERSION=3.11
FROM python:${PYTHON_VERSION}-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# curl + ca-certificates : health-check et requêtes HTTPS du keep-alive.
# `upgrade` réduit les vulnérabilités signalées par le scan Trivy de Choreo.
RUN apt-get update \
    && apt-get -y upgrade \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Dépendances Python du bot
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Code de l'application
COPY istidafa4_by_moon.py keepalive.py botconfig.py serverinfo.py countries_ar.py ./
COPY .env.example ./.env.example

# Script d'entrée : détecte automatiquement le port d'écoute
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# --- Utilisateur non-root (exigé par Choreo) -------------------------------
# UID numérique dans la plage 10000-20000 (un nom d'utilisateur seul est refusé).
RUN useradd --uid 10014 --user-group --no-create-home \
        --home-dir /app/data --shell /usr/sbin/nologin choreo

# --- Dossiers d'écriture -----------------------------------------------------
# /app/data : detected-host.json, murad-bot.json, scripts des utilisateurs et
# bibliothèques installées à la demande (libs/, via `pip --target`).
# chmod a+rwX : reste inscriptible même si la plateforme impose un autre UID.
ENV DATA_DIR=/app/data \
    HOME=/app/data/home
RUN mkdir -p /app/data/scripts /app/data/home /app/data/libs \
    && chown -R 10014:10014 /app \
    && chmod -R a+rwX /app
ENV APP_URL=https://06a55000-4d9c-4ccc-8f51-2726275c9fe7.e1-eu-west-cdp.choreoapps.dev
USER 10014

# Port par défaut : 3000 (variable PORT si la plateforme en injecte une).
# Il doit correspondre au port de .choreo/component.yaml.
EXPOSE 3000 8080

# Vérifie que le serveur web répond sur le port réellement utilisé
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD sh -c 'curl -fsS "http://127.0.0.1:${PORT:-3000}/health" || exit 1'

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "istidafa4_by_moon.py"]
