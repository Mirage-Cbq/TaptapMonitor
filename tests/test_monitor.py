import asyncio
from dataclasses import replace
import json
import os
import re
import time
from types import SimpleNamespace
from io import BytesIO
from pathlib import Path
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
from taptap_monitor.official import OfficialBridge, OfficialError, OfficialService, daily_metrics, metric_day


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
            if path == "moment-comment/v1/by-comment":
                raise SourceError("nested replies unavailable")
            return {"list": [
                {"id": 1, "created_time": 1, "contents": {"text": "First"}, "comments": 3,
                 "child_posts": [{"id": 11, "created_time": 1, "contents": {"text": "Public preview"}}]},
                {"id": 2, "created_time": 1, "contents": {"text": "Second"}},
            ], "next_page": ""}
    collector = Collector(store, Source(), settings)
    asyncio.run(collector.comments(item(), 'test/list', {"review_id": 1}))
    assert store.stream('comments:100:review:1')['status'] == 'ok'
    asyncio.run(collector.comments(replace(item(), kind='topic'), 'test/list', {"moment_id": 1}))
    assert store.stream('comments:100:topic:1')['status'] == 'restricted'
    assert store.get_item(100,'topic_comment','2')['body'] == 'Second'
    assert store.get_item(100, 'topic_comment', '11')['body'] == 'Public preview'


def test_nested_topic_replies_are_fetched_in_full(store, settings):
    calls = []

    class Source:
        safe_url = staticmethod(TapTapClient.safe_url)
        async def get(self, path, params=None):
            calls.append((path, params))
            if path == "moment-comment/v1/by-comment":
                assert params == {"id": "1", "moment_id": "m1", "from": 0, "limit": 10, "sort": "created_at", "order": "asc"}
                return {"list": [{"id": 11, "created_time": 2, "contents": {"text": "Reply A"}}], "next_page": "/webapiv2/moment-comment/v1/by-comment?from=10"}
            if path.startswith("/webapiv2/moment-comment/v1/by-comment"):
                return {"list": [{"id": 12, "created_time": 3, "contents": {"text": "Reply B"}}], "next_page": ""}
            return {"list": [{"id": 1, "created_time": 1, "contents": {"text": "Top"}, "comments": 2,
                              "child_posts": [{"id": 11, "created_time": 2, "contents": {"text": "Reply A"}}]}], "next_page": ""}
    collector = Collector(store, Source(), settings)
    asyncio.run(collector.comments(replace(item(), kind='topic'), 'test/list', {"moment_id": "m1"}))
    assert store.stream('comments:100:topic:1')['status'] == 'ok'
    assert store.get_item(100, 'topic_comment', '12')['body'] == 'Reply B'
    assert not collector.errors


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
    monkeypatch.setattr(app_module, 'time', SimpleNamespace(time=lambda: 10, monotonic=lambda: 10))

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


def seed_dashboard(store):
    from datetime import datetime, timedelta
    today = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    published = int((today - timedelta(days=1)).timestamp())
    store.add_developer(999, 'Other')
    store.save_game(999, {'id': 101, 'title': '第二个作品'})
    store.save_game(383549, {'id': 102, 'title': '空作品'})
    store.save_item(replace(item(), source_id='1', title='体验', body='Hello 中文 100%', author='Alice', score=5, published_at=published))
    store.save_item(replace(item(), source_id='2', body='hello again', author='Bob', score=1, official=True, published_at=published - 86400))
    store.save_item(replace(item(), source_id='3', kind='topic', title='HELLO Topic', published_at=published))
    store.save_item(replace(item(), source_id='4', kind='review_comment', official=True, published_at=published))
    store.save_item(replace(item(), source_id='5', app_id=101, kind='topic_comment', published_at=published))
    store.save_item(replace(item(), source_id='6', app_id=101, score=3, published_at=1))
    for errors in ([], ['broken', 'restricted']):
        run = store.start_run(383549)
        store.finish_run(run, {'created': 2, 'updated': 1}, errors)
    return published


def test_dashboard_stats(settings, store):
    published = seed_dashboard(store)
    # Shared games must never duplicate feedback in developer aggregates.
    store.save_game(999, {'id': 100, 'title': 'Test Game'})
    with TestClient(create_app(settings, scheduler_enabled=False)) as client:
        headers = {'Authorization': 'Bearer ' + settings.api_token}
        data = client.get('/api/stats?days=7', headers=headers).json()
        assert data['counts'] == {'review': 3, 'topic': 1, 'review_comment': 1, 'topic_comment': 1}
        assert data['totals'] == {'developers': 2, 'games': 3, 'items': 4, 'replies': 2, 'recent_replies_7_days': 2,
                                  'recent_7_days': 3, 'previous_7_days': 0, 'average_score': 3}
        assert data['score_distribution'] == {'1': 1, '2': 0, '3': 1, '4': 0, '5': 1}
        assert len(data['daily']) == 7
        assert sum(sum(day[k] for k in data['counts']) for day in data['daily']) == 5
        assert any(all(day[k] == 0 for k in data['counts']) for day in data['daily'])
        game = next(g for g in data['games'] if g['app_id'] == 100)
        assert game['review_count'] == 2 and game['average_score'] == 3
        assert next(g for g in data['games'] if g['app_id'] == 102)['average_score'] is None
        assert data['runs']['success_rate'] == .5
        assert data['runs']['items'][0]['error_count'] == 2
        filtered = client.get('/api/stats?developer_id=383549&app_id=100', headers=headers).json()
        assert filtered['totals']['items'] == 3 and filtered['totals']['replies'] == 1 and len(filtered['games']) == 1
        assert len(filtered['daily']) == 30
        assert client.get('/api/stats?days=6', headers=headers).status_code == 422
        assert client.get('/api/stats?days=366', headers=headers).status_code == 422
        empty = client.get('/api/stats?developer_id=123', headers=headers).json()
        assert empty['totals']['items'] == 0 and empty['totals']['average_score'] is None
        assert empty['runs']['success_rate'] is None
        assert len(empty['daily']) == 30
        assert published > 0


def test_search_filters_sort_and_total(settings, store):
    published = seed_dashboard(store)
    with TestClient(create_app(settings, scheduler_enabled=False)) as client:
        headers = {'Authorization': 'Bearer ' + settings.api_token}
        def search(**params):
            response = client.get('/api/items/search', params=params, headers=headers)
            assert response.status_code == 200, response.text
            return response.json()
        page = search(developer_id=383549, limit=1, offset=1)
        assert page['total'] == 3 and len(page['items']) == 1
        assert page['items'][0]['game_title'] == 'Test Game'
        assert search(app_id=101)['total'] == 1
        assert search(q='hElLo')['total'] == 3
        assert search(q='alice')['total'] == 1
        assert search(q='体验')['total'] == 1
        assert search(q='100%')['total'] == 1
        assert search(q="' OR 1=1 --")['total'] == 0
        assert search(q='_')['total'] == 0
        assert search(kind='review', score=5)['total'] == 1
        assert search(min_score=2, max_score=4)['total'] == 1
        assert search(official=True)['total'] == 1
        assert search(official=False)['total'] == 3
        assert search(since=published, until=published)['total'] == 2
        assert [x['score'] for x in search(kind='review', sort='score_desc')['items']] == [5, 3, 1]
        assert [x['score'] for x in search(kind='review', sort='score_asc')['items']] == [1, 3, 5]
        assert search(sort='oldest')['items'][0]['source_id'] == '6'
        assert search(sort='newest')['items'][-1]['source_id'] == '6'
        assert search(limit=1, offset=99) == {'items': [], 'total': 4}
        for params in ({'score': 6}, {'sort': 'sql'}, {'limit': 501}, {'offset': -1}, {'min_score': 5, 'max_score': 1}, {'since': 2, 'until': 1}):
            assert client.get('/api/items/search', params=params, headers=headers).status_code == 422
        assert isinstance(client.get('/api/items', headers=headers).json(), list)


