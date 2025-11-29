from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional, Set, Dict, Any
import httpx
import json
import os
from datetime import datetime

CONFIG_PATH = "config.json"
GITHUB_API_URL = "https://api.github.com/search/issues"


class SearchConfig(BaseModel):
    organizations: List[str] = []    # org 或 user 名稱
    languages: List[str] = []        # python, typescript ...
    polling_interval: int = 120      # 秒（給前端顯示用，實際頻率給 Cron 控制即可）


class NotificationConfig(BaseModel):
    webhook_url: Optional[str] = None


class AppConfig(BaseModel):
    search: SearchConfig
    notif: NotificationConfig
    is_active: bool = False
    known_issue_ids: Set[int] = set()


def load_config() -> AppConfig:
    if not os.path.exists(CONFIG_PATH):
        # 預設空設定
        default = AppConfig(
            search=SearchConfig(),
            notif=NotificationConfig(),
            is_active=False,
            known_issue_ids=set()
        )
        save_config(default)
        return default

    with open(CONFIG_PATH, "r") as f:
        raw = json.load(f)
    # known_issue_ids 要轉回 set
    raw["known_issue_ids"] = set(raw.get("known_issue_ids", []))
    return AppConfig(**raw)


def save_config(cfg: AppConfig) -> None:
    data = cfg.dict()
    # set 不能直接 json，要轉 list
    data["known_issue_ids"] = list(cfg.known_issue_ids)
    with open(CONFIG_PATH, "w") as f:
        json.dump(data, f, indent=2)


app = FastAPI()
config = load_config()


class UpdateConfigRequest(BaseModel):
    search: SearchConfig
    notif: NotificationConfig


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


async def fetch_github_issues(cfg: AppConfig) -> List[Dict[str, Any]]:
    # 組 query：org/user + language + good first issue
    parts = []

    # org/user
    for name in cfg.search.organizations:
        # 你可以自行決定是 org 還是 user，這裡簡單當作 org:user 都試著查
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
        embeds.append(
            {
                "title": issue.get("title"),
                "url": issue.get("html_url"),
                "description": f"Repo: {repo_full_name}\nState: {issue.get('state')}\n\n{(issue.get('body') or '')[:200]}...",
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


@app.get("/issues")
def get_issues():
    cfg = load_config()
    return {"items": cfg.last_items}


@app.get("/cron/check")
async def cron_check():
    """
    給 Render Cron Job 呼叫：
    - 若 is_active=False 直接略過
    - 否則查 GitHub，找出新 issue，發 Discord，更新 known_issue_ids
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

    # 存回已知 ID
    save_config(config)

    # 發 Discord
    if new_issues and config.notif.webhook_url:
        await send_discord_webhook(config.notif.webhook_url, new_issues)

    return {
        "checked_at": datetime.utcnow().isoformat() + "Z",
        "fetched": len(items),
        "new": len(new_issues),
    }
