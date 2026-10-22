# HTTPS template

These files are documented in [Configuration](../../docs/configuration.md).

The `./julie` command loads compose.proxy.yaml after the base files when `.env` contains `JULIE_PROXY_ENABLED=1`. Copy env.proxy.example to the repository root as `.env.proxy`. Do not activate these examples before configuring DNS, allowed hosts and the server network.

The example.org hostnames are placeholders; Julie does not require a particular domain. To share one hostname between functions, adapt the Caddy site blocks instead of repeating the same hostname in all three example blocks.