def test_runtime_settings_live_validation_reset_restart(settings):
    app = create_app(settings, scheduler_enabled=False)
    with TestClient(app) as client:
        headers = {'Authorization': 'Bearer ' + settings.api_token}
        initial = client.get('/api/settings', headers=headers).json()
        assert initial['overrides'] == {}
        assert initial['values']['request_interval'] == 0
        assert initial['info']['astrbot_configured'] is False
        assert 'api_token' not in initial['values'] and 'astrbot_key' not in initial['info']
        values = {'poll_seconds': 60, 'request_interval': 60, 'request_timeout': 120, 'max_pages': 1000,
                  'collect_comments': False, 'cache_images': False, 'digest_enabled': True,
                  'digest_interval': 60, 'digest_batch_size': 500}
        response = client.patch('/api/settings', headers=headers, json=values)
        assert response.status_code == 200
        assert response.json()['overrides'] == values
        assert app.state.settings is settings and app.state.collector.settings is settings
        assert settings.request_timeout == 120 and settings.digest_enabled is True
        assert app.state.collector.media is None
        for key, bad in [('poll_seconds', 59), ('request_interval', -1), ('request_interval', 61),
                         ('request_timeout', 0), ('request_timeout', 121), ('max_pages', 0), ('max_pages', 1001),
                         ('digest_interval', 59), ('digest_batch_size', 0), ('digest_batch_size', 501), ('cache_images', 'invalid'), ('api_token', 'secret')]:
            assert client.patch('/api/settings', headers=headers, json={key: bad}).status_code == 422
        assert settings.poll_seconds == 60
        reset = client.patch('/api/settings', headers=headers, json={'request_timeout': None, 'cache_images': None}).json()
        assert settings.request_timeout == initial['defaults']['request_timeout']
        assert 'request_timeout' not in reset['overrides']
        assert app.state.collector.media is not None
    fresh = replace(settings, poll_seconds=900, request_timeout=44)
    with TestClient(create_app(fresh, scheduler_enabled=False)) as client:
        data = client.get('/api/settings', headers=headers).json()
        assert data['values']['poll_seconds'] == 60
        assert data['defaults']['poll_seconds'] == 900
        assert data['values']['request_timeout'] == 44
        reset = client.patch('/api/settings', headers=headers, json={key: None for key in values}).json()
        assert reset['overrides'] == {} and fresh.poll_seconds == 900


def test_dashboard_auth_and_developer_rename(settings):
    with TestClient(create_app(settings, scheduler_enabled=False)) as client:
        for path in ('/api/stats', '/api/items/search', '/api/settings'):
            assert client.get(path).status_code == 401
        assert client.patch('/api/settings', json={'poll_seconds': 60}).status_code == 401
        headers = {'Authorization': 'Bearer ' + settings.api_token}
        path = '/api/developers/383549'
        assert client.patch(path, headers=headers, json={'name': '新厂商名'}).json()['name'] == '新厂商名'
        app_store = client.app.state.store
        app_store.save_profile(383549, {'name': 'TapTap 自动名称'})
        assert app_store.developer(383549)['name'] == '新厂商名'
        assert client.patch(path, headers=headers, json={'name': 'x' * 121}).status_code == 422
        assert client.delete(path).status_code == 401


def test_request_timeout_updates_between_requests(settings):
    observed = []
    def handle(request):
        observed.append(request.extensions['timeout']['read'])
        return httpx.Response(200, json={'success': True, 'data': {}})
    async def run():
        client = TapTapClient(settings, httpx.MockTransport(handle))
        await client.get('test/list')
        settings.request_timeout = 8
        await client.get('test/list')
        await client.close()
    asyncio.run(run())
    assert observed == [30, 8]


def test_dashboard_local_date_boundaries_and_recent_runs(settings, store):
    from datetime import datetime, timedelta
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start = int((midnight - timedelta(days=6)).timestamp())
    tomorrow = int((midnight + timedelta(days=1)).timestamp())
    for n, published in enumerate([start - 1, start, int(midnight.timestamp()), tomorrow]):
        store.save_item(replace(item(), source_id=str(n), published_at=published, author='ÄLICE'))
    for n in range(22):
        run = store.start_run()
        if n != 21:
            store.finish_run(run, {'created': n, 'updated': 0}, ['failure'] if n == 20 else [])
    with TestClient(create_app(settings, scheduler_enabled=False)) as client:
        headers = {'Authorization': 'Bearer ' + settings.api_token}
        stats = client.get('/api/stats?days=7', headers=headers).json()
        assert stats['totals']['items'] == 4
        assert stats['totals']['recent_7_days'] == 2
        assert stats['daily'][0]['review'] == 1 and stats['daily'][-1]['review'] == 1
        assert sum(day['review'] for day in stats['daily']) == 2
        assert len(stats['runs']['items']) == 20
        # Lifespan marks the unfinished run interrupted; neither it nor partial runs count as successful.
        assert stats['runs']['success_rate'] == .9
        assert client.get('/api/items/search?q=älice', headers=headers).json()['total'] == 4


def test_settings_load_runtime_bounds(tmp_path):
    for key, value in [('poll_seconds', 59), ('request_interval', 61), ('request_timeout', 121), ('max_pages', 1001)]:
        path = tmp_path / 'config.toml'
        path.write_text(f'[monitor]\n{key}={value}\n', encoding='utf-8')
        with pytest.raises(ValueError):
            Settings.load(str(path))


def test_site_data_game_stats(store):
    store.save_game(383549, {"id": 100, "title": "有评分的作品", "stat": {
        "rating": {"score": "7.7", "max": "10"}, "vote_info": {"1": 4, "2": 3, "3": 2, "4": 7, "5": 11},
        "review_count": 29, "topic_count": "20", "fans_count": 1900, "hits_total": 2305,
    }})
    stats = site_data(store, 383549)["games"][0]["stats"]
    assert stats == {"rating": {"score": 7.7, "max": 10},
                     "vote_info": {"1": 4, "2": 3, "3": 2, "4": 7, "5": 11},
                     "review_count": 29, "topic_count": 20, "fans_count": 1900, "hits_total": 2305}


@pytest.mark.parametrize('score', ['', '0', '0.0', None])
def test_site_data_unrated_game(store, score):
    store.save_game(383549, {"id": 100, "title": "暂无评分", "stat": {
        "rating": {"score": score, "max": score}, "review_count": 0,
    }})
    stats = site_data(store, 383549)["games"][0]["stats"]
    assert stats["rating"] == {"score": None, "max": None}
    assert stats["vote_info"] == {str(star): 0 for star in range(1, 6)}
    assert stats["review_count"] == 0 and stats["fans_count"] is None
    store.save_game(383549, {"id": 100, "title": "无统计"})
    assert site_data(store, 383549)["games"][0]["stats"]["rating"] == {"score": None, "max": None}


def test_public_site_never_exposes_player_reviews(store, settings):
    store.save_item(replace(item(), author='玩家昵称', score=5, body='这是一段足够长的真实玩家评价内容。'))
    assert 'highlights' not in site_data(store, 383549)
    assert 'highlights' not in site_data(store, 383549, include_hidden=True)
    with TestClient(create_app(settings, scheduler_enabled=False)) as client:
        assert '这是一段足够长的真实玩家评价内容' not in client.get('/').text
        assert 'highlights' not in client.get('/public/site?developer_id=383549').json()
        assert site_data(store, 383549)['synced_at'] is not None


def test_public_page_og_image_and_json_ld_escaping(store, settings):
    from html import escape, unescape

    dangerous = '</script><script>不可执行</script> & "工作室"'
    logo = '/media/logo.png?name="工作室"&x=<标识>'
    cover = '/media/cover.png?name="作品"&x=<封面>'
    store.save_presentation(383549, {'display_name': dangerous, 'logo_url': logo})
    store.save_game(383549, {'id': 100, 'title': dangerous, 'cover': {'url': cover},
                            'description': {'raw_text': '第一行\n第二行 ' + dangerous}})
    store.save_game(383549, {'id': 101, 'title': '隐藏游戏'})
    store.save_presentation(383549, {'visible': False}, 101)
    with TestClient(create_app(settings, scheduler_enabled=False)) as client:
        for path, image in [('/', logo), ('/studio/383549', logo), ('/studio/383549/games/100', cover)]:
            response = client.get(path)
            assert response.status_code == 200
            html = response.text
            assert not re.search(r'__[A-Z_]+__', html)
            assert f'<meta property="og:image" content="{escape(image, quote=True)}">' in html
            assert unescape(re.search(r'property="og:image" content="([^"]*)"', html)[1]) == image
            raw = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)[1]
            assert '<' not in raw and '\\u003c' in raw and '\\u0026' in raw
            graph = json.loads(raw)['@graph']
            assert graph[0]['@type'] == 'Organization' and graph[0]['name'] == dangerous
            assert graph[0]['logo'] == logo
            assert graph[1]['@type'] == 'ItemList' and graph[1]['numberOfItems'] == 1
            listed = graph[1]['itemListElement'][0]
            assert listed['position'] == 1 and listed['item']['name'] == dangerous
            assert listed['item']['url'] == '/studio/383549/games/100'
            assert dangerous not in html
            embedded = re.search(r'<script id="site-data" type="application/json">(.*?)</script>', html, re.S)[1]
            assert json.loads(embedded)['games'][0]['description'].startswith('第一行\n第二行')
            for image_tag in re.findall(r'<img\b[^>]*>', html):
                # The hero logo is the largest above-the-fold image, so it loads eagerly.
                loading = 'fetchpriority="high"' if 'hero-logo' in image_tag else 'loading="lazy"'
                assert all(attribute in image_tag for attribute in (loading, 'width=', 'height=', 'referrerpolicy="no-referrer"'))
        store.save_presentation(383549, {'visible': False}, 100)
        empty = client.get('/')
        assert empty.status_code == 200
        assert '作品正在准备中' in empty.text
        assert json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', empty.text, re.S)[1])['@graph'][1]['numberOfItems'] == 0
        assert client.get('/studio/383549/games/100').status_code == 404


