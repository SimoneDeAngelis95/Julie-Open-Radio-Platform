<img src="dashboard/static/julie-radio.png" alt="Julie" width="96">

# Julie · Open Radio Platform

Una piattaforma libera per gestire una radio scolastica dal browser: musica, jingle, podcast e dirette, con una regia automatica che continua a funzionare anche quando il pannello non è aperto.

Il progetto nasce presso **I.I.S. Medaglia D’Oro**, oggi **I.I.S. San Benedetto – Sede Medaglia D’Oro**, per costruire uno strumento semplice e riutilizzabile anche in altre scuole.

## Cosa offre

- Libreria audio, playlist e jingle.
- Podcast programmati, ricorrenze e calendario.
- Ingresso per le dirette, con precedenza sulla programmazione.
- API pubbliche per calendario e Replay.
- Ascolti, stato della radio, backup e ripristino.

L’interfaccia è attualmente **in italiano**. Il supporto multilingua è un possibile sviluppo futuro e non è ancora implementato. Il sito pubblico e il player per gli ascoltatori non sono inclusi: possono essere realizzati separatamente usando stream e API.

## Per iniziare

Julie usa Docker Compose e si prepara per un server Debian senza interfaccia grafica. Prima di avviare gli script, seguire [Installazione](docs/installazione.md): sono richiesti Docker Engine, Compose e Python 3.

La documentazione completa è in [docs](docs/README.md).

**Stato:** prima versione beta (`0.1.0-beta.1`). Il nucleo applicativo è stato verificato su ARM64 con PostgreSQL; il pacchetto di installazione va collaudato su un server pulito prima dell’uso continuativo. Le prove prolungate e di recupero dopo un’interruzione restano da completare.

## Licenza

Julie è distribuito con licenza **GNU GPL v3.0**. Il testo completo è nel file [LICENSE](LICENSE). Dipendenze e servizi esterni mantengono le rispettive licenze: [componenti](docs/componenti.md).
