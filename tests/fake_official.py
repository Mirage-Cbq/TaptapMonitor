"""Offline MCP stdio peer; raw envelopes mirror the read-only upstream server.js."""
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
home = Path(os.environ["HOME"])
authorized_file = home / "authorized.json"
authorized = authorized_file.exists()
pending, completes, selected = False, 0, None
initialized = False


def raw(data):
    return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]}


def tool(name, args):
    global pending, completes, selected, authorized
    assert initialized
    options_file = home / "behavior.json"
    options = json.loads(options_file.read_text()) if options_file.exists() else {}
    with (home / "calls.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps({"name": name, "args": args, "selected": selected}) + "\n")
    if options.get("invalid_on") == name:
        raise ValueError("HTTP 401 Unauthorized: invalid token")
    if name == "test_structured":
        return {"structuredContent": {"ok": True, "answer": 42}, "content": []}
    if name == "test_timeout":
        time.sleep(0.2)
        return raw({"ok": True})
    if name == "test_error":
        return raw({"ok": False, "error": "APP_NOT_SELECTED", "message": "choose an app"})
    if name == "test_rpc_error":
        raise ValueError("rpc failure")
    if name == "start_oauth_authorization_raw":
        pending, completes = True, 0
        return raw({"authorized": authorized, "already_authorized": authorized, "environment": "production",
                    "device_code": "fake-device-code", "qrcode_url": "https://test.invalid/qr",
                    "auth_url": "https://test.invalid/auth", "expires_in": options.get("expires_in", 20), "interval": 0.01})
    if name == "complete_oauth_authorization_raw":
        assert pending
        if options.get("auth_error"):
            raise ValueError(options["auth_error"])
        completes += 1
        if options.get("internal_poll"):
            time.sleep(0.03)
        elif completes < 2:
            return raw({"authorized": False, "error": "authorization_pending"})
        authorized, pending = True, False
        authorized_file.write_text("true")
        return raw({"authorized": True, "environment": "production", "token_type": "mac", "mac_algorithm": "hmac-sha-1", "kid": "fake-kid"})
    if name == "clear_auth_data_raw":
        assert args == {"clear_token": True, "clear_cache": True}
        authorized_file.unlink(missing_ok=True)
        authorized = False
        return raw({"ok": True, "cleared": ["token", "cache"], **args})
    if name == "check_environment_raw":
        return raw({"auth": {"has_mac_token": authorized}})
    if not authorized:
        raise ValueError("Unauthorized")
    if name == "list_developers_and_apps_raw":
        apps = [{"app_id": 100, "app_title": "Official Game", "is_level": True},
                {"app_id": 200, "app_title": "Unmonitored Game", "is_level": False}] + options.get("extra_apps", [])
        return raw({"list": [{"developer_id": 383549, "developer_name": "Test Studio", "apps": apps,
                              "levels": apps[:1], "crafts": apps[:1]}]})
    if name == "select_app_raw":
        selected = args["app_id"]
        if selected == options.get("fail_app"):
            raise ValueError("temporary app failure")
        assert selected != 200, "unmonitored game must never be selected"
        return raw({"developer_id": args["developer_id"], "app_id": selected, "app_title": "Official Game"})
    if name == "get_current_app_info_raw":
        return raw({"selected": bool(selected), "cache": {"app_id": selected}})
    day = date.today().isoformat()
    stamp = int(datetime.combine(date.today(), datetime.min.time(), timezone.utc).timestamp())
    reports = {
        "store_overview": {"page_view_count": 1000, "download_count": 80, "reserve_count": 12,
                           "trend": [{"date": day, "page_view_count": 50, "download_count": 4}]},
        "review_overview": {"rating_score": "8.5", "positive_review_count": 8, "neutral_review_count": 1,
                            "rating_trend": [{"stat_date": day.replace("-", ""), "value": 8.5}]},
        "community_overview": {"topic_count": 7, "favorite_count": 11,
                               "topic_trend": [{"timestamp": stamp * 1000, "count": 2}],
                               "favorite_trend": [{"time": stamp, "num": 3}]},
        "store_snapshot": {"version_status": "published", "rating_summary": {"score": 8.5}}
    }
    for kind, data in reports.items():
        if name == f"get_current_app_{kind}_raw":
            assert selected
            time.sleep(options.get("report_delay", 0))
            if kind != "store_snapshot":
                assert args["start_date"] <= args["end_date"] == day
                data.update(args)
            return raw({"app_id": selected, "app_title": "Official Game", "data": data})
    raise ValueError("unknown tool")


for line in sys.stdin:
    message = json.loads(line)
    if message["method"] == "notifications/initialized":
        initialized = True
        continue
    response = {"jsonrpc": "2.0", "id": message["id"]}
    try:
        if message["method"] == "initialize":
            result = {"protocolVersion": message["params"]["protocolVersion"], "capabilities": {"tools": {}},
                      "serverInfo": {"name": "fake-official", "version": "1"}}
        else:
            result = tool(message["params"]["name"], message["params"]["arguments"])
        response["result"] = result
    except Exception as exc:
        if message.get("params", {}).get("name") == "test_rpc_error":
            response["error"] = {"code": -32603, "message": str(exc)}
        else:
            response["result"] = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
    print(json.dumps(response, ensure_ascii=False), flush=True)
