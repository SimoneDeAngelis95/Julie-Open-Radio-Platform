# Configurazione

Julie può usare qualsiasi dominio oppure funzionare solo nella rete locale. L’acquisto e la gestione del dominio sono indipendenti dal progetto.

## Dal pannello

In **Manutenzione → Configurazione** l’amministratore imposta:

- Nome della radio, mantenendo il marchio Julie.
- URL completo dello stream, ad esempio `https://stream.example.org/stream.mp3`.
- Indirizzo base delle API, ad esempio `https://api.example.org`, senza `/api/v1/`.
- Sito pubblico della radio.
- Siti autorizzati a leggere API e Replay dal browser, uno per riga.

Per le origini browser, `*` ammette qualunque sito; il campo vuoto disabilita le letture tra siti diversi. È una regola CORS del browser, non un sistema di autenticazione: un contenuto pubblico resta raggiungibile tramite link. Il pannello di amministrazione non concede accesso tramite queste regole.

Stream e API vuoti usano le impostazioni del server o l’indirizzo della richiesta. Il sito è un’informazione della radio e non crea una pagina web. Il fuso del palinsesto è attualmente **Europe/Rome**, con cambio automatico dell’ora; la selezione di altri fusi non è implementata.

## Sul server

`.env` contiene porte, IP di ascolto, UID/GID, nome del progetto Compose e credenziali della trasmissione. `.env.dashboard` contiene segreto Django, database e impostazioni web.

| Variabile | Uso |
| --- | --- |
| `DJANGO_ALLOWED_HOSTS` | Host consentiti, separati da virgole, senza schema o porta |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Origini HTTPS attendibili, separate da virgole |
| `JULIE_REGIA_URL` | Indirizzo pubblico della regia mostrato nel pannello |
| `JULIE_PUBLIC_API_URL` | Indirizzo base pubblico delle API |
| `JULIE_STREAM_URL` | URL dello stream usato in assenza di un valore nel pannello |
| `JULIE_TRUST_PROXY=1` | Accetta l’indicazione HTTPS dal proxy attendibile |
| `JULIE_HTTPS_ONLY=1` | Cookie sicuri e reindirizzamento a HTTPS |

Dopo modifiche agli ambienti ricreare i servizi interessati. Le impostazioni pubbliche salvate dal pannello prevalgono sui relativi valori di default del server: controllarle dopo un cambio di dominio. Non modificare domini consentiti o fiducia nel proxy soltanto per eliminare un errore senza verificarne la causa.

## Dominio e HTTPS

Il modello Caddy usa tre hostname **distinti**, scelti dall’installazione: regia, API e stream. Possono appartenere a domini diversi. L’esempio non pubblica un sito pubblico.

1. Configurare DNS A verso l’IP pubblico; aggiungere AAAA solo se IPv6 è correttamente raggiungibile.
2. Con il responsabile della rete, indirizzare TCP 80 e 443 al server. Database e interfacce interne non devono essere pubblici. Le porte Docker richiedono attenzione alle regole di rete: consultare le [indicazioni Docker sul firewall](https://docs.docker.com/engine/install/debian/#firewall-limitations).
3. Copiare `dashboard/deployment/env.proxy.example` in `.env.proxy` e sostituire i tre hostname.
4. Integrare `dashboard/deployment/env.dashboard.https.example` in `.env.dashboard`, senza sovrascrivere le credenziali esistenti. Attivare proxy trust solo quando l’applicazione è isolata e il proxy sovrascrive `X-Forwarded-Proto`.
5. Impostare `JULIE_PROXY_ENABLED=1` in `.env`. Il comando `./julie` include da quel momento il modello `dashboard/deployment/compose.proxy.yaml`.
6. Verificare `./julie config --quiet`, poi avviare `./julie up -d`. La sostituzione delle porte del modello limita Django e Icecast a localhost; Caddy gestisce l’ingresso pubblico e i certificati.
7. Aggiornare gli URL pubblici nel pannello e provare dall’esterno login, caricamento, ascolto, Replay e calendario.

Questi passaggi possono interrompere brevemente la radio durante il cambio di configurazione. Programmare una finestra di manutenzione.

Il proxy inoltra a Icecast e Django in HTTP sulla rete privata Docker. Il dominio API espone soltanto gli endpoint pubblici; quello stream soltanto `/stream.mp3`. L’amministrazione Icecast non viene inoltrata. L’ingresso diretta sulla porta 8001 resta separato: non pubblicarlo accidentalmente, soprattutto se `.env` usa `JULIE_BIND_ADDRESS=0.0.0.0`.

Caddy richiede Compose >=2.24.4 per `!override`. Conservare i volumi del proxy per mantenere i certificati. Non abilitare HTTPS-only prima di avere il proxy pronto; il controllo interno `/health/` rimane HTTP. HSTS non è attivato automaticamente.

Includere anche `.env.proxy` e i volumi Caddy nella strategia di salvataggio del server: il backup applicativo della configurazione non comprende automaticamente questi dati. Il file Compose del proxy è invece già incluso nei sorgenti salvati.

Riferimenti: [Django e proxy HTTPS](https://docs.djangoproject.com/en/5.2/ref/settings/#secure-proxy-ssl-header), [Caddy reverse proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy), [merge Compose](https://docs.docker.com/reference/compose-file/merge/).
