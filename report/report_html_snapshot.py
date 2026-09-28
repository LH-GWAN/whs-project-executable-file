"""Private temporary HTML file for Qt WebEngine PDF export.

QWebEngineView.setHtml converts the whole HTML (including embedded base64 images)
into a data URL and fails around 2 MB. A short-lived, user-private file URL avoids
that limit without changing the evidence image bytes. This file is always cleaned
when the exporter finishes or fails, and must never enter case folders or Git.
"""
from __future__ import annotations

import os
import tempfile


class ReportHtmlSnapshot:
    def __init__(self, html: str):
        self._tempdir = tempfile.TemporaryDirectory(prefix="idas-report-")
        self.path = os.path.join(self._tempdir.name, "report.html")
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            with os.fdopen(os.open(self.path, flags, 0o600), "w", encoding="utf-8") as handle:
                handle.write(html)
        except BaseException:
            self._tempdir.cleanup()
            raise

    def close(self) -> None:
        if self._tempdir is not None:
            directory, self._tempdir = self._tempdir, None
            directory.cleanup()
