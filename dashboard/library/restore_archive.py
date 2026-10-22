"""Read Julie archives as data. Never extract links or execute archived code."""

import gzip
import hashlib
import json
import os
import shutil
import struct
import tarfile
import zipfile
from pathlib import PurePosixPath

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime


class RestoreError(Exception):
    pass


CHUNK = 1024 * 1024
MAX_MANIFEST = 8 * CHUNK
MAX_CONFIG = 128 * CHUNK
MAX_DATABASE = 512 * CHUNK
MAX_FILES = 100_000


def safe_name(value, roots=None):
    if not isinstance(value, str) or not value or len(value) > 350:
        raise RestoreError("Un percorso nell’archivio non è valido.")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in ("", ".", "..") for part in value.split("/"))
        or "\\" in value
        or any(ord(c) < 32 for c in value)
    ):
        raise RestoreError("L’archivio contiene percorsi non sicuri.")
    if roots and path.parts[0] not in roots:
        raise RestoreError("L’archivio contiene file fuori dalle cartelle previste.")
    return value


def read_json(source, limit=MAX_MANIFEST):
    raw = source.read(limit + 1)
    if len(raw) > limit:
        raise RestoreError("Il manifest supera il limite previsto.")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise RestoreError("Manifest non leggibile.") from error


