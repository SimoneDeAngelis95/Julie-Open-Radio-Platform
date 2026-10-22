# Installazione

## Requisiti

- Server Debian 13 a 64 bit, senza necessità di interfaccia grafica.
- Utente normale con accesso SSH e possibilità di usare sudo.
- Python 3, Git, Docker Engine e plugin Docker Compose.
- Spazio per originali, copie audio elaborate e backup. Il necessario dipende dalla libreria.

Il motore è stato provato su Raspberry Pi ARM64. La configurazione usa immagini per Linux; altre architetture, inclusa amd64, richiedono il collaudo del pacchetto completo. Non sono definiti requisiti minimi di RAM certificati.

Installare Docker dal [repository ufficiale per Debian](https://docs.docker.com/engine/install/debian/). Lo script Julie non installa Docker e non esegue script di terze parti scaricati al volo. Per HTTPS il modello richiede Compose **2.24.4 o successivo**.

## Scaricare il progetto

Clonare la repository GitHub e aprire la cartella ottenuta. Se si installa una release, selezionare il tag indicato nella pagina della release. L’indirizzo della repository verrà riportato dopo la pubblicazione.

## Primo avvio locale

Per un server raggiungibile nella rete interna, sostituire l’indirizzo di esempio con l’IPv4 assegnato al server:

```sh
./install.sh --host 192.168.10.20 --bind 192.168.10.20
```

`--host` è il nome o indirizzo usato dal browser; `--bind` è l’IPv4 di un’interfaccia del server su cui aprire le porte. Non è l’IP pubblico del router. Senza opzioni viene usato localhost: l’accesso è possibile dal server o tramite un tunnel SSH. Non eseguire l’intero script con sudo: viene richiesto solo per i comandi Docker quando necessario.

Lo script crea i segreti, le cartelle e la configurazione Icecast, costruisce le immagini, controlla Liquidsoap e avvia tutti i servizi. Non crea un account predefinito. Dopo l’avvio:

1. Aprire `http://INDIRIZZO-SERVER:8010`.
2. Leggere `audio/library/installation-code.txt` sul server e inserire il codice nella pagina iniziale.
3. Creare l’amministratore, scegliere il nome della radio e partire da zero o importare un backup completo.

Per leggere il codice via SSH:

```sh
cat audio/library/installation-code.txt
```

Il codice non va condiviso: permette la creazione del primo amministratore. Dopo la configurazione viene invalidato. Gli indirizzi locali sono riepilogati in `ACCESSO-LOCALE.txt`, escluso da Git.

Con libreria vuota la radio trasmette silenzio. Caricare alcuni brani e attendere che risultino pronti: entreranno nella rotazione.

## Verificare e gestire i servizi

```sh
./julie ps
./julie logs --tail=100 dashboard liquidsoap
./julie stop
./julie up -d
```

I servizi permanenti hanno riavvio automatico. Verificare che il servizio Docker sia abilitato all’avvio del sistema. `dashboard-init` terminato con codice zero è normale: applica le migrazioni e prepara l’accesso iniziale.

Usare sempre `./julie`: seleziona gli stessi file Compose e include il proxy quando abilitato. Non usare `down -v`: elimina il volume PostgreSQL. Non usare `--remove-orphans` durante una migrazione da installazioni precedenti.

## File e permessi

Il proprietario dell’installazione deve poter leggere e scrivere la cartella. I processi applicativi usano UID e GID registrati in `.env` durante la preparazione. Non spostare i dati tra utenti o filesystem senza verificare permessi e proprietà. I file `.env`, `.env.dashboard` e `config/icecast.xml` contengono password e non devono essere pubblicati.

Rieseguire la preparazione con una configurazione completa conserva i segreti esistenti; non modifica host e porte già salvati. Se una prima preparazione è stata interrotta e mancano file, lo script si ferma: completare o recuperare la configurazione prima di continuare. Non eliminare file di una radio già utilizzata per forzare una nuova installazione.

Per l’accesso da Internet seguire [Configurazione](configurazione.md), prima di pubblicare le porte.
