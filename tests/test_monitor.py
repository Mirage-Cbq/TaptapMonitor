import asyncio
from dataclasses import replace
import json
import time
from types import SimpleNamespace
from io import BytesIO
from PIL import Image

from fastapi.testclient import TestClient
import httpx
import pytest

from taptap_monitor.app import create_app
from taptap_monitor.client import SourceError, TapTapClient
from taptap_monitor.collector import Collector
from taptap_monitor.config import Settings
from taptap_monitor.content import Item, text_of, media_of
from taptap_monitor.delivery import Delivery, split_message
from taptap_monitor.store import Store
from taptap_monitor.website import site_data
from taptap_monitor.lock import ProcessLock


@pytest.fixture
def settings(tmp_path):
    return Settings(database=str(tmp_path / "test.sqlite3"), request_interval=0,
                    api_token="test-only-token-with-32-characters", developers=[{"id": 383549}])


@pytest.fixture
def store(settings):
    result = Store(settings.database)
    result.add_developer(383549, "Test Studio", "bot:FriendMessage:1")
    result.save_game(383549, {"id": 100, "title": "Test Game"})
    return result


def item(**kwargs):
    return Item(100, "review", "1", "", "Original feedback", "https://www.taptap.cn/review/1", published_at=int(time.time()) - 1000, **kwargs)


def test_baseline_edits_and_dedup_survive_restart(store, settings):
    original = item()
    assert store.save_item(original) == "created"
    assert store.events() == []
    assert len(store.events(include_baseline=True)) == 1
    restarted = Store(settings.database)
    assert restarted.save_item(replace(original, source_updated_at=99)) is None
    assert restarted.save_item(replace(original, body="Edited feedback")) == "updated"
    assert [e["action"] for e in restarted.events()] == ["updated"]
    assert restarted.events()[0]["payload"]["body"] == "Edited feedback"


def test_new_content_and_event_cursor(store):
    original = replace(item(), published_at=int(time.time()) + 1)
    store.save_item(original)
    first = store.events(limit=1)
    store.save_item(replace(original, body="new version"))
    second = store.events(after=first[-1]["id"])
    assert len(second) == 1 and second[0]["action"] == "updated"
    # Historical event snapshot stays unchanged after an edit.
    assert store.events()[0]["payload"]["body"] == "Original feedback"


def test_rich_text_and_html():
    assert text_of({"text": "hello<br />world &amp; friends"}) == "hello\nworld & friends"
    assert text_of({"json": [{"type": "paragraph", "children": [{"text": "nested "}, {"type": "tap_emoji", "children": [{"text": "[smile]"}]}]}]}) == "nested [smile]"


def test_pagination_keeps_query_and_rejects_external_urls(settings):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"success": True, "data": {"list": []}})

    async def run():
        client = TapTapClient(settings, httpx.MockTransport(handle))
        await client.get("/webapiv2/review/v2/list-by-app?app_id=100&from=10&limit=10")
        await client.close()

    asyncio.run(run())
    assert requests[0].url.params["from"] == "10"
    assert requests[0].url.params["app_id"] == "100"
    assert TapTapClient.safe_url("https://www.taptap.cn/webapiv2/x?a=1") == "https://m.taptap.cn/webapiv2/x?a=1"
    assert TapTapClient.safe_url("/webapiv2/x") == "https://m.taptap.cn/webapiv2/x"
    for url in ("https://attacker.invalid/webapiv2/x", "//attacker.invalid/x", "/private", "https://www.taptap.cn:443/webapiv2/x"):
        with pytest.raises(SourceError):
            TapTapClient.safe_url(url)


def test_non_json_verification_is_error(settings):
    async def run():
        client = TapTapClient(settings, httpx.MockTransport(lambda _: httpx.Response(200, text="<html>verification</html>")))
        with pytest.raises(SourceError, match="verification"):
            await client.get("app-list/v1/detail")
        await client.close()
    asyncio.run(run())