def test_system_upgrade_requires_installed_updater(settings):
    with TestClient(create_app(settings, scheduler_enabled=False)) as client:
        headers = {'Authorization': 'Bearer ' + settings.api_token}
        assert client.get('/api/system').status_code == 401
        info = client.get('/api/system', headers=headers).json()
        assert info['installed'] is False and info['auto'] is False
        for method, path, body in (('post', '/api/system/check', None), ('post', '/api/system/upgrade', {}),
                                   ('put', '/api/system/auto', {'enabled': True})):
            response = client.request(method.upper(), path, headers=headers, json=body)
            assert response.status_code == 409 and 'install.sh' in response.json()['detail']
    assert not (Path(settings.database).parent / 'upgrade' / 'request.json').exists()


def test_system_upgrade_writes_requests_for_local_updater(settings):
    state = Path(settings.database).parent / 'upgrade'
    state.mkdir()
    (state / 'status.json').write_text(json.dumps({
        'state': 'idle', 'result': 'checked', 'current': {'commit': 'a' * 40},
        'latest': {'commit': 'b' * 40}, 'behind': [{'commit': 'b' * 40, 'message': '新版本'}]}), encoding='utf-8')
    with TestClient(create_app(settings, scheduler_enabled=False)) as client:
        headers = {'Authorization': 'Bearer ' + settings.api_token}
        info = client.get('/api/system', headers=headers).json()
        assert info['installed'] is True and info['behind'][0]['message'] == '新版本' and info['pending'] is False
        assert client.post('/api/system/check', headers=headers).status_code == 202
        assert json.loads((state / 'request.json').read_text(encoding='utf-8')) == {'action': 'check', 'ref': 'latest'}
        response = client.post('/api/system/upgrade', headers=headers, json={'ref': 'c' * 7})
        assert response.status_code == 202 and response.json()['pending'] is True
        assert json.loads((state / 'request.json').read_text(encoding='utf-8')) == {'action': 'apply', 'ref': 'c' * 7}
        for bad in ('main; rm -rf', '../x', 'v1.0', 'main'):
            assert client.post('/api/system/upgrade', headers=headers, json={'ref': bad}).status_code == 422
        assert client.put('/api/system/auto', headers=headers, json={'enabled': True}).json()['auto'] is True
        assert json.loads((state / 'auto.json').read_text(encoding='utf-8')) == {'enabled': True}
        (state / 'status.json').write_text(json.dumps({'state': 'updating'}), encoding='utf-8')
        response = client.post('/api/system/check', headers=headers)
        assert response.status_code == 409 and '正在进行' in response.json()['detail']


@pytest.mark.skipif(os.name == 'nt', reason='the updater targets Linux servers')
def test_updater_checks_and_rejects_foreign_commits(tmp_path):
    import getpass
    import subprocess
    import sys

    def git(cwd, *args):
        return subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()

    upstream, app = tmp_path / 'upstream', tmp_path / 'app'
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(upstream)], check=True)
    for name in ('first', 'second'):
        (upstream / 'file.txt').write_text(name, encoding='utf-8')
        git(upstream, 'add', '.')
        git(upstream, '-c', 'user.name=t', '-c', 'user.email=t@example.com', 'commit', '-qm', f'{name} release')
    subprocess.run(['git', 'clone', '-q', str(upstream), str(app)], check=True)
    git(app, 'reset', '-q', '--hard', 'HEAD~1')
    git(app, '-c', 'user.name=t', '-c', 'user.email=t@example.com', 'commit', '-q', '--allow-empty', '-m', 'local only')
    foreign = git(app, 'rev-parse', 'HEAD')
    env = {**os.environ, 'TAPTAP_APP_DIR': str(app), 'TAPTAP_USER': getpass.getuser(), 'TAPTAP_LOCK': str(tmp_path / 'lock')}
    updater = [sys.executable, str(Path(__file__).resolve().parent.parent / 'scripts' / 'updater.py')]
    status_file = app / 'data' / 'upgrade' / 'status.json'

    assert subprocess.run([*updater, 'check'], env=env, capture_output=True).returncode == 0
    status = json.loads(status_file.read_text(encoding='utf-8'))
    assert status['result'] == 'checked' and [c['message'] for c in status['behind']] == ['second release']
    assert status['latest']['message'] == 'second release' and (app / 'VERSION.json').exists()

    assert subprocess.run([*updater, 'apply', '--ref', foreign], env=env, capture_output=True).returncode == 1
    status = json.loads(status_file.read_text(encoding='utf-8'))
    assert status['result'] == 'failed' and '官方分支' in status['message'] and git(app, 'rev-parse', 'HEAD') == foreign

    (app / 'data' / 'upgrade' / 'request.json').write_text('{"action": "apply", "ref": "main; id"}', encoding='utf-8')
    assert subprocess.run([*updater, 'request'], env=env, capture_output=True).returncode == 1
    assert not (app / 'data' / 'upgrade' / 'request.json').exists()
    assert json.loads(status_file.read_text(encoding='utf-8'))['message'] == '无效的升级请求'


def fake_official(settings, **options):
    import sys
    bridge = OfficialBridge(Path(settings.database).parent, node=sys.executable,
                            server=str(Path(__file__).with_name('fake_official.py').resolve()), timeout=3)
    bridge.home.mkdir(parents=True, exist_ok=True)
    (bridge.home / 'behavior.json').write_text(json.dumps(options), encoding='utf-8')
    return bridge


def official_calls(bridge):
    return [json.loads(line) for line in (bridge.home / 'calls.jsonl').read_text(encoding='utf-8').splitlines()]


def test_official_bridge_envelopes_errors_and_shutdown(settings):
    async def run():
        bridge = fake_official(settings)
        try:
            assert bridge.installed
            assert (await bridge.call('test_structured'))['answer'] == 42
            assert (await bridge.call('check_environment_raw'))['auth']['has_mac_token'] is False
            for tool, message in [('test_error', 'choose an app'), ('test_rpc_error', 'rpc failure')]:
                with pytest.raises(OfficialError, match=message):
                    await bridge.call(tool)
            old_process = bridge.process
            with pytest.raises(OfficialError, match='响应超时'):
                await bridge.call('test_timeout', timeout=0.01)
            assert old_process.returncode is not None and not bridge.pending
            assert not (await bridge.call('check_environment_raw'))['auth']['has_mac_token']
            process = bridge.process
        finally:
            await bridge.close()
        assert process.returncode is not None and not bridge.pending
    asyncio.run(run())


@pytest.mark.parametrize('internal_poll', [False, True])
def test_official_auth_sync_and_logout(settings, store, internal_poll):
    async def run():
        bridge = fake_official(settings, internal_poll=internal_poll)
        service = OfficialService(store, bridge)
        try:
            waiting = await service.begin_auth()
            assert waiting['auth']['status'] == 'waiting'
            assert waiting['auth']['qr'].startswith('data:image/svg+xml;base64,')
            assert waiting['auth']['expires_at'] > time.time()
            task = service.auth_task
            await service.begin_auth()
            assert service.auth_task is task
            await asyncio.wait_for(task, 3)
            status = service.status()
            assert status['authorized'] and status['auth']['status'] == 'success'
            assert status['account'] is None
            assert [(app['app_id'], app['monitored']) for app in status['apps']] == [(100, True), (200, False)]
            assert all(secret not in json.dumps(store.setting('official')) for secret in ('device_code', 'fake-device-code', 'fake-kid', 'token_type'))
            assert (await service.begin_auth())['auth']['status'] == 'success'
            service.start_sync()
            with pytest.raises(OfficialError, match='正在同步'):
                service.start_sync()
            await service.sync_task
            assert set(store.reports(100)) == {'store_overview', 'review_overview', 'community_overview', 'store_snapshot'}
            assert store.reports(100)['store_overview']['data']['data']['page_view_count'] == 1000
            assert not store.reports(200)
            daily = store.metrics([100], 'official')[0]
            assert daily['page_view_count'] == 50 and daily['rating_trend_value'] == 8.5
            assert daily['topic_trend_count'] == 2 and daily['favorite_trend_num'] == 3
            assert service.status()['synced_at'] and not service.status()['syncing']
            assert sum(call['name'] == 'list_developers_and_apps_raw' for call in official_calls(bridge)) == 3
            service.sync_if_due()
            assert service.sync_task.done()
            service.state['synced_at'] = time.time() - 6 * 3600 - 1
            service.sync_if_due()
            next_task = service.sync_task
            service.sync_if_due()
            assert service.sync_task is next_task
            await next_task
            assert OfficialService(store, bridge).status()['authorized']
            logged_out = await service.logout()
            assert logged_out['auth']['status'] == 'idle' and not logged_out['authorized']
            assert store.setting('official')['apps'] == [] and bridge.process is None
            assert not service.metrics()['games'][0]['official']
        finally:
            await service.close()
    asyncio.run(run())


