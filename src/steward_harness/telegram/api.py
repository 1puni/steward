"""Minimal, robust Telegram Bot API client using httpx."""

from __future__ import annotations

import logging
import mimetypes
import os
import uuid
from hashlib import sha256
from json import JSONDecodeError
from pathlib import Path
from typing import Any

import httpx

# httpx logs full request URLs at INFO, and these URLs embed the bot token
# (`.../bot<token>/...`). Quiet it so tokens never reach journald or log grep.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class TelegramAPIError(RuntimeError):
    """Raised when a Bot API request fails.

    ``retry_after`` is Telegram's own instruction, in seconds, present only on
    a 429. It is the wait that will actually clear; anything shorter is spent.
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


#: Telegram never asks for more than a few minutes; a larger number is a bug
#: or a hostile response, and blocking a worker on it is worse than retrying.
_MAX_RETRY_AFTER_SECONDS = 300.0


def _retry_after(response: httpx.Response) -> float | None:
    """Telegram's requested backoff for a rate-limited request, if it gave one."""
    if response.status_code != 429:
        return None
    try:
        parameters = response.json().get("parameters", {})
        seconds = float(parameters.get("retry_after"))
    except (JSONDecodeError, UnicodeDecodeError, AttributeError, TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return min(seconds, _MAX_RETRY_AFTER_SECONDS)


class TelegramAPI:
    """Synchronous HTTP client for Telegram Bot API."""

    def __init__(self, token: str, base_url: str = "https://api.telegram.org") -> None:
        self.token = token.strip()
        root = base_url.rstrip("/")
        self.base_url = f"{root}/bot{self.token}"
        self.file_base_url = f"{root}/file/bot{self.token}"
        # One transport owns TLS contexts and pooled connections for the bot's
        # lifetime. Per-poll clients leave SSL allocations in cyclic garbage.
        self._client = httpx.Client()

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, endpoint: str, timeout: float = 30.0, **kwargs: Any) -> Any:
        url = f"{self.base_url}/{endpoint}"
        try:
            res = self._client.request(method, url, timeout=timeout, **kwargs)
        except httpx.RequestError as exc:
            raise TelegramAPIError(f"Telegram network error: {exc}") from exc

        if res.status_code != 200:
            # 429 carries the only backoff Telegram will accept. Guessing a
            # shorter one spends the whole retry budget inside the window and
            # reports a permanent failure for a wait that was always temporary.
            raise TelegramAPIError(
                f"Telegram API HTTP {res.status_code}: {res.text[:200]}",
                retry_after=_retry_after(res),
            )

        try:
            data = res.json()
        except (JSONDecodeError, UnicodeDecodeError) as exc:
            raise TelegramAPIError("Invalid JSON from Telegram API") from exc

        if not data.get("ok"):
            desc = data.get("description", "Unknown error")
            raise TelegramAPIError(f"Telegram API returned not ok: {desc}")

        return data.get("result")

    def get_me(self) -> dict[str, Any]:
        return self._request("GET", "getMe")

    def set_my_commands(self, commands: list[dict[str, str]]) -> None:
        self._request("POST", "setMyCommands", json={"commands": commands})

    def get_chat(self, chat_id: int) -> dict[str, Any]:
        return self._request("POST", "getChat", json={"chat_id": chat_id})

    def get_chat_member(self, chat_id: int, user_id: int) -> dict[str, Any]:
        return self._request("POST", "getChatMember", json={"chat_id": chat_id, "user_id": user_id})

    def get_chat_administrators(self, chat_id: int) -> tuple[dict[str, Any], ...]:
        """List the chat's administrators.

        Telegram omits *other* bots but includes this one, so a caller deciding
        admission must filter `user.is_bot` itself. Verified against the live
        live chat, which returns the steward's own bot account alongside the
        two human administrators.
        """
        result = self._request("POST", "getChatAdministrators", json={"chat_id": chat_id})
        if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
            raise TelegramAPIError("getChatAdministrators returned an invalid result")
        return tuple(result)

    def get_updates(self, offset: int, timeout_seconds: int = 20) -> list[dict[str, Any]]:
        return self._request(
            "POST",
            "getUpdates",
            timeout=float(timeout_seconds + 10),
            json={
                "offset": offset,
                "timeout": timeout_seconds,
                "allowed_updates": ["message"],
            },
        )

    def download_file(
        self,
        file_id: str,
        destination: str | Path,
        *,
        max_bytes: int,
    ) -> tuple[int, str]:
        """Resolve and atomically download one bounded Telegram file."""
        if not file_id or len(file_id) > 512:
            raise TelegramAPIError("Telegram file id is invalid")
        metadata = self._request("POST", "getFile", json={"file_id": file_id})
        file_path = metadata.get("file_path") if isinstance(metadata, dict) else None
        if not isinstance(file_path, str) or not file_path or file_path.startswith("/"):
            raise TelegramAPIError("Telegram getFile returned no usable file path")
        if any(part in {"", ".", ".."} for part in file_path.split("/")):
            raise TelegramAPIError("Telegram getFile returned an unsafe file path")

        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.partial")
        digest = sha256()
        size = 0
        try:
            with self._client.stream(
                "GET", f"{self.file_base_url}/{file_path}", timeout=60.0,
            ) as response:
                if response.status_code != 200:
                    raise TelegramAPIError(
                        f"Telegram file download HTTP {response.status_code}"
                    )
                with temporary.open("xb") as handle:
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise TelegramAPIError(
                                f"Telegram file exceeds configured {max_bytes}-byte cap"
                            )
                        digest.update(chunk)
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        except httpx.RequestError as exc:
            raise TelegramAPIError(f"Telegram file download failed: {exc}") from exc
        finally:
            temporary.unlink(missing_ok=True)
        return size, digest.hexdigest()

    def send_chat_action(self, chat_id: int, action: str = "typing", topic_id: int | None = None) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "action": action}
        if topic_id and topic_id > 1:
            payload["message_thread_id"] = topic_id
        try:
            self._request("POST", "sendChatAction", timeout=5.0, json=payload)
        except TelegramAPIError:
            pass

    def send_message(
        self,
        chat_id: int,
        text: str,
        topic_id: int | None = None,
        *,
        parse_mode: str | None = None,
    ) -> int:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if topic_id and topic_id > 1:
            payload["message_thread_id"] = topic_id
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode

        res = self._request("POST", "sendMessage", json=payload)
        return res.get("message_id", 0)

    def pin_chat_message(
        self,
        chat_id: int,
        message_id: int,
        *,
        disable_notification: bool = True,
    ) -> None:
        """Pin one message in a caller-selected chat after harness authorization."""
        self._request(
            "POST",
            "pinChatMessage",
            json={
                "chat_id": chat_id,
                "message_id": message_id,
                "disable_notification": disable_notification,
            },
        )

    def send_photo(
        self,
        chat_id: int,
        photo_path: str | Path,
        caption: str | None = None,
        topic_id: int | None = None,
    ) -> int:
        path = Path(photo_path)
        if not path.is_file():
            raise FileNotFoundError(f"Photo file not found: {path}")

        data: dict[str, Any] = {"chat_id": str(chat_id)}
        if topic_id and topic_id > 1:
            data["message_thread_id"] = str(topic_id)
        if caption:
            data["caption"] = caption

        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return self._send_file(
            "sendPhoto", "photo", path, data, media_type=media_type, timeout=60.0
        )

    def send_document(
        self,
        chat_id: int,
        document_path: str | Path,
        caption: str | None = None,
        topic_id: int | None = None,
    ) -> int:
        """Upload a generic document, including large self-hosted-Bot-API artifacts."""
        path = Path(document_path)
        if not path.is_file():
            raise FileNotFoundError(f"Document file not found: {path}")

        data: dict[str, Any] = {"chat_id": str(chat_id)}
        if topic_id and topic_id > 1:
            data["message_thread_id"] = str(topic_id)
        if caption:
            data["caption"] = caption
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return self._send_file(
            "sendDocument",
            "document",
            path,
            data,
            media_type=media_type,
            timeout=600.0,
        )

    def _send_file(
        self,
        endpoint: str,
        field_name: str,
        path: Path,
        data: dict[str, Any],
        *,
        media_type: str,
        timeout: float,
    ) -> int:
        with path.open("rb") as file_handle:
            result = self._request(
                "POST", endpoint, timeout=timeout, data=data,
                files={field_name: (path.name, file_handle, media_type)},
            )
        return result.get("message_id", 0)
