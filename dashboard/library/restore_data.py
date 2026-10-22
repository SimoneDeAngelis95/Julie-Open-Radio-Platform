"""Decode COPY data without executing SQL from an uploaded PostgreSQL dump.

pg_restore only writes a bounded text file. Values are validated against trusted
Django fields and staged in SQLite; only parameterized inserts reach the live DB.
Accounts, credentials, sessions and operational backup/reset jobs are excluded.
"""

import json
import math
import os
import re
import resource
import shutil
import sqlite3
import subprocess
from contextlib import contextmanager

from django.core.management.color import no_style
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection
from django.utils import timezone

from .models import (
    Asset,
    AudioReprocessJob,
    Broadcast,
    BroadcastSeries,
    ListeningSample,
    Playlist,
    RadioSettings,
    RadioSnapshot,
)
from .restore_archive import RestoreError, safe_name

MODELS = [
    Asset,
    Playlist,
    Playlist.assets.through,
    BroadcastSeries,
    Broadcast,
    RadioSettings,
    ListeningSample,
    RadioSnapshot,
]
TABLES = {model._meta.db_table: model for model in MODELS}
REQUIRED = {
    Asset._meta.db_table,
    Playlist._meta.db_table,
    RadioSettings._meta.db_table,
    Broadcast._meta.db_table,
    ListeningSample._meta.db_table,
}
COPY = re.compile(
    r'COPY (?:public|"public")\.(library_[a-z_]+|"library_[a-z_]+") \(([^;]+)\) FROM stdin;'
)

MAX_ROWS = 2_000_000
MAX_LINE = 1024 * 1024
MAX_SQL = 512 * 1024 * 1024


