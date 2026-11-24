import hashlib
import hmac
from app.services.github_service import GitHubService


def test_webhook_signature_valid():
    secret = "my-secret-key-42"
    payload = b'{"action": "opened", "pull_request": {"number": 10}}'
    signature = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

    is_valid = GitHubService.verify_webhook_signature(
        payload_bytes=payload,
        signature_header=signature,
        secret=secret,
    )
    assert is_valid is True


def test_webhook_signature_tampered_payload():
    secret = "my-secret-key-42"
    payload = b'{"action": "opened", "pull_request": {"number": 10}}'
    tampered = b'{"action": "opened", "pull_request": {"number": 999}}'
    signature = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

    is_valid = GitHubService.verify_webhook_signature(
        payload_bytes=tampered,
        signature_header=signature,
        secret=secret,
    )
    assert is_valid is False


def test_webhook_signature_missing_header():
    is_valid = GitHubService.verify_webhook_signature(
        payload_bytes=b"test",
        signature_header=None,
        secret="my-secret",
    )
    assert is_valid is False

