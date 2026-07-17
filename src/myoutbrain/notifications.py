from __future__ import annotations

from dataclasses import dataclass
import base64
import html
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Protocol

from myoutbrain.persistence import atomic_write, json_document


class NotificationFailure(Exception):
    """Raised when a local notification could not be delivered."""


@dataclass(frozen=True)
class LocalNotification:
    title: str
    body: str
    action: str

    def to_data(self) -> dict[str, str]:
        return {"title": self.title, "body": self.body, "action": self.action}


class LocalNotifier(Protocol):
    def notify(self, notification: LocalNotification) -> None: ...


class RecordingLocalNotifier:
    def __init__(self, path: Path) -> None:
        self._path = path

    def notify(self, notification: LocalNotification) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self._path, json_document(notification.to_data()))


class WindowsLocalNotifier:
    def notify(self, notification: LocalNotification) -> None:
        if sys.platform != "win32":
            raise NotificationFailure(
                "native local notifications are only configured on Windows"
            )
        launch = html.escape(notification.action, quote=True)
        title = html.escape(notification.title)
        body = html.escape(notification.body)
        xml = (
            f'<toast launch="{launch}"><visual><binding template="ToastGeneric">'
            f"<text>{title}</text><text>{body}</text>"
            "</binding></visual></toast>"
        )
        script = (
            "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument;"
            f"$xml.LoadXml({json.dumps(xml)});"
            "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml);"
            "[Windows.UI.Notifications.ToastNotificationManager]::"
            "CreateToastNotifier('MyOutBrain').Show($toast)"
        )
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        try:
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-EncodedCommand",
                    encoded,
                ],
                capture_output=True,
                check=False,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise NotificationFailure("local notification delivery failed") from error
        if result.returncode != 0:
            raise NotificationFailure("local notification delivery failed")


def create_local_notifier() -> LocalNotifier:
    adapter = os.environ.get("MYOUTBRAIN_NOTIFICATION_ADAPTER", "windows")
    if adapter == "recording":
        path = os.environ.get("MYOUTBRAIN_NOTIFICATION_FILE")
        if path is None or not path.strip():
            raise NotificationFailure("recording notification file is not configured")
        return RecordingLocalNotifier(Path(path))
    if adapter == "windows":
        return WindowsLocalNotifier()
    raise NotificationFailure(f"unknown local notification adapter: {adapter}")