@pytest.mark.parametrize('auth_error, expires_in, expected', [
    ('', 0.001, 'expired'), ('expired_token', 20, 'expired'), ('access_denied', 20, 'failed')])
def test_official_auth_expiry_and_failure(settings, store, auth_error, expires_in, expected):
    async def run():
        service = OfficialService(store, fake_official(settings, auth_error=auth_error, expires_in=expires_in))
        try:
            await service.begin_auth()
            await asyncio.wait_for(service.auth_task, 3)
            assert service.status()['auth']['status'] == expected
            assert service.status()['auth']['qr'] is None and not service.status()['authorized']
        finally:
            await service.close()
    asyncio.run(run())


def test_official_logout_cancels_pending_authorization(settings, store):
    async def run():
        service = OfficialService(store, fake_official(settings, internal_poll=True))
        try:
            await service.begin_auth()
            await asyncio.sleep(0.015)
            old_process = service.bridge.process
            status = await service.logout()
            assert old_process.returncode is not None and service.auth_task.cancelled()
            assert not status['authorized'] and status['auth']['status'] == 'idle'
            assert not (service.bridge.home / 'authorized.json').exists()
        finally:
            await service.close()
    asyncio.run(run())


def test_official_sync_isolates_games_and_invalid_token(settings, store):
    async def run():
        bridge = fake_official(settings, fail_app=100, extra_apps=[{'app_id': 101, 'app_title': 'Other game'}])
        service = OfficialService(store, bridge)
        store.save_game(383549, {'id': 101, 'title': 'Other game'})
        try:
            await service.begin_auth()
            await service.auth_task
            await service.sync()
            assert 'temporary app failure' in service.status()['error']
            assert not store.reports(100) and len(store.reports(101)) == 4
            assert service.status()['authorized']
            (bridge.home / 'behavior.json').write_text(json.dumps({'invalid_on': 'get_current_app_store_overview_raw'}))
            await service.sync()
            assert not service.status()['authorized'] and '重新扫码' in service.status()['error']
            with pytest.raises(OfficialError, match='扫码授权'):
                service.start_sync()
            assert not store.reports(100)
        finally:
            await service.close()
    asyncio.run(run())


def test_official_endpoints_and_metrics(settings, store, monkeypatch):
    bridge = fake_official(settings, internal_poll=True, report_delay=0.03)
    monkeypatch.setenv('TAPTAP_OFFICIAL_SERVER', bridge.server)
    monkeypatch.setenv('TAPTAP_OFFICIAL_NODE', bridge.node)
    store.save_game(383549, {'id': 100, 'title': 'Official Game', 'icon': {'original_url': 'https://test.invalid/icon'}})
    store.save_game(383549, {'id': 102, 'title': 'Public Game', 'stat': {
        'rating': {'score': '9.0'}, 'review_count': 3, 'topic_count': 2, 'fans_count': 5, 'hits_total': 17,
        'reserve_count': 4, 'download_count': 0, 'vote_info': {'5': 3}}})
    app = create_app(settings, scheduler_enabled=False)
    headers = {'Authorization': 'Bearer ' + settings.api_token}
    with TestClient(app) as client:
        for method, path in [('GET', '/api/official'), ('POST', '/api/official/auth'),
                             ('DELETE', '/api/official/auth'), ('POST', '/api/official/sync'), ('GET', '/api/metrics')]:
            assert client.request(method, path).status_code == 401
        assert client.get('/api/official', headers=headers).json()['installed']
        assert client.post('/api/official/sync', headers=headers).status_code == 409
        response = client.post('/api/official/auth', headers=headers)
        assert response.status_code == 202 and response.json()['auth']['status'] == 'waiting'
        client.portal.call(lambda: asyncio.wait_for(app.state.official.auth_task, 3))
        assert client.get('/api/official', headers=headers).json()['authorized']
        response = client.post('/api/official/sync', headers=headers)
        assert response.status_code == 202 and response.json()['syncing']
        assert client.post('/api/official/sync', headers=headers).status_code == 409
        client.portal.call(lambda: asyncio.wait_for(app.state.official.sync_task, 3))
        result = client.get('/api/metrics?days=7&developer_id=383549', headers=headers).json()
        assert len(result['days']) == 7 and result['days'] == sorted(result['days'])
        from datetime import date, timedelta
        assert result['days'][-1] == date.today().isoformat()
        games = {game['app_id']: game for game in result['games']}
        assert set(games) == {100, 102}
        assert games[100]['official'] and games[100]['official_fetched_at']
        assert games[100]['icon'] == 'https://test.invalid/icon'
        assert games[100]['summary']['page_view_count'] == 1000
        assert games[100]['summary']['feed_count'] is None
        assert games[100]['official_daily'][0]['rating_trend_value'] == 8.5
        public = games[102]
        assert not public['official'] and public['summary'] == {} and public['official_daily'] == []
        assert public['official_fetched_at'] is None and public['icon'] is None
        assert public['public_daily'][0]['rating'] == 9 and public['public_daily'][0]['votes']['5'] == 3
        assert public['public_daily'][0]['download_count'] == 0
        store.save_metrics(100, 'official', {'outside': 1}, (date.today() + timedelta(days=1)).isoformat())
        store.save_metrics(100, 'official', {'outside': 1}, (date.today() - timedelta(days=7)).isoformat())
        filtered = client.get('/api/metrics?days=7&app_id=100', headers=headers).json()['games']
        assert len(filtered) == 1 and len(filtered[0]['official_daily']) == 1
        assert client.get('/api/metrics?developer_id=999&app_id=100', headers=headers).json()['games'] == []
        for days in (6, 181):
            assert client.get(f'/api/metrics?days={days}', headers=headers).status_code == 422
        assert client.delete('/api/official/auth', headers=headers).json()['authorized'] is False
        assert not client.get('/api/metrics?app_id=100', headers=headers).json()['games'][0]['official']
    assert app.state.official.bridge.process is None


def test_official_missing_component(settings, monkeypatch):
    monkeypatch.setenv('TAPTAP_OFFICIAL_SERVER', str(Path(settings.database).parent / 'missing.js'))
    with TestClient(create_app(settings, scheduler_enabled=False)) as client:
        headers = {'Authorization': 'Bearer ' + settings.api_token}
        assert not client.get('/api/official', headers=headers).json()['installed']
        for path in ('/api/official/auth', '/api/official/sync'):
            response = client.post(path, headers=headers)
            assert response.status_code == 409 and response.json()['detail']
        assert client.delete('/api/official/auth', headers=headers).json()['auth']['status'] == 'idle'


def test_official_daily_dates_and_numeric_fields():
    assert metric_day('2026-01-02') == metric_day(20260102) == metric_day(1767312000) == metric_day(1767312000000)
    rows = list(daily_metrics({'trend': [None, {'day': 'broken', 'dt': '20260102', 'count': 0, 'label': 'skip',
                                             'flag': True, 'bad': float('nan'), 'download_count': 3}],
                               'topic_trend': [{'date': '2026-02-30', 'count': 1}], 'favorite_trend': None}))
    assert rows == [('2026-01-02', {'trend_count': 0, 'download_count': 3})]


