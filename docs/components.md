# Components and credits

Julie began as a school radio project at **I.I.S. Medaglia D’Oro**, now **I.I.S. San Benedetto – Sede Medaglia D’Oro**. It is designed to be adapted and reused by other schools and communities.

Project code is released under **GNU GPL v3.0**, SPDX identifier `GPL-3.0-only`; see [LICENSE](../LICENSE). Documentation and the radio icon included in the repository use the same project license. No demonstration recordings or music catalogues are included. Station operators are responsible for the rights to the content they broadcast.

Third-party components are not renamed or relicensed by Julie. Their notices and terms are provided by the original projects and container images:

| Component | Role | Reference |
| --- | --- | --- |
| Django | Web application | https://www.djangoproject.com/ |
| PostgreSQL and Psycopg | Database and driver | https://www.postgresql.org/ · https://www.psycopg.org/ |
| Liquidsoap | Radio engine | https://www.liquidsoap.info/ |
| Icecast | Audio distribution | https://icecast.org/ |
| ffmpeg | Audio analysis and preparation | https://ffmpeg.org/ |
| Gunicorn and WhiteNoise | Web serving and static assets | https://gunicorn.org/ · https://whitenoise.readthedocs.io/ |
| Docker and Compose | Service runtime | https://docs.docker.com/ |
| Caddy | Optional HTTPS proxy | https://caddyserver.com/ |

The interface is currently Italian. Future multilingual support will require preparing translatable strings and language catalogues. Changing a language setting alone does not provide a translated interface.
