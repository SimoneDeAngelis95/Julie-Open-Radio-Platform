from .backup_guard import reset_requested
from .storage import data_path


def maintenance(request):
    return {
        "reset_in_progress": reset_requested(),
        "restore_in_progress": data_path("locks/restore-requested").exists(),
        "radio_stopped": data_path("locks/radio-stopped").exists(),
    }
