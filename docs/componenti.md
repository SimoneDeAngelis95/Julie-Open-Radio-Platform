# Componenti e crediti

Julie nasce come progetto di radio scolastica presso **I.I.S. Medaglia D’Oro**, oggi **I.I.S. San Benedetto – Sede Medaglia D’Oro**. La piattaforma è pensata per essere adattata e riutilizzata da altre scuole e comunità.

Il codice del progetto è distribuito con **GNU GPL v3.0**, identificatore SPDX `GPL-3.0-only`; vedere [LICENSE](../LICENSE). La documentazione e l’icona della radio incluse nella repository sono distribuite con la stessa licenza del progetto. Non sono inclusi audio dimostrativi o repertori musicali: chi utilizza la piattaforma gestisce i diritti sui contenuti trasmessi.

I componenti esterni non vengono ridenominati o relicenziati da Julie. I relativi avvisi e termini si trovano nei progetti originali e nelle immagini utilizzate:

| Componente | Ruolo | Riferimento |
| --- | --- | --- |
| Django | Applicazione web | https://www.djangoproject.com/ |
| PostgreSQL e Psycopg | Database e driver | https://www.postgresql.org/ · https://www.psycopg.org/ |
| Liquidsoap | Motore radio | https://www.liquidsoap.info/ |
| Icecast | Distribuzione audio | https://icecast.org/ |
| ffmpeg | Analisi e preparazione audio | https://ffmpeg.org/ |
| Gunicorn e WhiteNoise | Servizio web e file statici | https://gunicorn.org/ · https://whitenoise.readthedocs.io/ |
| Docker e Compose | Esecuzione dei servizi | https://docs.docker.com/ |
| Caddy | HTTPS facoltativo | https://caddyserver.com/ |

L’interfaccia è italiana; una futura traduzione richiederà la preparazione delle stringhe e dei cataloghi. Non è sufficiente cambiare una variabile di lingua per ottenere un’interfaccia già tradotta.