def test_capped_scan_resumes_and_refreshes_head(store, settings):
    calls, received = [], []

    class Source:
        safe_url = staticmethod(TapTapClient.safe_url)

        async def get(self, path, params=None):
            calls.append(path)
            if "from=10" in path:
                return {"list": [2], "next_page": ""}
            return {"list": [1], "next_page": "/webapiv2/test/list?from=10"}

    async def consume(data):
        received.extend(data["list"])

    collector = Collector(store, Source(), replace(settings, max_pages=1))
    asyncio.run(collector.walk("test", "test/list", {}, consume))
    assert store.stream("test")["status"] == "pending"
    # A new process can resume the saved page rather than restarting the capped range.
    collector = Collector(Store(settings.database), Source(), replace(settings, max_pages=1))
    asyncio.run(collector.walk("test", "test/list", {}, consume))
    assert received == [1, 1, 2]
    assert store.stream("test")["status"] == "ok"


def test_failed_page_does_not_advance_cursor(store, settings):
    class Source:
        safe_url = staticmethod(TapTapClient.safe_url)

        async def get(self, path, params=None):
            return {"list": [1], "next_page": "/webapiv2/test/list?from=10"}

    async def fail(data):
        raise SourceError("detail unavailable")

    collector = Collector(store, Source(), settings)
    asyncio.run(collector.walk("test", "test/list", {}, fail))
    state = store.stream("test")
    assert state["status"] == "error" and state["next_url"] == ""
    assert "detail unavailable" in state["error"]


def test_api_auth_and_no_public_feedback(settings):
    with TestClient(create_app(settings, scheduler_enabled=False)) as client:
        assert client.get("/healthz").status_code == 200
        for path in ("/api/status", "/api/items", "/api/openapi.json"):
            assert client.get(path).status_code == 401
        headers = {"Authorization": "Bearer " + settings.api_token}
        assert client.get("/api/developers", headers=headers).json()[0]["id"] == 383549
        assert client.post("/api/developers", headers=headers, json={"id": -1}).status_code == 422
        assert client.patch("/api/developers/383549", headers=headers, json={"enabled": False}).json()["enabled"] == 0
        assert client.get("/api/events?limit=0", headers=headers).status_code == 422
        assert client.post("/api/scan?developer_id=999", headers=headers).status_code == 404


def test_api_digest_cursors_are_read_only(settings, store):
    store.save_item(replace(item(), published_at=int(time.time()) + 1))
    with TestClient(create_app(settings, scheduler_enabled=False)) as client:
        headers = {"Authorization": "Bearer " + settings.api_token}
        result = client.get("/api/digest-input?developer_id=383549&limit=1", headers=headers).json()
        assert result["next_cursor"] > 0
        assert result["counts"] == {"评价": 1}
    assert store.developer(383549)["digest_cursor"] == 0


def test_delivery_failure_keeps_cursor_then_retries(store, settings):
    store.save_item(replace(item(), published_at=int(time.time()) + 1))
    with store.connect() as db:
        db.execute("UPDATE developers SET digest_at=0")
    configured = replace(settings, digest_enabled=True, astrbot_url="https://bot.test", astrbot_key="test-key")
    failed = Delivery(store, configured, httpx.MockTransport(lambda _: httpx.Response(500)))
    asyncio.run(failed.tick())
    assert store.developer(383549)["digest_cursor"] == 0
    assert store.query("SELECT status FROM deliveries")[0]["status"] == "pending"
    with store.connect() as db:
        db.execute("UPDATE deliveries SET retry_at=0")
    requests = []

    def accept(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "ok"})

    asyncio.run(Delivery(store, configured, httpx.MockTransport(accept)).tick())
    assert store.developer(383549)["digest_cursor"] == store.high_water()
    assert requests[0]["umo"] == "bot:FriendMessage:1"
    assert store.query("SELECT status FROM deliveries")[0]["status"] == "sent"
    assert len(store.query("SELECT * FROM deliveries")) == 1


def test_message_splitting_has_no_content_loss():
    text = "abcdefgh" * 1000
    parts = split_message(text)
    assert "".join(parts) == text
    assert all(len(p) <= 2800 for p in parts)


