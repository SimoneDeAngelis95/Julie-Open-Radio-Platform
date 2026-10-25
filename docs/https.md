# Publish Julie over HTTPS

This guide starts with a working Linux installation using the installer's default localhost bindings. Docker Compose 2.24.4 or later is required. Replace the example domain and server address with your own. Julie is not tied to a particular domain or DNS provider.

## First access through SSH

A headless server's `localhost` is not the same as your computer's. To open the setup page without publishing port 8010, run this on your computer:

```sh
ssh -N -L 127.0.0.1:8010:127.0.0.1:8010 user@SERVER_IP
```

Leave that terminal open and visit `http://localhost:8010/setup/` on your computer. Enter the installation code printed by the installer and create the administrator. The connection travels through encrypted SSH; the application and its data remain on the server. Closing the tunnel does not stop Julie. If local port 8010 is already occupied, use `127.0.0.1:18010:127.0.0.1:8010` and browse to port 18010 instead.

## DNS and public ports

At the provider hosting the domain's active DNS zone, create these A records pointing to the server's public IPv4 address:

| Name | Purpose |
| --- | --- |
| `regia.example.org` | Julie dashboard |
| `api.example.org` | Public calendar and Replay APIs |
| `stream.example.org` | Live audio |

The supplied proxy requires three distinct hostnames. A records map names to IPv4 addresses; a CNAME is an alias to another hostname. Add AAAA records only if the server is also reachable over IPv6. Edit existing conflicting records rather than creating additional destinations. Preserve unrelated mail and DNS records.

A separate public player website can use `example.org` and `www.example.org`; the Julie proxy template does not serve that website.

TCP ports 80 and 443 must reach the server from the Internet. Where NAT is used, configure forwarding with the network administrator. Do not expose PostgreSQL, port 8010, Icecast administration or the Liquidsoap control interface. The live input port is separate. Docker publishing and host firewall rules must be considered together.

Check resolution and existing listeners on the server:

```sh
getent ahostsv4 regia.example.org
getent ahostsv4 api.example.org
getent ahostsv4 stream.example.org
sudo ss -ltnp '( sport = :80 or sport = :443 )'
```

These checks do not prove external reachability; certificate validation provides a further check.

## Configure the included proxy

Run the following from the Julie installation directory. Keep the SSH tunnel open until public access is verified.

Create `.env.proxy` from `dashboard/deployment/env.proxy.example`, setting:

```dotenv
JULIE_REGIA_HOST=regia.example.org
JULIE_API_HOST=api.example.org
JULIE_STREAM_HOST=stream.example.org
```

Merge these entries into `.env.dashboard`. Replace existing entries with the same names; preserve database passwords, the Django secret and all unrelated settings. Do not replace the entire file with the example.

```dotenv
DJANGO_ALLOWED_HOSTS=regia.example.org,api.example.org,localhost,127.0.0.1
DJANGO_CSRF_TRUSTED_ORIGINS=https://regia.example.org
JULIE_REGIA_URL=https://regia.example.org
JULIE_PUBLIC_API_URL=https://api.example.org
JULIE_STREAM_URL=https://stream.example.org/stream.mp3
JULIE_TRUST_PROXY=1
JULIE_HTTPS_ONLY=0
```

Set `JULIE_PROXY_ENABLED=1` in `.env`, preserving its other settings. Keep environment files private. Proxy trust is appropriate here because the supplied overlay isolates the backend and Caddy overwrites the HTTPS forwarding header.

Validate and start the dashboard and proxy:

```sh
./julie config --quiet && ./julie up -d --no-deps dashboard proxy
./julie logs --tail 60 proxy
```

The overlay keeps direct dashboard and Icecast web access bound to localhost. The existing database and application dependencies must already be running. The dashboard is briefly unavailable while recreated; these commands do not restart Liquidsoap.

## Certificates: Caddy replaces a separate Certbot setup

Caddy is both the reverse proxy and the certificate manager. It requests certificates from an ACME certificate authority, normally Let's Encrypt, proves control of each hostname and serves HTTPS. A separate Certbot installation or renewal cron job is not needed.

The public connection is encrypted between the browser and Caddy. Caddy forwards requests over the private Docker network to Django or Icecast. The API hostname exposes only public GET, HEAD and OPTIONS endpoints; the stream hostname exposes only `/stream.mp3`, not Icecast administration.

On first startup, messages about creating an ACME account are expected. Look for `certificate obtained successfully` for each hostname. If issuance fails, inspect DNS, IPv6 records if present, external port reachability and the proxy logs. Do not repeatedly delete Caddy storage and request new certificates.

## Verify, then enforce HTTPS

From your computer, open `https://regia.example.org` and verify login. Open `https://api.example.org/api/v1/schedule/`; a JSON response with an empty schedule is valid. Test `https://stream.example.org/stream.mp3` while the station is transmitting. Verify browser uploads, Replay and calendar access as content becomes available.

After HTTPS works, change `JULIE_HTTPS_ONLY=1` in `.env.dashboard`. Recreate application services to load the new environment:

```sh
./julie up -d --no-deps dashboard dashboard-worker dashboard-scheduler dashboard-listening dashboard-backup
```

This enables secure cookies and the application's HTTP-to-HTTPS redirect. Caddy already redirects public HTTP traffic. The internal health endpoint remains available, and HSTS is not enabled automatically. Once enabled, use the public HTTPS address for administration; the plain-HTTP SSH tunnel is no longer the normal login route.

Under **Manutenzione → Configurazione**, check the saved stream URL, public API origin and website. Saved values override environment defaults. Use `https://api.example.org` as the API origin, without `/api/v1/`. For a player at the main domain, allowed browser origins can be:

```text
https://example.org
https://www.example.org
```

This controls browser CORS access to public content; it is not authentication or download protection. It does not configure the stream server's own CORS headers.

## Renewal and backups

Caddy renews certificates automatically while running and able to reach its certificate authority and complete any required validation. Keep the public DNS and required port routing working.

Retain the `julie_proxy_data` and `julie_proxy_config` Compose volumes, normally prefixed by the project's name. They hold Caddy's persistent state, including certificate material in the data volume. Include them and `.env.proxy` in server backups. Julie's application backup does not automatically include these proxy volumes or `.env.proxy`. Treat certificate private keys as secrets. Avoid `docker compose down -v`.

See [configuration](configuration.md) for the complete settings reference and [installation](installation.md) for the first startup.

## Add a separate public player website

The proxy optionally imports `*.caddy` files from `caddy-sites/` in the installation directory. Create that directory before recreating the proxy. Its contents are local configuration, ignored by Git; include them in server backups. These website definitions are not part of the application backup.

A separate player service can join the Julie Compose network and use a dedicated DNS alias. Place its hostname and reverse-proxy definition in `caddy-sites/player.caddy`. Validate with `./julie exec -T proxy caddy validate --config /etc/caddy/Caddyfile`, then reload with `./julie exec -T proxy caddy reload --config /etc/caddy/Caddyfile`. Adding a site does not require restarting Julie or Liquidsoap. Keep port 80/443 ownership with the existing proxy.

Older installations without the sites-directory mount require a deliberate proxy configuration upgrade before this feature is available. Do not bypass the guarded updater's deployment-change check.