def export_copy(dump, destination):
    arguments = ["pg_restore", "--data-only", "--no-owner", "--no-privileges"]
    for name in TABLES:
        arguments += ["--table", name]
    arguments += ["--file", str(destination), str(dump)]

    def limits():
        size = min(
            MAX_SQL,
            max(0, shutil.disk_usage(destination.parent).free - 256 * 1024 * 1024),
        )
        resource.setrlimit(resource.RLIMIT_FSIZE, (size, size))

    # Explicitly remove database credentials: this process must never connect.
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("PG")
    }
    try:
        result = subprocess.run(
            arguments,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=600,
            preexec_fn=limits,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RestoreError(
            "Il database non può essere letto con gli strumenti installati."
        ) from error
    if (
        result.returncode
        or not destination.is_file()
        or destination.stat().st_size > MAX_SQL
    ):
        raise RestoreError(
            "Dump PostgreSQL non leggibile o troppo grande. Nessun SQL dell’archivio è stato eseguito."
        )


def decode_copy(value):
    if value == r"\N":
        return None
    source = value.encode("utf-8")
    result, index = bytearray(), 0
    escapes = {
        ord("b"): 8,
        ord("f"): 12,
        ord("n"): 10,
        ord("r"): 13,
        ord("t"): 9,
        ord("v"): 11,
        92: 92,
    }
    while index < len(source):
        char = source[index]
        index += 1
        if char != 92:
            result.append(char)
            continue
        if index == len(source):
            raise RestoreError("Valore COPY non valido.")
        char = source[index]
        index += 1
        if char in b"01234567":
            digits = bytes([char])
            while (
                index < len(source) and len(digits) < 3 and source[index] in b"01234567"
            ):
                digits += bytes([source[index]])
                index += 1
            number = int(digits, 8)
            if number > 255:
                raise RestoreError("Valore COPY non valido.")
            result.append(number)
        elif char == ord("x"):
            digits = b""
            while (
                index < len(source)
                and len(digits) < 2
                and source[index] in b"0123456789abcdefABCDEF"
            ):
                digits += bytes([source[index]])
                index += 1
            if not digits:
                raise RestoreError("Valore COPY non valido.")
            result.append(int(digits, 16))
        else:
            result.append(escapes.get(char, char))
    if 0 in result:
        raise RestoreError("Il database contiene caratteri nulli non validi.")
    return result.decode("utf-8", errors="strict")


def typed_row(model, columns, values):
    fields = {field.column: field for field in model._meta.concrete_fields}
    if (
        len(columns) != len(set(columns))
        or set(columns) - fields.keys()
        or model._meta.pk.column not in columns
    ):
        raise RestoreError(
            "Il database contiene campi non supportati da questa versione di Julie."
        )
    raw = dict(zip(columns, values, strict=True))
    result = {}
    for column, field in fields.items():
        if column not in raw:
            if field.has_default():
                value = field.get_default()
            elif field.null:
                value = None
            elif field.blank:
                value = ""
            else:
                raise RestoreError("Il database è incompleto o non compatibile.")
        else:
            value = raw[column]
        if value is not None:
            if field.get_internal_type() == "JSONField" and isinstance(value, str):
                value = json.loads(value)
            value = field.to_python(value)
            if isinstance(value, float) and not math.isfinite(value):
                raise RestoreError("Il database contiene numeri non validi.")
            field.run_validators(value)
            if field.choices and value not in dict(field.flatchoices):
                raise RestoreError("Il database contiene uno stato non riconosciuto.")
            if field.get_internal_type() == "DateTimeField" and timezone.is_naive(
                value
            ):
                raise RestoreError("Il database contiene date senza fuso orario.")
        elif not field.null:
            raise RestoreError("Il database contiene valori obbligatori mancanti.")
        result[column] = value
    if model == RadioSettings and (
        result["id"] != 1 or not 1 <= result["jingle_every"] <= 100
    ):
        raise RestoreError("Impostazioni del jingle non valide.")
    if model == BroadcastSeries and (
        not isinstance(result["weekdays"], list)
        or not result["weekdays"]
        or any(type(day) is not int or not 0 <= day <= 6 for day in result["weekdays"])
    ):
        raise RestoreError("Ricorrenza non valida.")
    if model == Asset:
        for key in ("original_path", "prepared_path", "replay_path"):
            if result[key]:
                safe_name(
                    result[key], {"originals"} if key == "original_path" else {"ready"}
                )
        if not result["purged_at"] and not result["original_path"]:
            raise RestoreError("Un audio non indica il suo originale.")
    return result


@contextmanager
def connect(path):
    database = sqlite3.connect(path)
    try:
        database.execute("PRAGMA journal_mode=DELETE")
        with database:
            yield database
    finally:
        database.close()


def validate_copy(sql, destination, audio_files):
    counts, seen, table, columns, row_count = {}, set(), None, [], 0
    with connect(destination) as staged:
        staged.executescript("""
            CREATE TABLE records (table_name TEXT, primary_key TEXT, data TEXT, PRIMARY KEY(table_name,primary_key));
            CREATE TABLE links (target_table TEXT, target_key TEXT);
            CREATE TABLE unique_values (field_key TEXT, value TEXT, UNIQUE(field_key,value));
        """)
        with sql.open(encoding="utf-8", errors="strict", newline="") as source:
            while line := source.readline(MAX_LINE + 1):
                if len(line) > MAX_LINE:
                    raise RestoreError(
                        "Una riga del database supera il limite previsto."
                    )
                line = line.removesuffix("\n").removesuffix("\r")
                if table is None:
                    match = COPY.fullmatch(line)
                    if not match:
                        continue  # Comments/SET/other SQL are never executed.
                    table = match[1].strip('"')
                    if table not in TABLES or table in seen:
                        raise RestoreError("Tabella non prevista o duplicata.")
                    seen.add(table)
                    columns = []
                    for column in match[2].split(","):
                        token = re.fullmatch(
                            r'(?:([a-z_]+)|"([a-z_]+)")', column.strip()
                        )
                        if token is None:
                            raise RestoreError("Intestazione COPY non valida.")
                        columns.append(token[1] or token[2])
                    counts[table] = 0
                    continue
                if line == r"\.":
                    table = None
                    continue
                values = [decode_copy(value) for value in line.split("\t")]
                if len(values) != len(columns):
                    raise RestoreError("Riga del database incompleta.")
                model = TABLES[table]
                record = typed_row(model, columns, values)
                row_count += 1
                if (
                    row_count % 1000 == 0
                    and shutil.disk_usage(destination.parent).free < 256 * 1024 * 1024
                ):
                    raise RestoreError(
                        "Spazio insufficiente durante la verifica del database."
                    )
                if row_count > MAX_ROWS:
                    raise RestoreError(
                        "Il database supera il numero di record supportato."
                    )
                if model == Asset and not record["purged_at"]:
                    for name in ("original_path", "prepared_path", "replay_path"):
                        if record[name] and record[name] not in audio_files:
                            raise RestoreError(
                                "Il database fa riferimento ad audio mancanti nel backup."
                            )
                if model == Asset and not record["purged_at"]:
                    original = audio_files.get(record["original_path"])
                    if original is None or original[1] != record["checksum"]:
                        raise RestoreError(
                            "Un originale non corrisponde alla libreria del backup."
                        )
                key = str(record[model._meta.pk.column])
                staged.execute(
                    "INSERT INTO records VALUES(?,?,?)",
                    (table, key, json.dumps(record, cls=DjangoJSONEncoder)),
                )
                for field in model._meta.concrete_fields:
                    value = record[field.column]
                    if field.many_to_one and value is not None:
                        target = field.remote_field.model._meta.db_table
                        staged.execute(
                            "INSERT INTO links VALUES(?,?)", (target, str(value))
                        )
                    if field.unique and not field.primary_key and value is not None:
                        staged.execute(
                            "INSERT INTO unique_values VALUES(?,?)",
                            (table + "." + field.column, str(value)),
                        )
                if model == Playlist.assets.through:
                    staged.execute(
                        "INSERT INTO unique_values VALUES(?,?)",
                        (
                            "playlist-pair",
                            str(record["playlist_id"]) + ":" + str(record["asset_id"]),
                        ),
                    )
                if (
                    model == Broadcast
                    and record["series_id"] is not None
                    and record["occurrence_date"] is not None
                ):
                    staged.execute(
                        "INSERT INTO unique_values VALUES(?,?)",
                        (
                            "occurrence",
                            str(record["series_id"])
                            + ":"
                            + str(record["occurrence_date"]),
                        ),
                    )
                counts[table] += 1
        if table is not None or not REQUIRED <= seen:
            raise RestoreError("Il database non contiene tutte le tabelle necessarie.")
        if staged.execute(
            "SELECT 1 FROM links l LEFT JOIN records r ON r.table_name=l.target_table AND r.primary_key=l.target_key WHERE r.primary_key IS NULL LIMIT 1"
        ).fetchone():
            raise RestoreError(
                "Le relazioni fra audio, playlist e palinsesto sono incomplete."
            )
    return counts


def clear_content():
    AudioReprocessJob.objects.all().delete()
    Broadcast.objects.all().delete()
    BroadcastSeries.objects.all().delete()
    RadioSettings.objects.all().delete()
    Playlist.objects.all().delete()
    Asset.objects.all().delete()
    ListeningSample.objects.all().delete()
    RadioSnapshot.objects.all().delete()


def import_content(path):
    with connect(path) as staged, connection.cursor() as cursor:
        for model in MODELS:
            if model == RadioSnapshot:
                continue  # The collector must observe the destination, not stale source state.
            fields = list(model._meta.concrete_fields)
            columns = [field.column for field in fields]
            quoted = ",".join(connection.ops.quote_name(column) for column in columns)
            query = f"INSERT INTO {connection.ops.quote_name(model._meta.db_table)} ({quoted}) VALUES ({','.join(['%s'] * len(fields))})"
            batch = []
            for (raw,) in staged.execute(
                "SELECT data FROM records WHERE table_name=? ORDER BY primary_key",
                (model._meta.db_table,),
            ):
                data = json.loads(raw)
                if model == Asset:
                    data["removal_pending"] = False
                    if data["status"] == "processing":
                        data["status"] = "pending"
                    if data["replay_status"] == "processing":
                        data["replay_status"] = "pending"
                if model == Broadcast and data["status"] in ("playing", "dispatching"):
                    data.update(
                        status="interrupted",
                        ended_at=timezone.now(),
                        outcome="Trasmissione interrotta prima del ripristino; nessun reinvio automatico.",
                    )
                values = [
                    field.get_db_prep_save(
                        field.to_python(data[field.column]), connection
                    )
                    for field in fields
                ]
                batch.append(values)
                if len(batch) == 250:
                    cursor.executemany(query, batch)
                    batch = []
            if batch:
                cursor.executemany(query, batch)
    connection.check_constraints()


def reset_sequences():
    # PostgreSQL setval is not rolled back. Run only after the content commit,
    # while the maintenance gate still excludes writers.
    with connection.cursor() as cursor:
        for statement in connection.ops.sequence_reset_sql(no_style(), MODELS):
            cursor.execute(statement)