def test_website_auto_generation_and_overrides_survive_sync(store):
    store.save_game(383549, {"id": 100, "title": "Source title", "description": {"text": "Source introduction"}, "cover": {"url": "https://img.tapimg.com/a.jpg"}})
    assert site_data(store, 383549)["games"][0]["description"] == "Source introduction"
    store.save_presentation(383549, {"title": "Custom title", "description": "Custom introduction", "featured": True}, 100)
    store.save_game(383549, {"id": 100, "title": "Updated source title", "description": {"text": "Updated source introduction"}})
    assert site_data(store, 383549)["games"][0]["title"] == "Custom title"
    assert site_data(store, 383549)["games"][0]["description"] == "Custom introduction"
    store.save_presentation(383549, {"title": "", "description": ""}, 100)
    assert site_data(store, 383549)["games"][0]["title"] == "Updated source title"
    store.save_game(383549, {"id": 101, "title": "New automatically discovered game"})
    assert len(site_data(store, 383549)["games"]) == 2


def test_website_visibility_factory_pages_and_metadata_escaping(settings, store):
    with TestClient(create_app(settings, scheduler_enabled=False)) as client:
        headers = {"Authorization": "Bearer " + settings.api_token}
        assert client.get('/').status_code == 200
        assert client.patch('/api/sites/383549', json={"display_name": 'Studio </title><script>bad()</script>', "intro": '__SITE_DATA__ <script>test</script>'}, headers=headers).status_code == 200
        rendered = client.get('/').text
        assert 'Studio &lt;/title&gt;' in rendered
        assert '<script>bad()</script>' not in rendered
        assert '<script>test</script>' not in rendered
        assert '"target_umo"' not in client.get('/public/site').text
        assert client.patch('/api/sites/383549/games/100', headers=headers, json={"visible": False}).status_code == 200
        assert client.get('/public/site').json()['games'] == []
        assert client.get('/studio/383549/games/100').status_code == 404
        assert len(client.get('/api/sites/383549', headers=headers).json()['games']) == 1
        assert client.patch('/api/sites/383549/games/999', headers=headers, json={"visible": True}).status_code == 404
        assert client.post('/api/developers', headers=headers, json={"id": 999, "name": "Another studio"}).status_code == 201
        assert client.get('/studio/999').status_code == 200
        assert client.put('/api/site-default/999', headers=headers).status_code == 200
        assert client.get('/public/site').json()['developer_id'] == 999
        assert client.patch('/api/developers/999', headers=headers, json={"enabled": False}).status_code == 200
        assert client.get('/studio/999').status_code == 200
        assert client.patch('/api/sites/999', headers=headers, json={"published": False}).status_code == 200
        assert client.get('/studio/999').status_code == 404


def test_empty_comment_response_and_restricted_previews_keep_other_comments(store, settings):
    class Source:
        safe_url = staticmethod(TapTapClient.safe_url)
        async def get(self, path, params=None):
            if params.get("review_id"):
                return {"total": 0, "next_page": ""}
            return {"list": [
                {"id": 1, "created_time": 1, "contents": {"text": "First"}, "comments": 3, "child_posts": []},
                {"id": 2, "created_time": 1, "contents": {"text": "Second"}},
            ], "next_page": ""}
    collector = Collector(store, Source(), settings)
    asyncio.run(collector.comments(item(), 'test/list', {"review_id": 1}))
    assert store.stream('comments:100:review:1')['status'] == 'ok'
    asyncio.run(collector.comments(replace(item(), kind='topic'), 'test/list', {"moment_id": 1}))
    assert store.stream('comments:100:topic:1')['status'] == 'restricted'
    assert store.get_item(100,'topic_comment','2')['body'] == 'Second'


def test_only_one_writer_process_can_own_database(settings):
    with ProcessLock(settings.database):
        with pytest.raises(RuntimeError, match='Another monitor'):
            with ProcessLock(settings.database):
                pass
    with ProcessLock(settings.database):
        pass


