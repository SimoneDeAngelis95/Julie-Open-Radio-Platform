# Sviluppo, verifiche e aggiornamenti

## Ambiente locale

Python 3.13 e ffmpeg sono necessari per eseguire i test audio fuori da Docker. Creare un ambiente virtuale e installare `dashboard/requirements.txt`.

```sh
python3 scripts/test_installation.py
JULIE_TESTING=1 python3 dashboard/manage.py collectstatic --noinput
JULIE_TESTING=1 python3 dashboard/manage.py test library --noinput
JULIE_TESTING=1 python3 dashboard/manage.py makemigrations --check --dry-run
```

`JULIE_TESTING=1` usa SQLite e hash password veloci: non abilitarlo su un’installazione reale. Due prove richiedono PostgreSQL e vengono saltate nel percorso SQLite. La workflow GitHub esegue la suite con PostgreSQL 17, client PostgreSQL 17 e ffmpeg. Il suo esito deve essere controllato dopo il primo caricamento: non è ancora un’esecuzione effettuata su GitHub.

Per test PostgreSQL, usare un database e cartelle di lavoro separati dalla radio. Non eseguire test su un database di produzione. I test di ripristino verificano il nome del database e usano cartelle temporanee; i comandi del motore sono simulati.

## Stato delle verifiche

Il nucleo della versione comprende 218 test applicativi, già passati su PostgreSQL in un’installazione ARM64 del prototipo. La nuova preparazione dell’installazione ha tre test su generazione dei segreti, conservazione della configurazione esistente e rifiuto di input non validi. Nella cartella di distribuzione sono stati ripetuti 218 test applicativi: 216 superati e due saltati perché richiedono PostgreSQL. Sono passati anche i tre test della preparazione. La composizione dei servizi è stata verificata con Docker Compose sia in modalità locale sia con il modello HTTPS, in una cartella temporanea separata dalla radio. Il conteggio non sostituisce il collaudo del sistema completo.

Prima della prima pubblicazione stabile completare:

- Installazione da clone su Debian pulito, incluse autorizzazioni dei volumi.
- Avvio e riavvio completo di tutti i servizi.
- Ascolto prolungato, musica/podcast/diretta e ritorno alla rotazione.
- Backup completo e ripristino su un’installazione separata.
- Login, caricamenti, Replay Range/HEAD e calendario tramite HTTPS reale.
- Verifica del recupero dopo arresti imprevisti.

La sintassi Liquidsoap e il modello Caddy sono stati verificati nel prototipo; le modifiche del pacchetto e il cambio di UID richiedono un nuovo controllo integrato. La beta non è presentata come rilascio già collaudato su ogni macchina.

## Aggiornare una radio

1. Creare e scaricare un backup completo.
2. Leggere le note della versione di destinazione e programmare un’interruzione se necessaria.
3. Scaricare il codice e selezionare il tag della versione.
4. Ricostruire le immagini, applicare le migrazioni e ricreare i servizi applicativi.

```sh
./julie build dashboard
./julie run --rm --no-deps dashboard python manage.py migrate --noinput
./julie up -d dashboard dashboard-worker dashboard-scheduler dashboard-listening dashboard-backup
```

Questa sequenza aggiorna il pannello; non applica automaticamente modifiche del motore Liquidsoap o di Icecast. Seguire le note del rilascio per quelle modifiche. Un tag precedente potrebbe non essere compatibile con uno schema database più recente: non considerare un semplice checkout un rollback del database.

## Release

Il file VERSION identifica la versione prevista: `0.1.0-beta.1`. Creare il tag e la release GitHub solo dopo aver completato i controlli indicati nelle note di rilascio. Un clone della branch principale può contenere cambiamenti successivi: per un server usare una versione identificata.

Le immagini base Docker sono referenziate per tag; una ricostruzione futura può acquisire aggiornamenti di sistema. Per distribuzioni che richiedono riproducibilità esatta, registrare e verificare anche i digest delle immagini utilizzate.
