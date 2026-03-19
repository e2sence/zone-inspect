"""Shared fixtures for PCB Zone Check tests."""

import json
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

# Ensure project root is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
#  Dummy auth_keys for testing
# ---------------------------------------------------------------------------
_TEST_AUTH_KEYS = {
    "users": {
        "test-admin-key-0000": {
            "name": "Test Admin",
            "role": "admin",
        },
        "test-lead-key-1111": {
            "name": "Test Lead",
            "role": "lead",
            "group": "line1",
        },
        "test-operator-key-2222": {
            "name": "Test Operator",
            "role": "operator",
            "group": "line1",
        },
    },
    "api_keys": {
        "apk_test-read-key": {
            "name": "Test API Read",
            "scope": "read",
            "group": "line1",
        },
    },
}


@pytest.fixture(scope="session", autouse=True)
def _patch_env(tmp_path_factory):
    """Set env vars BEFORE app is imported so no real DB / R2 is touched."""
    os.environ["FLASK_SECRET"] = "test-secret-key"
    os.environ.pop("MONGODB_URI", None)

    # Write test auth_keys.json and point the module-level constant to it
    tmp = tmp_path_factory.mktemp("pcb_test")
    auth_file = tmp / "auth_keys.json"
    auth_file.write_text(json.dumps(_TEST_AUTH_KEYS))

    import app as app_module
    app_module.AUTH_KEYS_PATH = Path(str(auth_file))

    yield


@pytest.fixture(scope="session")
def flask_app(_patch_env):
    """Return the Flask app (session-scoped — one model load)."""
    import app as app_module

    app_module.app.config["TESTING"] = True
    return app_module.app


@pytest.fixture()
def client(flask_app):
    """Flask test client, fresh per test."""
    with flask_app.test_client() as c:
        yield c


@pytest.fixture()
def authed_client(client):
    """Test client pre-authenticated as admin."""
    client.set_cookie("pcb_auth_key", "test-admin-key-0000")
    return client


# ---------------------------------------------------------------------------
#  Test images (generated, no filesystem dependency)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def test_image():
    """480x640 BGR test image with simple geometric shapes."""
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    img[:] = (60, 60, 60)  # dark grey background
    cv2.rectangle(img, (50, 50), (200, 200), (0, 255, 0), -1)
    cv2.circle(img, (400, 300), 80, (0, 0, 255), -1)
    cv2.putText(img, "PCB", (250, 100), cv2.FONT_HERSHEY_SIMPLEX, 2,
                (255, 255, 255), 3)
    return img


@pytest.fixture(scope="session")
def test_image_bytes(test_image):
    """JPEG bytes of the test image (for uploading)."""
    _, buf = cv2.imencode(".jpg", test_image)
    return buf.tobytes()


@pytest.fixture(scope="session")
def bright_image():
    """Nearly white image (for glare mask tests)."""
    return np.full((100, 100, 3), 240, dtype=np.uint8)


@pytest.fixture(scope="session")
def dark_image():
    """Dark image (for glare mask tests)."""
    return np.full((100, 100, 3), 30, dtype=np.uint8)
