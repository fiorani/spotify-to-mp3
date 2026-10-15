#!/usr/bin/env python3
"""Spotify → MP3: server web locale.

Legge l'elenco dei brani di una playlist Spotify (solo metadati), cerca
ogni brano su YouTube tramite spotdl/yt-dlp e lo salva come MP3 320 kbps
con tag ID3 e copertina. Più schede = più playlist scaricate in parallelo,
ognuna con la propria sessione indipendente. Solo per uso personale.
"""
import copy
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import uuid
import zipfile
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

BASE_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
PORT = int(os.environ.get("PORT", "8765"))
CHUNK_SIZE = 4  # brani scaricati in parallelo per blocco (per scheda)
THREADS = 4

app = Flask(__name__)

# Il client Spotify di spotdl è un singleton di processo: si inizializza una
# volta sola e si condivide fra le schede. Ogni scheda ha invece il proprio
# Downloader (con event loop e impostazioni separate), così i download di
# playlist diverse procedono davvero in parallelo.
_init_lock = threading.Lock()
_spotify_ready = False
_search_lock = threading.Lock()  # serializza le ricerche sul client condiviso


def _blank_state():
    return {
        "phase": "idle",  # idle | loading | ready | downloading | done | error
        "message": "",
        "playlist": None,
        "songs": [],
        "folder": None,
        "total": 0,
        "done": 0,
        "errors": 0,
    }


def _sanitize(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "", name).strip().rstrip(".")
    return name or "playlist"


def _js_runtime_args() -> str:
    """yt-dlp richiede un runtime JavaScript per le challenge di YouTube:
    senza, molti video risultano falsamente «non disponibili»."""
    for runtime in ("deno", "node", "bun"):
        if shutil.which(runtime):
            return f"--js-runtimes {runtime}"
    return ""


def _downloader_settings() -> dict:
    from spotdl.utils.ffmpeg import get_ffmpeg_path

    return {
        "output": str(DOWNLOADS_DIR / "{artists} - {title}.{output-ext}"),
        "format": "mp3",
        "bitrate": "320k",
        "threads": THREADS,
        "overwrite": "skip",
        "simple_tui": True,
        "print_errors": True,
        "lyrics_providers": [],
        "audio_providers": ["youtube-music", "youtube"],
        "yt_dlp_args": _js_runtime_args(),
        "ffmpeg": str(get_ffmpeg_path() or "ffmpeg"),
    }


def _ensure_spotify(status_cb):
    """FFmpeg e client Spotify: inizializzati una sola volta per processo."""
    global _spotify_ready
    with _init_lock:
        if _spotify_ready:
            return
        from spotdl.utils.config import DEFAULT_CONFIG
        from spotdl.utils.ffmpeg import download_ffmpeg, is_ffmpeg_installed
        from spotdl.utils.spotify import SpotifyClient

        if not is_ffmpeg_installed():
            status_cb("Scarico FFmpeg (serve solo la prima volta)…")
            download_ffmpeg()

        SpotifyClient.init(
            client_id=DEFAULT_CONFIG["client_id"],
            client_secret=DEFAULT_CONFIG["client_secret"],
            user_auth=False,
            cache_path=None,
            no_cache=False,
            headless=True,
            use_official_api=False,
        )
        _spotify_ready = True


