# Backup e ripristino

## Creare una copia

Manutenzione → Backup è riservata agli amministratori. Sono disponibili database, audio, configurazione e **backup completo**. Gli archivi sono preparati in background e scaricabili dal browser.

Il backup completo coordina database e audio nello stesso salvataggio. Tre archivi separati, creati in momenti differenti, non sono necessariamente coerenti tra loro. Per un trasferimento o un ripristino usare il backup completo.

I file includono dati riservati. Non sono cifrati: conservarli in un luogo protetto, anche fuori dal server. Il download normalmente scade dopo 24 ore; le copie necessarie a un reset o ripristino in corso vengono conservate più a lungo. Lo spazio di un backup temporaneo sullo stesso server non sostituisce una copia esterna.

Sono disponibili anche:

```sh
./backup-database.sh
./backup-audio.sh
./backup-config.sh
./backup-full.sh
```

Questi comandi usano gli stessi servizi del pannello. La creazione di un backup non riavvia la radio; le modifiche ai contenuti possono restare brevemente in pausa durante la fotografia dei dati. Una copia audio e un backup completo richiedono spazio libero sufficiente sul filesystem di destinazione.

## Ripristinare

Manutenzione → Ripristina backup accetta un **backup completo ZIP di Julie**:

1. Caricare l’archivio e attendere la verifica.
2. Controllare il riepilogo dei contenuti.
3. Preparare e scaricare il backup di sicurezza della radio attuale.
4. Confermare l’operazione con la frase richiesta.
5. Attendere il completamento, verificare la Libreria e riattivare la radio dal pannello.

Il ripristino sostituisce i contenuti della radio e interrompe la trasmissione. Conserva invece account, password, impostazioni del server e configurazione degli indirizzi dell’installazione di destinazione. Il codice applicativo non viene sostituito con quello contenuto nel backup. Per questo il ripristino dal pannello non è un aggiornamento o un ripristino dell’intero sistema operativo.

I podcast in avvio o in onda nel salvataggio non vengono reinviati automaticamente. Sono mantenuti playlist, programmazione, metadati editoriali, disponibilità Replay e storico ascolti.

Il caricamento ha un limite predefinito di 20 GiB, modificabile dal server con `JULIE_MAX_RESTORE_BYTES`. Esistono anche limiti di sicurezza sui componenti interni: database e SQL 512 MiB, configurazione decompressa 128 MiB, 100.000 file audio e due milioni di record. Una libreria oltre questi limiti richiede una procedura amministrativa dedicata, non tentativi ripetuti dal browser.

In caso di errore dopo la conferma, conservare i file e il backup di sicurezza e usare il recupero previsto dalla pagina. Non sbloccare manualmente i marker di manutenzione e non cancellare cartelle per forzare la ripartenza.

## Riparti da zero

Manutenzione → Riparti da zero prepara prima una copia completa di sicurezza. Il reset richiede conferma esplicita e cancella contenuti, playlist, programmazione e storico ascolti, mantenendo account e configurazione dell’installazione. Al termine la radio resta ferma fino alla riattivazione manuale.

## Recupero del server

Per reinstallare su un altro server: predisporre una nuova installazione, creare l’amministratore e importare il backup completo. Conservare separatamente credenziali e configurazione della rete, eventuale `.env.proxy`, certificati e configurazioni esterne al progetto. Il volume PostgreSQL è l’archivio operativo; non eliminarlo pensando che sia una cache.
