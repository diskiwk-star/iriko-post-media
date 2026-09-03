#!/usr/bin/env python3
"""
伊吹いりこセンター 根ッCO LABO - Instagram自動投稿（GitHub Actions実行用）

環境変数:
  IG_ACCESS_TOKEN : Instagram Login APIのアクセストークン（Actions Secret）
  IG_USER_ID      : IGユーザーID
  SLOT            : "feed"（5:00枠: フィード/カルーセル/リール） or "story"（5:30枠）
  TARGET_TIME     : 投稿目標時刻（JST・"HH:MM"）。既定は feed=05:00 / story=05:30
  DRY_RUN         : "1" なら投稿せずAPIリクエスト内容を表示のみ

動作:
  queue/YYYY-MM-DD.json（JSTの今日）を読み、SLOTに対応する投稿を実行。
  成功したら queue/YYYY-MM-DD.{slot}.done を作成（ワークフロー側でコミット）。

投稿時刻について:
  GitHub Actionsの定時実行は混雑時に数時間遅れることがある（2026-08-13に約2時間20分の
  遅延を実測）。そのため cron は目標時刻より少し早めに設定し、早く起動したときは
  TARGET_TIME まで待ってから投稿する。遅れて起動したときは待たずにすぐ投稿する。
  同じslotに複数のcronを仕掛けてあるが、.done マーカーで二重投稿は防がれる。
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

# 投稿先アカウント。queueで "accounts": ["tadotsu","kanonji"] のように指定できる。
# 省略時は多度津のみ（従来どおり）。
ACCOUNTS = {
    "tadotsu": {
        "label": "多度津",
        "token": os.environ.get("IG_ACCESS_TOKEN", ""),
        "user_id": os.environ.get("IG_USER_ID", ""),
    },
    "kanonji": {
        "label": "観音寺",
        "token": os.environ.get("IG_ACCESS_TOKEN_KANONJI", ""),
        "user_id": os.environ.get("IG_USER_ID_KANONJI", ""),
    },
}
DEFAULT_ACCOUNTS = ["tadotsu"]

# 実行中のアカウント（use_account で切り替える）
TOKEN = ACCOUNTS["tadotsu"]["token"]
USER_ID = ACCOUNTS["tadotsu"]["user_id"]
SLOT = os.environ.get("SLOT", "feed")
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"
TARGET_TIME = os.environ.get("TARGET_TIME") or ("05:00" if SLOT == "feed" else "05:30")
MAX_WAIT_SEC = 90 * 60  # 早く起動しすぎた場合の待機上限（保険）

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
    """公開する。コンテナ処理待ち(9007)の場合は少し待って再試行する。"""
    last_err = None
    for attempt in range(6):
        try:
            return api_post(f"/{USER_ID}/media_publish", {"creation_id": creation_id})
        except RuntimeError as e:
            # まだコンテナが出来上がっていないときのエラー。少し待てば通る。
            # 2207027 = メディアが公開準備できていない
            # 2207006 = "Media Not Found"（作った直後だと稀にこれが返る。2026-09-04に発生）
            if not any(t in str(e) for t in ("2207027", "2207006", '"code":9007')):
                raise
            last_err = e
            wait = 10 * (attempt + 1)
            print(f"  まだ公開準備中。{wait}秒待って再試行（{attempt + 1}/6）")
            time.sleep(wait)
    raise RuntimeError(f"公開の再試行が上限に達しました: {last_err}")


def post_feed(entry: dict):
    typ = entry["type"]
    caption = entry.get("caption", "")
    media = entry["media"]

    # 共同投稿（コラボ）。相手に招待が飛び、相手が承認して初めて相手側に表示される。
    # ストーリーズは非対応なので post_story では扱わない。
    collab = entry.get("collaborators") or []
    collab_param = {"collaborators": json.dumps(collab)} if collab else {}
    if collab:
        print(f"  共同投稿の招待: {', '.join(collab)}")

    def create_container(params: dict) -> dict:
        """親コンテナを作る。collaborators で弾かれたら共同投稿なしで作り直す
        （共同投稿が通らなくても、その日の投稿自体は落とさない）"""
        try:
            return api_post(f"/{USER_ID}/media", {**params, **collab_param})
        except RuntimeError as e:
            if not collab_param:
                raise
            print(f"  警告: 共同投稿の指定でAPIエラー。共同投稿なしで投稿します → {e}")
            return api_post(f"/{USER_ID}/media", params)

    if typ == "image":
        c = create_container({"image_url": media[0], "caption": caption})
        result = publish(c["id"])
    elif typ == "carousel":
        children = []
        for url in media:
            c = api_post(f"/{USER_ID}/media",
                         {"image_url": url, "is_carousel_item": "true"})
            children.append(c["id"])
            time.sleep(2)
        parent = create_container({
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
        c = create_container(params)
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


def wait_until_target():
    """目標時刻(JST)より早く起動したら、その時刻まで待つ。遅れていたら即座に戻る。"""
    now = datetime.now(JST)
    hh, mm = (int(x) for x in TARGET_TIME.split(":"))
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    delta = (target - now).total_seconds()

    if delta <= 0:
        late = int(-delta // 60)
        print(f"目標 {TARGET_TIME} を{late}分過ぎて起動。待たずに投稿します。")
        return
    if delta > MAX_WAIT_SEC:
        print(f"目標 {TARGET_TIME} まで{int(delta // 60)}分あり、待機上限を超えるため中止。"
              "（cronの設定が早すぎる可能性があります）")
        sys.exit(0)

    print(f"目標 {TARGET_TIME} まで{int(delta // 60)}分{int(delta % 60)}秒待機します。")
    if not DRY_RUN:
        time.sleep(delta)


def already_posted_today(today: str, entry: dict) -> bool:
    """今日すでに同じ内容が投稿済みかをAPIで確認する（二重投稿の最終防波堤）。

    ストーリーズはmediaエッジに出ないため、feedのみキャプション先頭で判定する。
    判定に失敗した場合は False（＝投稿を続行）を返す。
    """
    if SLOT != "feed":
        return False
    head = (entry.get("caption") or "")[:20].strip()
    if not head:
        return False
    try:
        res = api_get("/me/media", {"fields": "caption,timestamp", "limit": "5"})
        for m in res.get("data", []):
            ts = datetime.fromisoformat(m["timestamp"].replace("+0000", "+00:00"))
            if ts.astimezone(JST).strftime("%Y-%m-%d") != today:
                continue
            if (m.get("caption") or "").startswith(head):
                print(f"  既存投稿を検出: {m['timestamp']}")
                return True
    except Exception as e:
        print(f"  （投稿済み確認をスキップ: {e}）")
    return False


def use_account(key: str):
    """以降のAPI呼び出しで使うアカウントを切り替える。"""
    global TOKEN, USER_ID
    acc = ACCOUNTS[key]
    TOKEN = acc["token"]
    USER_ID = acc["user_id"]


def done_marker_path(today: str, account: str) -> Path:
    # 多度津は従来のファイル名を維持する（過去分のマーカーと互換を保つため）
    if account == "tadotsu":
        return QUEUE_DIR / f"{today}.{SLOT}.done"
    return QUEUE_DIR / f"{today}.{SLOT}.{account}.done"


def main():
    today = os.environ.get("QUEUE_DATE") or datetime.now(JST).strftime("%Y-%m-%d")
    qfile = QUEUE_DIR / f"{today}.json"

    print(f"date(JST)={today} slot={SLOT} dry_run={DRY_RUN}")

    if not qfile.exists():
        print(f"queue無し（{qfile.name}）。今日は投稿予定なし。終了。")
        return

    queue = json.loads(qfile.read_text(encoding="utf-8"))
    slot_value = queue.get(SLOT)
    if not slot_value:
        print(f"このslot（{SLOT}）の予定なし。終了。")
        return

    # entryは単体dict（従来）またはリスト（アカウント別に内容を変える場合。
    # 例: 多度津用と観音寺用で別キャプション・別画像）。
    entries = slot_value if isinstance(slot_value, list) else [slot_value]

    # (entry, account) の組を作る。各entryの投稿先はentry内のaccounts（省略時は多度津）。
    # ※ done markerはアカウント単位なので、同じアカウントを複数entryに書かないこと。
    jobs = []
    for e in entries:
        for a in (e.get("accounts") or DEFAULT_ACCOUNTS):
            jobs.append((e, a))

    unknown = [a for _, a in jobs if a not in ACCOUNTS]
    if unknown:
        print(f"ERROR: 未知のアカウント指定 {unknown}")
        sys.exit(1)
    dup = [a for _, a in jobs if [x for _, x in jobs].count(a) > 1]
    if dup:
        print(f"ERROR: 同じアカウントが複数エントリに指定されています {sorted(set(dup))}")
        sys.exit(1)

    # まだ投稿していないアカウントだけに絞る
    pending = [(e, a) for e, a in jobs if not done_marker_path(today, a).exists()]
    if not pending:
        print("対象アカウントはすべて投稿済み。終了。")
        return
    print(f"投稿先: {'、'.join(ACCOUNTS[a]['label'] for _, a in pending)}")

    for _, a in pending:
        if not ACCOUNTS[a]["token"] or not ACCOUNTS[a]["user_id"]:
            print(f"ERROR: {ACCOUNTS[a]['label']}のトークン/ユーザーIDが未設定")
            sys.exit(1)

    # 投稿する分があると確定してから、目標時刻まで待つ（空振りの日は待たない）
    wait_until_target()

    failed = []
    for entry, account in pending:
        use_account(account)
        label = ACCOUNTS[account]["label"]
        print(f"--- {label} ---")

        # 待機中に別の実行が投稿を終えている場合があるので、投稿直前にAPIで最終確認する。
        # （done markerはコミットされるまで他の実行から見えないため、これが唯一確実な砦）
        if not DRY_RUN and already_posted_today(today, entry):
            print("  待機中に別の実行が同じ内容を投稿済みでした。二重投稿を回避。")
            done_marker_path(today, account).write_text(
                datetime.now(JST).isoformat(), encoding="utf-8")
            continue

        print(f"  投稿実行: {entry['type']} / media {len(entry['media'])}件")
        try:
            if SLOT == "feed":
                post_feed(entry)
            else:
                post_story(entry)
        except Exception as e:
            # 1アカウントの失敗で他のアカウントを巻き込まない
            print(f"  ERROR({label}): {e}")
            failed.append(label)
            continue

        if not DRY_RUN:
            marker = done_marker_path(today, account)
            marker.write_text(datetime.now(JST).isoformat(), encoding="utf-8")
            print(f"  done marker作成: {marker.name}")

    if failed:
        raise SystemExit(f"投稿に失敗したアカウントがあります: {'、'.join(failed)}")


if __name__ == "__main__":
    main()