def test_image_only_edits_are_captured(store):
    images = media_of({"text": '<p><img data-origin-url="https://img.tapimg.com/a.jpg" src="small.jpg"></p>',
                       "images": [{"original_url": "https://img.tapimg.com/a.jpg"}]})
    assert images == ['https://img.tapimg.com/a.jpg']
    original = replace(item(), media=images)
    store.save_item(original)
    store.save_item(replace(original, media=['https://img.tapimg.com/b.jpg']))
    assert store.events()[0]['action'] == 'updated'


def test_digest_new_subscriber_starts_after_existing_events(settings, store):
    store.save_item(replace(item(), published_at=int(time.time())+1))
    store.add_developer(999,'Second Studio')
    assert store.developer(999)['digest_cursor'] == store.high_water()


def test_feedback_list_orders_by_publication_not_collection_order(store):
    newer = replace(item(), source_id='2', published_at=200)
    older = replace(item(), source_id='1', published_at=100)
    store.save_item(newer)
    store.save_item(older)
    store.save_item(replace(older, body='Edited old feedback'))
    assert [row['source_id'] for row in store.items()] == ['2','1']


def test_fresh_server_scans_immediately_even_just_after_boot(settings, monkeypatch):
    import taptap_monitor.app as app_module
    monkeypatch.setattr(app_module, 'time', SimpleNamespace(monotonic=lambda: 10))

    class Source:
        safe_url = staticmethod(TapTapClient.safe_url)
        async def get(self, path, params=None):
            if path == 'developer/v1/detail':
                return {'developer': {'name': 'Discovered studio'}}
            if path == 'app-list/v1/detail':
                return {'list': [{'id': 100, 'title': 'Discovered game', 'description': {'text': 'Automatically imported'}}], 'next_page': ''}
            if path == 'group/v1/detail':
                return {'group': {'id': 10}}
            return {'list': [], 'next_page': '', 'total': 0}
        async def close(self):
            pass

    with TestClient(create_app(replace(settings, cache_images=False), client=Source())) as client:
        deadline = time.time() + 2
        while time.time() < deadline:
            data = client.get('/public/site').json()
            if data['games']:
                break
            time.sleep(.02)
        assert data['name'] == 'Discovered studio'
        assert data['games'][0]['title'] == 'Discovered game'
        assert data['games'][0]['description'] == 'Automatically imported'


def test_managed_logo_upload_preserves_alpha_and_survives_source_sync(settings, store):
    buffer = BytesIO()
    picture = Image.new('RGBA',(2,2),(0,0,0,0))
    picture.putpixel((1,1),(255,255,240,255))
    picture.save(buffer,format='PNG')
    original = buffer.getvalue()
    with TestClient(create_app(settings,scheduler_enabled=False)) as client:
        path = '/api/sites/383549/logo'
        headers = {'Authorization':'Bearer '+settings.api_token,'Content-Type':'image/png'}
        assert client.put(path,content=original).status_code == 401
        response = client.put(path,headers=headers,content=original)
        assert response.status_code == 200
        url = response.json()['logo_url']
        assert client.get(url).content == original
        assert client.get('/public/site').json()['logo'] == url
        store.save_profile(383549,{'name':'Updated studio','avatar':{'url':'https://img.tapimg.com/source.png'}})
        assert client.get('/public/site').json()['logo'] == url
        assert client.put(path,headers=headers,content=b'<svg onload="bad()"/>').status_code == 422
        assert client.put(path,headers=headers,content=b'x'*(8*1024*1024+1)).status_code == 413
        assert client.delete(path,headers=headers).status_code == 200
        assert client.get('/public/site').json()['logo'] == 'https://img.tapimg.com/source.png'


def test_bundled_logo_is_seeded_once_and_manual_choice_wins(settings, store):
    initial = replace(settings,developers=[{'id':383549,'logo':'/brand/studio-logo-transparent.png'}])
    with TestClient(create_app(initial,scheduler_enabled=False)) as client:
        assert client.get('/public/site').json()['logo'] == '/brand/studio-logo-transparent.png'
    store.save_presentation(383549,{'logo_url':'/media/custom.png'})
    with TestClient(create_app(initial,scheduler_enabled=False)) as client:
        assert client.get('/public/site').json()['logo'] == '/media/custom.png'