class Session:
    """Una scheda del sito: playlist, stato e download indipendenti."""

    def __init__(self, sid: str):
        self.id = sid
        self.lock = threading.Lock()
        self.state = _blank_state()
        self.closed = False  # scheda chiusa: abbandona il lavoro in coda
        self.stop = threading.Event()
        self.jobs: queue.Queue = queue.Queue()
        self.song_objs: list = []  # oggetti Song, indice allineato a state["songs"]
        self.downloader = None  # creato dal thread engine alla prima richiesta
        threading.Thread(target=self._engine, daemon=True).start()

    def _set(self, **kwargs):
        with self.lock:
            self.state.update(kwargs)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.state)

    def close(self):
        self.closed = True
        self.stop.set()
        self.jobs.put(("quit", None))

    # -- tutto ciò che segue gira solo nel thread engine della scheda --

    def _engine(self):
        while True:
            kind, arg = self.jobs.get()
            if kind == "quit" or self.closed:
                break
            try:
                if kind == "load":
                    self._do_load(arg)
                elif kind == "download":
                    self._do_download()
                elif kind == "choose":
                    self._do_choose(arg)
            except Exception as exc:  # errore imprevisto: mostralo nella UI
                self._set(phase="error", message=f"{type(exc).__name__}: {exc}")

    def _get_downloader(self):
        if self.downloader is None:
            _ensure_spotify(lambda msg: self._set(message=msg))
            from spotdl.download.downloader import Downloader

            self.downloader = Downloader(settings=_downloader_settings())
        return self.downloader

    def _search(self, url: str):
        """Come Spotdl.search, ma sul client Spotify condiviso."""
        from spotdl.utils.search import parse_query

        s = self.downloader.settings
        self._set(message="In coda: aspetto la ricerca di un'altra scheda…")
        with _search_lock:
            if self.closed:
                return []
            self._set(message="Recupero l'elenco dei brani da Spotify…")
            return parse_query(
                query=[url],
                threads=s["threads"],
                use_ytm_data=s["ytm_data"],
                playlist_numbering=s["playlist_numbering"],
                album_type=s["album_type"],
                playlist_retain_track_cover=s["playlist_retain_track_cover"],
            )

    def _expected_file(self, song):
        """Percorso in cui spotdl salva (o ha già salvato) il brano."""
        try:
            from spotdl.utils.formatter import create_file_name

            template = self.downloader.settings["output"]
            return Path(create_file_name(song, template, "mp3"))
        except Exception:
            return None

    def _do_load(self, url: str):
        with self.lock:
            self.state.update(_blank_state())
            self.state["phase"] = "loading"
            self.state["message"] = "Recupero l'elenco dei brani da Spotify…"

        dl = self._get_downloader()
        songs = self._search(url)
        if self.closed:
            return
        if not songs:
            raise RuntimeError("Nessun brano trovato a questo link.")

        playlist_name = _sanitize(songs[0].list_name or "Playlist")
        folder = DOWNLOADS_DIR / playlist_name
        folder.mkdir(parents=True, exist_ok=True)
        dl.settings["output"] = str(folder / "{artists} - {title}.{output-ext}")

        entries = []
        for i, song in enumerate(songs):
            expected = self._expected_file(song)
            exists = expected is not None and expected.exists()
            entries.append(
                {
                    "i": i,
                    "name": song.name,
                    "artists": ", ".join(song.artists),
                    "duration": int(song.duration or 0),
                    "cover": song.cover_url,
                    "status": "done" if exists else "pending",
                    "note": "già presente" if exists else "",
                    "file": str(expected) if exists else None,
                    "candidates": None,
                }
            )

        self.song_objs = songs
        with self.lock:
            self.state["playlist"] = {
                "name": playlist_name,
                "url": url,
                "cover": songs[0].cover_url,
            }
            self.state["songs"] = entries
            self.state["total"] = len(entries)
            self.state["done"] = sum(e["status"] == "done" for e in entries)
            self.state["folder"] = str(folder)
            self.state["phase"] = "ready"
            self.state["message"] = ""

    def _download_indices(self, indices, success_note=""):
        """Scarica i brani indicati a blocchi paralleli, aggiornando lo stato."""
        dl = self.downloader
        for start in range(0, len(indices), CHUNK_SIZE):
            if self.stop.is_set():
                break
            chunk = indices[start : start + CHUNK_SIZE]
            with self.lock:
                for i in chunk:
                    self.state["songs"][i]["status"] = "downloading"
            try:
                results = dl.download_multiple_songs(
                    [self.song_objs[i] for i in chunk]
                )
            except Exception as exc:
                results = [(self.song_objs[i], None) for i in chunk]
                self._set(message=f"Errore durante il download: {exc}")
            with self.lock:
                for i, (song, path) in zip(chunk, results):
                    if path is None:
                        expected = self._expected_file(song)
                        if expected is not None and expected.exists():
                            path = expected
                    entry = self.state["songs"][i]
                    if path is not None:
                        entry.update(
                            status="done", file=str(path), note=success_note
                        )
                    else:
                        entry.update(
                            status="error",
                            file=None,
                            note="non trovato o download fallito",
                        )
                self.state["done"] = sum(
                    e["status"] == "done" for e in self.state["songs"]
                )
                self.state["errors"] = sum(
                    e["status"] == "error" for e in self.state["songs"]
                )

    def _do_download(self):
        dl = self._get_downloader()
        self.stop.clear()
        with self.lock:
            todo = [
                e["i"] for e in self.state["songs"] if e["status"] != "done"
            ]
            self.state["phase"] = "downloading"
            self.state["message"] = ""
            for i in todo:
                self.state["songs"][i].update(
                    status="queued", note="", file=None, candidates=None
                )

        self._download_indices(todo)

        # Per i brani ancora in errore: propone i risultati della ricerca
        # su YouTube e lascia scegliere all'utente la versione giusta.
        with self.lock:
            leftovers = [
                e["i"] for e in self.state["songs"] if e["status"] == "error"
            ]
        if leftovers and not self.stop.is_set():
            self._set(message="Cerco alternative per i brani non trovati…")
            for i in leftovers:
                if self.stop.is_set():
                    break
                cands = self._get_candidates(self.song_objs[i])
                with self.lock:
                    e = self.state["songs"][i]
                    if cands:
                        e.update(
                            status="choice",
                            note="scegli tu la versione giusta",
                            candidates=cands,
                        )
                    else:
                        e["note"] = "nessun risultato su YouTube"
                    self.state["errors"] = sum(
                        x["status"] == "error" for x in self.state["songs"]
                    )

        with self.lock:
            stopped = self.stop.is_set()
            for e in self.state["songs"]:
                if e["status"] in ("queued", "downloading"):
                    e["status"] = "pending"
            self.state["phase"] = "ready" if stopped else "done"
            choices = sum(e["status"] == "choice" for e in self.state["songs"])
            if stopped:
                self.state["message"] = (
                    "Download interrotto: puoi riprenderlo quando vuoi."
                )
            else:
                parts = []
                if choices:
                    parts.append(
                        f"{choices} brani non trovati automaticamente: "
                        "scegli tu la versione giusta dall'elenco qui sotto."
                    )
                if self.state["errors"]:
                    parts.append(
                        f"{self.state['errors']} brani non scaricati. "
                        "Premi «Riprova gli errori» per un nuovo tentativo."
                    )
                self.state["message"] = " ".join(parts)

    def _get_candidates(self, song, limit=8):
        """Risultati della ricerca su YouTube fra cui far scegliere l'utente."""
        from spotdl.utils.formatter import create_song_title

        term = create_song_title(song.name, song.artists)
        seen, cands = set(), []
        for provider in self.downloader.audio_providers:
            try:
                results = provider.get_results(term)
            except Exception:
                continue
            for r in results:
                if not r.url or r.url in seen:
                    continue
                seen.add(r.url)
                cands.append(
                    {
                        "url": r.url,
                        "name": r.name,
                        "author": r.author,
                        "duration": int(r.duration or 0),
                        "source": r.source,
                    }
                )
        # prima i risultati con durata più vicina all'originale
        cands.sort(key=lambda c: abs(c["duration"] - int(song.duration or 0)))
        return cands[:limit]

    def _do_choose(self, arg):
        """Scarica il video scelto dall'utente per un brano non trovato."""
        i, url = arg
        self._get_downloader()
        self.stop.clear()
        song = self.song_objs[i]
        song.download_url = url
        self._download_indices([i], success_note="scelta manuale")
        with self.lock:
            ok = self.state["songs"][i]["status"] == "done"
        if not ok:
            song.download_url = None  # i prossimi tentativi ricercano da capo


