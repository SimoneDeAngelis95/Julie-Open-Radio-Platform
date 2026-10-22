# Public APIs

Julie supplies the APIs and audio stream. The listener-facing website and Replay interface are built separately. Do not use internal library APIs for a public website.

## Calendar

`GET /api/v1/schedule/` exposes only appointments marked public.

| Parameter | Meaning |
| --- | --- |
| `from`, `to` | ISO dates or timezone-aware timestamps; maximum window 366 days |
| `page` | Page number, starting at 1 |
| `page_size` | Results per page, from 1 to 200 |

Responses contain `timezone`, `from`, `to`, `count`, `page`, `pages` and `results`. The reported timezone is Europe/Rome. Publishing an appointment does not automatically publish its podcast audio.

`GET /calendar.ics` returns an iCalendar feed. A calendar application can subscribe to the URL and refresh periodically. Downloading and importing a file once does not create a subscription. Refresh frequency depends on the calendar application. Events retain stable UIDs after edits or cancellations. The default window covers the previous 30 days and the next 336 days.

## Replay

- `GET /api/v1/podcasts/`: public catalogue, `page` starting at 1 and `page_size` from 1 to 100.
- `GET /api/v1/podcasts/UUID/`: podcast details.
- `GET /api/v1/podcasts/UUID/audio.mp3`: public MP3 copy, supporting Range requests.

HEAD and OPTIONS are supported. Public responses use `Cache-Control: no-store`; do not cache them in the website if access must remain revocable. Making a podcast private or moving it to the trash revokes new requests, including requests to a previously known MP3 URL. This cannot retract downloaded copies or bytes already received by a player.

Absolute detail and audio URLs use the configured public API origin. If the website uses a different origin, check CORS under **Manutenzione → Configurazione**. The public proxy template exposes only public APIs and the calendar, not administration.

## Live stream

The default mount is `/stream.mp3`, MP3 at 128 kbit/s. An HTTPS website should use the public HTTPS stream URL. Never put Icecast or live-input credentials in public website code.
