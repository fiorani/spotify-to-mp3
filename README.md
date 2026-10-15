# Spotify → MP3 (sito locale)

Sito web che gira solo sul tuo computer: incolli il link di una playlist
Spotify e scarica tutti i brani in MP3 320 kbps, con titolo, artista e
copertina nei tag. Da Spotify vengono letti solo i metadati; l'audio viene
cercato e scaricato da YouTube tramite [spotdl](https://github.com/spotDL/spotify-downloader)
e yt-dlp. **Solo per uso personale.**

## Avvio

```bash
./run.sh
```

Il browser si apre da solo su <http://127.0.0.1:8765>. Per fermare il
server: `Ctrl+C` nel terminale.

## Come si usa

1. Incolla il link della playlist e premi **Carica playlist**.
2. Premi **Scarica tutto in MP3** e aspetta (4 brani alla volta).
3. I file finiscono in `downloads/<nome playlist>/`. Puoi usare
   **Apri cartella**, **Scarica ZIP** o il link *salva* sul singolo brano.
4. Col pulsante **＋ scheda** apri altre schede: ognuna scarica una playlist
   diversa in parallelo, indipendente dalle altre. Usa playlist diverse per
   scheda (la stessa playlist in due schede scrive sugli stessi file).

## Note

- Al primo avvio scarica FFmpeg automaticamente se non è installato.
- I brani già scaricati vengono saltati: puoi interrompere e riprendere,
  o riavviare il server, senza perdere nulla.
- Se qualche brano fallisce (raro), il pulsante **Riprova gli errori**
  ritenta solo quelli.