_sessions: dict = {}  # sid -> Session, in ordine di creazione
_sessions_lock = threading.Lock()


def _new_session() -> Session:
    sid = uuid.uuid4().hex[:8]
    s = Session(sid)
    with _sessions_lock:
        _sessions[sid] = s
    return s


def _get_session(sid):
    with _sessions_lock:
        return _sessions.get(sid)


@app.route("/")
def index():
    return render_template("index.html")


@app.get("/api/tabs")
def api_tabs():
    with _sessions_lock:
        ids = list(_sessions)
    return jsonify(tabs=ids)


@app.post("/api/tabs")
def api_tabs_new():
    return jsonify(id=_new_session().id)


@app.delete("/api/tabs/<sid>")
def api_tabs_close(sid):
    with _sessions_lock:
        s = _sessions.pop(sid, None)
    if s is not None:
        s.close()
    return jsonify(ok=True)


@app.post("/api/<sid>/load")
def api_load(sid):
    s = _get_session(sid)
    if s is None:
        return jsonify(error="Scheda inesistente."), 404
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if "spotify.com" not in url and not url.startswith("spotify:"):
        return jsonify(error="Inserisci un link Spotify valido."), 400
    with s.lock:
        if s.state["phase"] in ("loading", "downloading"):
            return (
                jsonify(error="C'è già un'operazione in corso in questa scheda."),
                409,
            )
    s.jobs.put(("load", url))
    return jsonify(ok=True)


