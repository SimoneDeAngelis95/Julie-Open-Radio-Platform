# API pubbliche

Julie fornisce le API e lo stream; il sito degli ascoltatori e l’interfaccia Replay si realizzano separatamente. Non usare le API interne della Libreria per un sito pubblico.

## Calendario

`GET /api/v1/schedule/` espone soltanto gli appuntamenti resi pubblici. Parametri:

| Parametro | Significato |
| --- | --- |
| `from`, `to` | Date ISO o istanti con fuso, intervallo massimo 366 giorni |
| `page` | Pagina, da 1 |
| `page_size` | Risultati per pagina, da 1 a 200 |

La risposta contiene `timezone`, `from`, `to`, `count`, `page`, `pages` e `results`. Il fuso indicato è Europe/Rome. I dettagli di un appuntamento non pubblicano automaticamente l’audio del podcast.

`GET /calendar.ics` restituisce un calendario iCalendar. Un’applicazione può sottoscrivere l’URL per aggiornarsi periodicamente; scaricare e importare un file una sola volta non crea un abbonamento. La frequenza di aggiornamento dipende dall’applicazione calendario. Gli eventi mantengono UID stabili anche dopo una modifica o un annullamento. L’intervallo predefinito include 30 giorni passati e 336 futuri.

## Replay

- `GET /api/v1/podcasts/`: catalogo pubblico, `page` da 1 e `page_size` da 1 a 100.
- `GET /api/v1/podcasts/UUID/`: scheda del podcast.
- `GET /api/v1/podcasts/UUID/audio.mp3`: copia MP3 pubblica, con supporto Range.

Sono supportati HEAD e OPTIONS. Le risposte pubbliche usano `Cache-Control: no-store`; non conservarle in cache sul sito se deve essere possibile revocare l’accesso. Rendere privato o cestinare un podcast revoca le nuove richieste anche a un link MP3 conosciuto. Non può ritirare una copia già scaricata o i byte già ricevuti dal player.

Gli URL assoluti della scheda e dell’audio usano l’origine API configurata nell’installazione. Controllare CORS in Manutenzione → Configurazione se il sito è ospitato su un’altra origine. Il modello proxy pubblico espone solo API e calendario, senza accesso amministrativo.

## Diretta

Il mount predefinito è `/stream.mp3`, MP3 128 kbit/s. Il player del sito deve usare l’URL HTTPS pubblico quando la pagina è HTTPS. Non usare le credenziali Icecast o della diretta nel codice del sito.
