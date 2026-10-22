"""Audio preparation: constant gain, lossless intermediate, final measurements."""

import json
import math
import subprocess
import time


class AudioError(Exception):
    pass


def command(arguments, deadline):
    remaining = int(deadline - time.monotonic())
    if remaining <= 0:
        raise AudioError("Tempo massimo di preparazione superato.")
    try:
        result = subprocess.run(
            arguments, capture_output=True, text=True, timeout=remaining, check=False
        )
    except subprocess.TimeoutExpired as error:
        raise AudioError("Tempo massimo di preparazione superato.") from error
    if result.returncode:
        raise AudioError(
            "Il file non è decodificabile o FFmpeg non ha completato la preparazione."
        )
    return result


def probe(path, deadline):
    result = command(
        [
            "ffprobe",
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-show_entries",
            "format=duration:format_tags:stream=codec_type,codec_name,sample_rate,channels",
            "-of",
            "json",
            str(path),
        ],
        deadline,
    )
    return json.loads(result.stdout)


def measure(path, deadline):
    result = command(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-threads",
            "1",
            "-filter_threads",
            "1",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-af",
            "loudnorm=I=-20:TP=-1.5:LRA=50:print_format=json,asetpts=N/SR/TB",
            "-progress",
            "pipe:1",
            "-f",
            "null",
            "-",
        ],
        deadline,
    )
    try:
        stats = json.JSONDecoder().raw_decode(
            result.stderr[result.stderr.rfind("{") :]
        )[0]
        values = {
            key: float(stats[key]) for key in ("input_i", "input_tp", "input_lra")
        }
    except (ValueError, KeyError) as error:
        raise AudioError("Misurazione del volume non disponibile.") from error
    if not all(math.isfinite(value) for value in values.values()):
        raise AudioError("Il file è silenzioso o il volume non è misurabile.")
    try:
        # Output timestamps are rebuilt from decoded samples, not container estimates.
        durations = [
            int(line.split("=", 1)[1]) / 1_000_000
            for line in result.stdout.splitlines()
            if line.startswith("out_time_us=")
        ]
        duration = durations[-1]
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError
    except (ValueError, IndexError) as error:
        raise AudioError("Durata dell’audio decodificato non disponibile.") from error
    values["duration"] = duration
    return values


def prepare(original, workspace, target, ceiling):
    """Return a verified FLAC plus measurements. Publication is the caller's job."""
    deadline = time.monotonic() + 3600
    info = probe(original, deadline)
    if not any(
        stream.get("codec_type") == "audio" for stream in info.get("streams", [])
    ):
        raise AudioError("Il file non contiene una traccia audio.")
    tags = {
        key.lower(): str(value)
        for key, value in info.get("format", {}).get("tags", {}).items()
    }
    before = measure(original, deadline)
    duration = before["duration"]
    gain = min(target - before["input_i"], ceiling - 0.2 - before["input_tp"])
    prepared = workspace / "prepared.flac"
    for _ in range(3):
        command(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-threads",
                "1",
                "-filter_threads",
                "1",
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                str(original),
                "-map",
                "0:a:0",
                "-map_metadata",
                "-1",
                "-af",
                f"volume={gain:.6f}dB",
                "-ar",
                "44100",
                "-ac",
                "2",
                "-c:a",
                "flac",
                "-threads",
                "1",
                "-sample_fmt",
                "s32",
                "-bits_per_raw_sample",
                "24",
                str(prepared),
            ],
            deadline,
        )
        after = measure(prepared, deadline)
        if after["input_tp"] <= ceiling:
            break
        gain -= after["input_tp"] - ceiling + 0.2
    else:
        raise AudioError("Il file non supera il controllo sui picchi.")
    final = probe(prepared, deadline)
    audio = final["streams"][0]
    if (
        audio["codec_name"] != "flac"
        or audio["sample_rate"] != "44100"
        or audio["channels"] != 2
    ):
        raise AudioError("La copia preparata ha un formato inatteso.")
    final_duration = float(final["format"]["duration"])
    if not math.isfinite(final_duration) or final_duration <= 0:
        raise AudioError("Durata del file preparato non valida.")
    if abs(duration - final_duration) > 0.25:
        raise AudioError("La durata è cambiata durante la preparazione.")
    if after["input_i"] > target + 0.3:
        raise AudioError("Il livello finale supera quello previsto.")
    return {
        "path": prepared,
        "duration": final_duration,
        "tags": tags,
        "before": before,
        "after": after,
        "gain": gain,
        "peak_constrained": after["input_i"] < target - 0.5,
        "deadline": deadline,
    }


def apply_tags(source, destination, metadata, deadline):
    arguments = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-protocol_whitelist",
        "file,pipe",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-c:a",
        "copy",
        "-map_metadata",
        "-1",
    ]
    for key, value in metadata.items():
        if value:
            arguments += ["-metadata", f"{key}={value}"]
    command(arguments + [str(destination)], deadline)
    actual = {
        key.lower(): value
        for key, value in probe(destination, deadline)["format"].get("tags", {}).items()
    }
    for key, value in metadata.items():
        if value and actual.get(key) != value:
            raise AudioError("Verifica dei metadati fallita.")
