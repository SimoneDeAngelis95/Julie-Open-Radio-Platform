<img src="dashboard/static/julie-radio.png" alt="Julie" width="96">

# Julie · Open Radio Platform

A free software platform for running a school radio station from a browser: music, jingles, podcasts and live broadcasts, with an automated playout engine that keeps working when the dashboard is closed.

The project was born at **I.I.S. Medaglia D’Oro**, now **I.I.S. San Benedetto – Sede Medaglia D’Oro**, to build a simple platform that other schools and communities can reuse.

## Features

- Audio library, playlists and jingles.
- Scheduled podcasts, recurring broadcasts and a calendar.
- Live input with priority over scheduled content.
- Public APIs for the calendar and Replay, the on-demand podcast catalogue.
- Listener statistics, station status, backups and restore.

The interface is currently **in Italian**. Multilingual support is a possible future development and is not implemented yet. A public website and listener-facing Replay player are not included; they can be built separately using the stream and APIs.

## Getting started

**Linux is the recommended platform**, including headless servers. Julie runs Linux containers with Docker Compose. Read [Installation](docs/installation.md) before running the scripts: Docker Engine, Compose and Python 3 are required.

See [docs](docs/README.md) for the full documentation.

**Status:** first beta (`0.1.0-beta.1`). Validate the complete installation on the target system before continuous use. Extended operation and recovery after unexpected interruptions still need further testing.

## License

Julie is released under the **GNU General Public License v3.0**. See [LICENSE](LICENSE). Third-party components retain their own licenses: [Components and credits](docs/components.md).
