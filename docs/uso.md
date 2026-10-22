# Uso della radio

## Libreria e musica

Caricare uno o più file dalla Libreria, selezionando il tipo corretto. Per la musica sono disponibili titolo e artista; per i podcast titolo e descrizione. I campi editoriali inseriti manualmente vengono conservati durante la preparazione audio. I formati riconosciuti dipendono dai decoder ffmpeg disponibili; usare file audio validi e verificarne la preparazione.

Julie conserva l’originale e prepara una copia audio per la trasmissione. Solo i brani pronti, attivi e fuori dal cestino possono entrare nella rotazione. I file con errori restano visibili per il controllo e un eventuale nuovo tentativo.

Le playlist organizzano i brani senza duplicarli. Si possono assegnare durante il caricamento o sulla riga del brano. Nella pagina Playlist si sceglie quale raccolta alimenta la rotazione casuale; in alternativa viene usata tutta la musica attiva. La programmazione delle playlist per fasce orarie non è implementata.

## Jingle

La pagina Jingle consente di scegliere un jingle e l’intervallo in numero di brani. L’inserimento è riferito alla rotazione musicale, non a un orario esatto.

## Podcast e calendario

Il podcast viene caricato una sola volta e può essere programmato più volte. Nel Palinsesto, selezionare una data per creare un appuntamento oppure aprire un evento per modificarlo o annullarlo. Sono disponibili ricorrenze settimanali e modifiche delle singole occorrenze.

Il controllo degli orari impedisce sovrapposizioni tra i podcast, considerando durata e margine di transizione. Un appuntamento saltato non viene trasmesso automaticamente in ritardo. Lo storico delle trasmissioni si consulta dal calendario.

La visibilità nel calendario pubblico e la disponibilità nel Replay sono scelte distinte. Un episodio può essere reso ascoltabile senza essere mai andato in onda. L’opzione di pubblicazione dopo la prima trasmissione aspetta invece il completamento effettivo.

## Dirette

La precedenza è **diretta → podcast → musica**. L’ingresso usa Liquidsoap sulla porta interna 8001, mount `live`, con password `JULIE_LIVE_PASSWORD` dell’installazione. In BUTT usare il tipo Icecast e i dati della rete autorizzata; il collegamento interno non usa automaticamente il certificato del sito pubblico.

La diretta interrompe il podcast e lo scarta: non riparte dal punto interrotto. Al termine si torna alla musica. Le registrazioni automatiche delle dirette non sono implementate.

## In onda, Ascolti e Stato della radio

In onda mostra l’ultima osservazione disponibile e offre un player. Gli ascoltatori indicati sono connessioni allo stream, non persone uniche. Anche il player del pannello viene contato se avviato.

Ascolti conserva lo storico con filtro per anno e paginazione a venti giorni. Gli intervalli privi di informazioni rimangono sconosciuti: non vengono trasformati in zero ascoltatori.

Stato della radio, in Manutenzione, riunisce trasmissione, spazio, backup e problemi da controllare. Le informazioni vecchie sono indicate come non aggiornate; una rilevazione mancante non dimostra che l’audio sia fermo.

## Cestino

Il cestino permette di escludere e recuperare i file. La cancellazione definitiva richiede un’azione esplicita e può comprendere anche l’originale. Controllare le conferme prima di liberare lo spazio. Per azzerare la radio consultare [Backup e ripristino](backup.md).
