"""Login and Telegram chart-photo helpers."""

from __future__ import annotations

import os

from api.candle_cache import _secret_value


def app_login_code() -> str:
    return _secret_value("APP_LOGIN_CODE") or _secret_value("APP_PASSCODE")


class TelegramNotifier:
    def __init__(self):
        self.recipients = self._recipients()
        self.enabled = bool(self.recipients)

    def _recipients(self) -> list[tuple[str, str]]:
        recipients: list[tuple[str, str]] = []

        for index in range(1, 6):
            token = _secret_value(f"TELEGRAM_BOT_TOKEN_{index}")
            chat_id = _secret_value(f"TELEGRAM_CHAT_ID_{index}")
            if token and chat_id:
                recipients.append((token, chat_id))

        token = _secret_value("TELEGRAM_BOT_TOKEN")
        raw_chats = _secret_value("TELEGRAM_CHAT_IDS") or os.getenv("TELEGRAM_CHAT_IDS", "")
        if token and raw_chats:
            for chat_id in raw_chats.replace("\n", ",").split(","):
                chat_id = chat_id.strip()
                if chat_id:
                    recipients.append((token, chat_id))

        return recipients
