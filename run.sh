#!/bin/bash
# Avvia il sito Spotify → MP3 e apre il browser.
cd "$(dirname "$0")" || exit 1

if [ ! -x .venv/bin/python ]; then
  echo "Creo l'ambiente Python (solo la prima volta)…"
  python3 -m venv .venv || exit 1
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt || exit 1
fi

( sleep 2; open "http://127.0.0.1:8765" 2>/dev/null ) &
exec .venv/bin/python app.py