@app.post("/api/<sid>/download")
def api_download(sid):
    s = _get_session(sid)
    if s is None:
        return jsonify(error="Scheda inesistente."), 404
    with s.lock:
        if s.state["phase"] not in ("ready", "done"):
            return jsonify(error="Carica prima una playlist."), 409
    s.jobs.put(("download", None))
    return jsonify(ok=True)


@app.post("/api/<sid>/choose")
def api_choose(sid):
    s = _get_session(sid)
    if s is None:
        return jsonify(error="Scheda inesistente."), 404
    data = request.get_json(force=True, silent=True) or {}
    i, url = data.get("i"), data.get("url")
    with s.lock:
        if not isinstance(i, int) or i < 0 or i >= len(s.state["songs"]):
            return jsonify(error="Brano inesistente."), 404
        entry = s.state["songs"][i]
        urls = [c["url"] for c in entry.get("candidates") or []]
        if entry["status"] != "choice" or url not in urls:
            return jsonify(error="Scelta non valida."), 400
        entry.update(status="queued", note="")
    s.jobs.put(("choose", (i, url)))
    return jsonify(ok=True)


@app.post("/api/<sid>/stop")
def api_stop(sid):
    s = _get_session(sid)
    if s is None:
        return jsonify(error="Scheda inesistente."), 404
    s.stop.set()
    return jsonify(ok=True)


@app.get("/api/<sid>/status")
def api_status(sid):
    s = _get_session(sid)
    if s is None:
        return jsonify(error="Scheda inesistente."), 404
    return jsonify(s.snapshot())


@app.get("/api/<sid>/file/<int:i>")
def api_file(sid, i: int):
    s = _get_session(sid)
    if s is None:
        return jsonify(error="Scheda inesistente."), 404
    with s.lock:
        if i < 0 or i >= len(s.state["songs"]):
            return jsonify(error="Brano inesistente."), 404
        file = s.state["songs"][i].get("file")
    if not file or not Path(file).exists():
        return jsonify(error="File non ancora scaricato."), 404
    return send_file(file, as_attachment=True, download_name=Path(file).name)


@app.get("/api/<sid>/zip")
def api_zip(sid):
    s = _get_session(sid)
    if s is None:
        return jsonify(error="Scheda inesistente."), 404
    with s.lock:
        files = [
            e["file"]
            for e in s.state["songs"]
            if e["status"] == "done" and e["file"]
        ]
        name = s.state["playlist"]["name"] if s.state["playlist"] else "playlist"
    if not files:
        return jsonify(error="Nessun brano scaricato."), 404
    zip_path = DOWNLOADS_DIR / f"{name}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
        for file in dict.fromkeys(files):  # senza duplicati
            p = Path(file)
            if p.exists():
                zf.write(p, p.name)
    return send_file(zip_path, as_attachment=True, download_name=zip_path.name)


@app.post("/api/<sid>/open-folder")
def api_open_folder(sid):
    s = _get_session(sid)
    if s is None:
        return jsonify(error="Scheda inesistente."), 404
    with s.lock:
        folder = s.state["folder"] or str(DOWNLOADS_DIR)
    Path(folder).mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        subprocess.Popen(["open", folder])
    elif sys.platform.startswith("win"):
        subprocess.Popen(["explorer", folder])
    else:
        subprocess.Popen(["xdg-open", folder])
    return jsonify(ok=True)


if __name__ == "__main__":
    DOWNLOADS_DIR.mkdir(exist_ok=True)
    print(f"\n  ♫  Spotify → MP3 attivo su http://127.0.0.1:{PORT}\n")
    app.run(host="127.0.0.1", port=PORT, threaded=True)