def test_collection_completion_starts_official_sync_when_due(settings, store, monkeypatch):
    bridge = fake_official(settings)
    (bridge.home / 'authorized.json').write_text('true')
    store.save_setting('official', {'authorized': True, 'synced_at': time.time() - 21601})
    monkeypatch.setenv('TAPTAP_OFFICIAL_SERVER', bridge.server)
    monkeypatch.setenv('TAPTAP_OFFICIAL_NODE', bridge.node)
    completed = []

    async def collect(self, developer_id, run_id, mode="incremental"):
        completed.append(run_id)
        self.store.finish_run(run_id, {'created': 0, 'updated': 0}, [])
        return {'run_id': run_id}

    monkeypatch.setattr(Collector, 'collect', collect)
    app = create_app(settings, scheduler_enabled=False)
    with TestClient(app) as client:
        headers = {'Authorization': 'Bearer ' + settings.api_token}
        assert client.post('/api/scan', headers=headers).status_code == 202

        async def wait_for_sync():
            while app.state.official.sync_task is None:
                await asyncio.sleep(0.01)
            await app.state.official.sync_task

        client.portal.call(lambda: asyncio.wait_for(wait_for_sync(), 3))
        assert completed and len(store.reports(100)) == 4
        task = app.state.official.sync_task
        assert client.post('/api/scan', headers=headers).status_code == 202
        client.portal.call(asyncio.sleep, 0.02)
        assert len(completed) == 2 and app.state.official.sync_task is task


def test_official_updater_install_is_optional_and_pinned(tmp_path, monkeypatch):
    import importlib.util
    import subprocess
    import sys

    if os.name == 'nt':
        for name in ('fcntl', 'pwd'):
            monkeypatch.setitem(sys.modules, name, SimpleNamespace())
    spec = importlib.util.spec_from_file_location('official_updater_test', Path(__file__).parents[1] / 'scripts/updater.py')
    updater = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(updater)
    updater.APP = tmp_path / 'app'
    source = updater.APP / 'deploy' / 'official'
    source.mkdir(parents=True)
    for name in ('package.json', 'package-lock.json'):
        (source / name).write_text('{}')
    messages, calls = [], []
    (tmp_path / 'official').mkdir()
    (tmp_path / 'official' / '.installed-package-lock.json').write_text('old lock')
    updater.log = messages.append
    monkeypatch.setattr(updater.shutil, 'which', lambda _: 'npm')
    monkeypatch.setenv('NPM_REGISTRY', 'https://registry.test')

    def execute(args, **kwargs):
        calls.append(args)
        if args[0] == 'npm':
            raise subprocess.CalledProcessError(1, args)
        return SimpleNamespace(stdout='v18.19.0')

    monkeypatch.setattr(updater.subprocess, 'run', execute)
    updater.install_official()
    assert any('警告' in message for message in messages)
    assert calls[-1] == ['npm', 'ci', '--omit=dev', '--ignore-scripts', '--no-audit', '--no-fund', '--registry=https://registry.test']
    assert not (tmp_path / 'official' / '.installed-package-lock.json').exists()


def review_moment(source_id, comments=0, body='Review'):
    return {'moment': {'author': {'user': {'id': 7, 'name': 'Player'}}, 'created_time': 1,
                       'stat': {'comments': comments}, 'review': {'id': source_id, 'score': 4, 'contents': {'text': body}}}}


def test_run_mode_migrates_existing_database(settings):
    import sqlite3
    with sqlite3.connect(settings.database) as db:
        db.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY,developer_id INTEGER,started_at REAL,finished_at REAL,"
                   "status TEXT,new_items INTEGER DEFAULT 0,updated_items INTEGER DEFAULT 0,errors TEXT DEFAULT '[]')")
        db.execute("INSERT INTO runs(id,status) VALUES(1,'ok')")
    migrated = Store(settings.database)
    assert migrated.query('SELECT mode FROM runs')[0]['mode'] == 'incremental'
    full = Store(settings.database).start_run(383549, 'full')
    assert migrated.query('SELECT mode FROM runs WHERE id=?', (full,))[0]['mode'] == 'full'


def test_incremental_stops_at_unchanged_page_and_full_keeps_paging(store, settings):
    class Source:
        safe_url = staticmethod(TapTapClient.safe_url)
        def __init__(self):
            self.calls, self.new, self.edit = [], False, False
        async def get(self, path, params=None):
            self.calls.append(path)
            if path == 'review/v2/list-by-app':
                assert params['sort'] == 'new'
                return {'list': [review_moment(4 if self.new else 3)], 'next_page': '/webapiv2/reviews?from=10'}
            if path == '/webapiv2/reviews?from=10':
                return {'list': [review_moment(2)], 'next_page': '/webapiv2/reviews?from=20'}
            if path == '/webapiv2/reviews?from=20':
                return {'list': [review_moment(1, body='Edited' if self.edit else 'Review')], 'next_page': ''}
            return {'list': [], 'next_page': ''}
    source = Source()
    collector = Collector(store, source, settings)
    asyncio.run(collector.reviews(100))
    assert len([path for path in source.calls if 'reviews?' in path]) == 2
    source.calls.clear()
    asyncio.run(collector.reviews(100))
    assert source.calls == ['review/v2/list-by-app']
    source.new, source.edit = True, True
    source.calls.clear()
    asyncio.run(collector.reviews(100))
    assert '/webapiv2/reviews?from=10' in source.calls and '/webapiv2/reviews?from=20' not in source.calls
    assert store.get_item(100, 'review', '1')['body'] == 'Review'
    collector.mode = 'full'
    source.calls.clear()
    asyncio.run(collector.reviews(100))
    assert '/webapiv2/reviews?from=20' in source.calls
    assert source.calls.count('review-comment/v1/by-review') == 3
    assert store.get_item(100, 'review', '1')['body'] == 'Edited'


def test_comments_only_refresh_for_changed_count_new_parent_or_incomplete_stream(store, settings):
    class Source:
        safe_url = staticmethod(TapTapClient.safe_url)
        count, comment_calls, fail = 1, 0, False
        async def get(self, path, params=None):
            if path == 'review/v2/list-by-app':
                return {'list': [review_moment(1, self.count)], 'next_page': ''}
            self.comment_calls += 1
            if self.fail:
                raise SourceError('offline failure')
            return {'list': [{'id': n, 'created_time': 2, 'comments': 0} for n in range(self.count)], 'next_page': ''}
    source = Source()
    collector = Collector(store, source, settings)
    asyncio.run(collector.reviews(100))
    asyncio.run(collector.reviews(100))
    assert source.comment_calls == 1
    source.count, source.fail = 2, True
    asyncio.run(collector.reviews(100))
    assert source.comment_calls == 2 and store.stream('comments:100:review:1')['status'] == 'error'
    source.fail = False
    asyncio.run(collector.reviews(100))
    assert source.comment_calls == 3 and store.get_item(100, 'review', '1')['source_reply_count'] == 2
    assert not [event for event in store.events(include_baseline=True) if event['kind'] == 'review' and event['action'] == 'updated']


def test_nested_reply_count_skip_full_and_continuation(store, settings):
    class Source:
        safe_url = staticmethod(TapTapClient.safe_url)
        nested_calls = 0
        async def get(self, path, params=None):
            if path == 'moment-comment/v1/by-comment':
                self.nested_calls += 1
                return {'list': [{'id': 11, 'created_time': 2}], 'next_page': '/webapiv2/nested?from=10'}
            if path == '/webapiv2/nested?from=10':
                return {'list': [{'id': 12, 'created_time': 3, 'reply_to': {'id': 1}}], 'next_page': ''}
            return {'list': [{'id': 1, 'created_time': 1, 'comments': 2, 'child_posts': []}], 'next_page': ''}
    source = Source()
    parent = replace(item(), kind='topic', source_reply_count=3)
    collector = Collector(store, source, replace(settings, max_pages=1))
    asyncio.run(collector.comments(parent, 'comments/list', {'moment_id': 'm1'}))
    key = 'replies:100:topic:1:1'
    assert store.stream(key)['status'] == 'pending' and store.get_item(100, 'topic_comment', '11')
    assert collector.needs_comments(parent, parent.payload(), 'comments:100:topic:1')
    asyncio.run(collector.comments(parent, 'comments/list', {'moment_id': 'm1'}))
    assert store.stream(key)['status'] == 'ok' and store.get_item(100, 'topic_comment', '12')['reply_to'] == {'id': 1}
    calls = source.nested_calls
    asyncio.run(collector.comments(parent, 'comments/list', {'moment_id': 'm1'}))
    assert source.nested_calls == calls
    collector.mode = 'full'
    asyncio.run(collector.comments(parent, 'comments/list', {'moment_id': 'm1'}))
    assert source.nested_calls == calls + 1


def test_source_reply_counts_and_fingerprint(store):
    from taptap_monitor.content import comment_item, review_item, source_reply_count
    parsed = review_item(100, review_moment(1, 182)['moment'])
    assert parsed.source_reply_count == 182
    assert source_reply_count({'comments': {'count': '12'}}) == 12
    assert source_reply_count({'comments': -1}) is None
    comment = comment_item(100, 'topic_comment', '1', {'id': 2, 'created_time': 1, 'comments': 4, 'reply_to': 7}, 'url')
    assert comment.source_reply_count == 4 and comment.reply_to == 7
    store.save_item(parsed)
    assert store.save_item(replace(parsed, source_reply_count=183)) is None
    assert store.get_item(100, 'review', '1')['source_reply_count'] == 183


