settings.log.stdout := true
settings.log.file := false
settings.server.telnet := true
settings.server.telnet.bind_addr := "0.0.0.0"
settings.server.telnet.port := 1234

# A single atomic snapshot is read at each track boundary. No database or web
# request is needed to keep the last verified library playing.
rotation_count = ref(0)
last_music = ref("")

def next_library_track()
  snapshot_path = "/audio/library/playout/rotation.txt"
  snapshot = if file.exists(snapshot_path) then file.lines(snapshot_path) else ["0", ""] end
  interval = int_of_string(default=0, list.nth(default="0", snapshot, 0))
  jingle_uri = list.nth(default="", snapshot, 1)
  tracks = list.filter(fun (line) -> line != "", list.tl(list.tl(snapshot)))
  if list.length(tracks) == 0 then
    null
  elsif interval > 0 and jingle_uri != "" and rotation_count() >= interval then
    rotation_count := 0
    request.create(jingle_uri)
  else
    alternatives = list.filter(fun (uri) -> uri != last_music(), tracks)
    selected = list.pick(if list.length(alternatives) > 0 then alternatives else tracks end)
    last_music := selected
    rotation_count := rotation_count() + 1
    request.create(selected)
  end
end

# Liquidsoap needs one queued request to establish readiness.
# A snapshot change clears that pending request without stopping the current track.
music = request.dynamic(id="music", prefetch=1, synchronous=true, retry_delay=2., next_library_track)
rotation = music

started_music_count = ref(0)
def remember_started_track(tags)
  if tags["julie_kind"] == "music" then
    started_music_count := started_music_count() + 1
  elsif tags["julie_kind"] == "jingle" then
    started_music_count := 0
  end
end
music.on_track(synchronous=true, remember_started_track)

def current_snapshot()
  path = "/audio/library/playout/rotation.txt"
  if file.exists(path) then file.contents(path) else "" end
end
last_snapshot = ref(current_snapshot())
def refresh_pending_track()
  latest = current_snapshot()
  if latest != last_snapshot() then
    last_snapshot := latest
    pending = music.queue()
    music.set_queue([])
    list.iter(fun (r) -> request.destroy(r), pending)
    rotation_count := started_music_count()
    log("JULIE: rotation settings refreshed; current track preserved")
  end
end
thread.run(every=1., refresh_pending_track)

# Internal appointment protocol: identifiers make dispatch idempotent.
engine_session = "#{time()}"
engine_events = ref([])
engine_seen = ref([])
active_broadcast = ref("")
live_connected = ref(false)
podcasts = request.queue(id="podcasts")

def broadcast_event(id, event)
  if id != "" then
    entry = "#{id}|#{event}|#{time()}"
    engine_events := list.prefix(2000, [entry, ...engine_events()])
  end
end

def podcast_started(tags)
  id = tags["julie_broadcast"]
  active_broadcast := id
  broadcast_event(id, "started")
end

def podcast_finished(_, _)
  broadcast_event(active_broadcast(), "completed")
  active_broadcast := ""
end
podcasts.on_track(synchronous=true, podcast_started)
podcasts.on_position(synchronous=true, remaining=true, position=0., allow_partial=false, podcast_finished)

def start_live(_)
  live_connected := true
  broadcast_event(active_broadcast(), "interrupted")
  active_broadcast := ""
  podcasts.set_queue([])
  podcasts.skip()
  log("JULIE: live connected; pending/current podcast discarded")
end

def stop_live()
  live_connected := false
end
live = input.harbor(id="live", buffer=1., max=5., port=8001, password=environment.get("JULIE_LIVE_PASSWORD"), on_connect=start_live, on_disconnect=stop_live, "live")

def schedule_broadcast(argument)
  parts = string.split(separator="\\|", argument)
  id = list.nth(default="", parts, 0)
  uri = string.concat(separator="|", list.tl(parts))
  if list.mem(id, engine_seen()) then
    "duplicate"
  elsif id == "" or uri == "" then
    "invalid"
  else
    engine_seen := [id, ...engine_seen()]
    if live_connected() then
      broadcast_event(id, "live")
      "live"
    elsif active_broadcast() != "" or podcasts.is_ready() or list.length(podcasts.queue()) > 0 then
      broadcast_event(id, "busy")
      "busy"
    else
      active_broadcast := id
      request = request.create('annotate:julie_broadcast="#{id}":#{uri}')
      podcasts.push(request)
      broadcast_event(id, "accepted")
      "accepted"
    end
  end
end

def broadcast_status(_)
  "JULIE1|#{engine_session}|#{live_connected()}|#{active_broadcast()}\n" ^ string.concat(separator="\n", list.rev(engine_events()))
end
def abort_broadcast(broadcast_id)
  if active_broadcast() == broadcast_id then
    active_broadcast := ""
    podcasts.set_queue([])
    podcasts.skip()
    "aborted"
  else
    "absent"
  end
end
server.register("julie.abort", abort_broadcast)
server.register("julie.schedule", schedule_broadcast)
server.register("julie.status", broadcast_status)

def enter_podcast(old, new)
  sequence([(fade.out(duration=3., max_duration(3., old)) : source), new])
end
scheduled = fallback(id="scheduled", track_sensitive=false, transition_length=3., transitions=[enter_podcast], [podcasts, rotation])
radio = fallback(id="radio", track_sensitive=false, [live, scheduled, blank()])
radio.on_track(synchronous=false, fun (m) -> log("JULIE TRACK: #{m}"))
output.icecast(%mp3(bitrate=128), start=(not file.exists("/audio/library/locks/radio-stopped")), host="icecast", port=8000, password=environment.get("ICECAST_SOURCE_PASSWORD"), mount="/stream.mp3", name="Julie", description="Open Radio Platform", radio)
