"""ScienceWorld task family normalization."""

import re
from typing import Any, Optional

def normalize_scienceworld_task_family(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower()
    if not text:
        return None
    text = re.sub(r"^task[_ -]*\d+[_ -]*", "", text)
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text or None
