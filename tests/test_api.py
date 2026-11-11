import importlib

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "cid")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "sec")
    from clipforge import config, db

    importlib.reload(config)
    monkeypatch.setattr(db, "settings", config.settings)
    db.reset_for_tests()
    from clipforge import app as appmod
    from clipforge.publishers import youtube

    monkeypatch.setattr(appmod, "settings", config.settings)
    monkeypatch.setattr(youtube, "settings", config.settings)
    monkeypatch.setattr(appmod.worker, "start", lambda: None)
    from fastapi.testclient import TestClient

    with TestClient(appmod.app) as c:
        yield c, db
    db.reset_for_tests()


def test_status(client):
    c, _ = client
    st = c.get("/api/status").json()
    yt = next(p for p in st["platforms"] if p["name"] == "youtube")
    assert yt["configured"] and not yt["connected"]


def test_oauth_state_roundtrip(client):
    c, db = client
    r = c.get("/oauth/youtube/start", follow_redirects=False)
    assert r.status_code == 307 and "code_challenge=" in r.headers["location"]
    assert db.one("SELECT COUNT(*) n FROM oauth_states")["n"] == 1
    bad = c.get("/oauth/youtube/callback?state=nope&code=x", follow_redirects=False)
    assert bad.status_code == 400


def test_posting_requires_connection(client):
    c, _ = client
    r = c.post("/api/posts", json={"clip_ids": ["x"], "platforms": ["youtube"]})
    assert r.status_code == 400 and "Connect" in r.json()["detail"]


def test_schedule_posts(client):
    c, db = client
    db.insert("videos", id="v", title="t", status="ready", created_at=db.now())
    db.insert("clips", id="c1", video_id="v", rank=1, start=0, end=20, status="ready", title="T", hook="H",
              meta={"captions": {"tiktok": "hi"}, "hashtags": ["a"]}, created_at=db.now())
    db.insert("clips", id="c2", video_id="v", rank=2, start=30, end=50, status="ready", title="T2", hook="H2",
              meta={}, created_at=db.now())
    db.insert("accounts", platform="tiktok", access_token="tok", connected_at=db.now())
    r = c.post("/api/posts", json={"clip_ids": ["c1", "c2"], "platforms": ["tiktok"], "start_at": 2e9,
                                    "interval_minutes": 60})
    assert r.status_code == 200, r.text
    posts = db.query("SELECT * FROM posts ORDER BY scheduled_at")
    assert [p["scheduled_at"] for p in posts] == [2e9, 2e9 + 3600]
    assert posts[0]["caption"] == "hi\n#a"
    assert c.post(f"/api/posts/{posts[0]['id']}/cancel").status_code == 200
    assert db.one("SELECT status FROM posts WHERE id=?", (posts[0]["id"],))["status"] == "canceled"
