"""Read selected Linux host counters through explicit read-only bind mounts."""

import os
from pathlib import Path


def read_metrics():
    root = os.environ.get("JULIE_HOST_METRICS_ROOT")
    result = {
        "available": False,
        "cpu": None,
        "memory": None,
        "uptime_seconds": None,
        "temperature": None,
    }
    if not root:
        return result
    root = Path(root)
    try:
        lines = (root / "stat").read_text().splitlines()
        values = [int(value) for value in lines[0].split()[1:9]]
        if lines[0].startswith("cpu ") and len(values) == 8 and min(values) >= 0:
            result["cpu"] = {
                "total": sum(values),
                "idle": values[3] + values[4],
                "cores": sum(
                    line.startswith("cpu") and line[3:4].isdigit() for line in lines
                ),
            }
    except (OSError, ValueError, IndexError):
        pass
    try:
        memory = {}
        for line in (root / "meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            memory[key] = int(value.strip().split()[0]) * 1024
        total, available = memory["MemTotal"], memory["MemAvailable"]
        if total > 0 and 0 <= available <= total:
            result["memory"] = {
                "total": total,
                "available": available,
                "used": total - available,
                "percent": round(100 * (total - available) / total, 1),
            }
    except (OSError, ValueError, IndexError, KeyError):
        pass
    try:
        uptime = float((root / "uptime").read_text().split()[0])
        if 0 <= uptime < 10**12:
            result["uptime_seconds"] = int(uptime)
    except (OSError, ValueError, IndexError, OverflowError):
        pass
    temperatures = []
    for sensor in (root / "sys/class/thermal").glob("thermal_zone*/temp"):
        try:
            value = int(sensor.read_text().strip()) / 1000
            if -20 <= value <= 150:
                temperatures.append(value)
        except (OSError, ValueError):
            pass
    if temperatures:
        result["temperature"] = round(max(temperatures), 1)
    result["available"] = any(
        result[key] is not None
        for key in ("cpu", "memory", "uptime_seconds", "temperature")
    )
    return result
