# Configuration

Julie can use any domain or run entirely on a local network. Domain registration and DNS hosting are independent of the project.

## Dashboard settings

Under **Manutenzione → Configurazione** (Maintenance → Configuration), an administrator can set:

- The station name, while retaining the Julie brand.
- The complete stream URL, such as `https://stream.example.org/stream.mp3`.
- The public API origin, such as `https://api.example.org`, without `/api/v1/`.
- The station website.
- Website origins allowed to read the public APIs and Replay from a browser, one per line.

For browser origins, `*` allows any website; an empty field disables cross-origin reads. This is a browser CORS policy, not authentication: public content remains accessible through its URL. These settings do not grant access to the administration dashboard.

Blank stream and API fields use the server defaults or the request address. The website field stores an address; it does not create a website. Scheduling currently uses **Europe/Rome**, including daylight saving changes. Selecting other time zones is not implemented.

## Server settings

`.env` contains ports, bind address, UID/GID, the Compose project name and streaming credentials. `.env.dashboard` contains the Django secret, database credentials and web settings.

| Variable | Purpose |
| --- | --- |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated allowed hostnames, without scheme or port |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Comma-separated trusted HTTPS origins |
| `JULIE_REGIA_URL` | Public dashboard address shown in the application |
| `JULIE_PUBLIC_API_URL` | Public API base address |
| `JULIE_STREAM_URL` | Stream URL used when the dashboard field is empty |
| `JULIE_TRUST_PROXY=1` | Trust the HTTPS indication supplied by the reverse proxy |
| `JULIE_HTTPS_ONLY=1` | Enable secure cookies and redirect HTTP to HTTPS |

Recreate affected services after changing their environment. Public addresses saved in the dashboard override the corresponding server defaults, so check both when moving to a new domain. Do not broaden allowed hosts or proxy trust merely to dismiss an error without investigating its cause.

## Domains and HTTPS

The Caddy template uses three **distinct** hostnames chosen by the installation: dashboard, API and stream. They may belong to different domains. It does not publish a public website.

1. Point DNS A records to the public IP. Add AAAA records only if IPv6 is correctly routed.
2. With the network administrator, forward TCP 80 and 443 to the server. Keep the database and internal interfaces private. Docker port publishing requires care when configuring firewalls; see [Docker’s firewall guidance](https://docs.docker.com/engine/install/debian/#firewall-limitations).
3. Copy `dashboard/deployment/env.proxy.example` to `.env.proxy` and replace the three hostnames.
4. Merge `dashboard/deployment/env.dashboard.https.example` into `.env.dashboard`, preserving existing credentials. Enable proxy trust only when the backend is isolated and the proxy overwrites `X-Forwarded-Proto`.
5. Set `JULIE_PROXY_ENABLED=1` in `.env`. The `./julie` command will then include `dashboard/deployment/compose.proxy.yaml`.
6. Run `./julie config --quiet`, then `./julie up -d`. The overlay replaces direct Django and Icecast ports with localhost bindings; Caddy handles public access and certificates.
7. Update public addresses in the dashboard and test login, uploads, streaming, Replay and the calendar from outside the network.

These changes may briefly interrupt the station. Schedule a maintenance window.

The proxy connects to Icecast and Django over HTTP on the private Docker network. The API hostname exposes only public endpoints; the stream hostname exposes only `/stream.mp3`. Icecast administration is not forwarded. Live input on port 8001 is separate: do not expose it unintentionally, particularly if `.env` uses `JULIE_BIND_ADDRESS=0.0.0.0`.

The proxy template requires Compose >=2.24.4 for `!override`. Retain Caddy’s volumes to preserve certificates. Do not enable HTTPS-only before the proxy is ready. The internal `/health/` check remains available over HTTP; HSTS is not enabled automatically.

Include `.env.proxy` and Caddy volumes in server backups: application configuration backups do not automatically include them. The proxy Compose template itself is included in the saved source files.

References: [Django HTTPS proxy settings](https://docs.djangoproject.com/en/5.2/ref/settings/#secure-proxy-ssl-header), [Caddy reverse proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy), [Compose merge rules](https://docs.docker.com/reference/compose-file/merge/).
