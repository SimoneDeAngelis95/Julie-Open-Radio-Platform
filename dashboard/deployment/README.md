# Modello HTTPS

I file di questa cartella sono descritti in [Configurazione](../../docs/configurazione.md).

Il comando `./julie` carica compose.proxy.yaml dopo i file di base quando `.env` contiene `JULIE_PROXY_ENABLED=1`. env.proxy.example va copiato nella radice come `.env.proxy`. Non copiare o attivare questi esempi senza configurare DNS, host consentiti e rete del server.

I nomi example.org sono segnaposto: nessun dominio è richiesto dalla piattaforma. Per usare lo stesso hostname per più funzioni occorre adattare i blocchi Caddy, non ripetere lo stesso hostname nei tre blocchi dell’esempio.
