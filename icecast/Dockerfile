FROM debian:trixie-slim
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends icecast2 && rm -rf /var/lib/apt/lists/*
USER icecast2
CMD ["icecast2", "-c", "/etc/icecast2/icecast.xml"]