def manifest_files(manifest, identifier):
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") != "julie-backup-v1"
        or manifest.get("id") != identifier
    ):
        raise RestoreError("I componenti non appartengono allo stesso backup Julie.")
    result = {}
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) > MAX_FILES:
        raise RestoreError("Elenco dei file non valido.")
    for item in files:
        if not isinstance(item, dict):
            raise RestoreError("Elenco dei file non valido.")
        name = safe_name(item.get("path"))
        digest, size = item.get("sha256"), item.get("size")
        if (
            name in result
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise RestoreError("Dimensioni, hash o nomi duplicati nel manifest.")
        result[name] = (size, digest)
    return result


class BoundedReader:
    """Bound decompression and hash the exact bytes read, including TAR padding."""

    def __init__(self, source, limit):
        self.source, self.limit, self.count = source, limit, 0
        self.digest = hashlib.sha256()

    def read(self, size):
        if size < 0 or size > CHUNK:
            raise RestoreError("Lettura dell’archivio fuori limite.")
        data = self.source.read(size)
        self.count += len(data)
        if self.count > self.limit:
            raise RestoreError("L’archivio supera le dimensioni consentite.")
        self.digest.update(data)
        return data

    def exact(self, size):
        chunks, remaining = [], size
        while remaining:
            block = self.read(min(CHUNK, remaining))
            if not block:
                raise RestoreError("Archivio incompleto.")
            chunks.append(block)
            remaining -= len(block)
        return b"".join(chunks)

    def drain(self):
        while self.read(CHUNK):
            pass


def pax_path(content):
    """Our writer uses PAX only for long/non-ASCII paths; reject size/link overrides."""
    result, offset = None, 0
    while offset < len(content):
        space = content.find(b" ", offset)
        if space < 0:
            raise RestoreError("Intestazione TAR non valida.")
        try:
            length = int(content[offset:space])
        except ValueError as error:
            raise RestoreError("Intestazione TAR non valida.") from error
        end = offset + length
        if (
            length <= space - offset + 1
            or end > len(content)
            or content[end - 1 : end] != b"\n"
        ):
            raise RestoreError("Intestazione TAR non valida.")
        key, separator, value = content[space + 1 : end - 1].partition(b"=")
        if not separator or key not in (b"path", b"mtime", b"atime", b"ctime"):
            raise RestoreError("Estensioni TAR non supportate.")
        if key == b"path":
            if result is not None:
                raise RestoreError("Percorso TAR duplicato.")
            result = value.decode("utf-8")
        offset = end
    return result


def read_tar(source, limit, identifier, destination=None):
    """Read fixed-size headers ourselves so hostile PAX sizes cannot allocate RAM."""
    reader = BoundedReader(source, limit)
    observed, manifest, pending_path = {}, None, None
    while True:
        header = reader.exact(512)
        if header == b"\0" * 512:
            break
        info = tarfile.TarInfo.frombuf(header, "utf-8", "strict")
        if info.size < 0 or info.size > limit:
            raise RestoreError("Dimensione TAR non valida.")
        if info.type == tarfile.XHDTYPE:
            if pending_path is not None or info.size > 65536:
                raise RestoreError("Intestazione TAR troppo grande.")
            pending_path = pax_path(reader.exact(info.size))
            reader.exact((-info.size) % 512)
            continue
        if not info.isreg():
            raise RestoreError(
                "Il backup contiene collegamenti o file speciali non consentiti."
            )
        name = safe_name(pending_path or info.name)
        if destination is not None and name != "manifest.json":
            safe_name(name, {"originals", "ready"})
        pending_path = None
        if name in observed or len(observed) >= MAX_FILES:
            raise RestoreError("File duplicati o troppi file nell’archivio.")
        if name == "manifest.json":
            if manifest is not None or info.size > MAX_MANIFEST:
                raise RestoreError("Manifest TAR duplicato o troppo grande.")
            manifest = json.loads(reader.exact(info.size))
        else:
            digest, remaining = hashlib.sha256(), info.size
            target = None
            try:
                if destination is not None:
                    target_path = destination / name
                    target_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    target = target_path.open("xb")
                while remaining:
                    block = reader.read(min(CHUNK, remaining))
                    if not block:
                        raise RestoreError("File audio incompleto.")
                    digest.update(block)
                    if target:
                        target.write(block)
                    remaining -= len(block)
            finally:
                if target:
                    target.flush()
                    os.fsync(target.fileno())
                    target.close()
            observed[name] = (info.size, digest.hexdigest())
        reader.exact((-info.size) % 512)
    reader.drain()
    if pending_path is not None or manifest_files(manifest, identifier) != observed:
        raise RestoreError("I file non corrispondono al manifest del backup.")
    return observed


def inspect_archive(archive_path, workspace, media_destination):
    """Verify all three coordinated components; config is checked but never applied."""
    # Reject oversized central directories before ZipFile allocates its entry list.
    with archive_path.open("rb") as raw:
        raw.seek(max(0, archive_path.stat().st_size - 65557))
        tail = raw.read()
    position = tail.rfind(b"PK\x05\x06")
    if position < 0 or len(tail) - position < 22:
        raise RestoreError("Il file non è uno ZIP completo.")
    end = struct.unpack("<4s4H2LH", tail[position : position + 22])
    if end[4] > 16 or end[5] > CHUNK:
        raise RestoreError("Struttura ZIP non supportata.")
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        expected = {
            "manifest.json",
            "README.txt",
            "database.dump",
            "audio.tar",
            "configuration.tar.gz",
        }
        if (
            len(set(names)) != len(names)
            or set(names) - expected
            or not expected - {"README.txt"} <= set(names)
        ):
            raise RestoreError(
                "Scegli un backup completo di Julie, non un archivio separato."
            )
        for info in archive.infolist():
            if info.flag_bits & 1 or info.is_dir():
                raise RestoreError("Archivio cifrato o struttura non supportata.")
        with archive.open("manifest.json") as source:
            manifest = read_json(source)
        if (
            not isinstance(manifest, dict)
            or manifest.get("kind") != "full"
            or manifest.get("coordinated") is not True
        ):
            raise RestoreError("Il ripristino richiede un backup completo coordinato.")
        captured = manifest.get("captured_at")
        captured_date = parse_datetime(captured) if isinstance(captured, str) else None
        if captured_date is None or timezone.is_naive(captured_date):
            raise RestoreError("La data del backup non è valida.")
        identifier = manifest.get("id")
        from uuid import UUID

        try:
            UUID(identifier)
        except (ValueError, TypeError, AttributeError) as error:
            raise RestoreError("Identificativo del backup non valido.") from error
        components = manifest_files(manifest, identifier)
        if set(components) != {"database.dump", "audio.tar", "configuration.tar.gz"}:
            raise RestoreError("Il backup completo non contiene tutti i componenti.")
        declared = sum(size for size, _ in components.values())
        if declared > settings.MAX_RESTORE_BYTES:
            raise RestoreError(
                "I componenti decompressi superano il limite supportato."
            )
        audio_needed = components["audio.tar"][0] + 256 * CHUNK
        metadata_needed = (
            max(components["database.dump"][0] * 4, 128 * CHUNK) + 256 * CHUNK
        )
        if os.stat(media_destination.parent).st_dev == os.stat(workspace).st_dev:
            enough = shutil.disk_usage(workspace).free >= audio_needed + metadata_needed
        else:
            enough = (
                shutil.disk_usage(media_destination.parent).free >= audio_needed
                and shutil.disk_usage(workspace).free >= metadata_needed
            )
        if not enough:
            raise RestoreError(
                "Spazio insufficiente per verificare ed estrarre il backup."
            )
        audio_files = {}
        for name, (size, digest) in components.items():
            if archive.getinfo(name).file_size != size:
                raise RestoreError("Dimensioni dei componenti non corrispondenti.")
            if (
                name == "database.dump"
                and size > MAX_DATABASE
                or name == "configuration.tar.gz"
                and size > MAX_CONFIG
            ):
                raise RestoreError(
                    "Il database o la configurazione supera il limite supportato."
                )
            with archive.open(name) as source:
                reader = BoundedReader(source, size)
                if name == "database.dump":
                    with (workspace / name).open("xb") as output:
                        while block := reader.read(CHUNK):
                            output.write(block)
                elif name == "audio.tar":
                    audio_files = read_tar(reader, size, identifier, media_destination)
                else:
                    with gzip.GzipFile(fileobj=reader) as uncompressed:
                        read_tar(uncompressed, MAX_CONFIG, identifier)
                    reader.drain()
                if reader.count != size or reader.digest.hexdigest() != digest:
                    raise RestoreError(
                        "La verifica SHA-256 di un componente non è riuscita."
                    )
        return {
            "backup_id": identifier,
            "captured_at": manifest.get("captured_at"),
            "files": len(audio_files),
            "bytes": sum(size for size, _ in audio_files.values()),
            "archive_bytes": declared,
        }, audio_files