def test_primary_order_and_nested_replies_api(settings, store):
    published = seed_dashboard(store)
    store.save_setting('primary_site', 383549)
    store.save_item(replace(item(), source_id='reply1', kind='review_comment', parent_id='1', published_at=published + 2, reply_to={'id': 'author'}))
    store.save_item(replace(item(), source_id='reply2', kind='review_comment', parent_id='1', published_at=published + 1))
    store.save_item(replace(item(), source_id='wrong-app', app_id=101, kind='review_comment', parent_id='1'))
    store.save_item(replace(item(), source_id='wrong-kind', kind='topic_comment', parent_id='1'))
    with TestClient(create_app(settings, scheduler_enabled=False)) as client:
        headers = {'Authorization': 'Bearer ' + settings.api_token}
        developers = client.get('/api/developers', headers=headers).json()
        assert developers[0]['id'] == 383549 and developers[0]['is_primary'] and not developers[1]['is_primary']
        assert client.get('/api/status', headers=headers).json()['primary_developer_id'] == 383549
        assert [g['id'] for g in client.get('/api/games', headers=headers).json()] == [102, 100, 101]
        stats = client.get('/api/stats', headers=headers).json()
        assert [g['app_id'] for g in stats['games']][-1] == 101
        game = next(g for g in stats['games'] if g['app_id'] == 100)
        assert game['main_count'] == 3 and game['reply_count'] == 4
        main = client.get('/api/items/search?kind=review&app_id=100', headers=headers).json()['items']
        assert next(row for row in main if row['source_id'] == '1')['reply_count'] == 2
        assert all('source_reply_count' in row for row in main)
        path = '/api/items/100/review/1/replies'
        assert client.get(path).status_code == 401
        result = client.get(path + '?limit=1&offset=1', headers=headers).json()
        assert result['total'] == 2 and result['items'][0]['source_id'] == 'reply1'
        assert result['items'][0]['reply_to'] == {'id': 'author'}
        assert client.get('/api/items/100/review/missing/replies', headers=headers).status_code == 404
        assert client.get(path + '?limit=201', headers=headers).status_code == 422
        assert client.get('/api/items/search?kind=review_comment', headers=headers).json()['total'] == 4
        client.put('/api/site-default/999', headers=headers)
        assert client.get('/api/developers', headers=headers).json()[0]['id'] == 999
        assert client.get('/api/games', headers=headers).json()[0]['id'] == 101


