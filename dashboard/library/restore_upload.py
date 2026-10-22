"""Stop oversized restore uploads while streaming, before temporary storage fills."""

import shutil
import tempfile

from django.conf import settings
from django.core.files.uploadhandler import FileUploadHandler, StopUpload


class RestoreUploadLimit(FileUploadHandler):
    def receive_data_chunk(self, raw_data, start):
        if self.request.path == "/maintenance/restore/":
            directory = settings.FILE_UPLOAD_TEMP_DIR or tempfile.gettempdir()
            if (
                start + len(raw_data) > settings.MAX_RESTORE_BYTES
                or shutil.disk_usage(directory).free < 256 * 1024**2
            ):
                self.request.restore_upload_error = (
                    "Archivio troppo grande o spazio temporaneo insufficiente."
                )
                raise StopUpload(connection_reset=False)
        return raw_data

    def file_complete(self, file_size):
        return None
