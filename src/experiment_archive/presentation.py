"""Generic dates and explicit lifecycle states for archive readers."""
from datetime import datetime, timezone
import re


def record_time(metadata, fallback=None):
    candidates = [(metadata.get(key), key) for key in ("experiment_time", "experiment_date", "date", "started_at", "created_at")]
    for key in ("experiment_id", "title"):
        match = re.search(r"(?<!\d)(20\d{2})[-/]?(\d{2})[-/]?(\d{2})(?!\d)", metadata.get(key, ""))
        if match:
            candidates.append(("-".join(match.groups()), key))
    candidates.append((fallback, "archive_revision"))
    for value, basis in candidates:
        if not isinstance(value, str):
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return {"time": value, "time_basis": basis, "time_key": parsed.timestamp()}
        except ValueError:
            continue
    return {"time": None, "time_basis": "unknown", "time_key": None}


def sort_by_time(items, order, identity):
    # Missing dates always follow dated records; identity breaks ties consistently.
    return sorted(items, key=lambda item: (item.get("time_key") is None,
        (item.get("time_key") or 0) * (-1 if order == "time_desc" else 1), item[identity]))


def lifecycle(metadata):
    value = metadata.get("lifecycle", {})
    return {key: value.get(key, metadata.get(key + "_status", "unknown"))
            for key in ("run", "evaluation", "conclusion")}
