#!/usr/bin/env python3
"""
伊吹いりこセンター 根ッCO LABO - Instagram自動投稿（GitHub Actions実行用）

環境変数:
  IG_ACCESS_TOKEN : Instagram Login APIのアクセストークン（Actions Secret）
  IG_USER_ID      : IGユーザーID
  SLOT            : "feed"（5:00枠: フィード/カルーセル/リール） or "story"（5:30枠）
  DRY_RUN         : "1" なら投稿せずAPIリクエスト内容を表示のみ

動作:
  queue/YYYY-MM-DD.json（JSTの今日）を読み、SLOTに対応する投稿を実行。
  成功したら queue/YYYY-MM-DD.{slot}.done を作成（ワークフロー側でコミット）。
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = "https://graph.instagram.com/v23.0"
JST = timezone(timedelta(hours=9))

TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
USER_ID = os.environ.get("IG_USER_ID", "")
SLOT = os.environ.get("SLOT", "feed")
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"

REPO_ROOT = Path(__file__).resolve().parent.parent
QUEUE_DIR = REPO_ROOT / "queue"


def api_post(path: str, params: dict) -> dict:
    params = {**params, "access_token": TOKEN}
    if DRY_RUN:
        safe = {k: (v[:60] + "..." if isinstance(v, str) and len(v) > 60 else v)
                for k, v in params.items() if k != "access_token"}
        print(f"  [DRY RUN] POST {path} {json.dumps(safe, ensure_ascii=False)}")
        return {"id": "DRYRUN_ID"}
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"{API}{path}", data=data)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"API error {e.code} on {path}: {body}") from e


def api_get(path: str, params: dict) -> dict:
    params = {**params, "access_token": TOKEN}
    if DRY_RUN:
        return {"status_code": "FINISHED"}
    url = f"{API}{path}?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read())


def wait_until_ready(creation_id: str, timeout_sec: int = 600):
    """動画コンテナの処理完了を待つ"""
    start = time.time()
    while time.time() - start < timeout_sec:
        st = api_get(f"/{creation_id}", {"fields": "status_code"})
        code = st.get("status_code")
        print(f"  container {creation_id}: {code}")
        if code == "FINISHED":
            return
        if code == "ERROR":
            raise RuntimeError(f"container {creation_id} processing ERROR: {st}")
        time.sleep(15)
    raise RuntimeError(f"container {creation_id} not ready after {timeout_sec}s")


def publish(creation_id: str) -> dict:
    return api_post(f"/{USER_ID}/media_publish", {"creation_id": creation_id})


def post_feed(entry: dict):
    typ = entry["type"]
    caption = entry.get("caption", "")
    media = entry["media"]

    if typ == "image":
        c = api_post(f"/{USER_ID}/media", {"image_url": media[0], "caption": caption})
        result = publish(c["id"])
    elif typ == "carousel":
        children = []
        for url in media:
            c = api_post(f"/{USER_ID}/media",
                         {"image_url": url, "is_carousel_item": "true"})
            children.append(c["id"])
            time.sleep(2)
        parent = api_post(f"/{USER_ID}/media", {
            "media_type": "CAROUSEL",
            "children": ",".join(children),
            "caption": caption,
        })
        result = publish(parent["id"])
    elif typ == "reel":
        params = {
            "media_type": "REELS",
            "video_url": media[0],
            "caption": caption,
            "share_to_feed": "true",
        }
        if entry.get("cover"):
            params["cover_url"] = entry["cover"]
        c = api_post(f"/{USER_ID}/media", params)
        wait_until_ready(c["id"])
        result = publish(c["id"])
    else:
        raise ValueError(f"unknown feed type: {typ}")

    print(f"  published: {result}")


def post_story(entry: dict):
    url = entry["media"][0]
    is_video = url.lower().endswith((".mp4", ".mov"))
    params = {"media_type": "STORIES"}
    params["video_url" if is_video else "image_url"] = url
    c = api_post(f"/{USER_ID}/media", params)
    if is_video:
        wait_until_ready(c["id"])
    result = publish(c["id"])
    print(f"  published: {result}")


def main():
    if not TOKEN or not USER_ID:
        print("ERROR: IG_ACCESS_TOKEN / IG_USER_ID が未設定")
        sys.exit(1)

    today = os.environ.get("QUEUE_DATE") or datetime.now(JST).strftime("%Y-%m-%d")
    qfile = QUEUE_DIR / f"{today}.json"
    done_marker = QUEUE_DIR / f"{today}.{SLOT}.done"

    print(f"date(JST)={today} slot={SLOT} dry_run={DRY_RUN}")

    if not qfile.exists():
        print(f"queue無し（{qfile.name}）。今日は投稿予定なし。終了。")
        return
    if done_marker.exists():
        print(f"{done_marker.name} が存在。既に投稿済み。終了。")
        return

    queue = json.loads(qfile.read_text(encoding="utf-8"))
    entry = queue.get(SLOT)
    if not entry:
        print(f"このslot（{SLOT}）の予定なし。終了。")
        return

    print(f"投稿実行: {entry['type']} / media {len(entry['media'])}件")
    if SLOT == "feed":
        post_feed(entry)
    else:
        post_story(entry)

    if not DRY_RUN:
        done_marker.write_text(datetime.now(JST).isoformat(), encoding="utf-8")
        print(f"done marker作成: {done_marker.name}")


if __name__ == "__main__":
    main()