def test_manual_modes_and_added_developer_queue_full(settings, store, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    real_sleep = asyncio.sleep

    async def fast_sleep(delay):
        await real_sleep(0.01 if delay == 15 else delay)

    async def collect(self, developer_id=None, run_id=None, mode='incremental'):
        calls.append((developer_id, mode))
        if len(calls) == 1:
            entered.set()
            await release.wait()
        self.store.finish_run(run_id, {'created': 0, 'updated': 0}, [])
        return {'run_id': run_id}

    monkeypatch.setattr(Collector, 'collect', collect)
    monkeypatch.setattr(asyncio, 'sleep', fast_sleep)
    with TestClient(create_app(settings)) as client:
        headers = {'Authorization': 'Bearer ' + settings.api_token}
        client.portal.call(lambda: asyncio.wait_for(entered.wait(), 2))
        assert calls == [(None, 'incremental')]
        assert client.post('/api/developers', headers=headers, json={'id': 998}).status_code == 201
        client.portal.call(release.set)

        async def wait_for_added():
            while len(calls) < 2:
                await real_sleep(0.01)
        client.portal.call(lambda: asyncio.wait_for(wait_for_added(), 2))
        assert calls[1] == (998, 'full')
        assert client.get('/api/runs', headers=headers).json()[0]['mode'] == 'full'
        assert client.post('/api/scan?mode=unknown', headers=headers).status_code == 422
        response = client.post('/api/scan?mode=full&developer_id=383549', headers=headers)
        assert response.status_code == 202 and response.json()['mode'] == 'full'


@pytest.mark.parametrize('full', [False, True])
def test_cli_collect_mode(settings, monkeypatch, full):
    import sys
    from taptap_monitor import __main__ as cli
    modes = []
    monkeypatch.setattr(cli.Settings, 'load', lambda _: settings)
    monkeypatch.setattr(sys, 'argv', ['taptap_monitor', 'collect'] + (['--full'] if full else []))

    async def collect(self, developer_id=None, run_id=None, mode='incremental'):
        modes.append(mode)
        return {'errors': []}
    monkeypatch.setattr(Collector, 'collect', collect)
    with pytest.raises(SystemExit) as exit_code:
        cli.main()
    assert exit_code.value.code == 0 and modes == ['full' if full else 'incremental']


def test_insights_rating_keywords_response_and_growth(settings, store):
    from datetime import date, datetime, timedelta
    from taptap_monitor.insights import analyze
    today = date.today()
    noon = int(datetime.combine(today, datetime.min.time()).timestamp()) + 12 * 3600
    for n in range(6):
        store.save_item(replace(item(), source_id=f'new{n}', score=1, body='操作卡顿 操作卡顿 crash crash 游戏没有', published_at=noon))
    for n in range(5):
        store.save_item(replace(item(), source_id=f'old{n}', score=5, body='画面精美 fluid', published_at=noon - 8 * 86400))
    store.save_item(replace(item(), source_id='invalid-score', score=6, published_at=noon))
    store.save_item(replace(item(), source_id='topic', kind='topic', published_at=noon, source_reply_count=40))
    store.add_developer(999, 'Other')
    store.save_game(999, {'id': 101, 'title': 'Other game'})
    store.save_item(replace(item(), app_id=101, source_id='prior', score=5, published_at=noon - 60 * 86400))
    for n, hours in enumerate((2, 4, 1)):
        store.save_item(replace(item(), source_id=f'answer{n}', kind='review_comment', parent_id='new0', official=n < 2, published_at=noon + hours * 3600))
    for n in range(4):
        store.save_item(replace(item(), source_id=f'topicreply{n}', kind='topic_comment', parent_id='topic', published_at=noon))
    store.save_item(replace(item(), app_id=101, source_id='wrong-app', kind='review_comment', parent_id='new1', official=True, published_at=noon))
    first = (today - timedelta(days=9)).isoformat()
    for app_id in (100, 101):
        store.save_metrics(app_id, 'public', {'fans_count': 10, 'hits_total': 20, 'review_count': 2}, first)
        store.save_metrics(app_id, 'public', {'fans_count': 15, 'hits_total': 50, 'review_count': 4})
    store.save_metrics(100, 'official', {'page_view_count': 50, 'download_count': 10}, first)
    store.save_metrics(100, 'official', {'page_view_count': 80, 'download_count': 14})
    result = analyze(store, 30, app_id=100)
    rating = result['rating']
    assert rating['count'] == 11 and rating['mean'] == pytest.approx(31 / 11)
    assert 'bayesian' not in rating and not any(m['key'] == 'bayesian' for m in result['methods'])
    assert rating['positive_rate'] == pytest.approx(5 / 11) and rating['negative_rate'] == pytest.approx(6 / 11)
    assert 0 < rating['wilson_lower'] < rating['positive_rate']
    assert rating['recent_mean'] == 1 and rating['previous_mean'] == 5 and '下降' in rating['alert']
    assert sum(week['reviews'] for week in result['weekly']) == 12 and sum(week['topics'] for week in result['weekly']) == 1
    negative = {row['term']: row for row in result['keywords']['negative']}
    assert negative['操作卡顿']['count'] == negative['crash']['count'] == 6
    assert negative['crash']['lift'] == 6 and '卡顿' not in negative and '游戏' not in negative
    assert result['response'] == {'total': 12, 'replied': 1, 'official_reply_rate': 1 / 12, 'median_hours': 2}
    assert result['hot_topics'][0]['source_id'] == 'topic' and result['hot_topics'][0]['reply_count'] == 4
    assert result['hot_topics'][0]['source_reply_count'] == 40
    assert result['growth'][0] == {'app_id': 100, 'title': 'Test Game', 'source': 'official', 'fans_delta': None,
                                   'hits_delta': 30, 'reviews_delta': None, 'downloads_delta': 4, 'days': 10}
    assert analyze(store, 30, developer_id=999)['growth'][0]['fans_delta'] == 5
    assert analyze(store, 7, app_id=100)['rating']['previous_mean'] == 5
    assert len(result['methods']) == 6
    with TestClient(create_app(settings, scheduler_enabled=False)) as client:
        headers = {'Authorization': 'Bearer ' + settings.api_token}
        assert client.get('/api/insights').status_code == 401
        assert client.get('/api/insights?app_id=100', headers=headers).json()['rating']['count'] == 11
        for days in (6, 366):
            assert client.get(f'/api/insights?days={days}', headers=headers).status_code == 422
        empty = client.get('/api/insights?developer_id=888&app_id=100', headers=headers).json()
        assert empty['rating']['count'] == 0 and empty['rating']['mean'] is None and empty['hot_topics'] == []


def test_insights_rolling_baseline_uses_days_before_window(store):
    from datetime import date, datetime, timedelta
    from taptap_monitor.insights import analyze, wilson
    today = date.today()
    for before in range(1, 15):
        stamp = int(datetime.combine(today - timedelta(days=before), datetime.min.time()).timestamp()) + 3600
        for n in range(10):
            store.save_item(replace(item(), kind='topic', source_id=f'{before}-{n}', published_at=stamp))
    stamp = int(datetime.combine(today, datetime.min.time()).timestamp()) + 3600
    for n in range(16):
        store.save_item(replace(item(), kind='review' if n < 6 else 'topic', score=1 if n < 6 else None,
                                source_id=f'today-{n}', published_at=stamp))
    result = analyze(store, 7)
    anomalies = [row for row in result['anomalies'] if row['day'] == today.isoformat()]
    assert {row['type'] for row in anomalies} == {'volume', 'negative'}
    volume = next(row for row in anomalies if row['type'] == 'volume')
    assert volume['baseline'] == 10 and volume['count'] == 16 and volume['score'] == pytest.approx(0.6745 * 6)
    assert wilson(0, 5) == 0 and wilson(5, 5) == pytest.approx(0.5655, abs=0.0001)


def test_insights_empty_window_and_metric_fallback(store):
    from datetime import date, timedelta
    from taptap_monitor.insights import analyze
    empty = analyze(store, 7)
    assert empty['rating']['mean'] is None and empty['rating']['count'] == 0
    assert empty['response']['official_reply_rate'] is None and empty['response']['median_hours'] is None
    assert empty['keywords'] == {'negative': [], 'positive': []} and empty['anomalies'] == []
    store.save_metrics(100, 'official', {'download_count': 99})
    store.save_metrics(100, 'public', {'fans_count': 2, 'hits_total': 10}, (date.today() - timedelta(days=2)).isoformat())
    store.save_metrics(100, 'public', {'fans_count': 2, 'hits_total': 15})
    row = analyze(store, 7)['growth'][0]
    assert row['source'] == 'public' and row['fans_delta'] == 0 and row['hits_delta'] == 5 and row['days'] == 3


def test_fresh_install_without_studios_shows_welcome_page(tmp_path):
    settings = Settings(database=str(tmp_path / "fresh.sqlite3"), api_token="test-only-token-with-32-characters")
    with TestClient(create_app(settings, scheduler_enabled=False)) as client:
        response = client.get('/')
        assert response.status_code == 200 and '还没有添加要监测的 TapTap 厂商' in response.text and 'href="/admin"' in response.text
        assert client.get('/public/site').status_code == 404


def test_official_summary_reads_live_store_snapshot_and_review_trend():
    from taptap_monitor.official import daily_metrics, official_summary, report_data
    reports = {
        "store_overview": {"data": {"app_id": 1, "data": {"page_view_count": 2316, "download_count": 0, "reserve_count": 9,
                           "trend": [{"date": "2026-11-08", "download_request_count": 2}]}}},
        "review_overview": {"data": {"data": {"rating_summary": {"stat": {"review_count": 29}}, "positive_review_count": 13,
                            "neutral_review_count": 2, "negative_review_count": 6,
                            "rating_trend": [{"date": "2026-11-08", "timestamp": 1794067200, "positive_count": 3, "negative_count": 1}]}}},
        "community_overview": {"data": {"data": {"favorite_count": 1941, "topic_trend": [{"date": "2026-11-08", "value": 2}],
                               "favorite_trend": [{"date": "2026-11-08", "value": 5}]}}},
        "store_snapshot": {"data": {"data": {"app": {"stat": {"rating": {"score": "7.7", "latest_version_score": "0"},
                           "vote_info": {"1": 4, "5": 11}, "fans_count": 1901, "hits_total": 2316, "reserve_count": 9, "wish_count": 3}}}}},
    }
    summary = official_summary(reports)
    assert summary["rating_score"] == 7.7 and summary["latest_version_score"] is None
    assert summary["review_count"] == 29 and summary["fans_count"] == 1901 and summary["vote_info"]["5"] == 11
    assert summary["positive_review_count"] == 13 and summary["reserve_count"] == 9 and summary["wish_count"] == 3
    days = dict(daily_metrics(report_data(reports["review_overview"]["data"])))
    assert days["2026-11-08"]["positive_count"] == 3 and "timestamp" not in days["2026-11-08"]
    community = dict(daily_metrics(report_data(reports["community_overview"]["data"])))
    assert community["2026-11-08"] == {"topic_trend_value": 2} or community["2026-11-08"].get("favorite_trend_value") == 5


def test_console_daily_figures_parse_rates_and_skip_immature_retention():
    from datetime import date
    from taptap_monitor.console import daily_figures, number, clean_cookie
    assert number("9.53%") == pytest.approx(0.0953) and number("82.05") == 82.05 and number("-") is None and number(True) is None
    assert clean_cookie("Cookie: a=1; b=2\n") == "a=1; b=2"
    with pytest.raises(Exception):
        clean_cookie("not a cookie")
    days = daily_figures(
        funnel=[{"date": "2026-11-08", "impression_cnt": 1563, "click_cnt": 149, "click_rate": "9.53%", "convert_detail_cnt": 52, "convert_detail_rate": "34.90%"}],
        retention=[{"date": "2026-11-01", "ret_new_active_device_rate_a1d": "17.19%", "ret_new_active_device_rate_a7d": "0.00%"},
                   {"date": "2026-11-08", "ret_new_active_device_rate_a1d": "0.00%"}],
        revenue=[{"date": "2026-11-07", "revenue": "54.78"}], today=date(2026, 11, 8))
    assert days["2026-11-08"] == {"impressions": 1563, "detail_visits": 149, "click_rate": pytest.approx(0.0953), "launches": 52, "launch_rate": pytest.approx(0.349)}
    assert days["2026-11-01"] == {"retention_1d": pytest.approx(0.1719)}  # 7-day cell not mature yet
    assert days["2026-11-07"] == {"ad_revenue": 54.78}


def console_backend(state):
    def handle(request):
        state.setdefault("cookies", []).append(request.headers.get("cookie"))
        if state.get("expired"):
            return httpx.Response(401, json={"success": False, "data": {"code": -1, "error": "access_denied", "msg": "未认证"}})
        path, query = request.url.path, request.url.params
        rows = lambda *items: {"success": True, "data": {"list": list(items)}}
        day = query.get("start_date") or query.get("start_time")
        payloads = {
            "/api/user/v1/me": {"success": True, "data": {"user": {"name": "Studio Owner"}}},
            "/api/developer/v1/list": {"success": True, "data": {"list": [{"id": 383549, "name": "Test Studio"}]}},
            "/api/app/v2/list": {"success": True, "data": {"total": 2, "list": [{"id": 100, "title": "Test Game"}, {"id": 200, "title": "Unmonitored"}]}},
            "/api/dashboard/v2/stats-by-day/position/cn": rows({"date": day, "impression_cnt": 1000, "click_cnt": 100, "click_rate": "10.00%", "convert_detail_cnt": 40, "convert_detail_rate": "40.00%"}),
            "/api/dashboard/v3/stats-by-day/pv/cn": rows({"date": day, "pv_from_total": 120}),
            "/api/mini-app/v1/stats-by-day/device": rows({"date": day, "active_device_num": 50, "new_active_device_num": 30}),
            "/api/mini-app/v1/stats-by-day/ret": rows(),
            "/api/mini-app/v1/ad/info": {"success": True, "data": {"is_enabled": True, "settlement_entity": {"bank_card_number": "secret"}}},
            "/api/mini-app/v1/ad/payout-report-data": rows({"date": day, "revenue": "12.34"}),
        }
        state.setdefault("urls", []).append(str(request.url))
        return httpx.Response(200, json=payloads[path]) if path in payloads else httpx.Response(404, json={"success": False})
    return httpx.MockTransport(handle)


def test_console_session_syncs_funnel_and_revenue_without_exposing_cookie(settings, store):
    backend = {}
    headers = {"Authorization": "Bearer " + settings.api_token}
    with TestClient(create_app(settings, scheduler_enabled=False, console_transport=console_backend(backend))) as client:
        assert client.post("/api/console/sync", headers=headers).status_code == 409
        assert client.put("/api/console", headers=headers, json={"cookie": "plain"}).status_code == 409
        status = client.put("/api/console", headers=headers, json={"cookie": "Cookie: session=abc123"}).json()
        assert status["configured"] and status["valid"] and "cookie" not in status and "abc123" not in json.dumps(status)
        assert {app["app_id"]: app["monitored"] for app in status["apps"]} == {100: True, 200: False}
        for _ in range(100):
            status = client.get("/api/console", headers=headers).json()
            if not status["syncing"] and status["synced_at"]:
                break
            time.sleep(0.02)
        assert status["error"] is None and set(backend["cookies"]) == {"session=abc123"}
        assert any("app_id=100" in url for url in backend["urls"]) and not any("app_id=200" in url for url in backend["urls"])
        game = next(g for g in client.get("/api/metrics?days=30", headers=headers).json()["games"] if g["app_id"] == 100)
        row = game["console_daily"][0]
        assert row["impressions"] == 1000 and row["click_rate"] == pytest.approx(0.1) and row["launches"] == 40
        assert row["ad_revenue"] == 12.34 and row["active_devices"] == 50 and row["store_page_views"] == 120
        assert "secret" not in json.dumps(store.setting("console"))
        backend["expired"] = True
        client.post("/api/console/sync", headers=headers)
        for _ in range(100):
            status = client.get("/api/console", headers=headers).json()
            if not status["syncing"] and not status["valid"]:
                break
            time.sleep(0.02)
        assert not status["valid"] and "失效" in status["error"]
        cleared = client.delete("/api/console", headers=headers).json()
        assert not cleared["configured"] and store.setting("console")["cookie"] is None


def test_delete_developer_removes_owned_games_but_keeps_shared(settings, store):
    store.add_developer(999, "Other Studio")
    store.save_game(999, {"id": 100, "title": "Test Game"})  # shared with 383549
    store.save_game(383549, {"id": 300, "title": "Owned Game"})
    store.save_item(Item(300, "review", "9", "", "Owned feedback", "https://www.taptap.cn/review/9", published_at=int(time.time())))
    store.save_item(item())
    store.save_metrics(300, "console", {"impressions": 5})
    store.save_stream("reviews:300", "", "ok")
    store.save_stream("comments:300:review:9", "", "ok")
    store.save_stream("reviews:100", "", "ok")
    headers = {"Authorization": "Bearer " + settings.api_token}
    with TestClient(create_app(settings, scheduler_enabled=False)) as client:
        assert client.delete("/api/developers/12345", headers=headers).status_code == 404
        result = client.delete("/api/developers/383549", headers=headers).json()
        assert result == {"games": 1, "items": 1}
        assert [d["id"] for d in client.get("/api/developers", headers=headers).json()] == [999]
    assert {g["id"] for g in store.games()} == {100}
    assert store.query("SELECT COUNT(*) n FROM items WHERE app_id=300")[0]["n"] == 0
    assert store.metrics([300], "console") == []
    assert store.stream("reviews:300") is None and store.stream("comments:300:review:9") is None and store.stream("reviews:100")
    assert store.query("SELECT COUNT(*) n FROM items WHERE app_id=100")[0]["n"] == 1
    # config.toml still lists 383549, but a restart must not bring it back until it is added again.
    with TestClient(create_app(settings, scheduler_enabled=False)) as client:
        assert [d["id"] for d in client.get("/api/developers", headers=headers).json()] == [999]
        assert client.post("/api/developers", headers=headers, json={"id": 383549}).status_code == 201
    assert store.setting("removed_developers") == []


def login_backend(state):
    """accounts.taptap.cn device-code login + developer.taptap.cn OAuth handshake + console data."""
    console = console_backend(state)

    def handle(request):
        host, path = request.url.host, request.url.path
        cookies = request.headers.get("cookie", "")
        ok = lambda data, **kw: httpx.Response(200, json={"success": True, "data": data}, **kw)
        fail = lambda status, error: httpx.Response(status, json={"success": False, "data": {"error": error, "msg": error}})
        if host == "accounts.taptap.cn":
            assert request.url.params.get("X-UA", "").startswith("V=1&PN=Accounts")
            if path == "/api/device-code/get":
                assert b"session_id=" in request.content
                return ok({"device_code": "dc", "qrcode_url": "https://accounts.taptap.cn/device?qrcode=1&user_code=AB", "expires_in": 30, "interval": 0.01})
            if path == "/api/device-code/login":
                state["polls"] = state.get("polls", 0) + 1
                if state["polls"] == 1:
                    return fail(400, "authorization_pending")
                if state["polls"] == 2:
                    return fail(400, "authorization_waiting")
                return ok({}, headers={"set-cookie": "ACCOUNT_SID=acc; Domain=.accounts.taptap.cn; Path=/"})
            if path == "/api/oauth2/auth/v2":
                if "ACCOUNT_SID=acc" not in cookies:
                    return fail(401, "access_denied")
                assert "client_id=cid" in request.url.params["parameters"]
                return ok({"redirect_uri": "https://developer.taptap.cn/auth/callback/taptap?code=CODE&state=ST"})
        if host == "developer.taptap.cn" and path == "/api/auth/login/url":
            return ok({"url": "https://accounts.taptap.cn/authorize?client_id=cid&response_type=code&state=ST"},
                      headers={"set-cookie": "DC_XSRF_TOKEN=xsrf; Path=/"})
        if host == "developer.taptap.cn" and path == "/api/auth/login":
            assert request.headers["x-dc-xsrf-token"] == "xsrf" and json.loads(request.content) == {
                "host": "developer.taptap.cn", "state": "ST", "code": "CODE", "path_prefix": "/v3"}
            state["sessions"] = state.get("sessions", 0) + 1
            return ok({"refer": "/v3/"}, headers={"set-cookie": f"DC_SESSION=s{state['sessions']}; Path=/; HttpOnly"})
        if host == "developer.taptap.cn" and state.get("stale") and f"DC_SESSION=s{state.get('stale')}" in cookies:
            return fail(401, "access_denied")
        return console.handler(request)
    return httpx.MockTransport(handle)


def wait_for(client, headers, done, path="/api/console"):
    for _ in range(200):
        status = client.get(path, headers=headers).json()
        if done(status):
            return status
        time.sleep(0.02)
    raise AssertionError(status)


def test_console_qr_login_connects_and_renews_expired_session(settings, store):
    backend = {}
    headers = {"Authorization": "Bearer " + settings.api_token}
    with TestClient(create_app(settings, scheduler_enabled=False, console_transport=login_backend(backend))) as client:
        started = client.post("/api/console/login", headers=headers).json()
        assert started["auth"]["status"] == "waiting" and started["auth"]["qr"].startswith("data:image/svg+xml;base64,")
        status = wait_for(client, headers, lambda s: s["auth"]["status"] in ("success", "failed", "expired") and not s["syncing"] and s["synced_at"])
        assert status["auth"]["status"] == "success", status["auth"]
        assert status["valid"] and status["method"] == "qr" and status["error"] is None
        dumped = json.dumps(status)
        assert "DC_SESSION" not in dumped and "ACCOUNT_SID" not in dumped and "jar" not in status
        saved = store.setting("console")
        assert "DC_SESSION=s1" in saved["cookie"] and "ACCOUNT_SID" not in saved["cookie"]
        assert any(c["name"] == "ACCOUNT_SID" for c in saved["jar"])
        assert store.metrics([100], "console")[0]["impressions"] == 1000
        # The console session expires; the stored accounts session silently mints a new one.
        backend["stale"] = 1
        client.post("/api/console/sync", headers=headers)
        status = wait_for(client, headers, lambda s: not s["syncing"] and backend.get("sessions") == 2)
        assert status["valid"] and status["error"] is None and "DC_SESSION=s2" in store.setting("console")["cookie"]


def test_restart_does_not_rescan_before_poll_interval(settings, store):
    store.finish_run(store.start_run(), {"created": 0, "updated": 0}, [])
    calls = []

    class Source:
        safe_url = staticmethod(TapTapClient.safe_url)
        async def get(self, path, params=None):
            calls.append(path)
            return {"list": [], "next_page": ""}
        async def close(self):
            pass

    with TestClient(create_app(replace(settings, cache_images=False), client=Source())) as client:
        time.sleep(0.3)
        assert calls == [] and not client.get("/api/status", headers={"Authorization": "Bearer " + settings.api_token}).json()["collecting"]
