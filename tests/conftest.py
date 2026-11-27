"""Shared fixtures for Zone Inspect tests."""

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


# In-memory stand-in for r2_storage (key → bytes)
FAKE_R2: dict[str, bytes] = {}


def _install_fake_r2():
    import r2_storage as r2

    def upload_bytes(key, data, content_type="application/octet-stream"):
        FAKE_R2[key] = bytes(data)

    def upload_json(key, obj):
        FAKE_R2[key] = json.dumps(obj).encode()

    def download_bytes(key):
        return FAKE_R2.get(key)

    def download_json(key):
        data = FAKE_R2.get(key)
        return json.loads(data) if data is not None else None

    def append_line(key, line):
        FAKE_R2[key] = FAKE_R2.get(key, b"") + (line + "\n").encode()

    def list_keys(prefix, max_keys=10000):
        return [k for k in FAKE_R2 if k.startswith(prefix)][:max_keys]

    def delete_key(key):
        FAKE_R2.pop(key, None)

    def delete_prefix(prefix):
        keys = list_keys(prefix)
        for k in keys:
            del FAKE_R2[k]
        return len(keys)

    def key_exists(key):
        return key in FAKE_R2

    for name, fn in list(locals().items()):
        if callable(fn):
            setattr(r2, name, fn)


class _SyncPool:
    """Runs 'background' R2 uploads inline so tests can assert on them."""

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


@pytest.fixture(scope="session", autouse=True)
def _patch_env(tmp_path_factory):
    """Set env vars BEFORE app is imported so no real DB / R2 is touched."""
    os.environ["FLASK_SECRET"] = "test-secret-key"
    # Never the production DB: an empty value also stops app.py's
    # load_dotenv() from pulling MONGODB_URI back in from .env.
    # Opt in to Mongo tests with a dedicated database: TEST_MONGODB_URI=...
    os.environ["MONGODB_URI"] = os.environ.get("TEST_MONGODB_URI", "")
    # Dummy R2 credentials: r2_storage refuses to import without them.
    # Every R2 call is replaced by the in-memory fake below, so tests can
    # never touch a real bucket even if real credentials are present.
    for var in ("R2_ACCOUNT_ID", "R2_BUCKET", "R2_ACCESS_KEY_ID",
                "R2_SECRET_ACCESS_KEY"):
        os.environ.setdefault(var, "test")

    # Write test auth_keys.json and point the module-level constant to it
    tmp = tmp_path_factory.mktemp("pcb_test")
    auth_file = tmp / "auth_keys.json"
    auth_file.write_text(json.dumps(_TEST_AUTH_KEYS))

    import app as app_module
    app_module.AUTH_KEYS_PATH = Path(str(auth_file))
    app_module._MOBILE_TOKENS_PATH = tmp / "mobile_tokens.json"
    app_module._mobile_tokens.clear()
    _install_fake_r2()
    app_module._r2_pool = _SyncPool()

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


# ---------------------------------------------------------------------------
#  Composite fixtures for multi-step workflows
# ---------------------------------------------------------------------------
@pytest.fixture()
def session_with_zones(authed_client, test_image_bytes):
    """Create a session, set 2 zones, return (session_id, zones)."""
    import io as _io
    resp = authed_client.post(
        "/api/session",
        data={"image": (_io.BytesIO(test_image_bytes), "board.jpg")},
        content_type="multipart/form-data",
    )
    sid = resp.get_json()["session_id"]
    zones = [
        {"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5, "label": "IC1"},
        {"x": 0.5, "y": 0.5, "w": 0.4, "h": 0.4, "label": "R1"},
    ]
    authed_client.post(f"/api/session/{sid}/zones", json={"zones": zones})
    return sid, zones


@pytest.fixture(scope="session")
def mongo_available(_patch_env):
    """True if MongoDB is connected. Starts from empty collections — only
    ever on a database whose name ends in '_test'."""
    import app as app_module
    if app_module.MONGO_AVAILABLE:
        db = app_module._mongo_db
        assert db.name.endswith("_test"), (
            f"refusing to run Mongo tests against '{db.name}'")
        for col in ("templates", "recipe_versions", "samples",
                    "inspection_results"):
            db[col].delete_many({})
    return app_module.MONGO_AVAILABLE


def _skip_no_mongo(mongo_available):
    if not mongo_available:
        pytest.skip("MongoDB not available")
