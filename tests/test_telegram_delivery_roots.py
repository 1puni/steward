"""Writable attachment paths must not redefine trusted controller roots."""
import os

import pytest

from test_telegram import _service
from steward_harness.telegram.service import TelegramDeliveryError


@pytest.mark.parametrize("marker,method", [("send_document", "send_document"),
                                           ("send_image", "send_photo")])
def test_attachment_root_replacement_cannot_expose_slack_secret(tmp_path, monkeypatch, marker, method):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    secret_dir = tmp_path / "slack-secrets"
    secret_dir.mkdir()
    (secret_dir / "bot-token").write_bytes(b"fake-controller-slack-secret")
    service = _service(tmp_path, delivery_roots=[str(artifacts)])
    uploads = []
    monkeypatch.setattr(service.api, "send_message", lambda *args, **kwargs: 1)
    monkeypatch.setattr(service.api, method,
                        lambda chat, path, **kwargs: uploads.append(path.read_bytes()) or 1)
    try:
        # Ordinary generated attachments still use the existing delivery path.
        output = artifacts / "report.bin"
        output.write_bytes(b"ordinary-generated-output")
        service.send_reply(1, 9, f"[[{marker}:{output}]]")
        assert uploads == [b"ordinary-generated-output"]

        # The model can rename its writable directory and replace the name.
        artifacts.rename(tmp_path / "old-artifacts")
        artifacts.symlink_to(secret_dir, target_is_directory=True)
        with pytest.raises(TelegramDeliveryError, match="outside configured delivery_roots"):
            service.send_reply(1, 9, f"[[{marker}:{artifacts / 'bot-token'}]]")
        assert uploads == [b"ordinary-generated-output"]
    finally:
        service.api.close()


@pytest.mark.parametrize("marker", ["send_document", "send_image"])
@pytest.mark.parametrize("replacement", ["parent", "file", "fifo"])
def test_upload_refuses_swap_after_validation_before_open(tmp_path, monkeypatch, marker, replacement):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    output = artifacts / "report.bin"
    output.write_bytes(b"ordinary-generated-output")
    secrets = tmp_path / "slack-secrets"
    secrets.mkdir()
    (secrets / output.name).write_bytes(b"fake-controller-slack-secret")
    service = _service(tmp_path, delivery_roots=[str(artifacts)])
    upload = service.api._send_file
    uploads = []

    def record_request(method, endpoint, **kwargs):
        if "files" in kwargs:
            uploads.extend(stream.read() for _, stream, _ in kwargs["files"].values())
        return {"message_id": 1}

    def swap_then_upload(*args, **kwargs):
        # send_reply and send_photo/send_document have already checked the
        # real file; only the privileged API open remains at this boundary.
        assert output.is_file() and not output.is_symlink()
        if replacement == "parent":
            artifacts.rename(tmp_path / "old-artifacts")
            artifacts.symlink_to(secrets, target_is_directory=True)
        else:
            output.unlink()
            if replacement == "file":
                output.symlink_to(secrets / output.name)
            else:
                os.mkfifo(output)
        return upload(*args, **kwargs)

    monkeypatch.setattr(service.api, "_request", record_request)
    monkeypatch.setattr(service.api, "_send_file", swap_then_upload)
    try:
        with pytest.raises(TelegramDeliveryError):
            service.send_reply(1, 9, f"[[{marker}:{output}]]")
        assert uploads == []
    finally:
        service.api.close()


@pytest.mark.parametrize("marker,endpoint,field", [("send_document", "sendDocument", "document"),
                                                 ("send_image", "sendPhoto", "photo")])
def test_verified_attachment_streams_from_open_descriptor(tmp_path, monkeypatch, marker, endpoint, field):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    output = artifacts / "report.png"
    output.write_bytes(b"ordinary-generated-output")
    alias = artifacts / "latest.png"
    alias.symlink_to(output)
    service = _service(tmp_path, delivery_roots=[str(artifacts)])
    streams = []

    def request(method, actual_endpoint, **kwargs):
        assert method == "POST" and actual_endpoint == endpoint
        assert kwargs["data"] == {"chat_id": "1", "message_thread_id": "9"}
        name, stream, media_type = kwargs["files"][field]
        assert name == "report.png" and media_type == "image/png"
        assert stream.read() == b"ordinary-generated-output"
        streams.append(stream)
        return {"message_id": 1}

    monkeypatch.setattr(service.api, "_request", request)
    try:
        service.send_reply(1, 9, f"[[{marker}:{alias}]]")
        assert len(streams) == 1 and streams[0].closed
    finally:
        service.api.close()
