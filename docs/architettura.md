# Architettura

Julie separa l’elaborazione audio dal percorso di trasmissione.

| Servizio | Funzione |
| --- | --- |
| `icecast` | Distribuisce lo stream agli ascoltatori |
| `liquidsoap` | Gestisce musica, jingle, podcast e ingresso diretta |
| `dashboard-db` | PostgreSQL, archivio operativo dei dati |
| `dashboard-init` | Migrazioni e preparazione dell’accesso iniziale |
| `dashboard` | Pannello Django/Gunicorn e API |
| `dashboard-worker` | Elaborazione audio e operazioni sui file |
| `dashboard-scheduler` | Invio dei podcast programmati e ricorrenze |
| `dashboard-listening` | Raccolta dello stato e degli ascolti |
| `dashboard-backup` | Backup, reset e ripristino |
| `proxy` | Ingresso HTTPS facoltativo con Caddy |

Il motore legge una fotografia della rotazione su file, pubblicata atomicamente. Il brano in onda non richiede una richiesta web o un accesso al database per proseguire. Un’indisponibilità del pannello non equivale però alla disponibilità di tutte le funzioni: nuove elaborazioni, programmazione e aggiornamenti dello stato dipendono dai rispettivi servizi.

Gli audio originali sono conservati in `audio/library/originals`; le copie per la trasmissione e Replay in `ready`. L’elaborazione usa ffmpeg e FLAC per il playout, con normalizzazione predefinita a -20 LUFS e limite -1,5 dBTP. Lo stream finale è MP3 128 kbit/s. Il profilo audio è attualmente parte della configurazione tecnica.

Il protocollo interno di programmazione usa identificatori e riconciliazione degli eventi per evitare reinvii in caso di esito incerto. Le ricorrenze mantengono l’orario civile italiano; i casi di cambio dell’ora sono gestiti esplicitamente. Non è una garanzia di messa in onda in ogni condizione: gli esiti si controllano dal calendario.

Il database usa un volume Docker dedicato. Audio e backup sono cartelle persistenti del server. I blocchi di manutenzione coordinano scritture e operazioni distruttive; non eliminarli manualmente. Il ripristino verifica l’archivio, prepara una copia di sicurezza e importa solo i dati applicativi previsti: non esegue SQL arbitrario dell’archivio sul database corrente.

Il controllo Liquidsoap non è pubblicato sulle interfacce host. L’ingresso diretta è un servizio distinto dalle API e dalla regia. Il modello HTTPS limita le porte dirette di Django e Icecast a localhost.
