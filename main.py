from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional, Set, Dict, Any
import httpx
import json
import os
from datetime import datetime
import threading
import time
import asyncio

CONFIG_PATH = "config.json"
GITHUB_API_URL = "https://api.github.com/search/issues"


# ====== Models ======

class SearchConfig(BaseModel):
    organizations: List[str] = []    # org 或 user 名稱
    languages: List[str] = []        # python, typescript ...
    polling_interval: int = 120      # 秒（背景 worker 的輪詢間隔）


class NotificationConfig(BaseModel):
    webhook_url: Optional[str] = None


class AppConfig(BaseModel):
    search: SearchConfig
    notif: NotificationConfig
    is_active: bool = False
    known_issue_ids: Set[int] = set()
    last_items: List[Dict[str, Any]] = []  # 最近一次抓到的 issues


# ====== Config 讀寫 ======

def load_config() -> AppConfig:
    if not os.path.exists(CONFIG_PATH):
        # 預設空設定
        default = AppConfig(
            search=SearchConfig(),
            notif=NotificationConfig(),
            is_active=False,
            known_issue_ids=set(),
            last_items=[]
        )
        save_config(default)
        return default

    with open(CONFIG_PATH, "r") as f:
        raw = json.load(f)

    raw["known_issue_ids"] = set(raw.get("known_issue_ids", []))
    raw["last_items"] = raw.get("last_items", [])
    return AppConfig(**raw)


def save_config(cfg: AppConfig) -> None:
    data = cfg.dict()
    data["known_issue_ids"] = list(cfg.known_issue_ids)  # set 轉 list
    with open(CONFIG_PATH, "w") as f:
        json.dump(data, f, indent=2)


# ====== App & 全域 config ======

app = FastAPI()
config = load_config()


class UpdateConfigRequest(BaseModel):
    search: SearchConfig
    notif: NotificationConfig


# ====== API ======

@app.get("/health")
def health():
    return {"status": "ok", "active": config.is_active}


@app.post("/config")
def update_config(body: UpdateConfigRequest):
    global config
    config.search = body.search
    config.notif = body.notif
    save_config(config)
    return {"message": "config updated"}


@app.post("/watch/start")
def start_watch():
    global config
    config.is_active = True
    save_config(config)
    return {"message": "watch started"}


@app.post("/watch/stop")
def stop_watch():
    global config
    config.is_active = False
    save_config(config)
    return {"message": "watch stopped"}


@app.get("/issues")
def get_issues():
    """
    回傳最近一次 worker / 手動檢查時抓到的 issues。
    """
    cfg = load_config()
    return {"items": cfg.last_items}


# ====== GitHub & Discord 邏輯 ======

async def fetch_github_issues(cfg: AppConfig) -> List[Dict[str, Any]]:
    # 組 query：org/user + language + good first issue
    parts: List[str] = []

    # org/user
    for name in cfg.search.organizations:
        parts.append(f"org:{name}")
        parts.append(f"user:{name}")

    # language
    for lang in cfg.search.languages:
        parts.append(f"language:{lang}")

    # good first issue
    parts.append('label:"good first issue"')

    # 若沒設定 org/user，GitHub 會在全平台找
    q = " ".join(parts) if parts else 'label:"good first issue"'

    params = {
        "q": q,
        "sort": "updated",      # 抓最近有變動的
        "order": "desc",
        "per_page": 50
    }

    async with httpx.AsyncClient() as client:
        resp = await client.get(GITHUB_API_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
        return data.get("items", [])


async def send_discord_webhook(webhook_url: str, issues: List[Dict[str, Any]]):
    if not webhook_url:
        return

    count = len(issues)
    if count == 0:
        return

    embeds = []
    for issue in issues[:5]:
        repo_full_name = issue.get("repository_url", "").replace(
            "https://api.github.com/repos/", ""
        )
        body = issue.get("body") or ""
        embeds.append(
            {
                "title": issue.get("title"),
                "url": issue.get("html_url"),
                "description": f"Repo: {repo_full_name}\nState: {issue.get('state')}\n\n{body[:200]}...",
                "color": 5814783,
                "footer": {"text": "GitScout Notification"},
            }
        )

    payload = {
        "content": f"🚀 GitScout Alert: Found {count} new 'good first issue'{'' if count == 1 else 's'}!",
        "embeds": embeds,
    }

    async with httpx.AsyncClient() as client:
        await client.post(webhook_url, json=payload, timeout=10.0)


# ====== 核心檢查邏輯（worker & /cron 共用） ======

async def run_check_once() -> Dict[str, Any]:
    """
    只做一次 GitHub 檢查：
    - 若未啟用 watch，直接略過
    - 否則抓 issues、判斷新 issue、更新 config、發 Discord
    """
    global config

    if not config.is_active:
        return {"message": "watch inactive, skip"}

    try:
        items = await fetch_github_issues(config)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"github error: {e}")

    new_issues: List[Dict[str, Any]] = []
    for it in items:
        iid = it.get("id")
        if iid is None:
            continue
        if iid not in config.known_issue_ids:
            config.known_issue_ids.add(iid)
            new_issues.append(it)

    # 更新最後一次抓到的清單
    config.last_items = items
    save_config(config)

    # 發 Discord
    if new_issues and config.notif.webhook_url:
        await send_discord_webhook(config.notif.webhook_url, new_issues)

    result = {
        "checked_at": datetime.utcnow().isoformat() + "Z",
        "fetched": len(items),
        "new": len(new_issues),
    }
    print("run_check_once result:", result)
    return result


@app.get("/cron/check")
async def cron_check():
    """
    仍然保留這個 endpoint，方便你手動觸發或本機測試。
    """
    return await run_check_once()


# ====== 背景 worker thread ======

def background_worker():
    global config
    print("Background worker started")
    while True:
        try:
            # 每輪讀一次最新 config（避免只用記憶體版本）
            cfg = load_config()
            # 更新 global config 參考
            config.search = cfg.search
            config.notif = cfg.notif
            config.is_active = cfg.is_active
            config.known_issue_ids = cfg.known_issue_ids
            config.last_items = cfg.last_items

            interval = max(cfg.search.polling_interval, 30)  # 最少 30 秒
            if cfg.is_active:
                # 用 asyncio.run 執行一次檢查
                asyncio.run(run_check_once())
            else:
                print("watch inactive, worker idle")

            time.sleep(interval)
        except Exception as e:
            print("background worker error:", e)
            # 避免狂刷 log，出錯時暫停一段時間
            time.sleep(30)


@app.on_event("startup")
def start_background_worker():
    t = threading.Thread(target=background_worker, daemon=True)
    t.start()
    print("Background worker thread launched")
