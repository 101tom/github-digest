#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""github-digest —— GitHub 优质项目发现器。

流程：采集(GitHub Trending + Search API) -> 打分排序 -> 去重 -> 生成简报
      -> 推送到微信(Server酱 或 PushPlus) -> 回写去重状态。

只用 Python 标准库，无需安装依赖。所有凭据通过环境变量注入。
"""

import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------- 基础路径

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.json")
STATE_PATH = os.path.join(ROOT, "state", "sent.json")

# Windows 控制台默认 GBK，统一切到 UTF-8，避免 emoji/中文报错
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

API = "https://api.github.com"
UA = "github-digest-bot (+https://github.com)"
CST = timezone(timedelta(hours=8))  # 北京时间


def log(*args):
    print(*args, file=sys.stderr, flush=True)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)


# ---------------------------------------------------------------- HTTP

def http_get(url, params=None, token=None, accept=None, raw=False, retries=3):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    headers = {"User-Agent": UA}
    if token:
        headers["Authorization"] = "Bearer " + token
    if accept:
        headers["Accept"] = accept
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8", "replace")
            return body if raw else json.loads(body)
        except urllib.error.HTTPError as e:
            last_err = e
            # 403 多为速率限制，退避重试
            if e.code in (403, 429) and attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise
    raise last_err


def http_post_json(url, payload, token=None, extra_headers=None, retries=2, timeout=60):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"User-Agent": UA, "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if extra_headers:
        headers.update(extra_headers)
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            last_err = e
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            log(f"POST {url} HTTP {e.code}: {detail}")
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                time.sleep(4 * (attempt + 1))
                continue
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            raise
    raise last_err


# ---------------------------------------------------------------- 采集

def fetch_trending(since, token):
    """抓取 https://github.com/trending，返回 {full_name: stars_today}。"""
    try:
        html = http_get("https://github.com/trending", {"since": since}, token=token, raw=True)
    except Exception as e:  # noqa: BLE001
        log("trending 抓取失败（忽略）:", e)
        return {}

    result = {}
    blocks = html.split('<article class="Box-row">')[1:]
    for block in blocks:
        # 注意：<a> 上可能先有 data-hydro-click 等属性，href 不一定紧跟 <a
        m = re.search(
            r'<h2 class="h3 lh-condensed">\s*<a[^>]*?href="/([^"/]+)/([^"]+?)"', block
        )
        if not m:
            continue
        full_name = f"{m.group(1)}/{m.group(2)}".rstrip()
        m2 = re.search(r'([\d,]+)\s+stars?\s+today', block)
        today = int(m2.group(1).replace(",", "")) if m2 else 0
        result[full_name] = today
    log(f"trending({since}) 命中 {len(result)} 个仓库")
    return result


def fetch_search(queries, token, per_page=40):
    """执行若干 GitHub 搜索查询，返回原始 item 列表。"""
    items = []
    for q in queries:
        try:
            data = http_get(
                f"{API}/search/repositories",
                {"q": q, "sort": "stars", "order": "desc", "per_page": per_page},
                token=token,
                accept="application/vnd.github+json",
            )
            got = data.get("items", [])
            log(f"search[{q}] -> {len(got)}")
            items.extend(got)
            time.sleep(2)  # 搜索接口有独立速率限制，放慢一点
        except Exception as e:  # noqa: BLE001
            log(f"search[{q}] 失败（忽略）: {e}")
    return items


def fetch_repo(full_name, token):
    try:
        return http_get(f"{API}/repos/{full_name}", token=token, accept="application/vnd.github+json")
    except Exception as e:  # noqa: BLE001
        log(f"repos/{full_name} 失败: {e}")
        return None


def normalize(item):
    return {
        "full_name": item.get("full_name", ""),
        "url": item.get("html_url", ""),
        "desc": (item.get("description") or "").strip(),
        "stars": item.get("stargazers_count", 0) or 0,
        "lang": item.get("language") or "",
        "topics": list(item.get("topics") or []),
        "created": item.get("created_at") or "",
        "pushed": item.get("pushed_at") or "",
        "archived": bool(item.get("archived")),
        "fork": bool(item.get("fork")),
    }


# ---------------------------------------------------------------- 打分

def days_since(iso):
    if not iso:
        return 100000
    try:
        dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return 100000
    return max((datetime.now(timezone.utc) - dt).days, 0)


def score(repo, stars_today=0):
    """综合分：绝对热度 + 星标增速 + 新鲜度 + 今日趋势加成。"""
    stars = repo["stars"]
    age = max(days_since(repo["created"]), 1)

    base = math.log10(stars + 1) * 3.0                     # 绝对热度
    velocity = math.log10(stars / age + 1) * 4.0           # 平均每天涨星
    freshness = max(0.0, (365 - age) / 365.0) * 2.0        # 越新越加分
    trending = min(math.sqrt(stars_today) , 8.0) if stars_today else 0.0
    return base + velocity + freshness + trending


# ---------------------------------------------------------------- 过滤/选取

def passes_filters(repo, cfg, sent, today):
    name = repo["full_name"]
    if not name or repo["archived"] or repo["fork"]:
        return False
    if not repo["desc"]:
        return False
    if repo["stars"] < cfg.get("min_stars", 0):
        return False
    if name in set(cfg.get("exclude_repos") or []):
        return False
    lower_topics = {t.lower() for t in repo["topics"]}
    if lower_topics & {t.lower() for t in (cfg.get("exclude_topics") or [])}:
        return False
    include = {t.lower() for t in (cfg.get("include_topics") or [])}
    if include and not (lower_topics & include):
        return False
    lang = cfg.get("language")
    if lang and repo["lang"].lower() != lang.lower():
        return False
    # 冷却期去重
    last = sent.get(name)
    if last:
        try:
            last_dt = datetime.strptime(last, "%Y-%m-%d").date()
            if (today - last_dt).days < cfg.get("resend_cooldown_days", 90):
                return False
        except ValueError:
            pass
    return True


def pick(repos_by_name, trending_map, cfg, sent, today):
    scored = []
    for name, repo in repos_by_name.items():
        if not passes_filters(repo, cfg, sent, today):
            continue
        scored.append((score(repo, trending_map.get(name, 0)), repo))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[: cfg.get("top_n", 5)]]


# ---------------------------------------------------------------- 一句话点评

def llm_comment(repo, cfg):
    """调用 GitHub Models 生成一句中文点评；任何失败都静默降级为空。"""
    llm = cfg.get("llm") or {}
    if not llm.get("enabled"):
        return ""
    token = (
        os.environ.get("LLM_API_KEY")
        or os.environ.get("GITHUB_TOKEN")
        or os.environ.get("GH_TOKEN")
    )
    if not token:
        log("未找到可用于点评的令牌，跳过")
        return ""

    endpoint = llm.get("endpoint", "https://models.github.ai/inference/chat/completions")
    model = llm.get("model", "openai/gpt-4o-mini")
    prompt = (
        f"开源项目：{repo['full_name']}\n"
        f"官方简介：{repo['desc']}\n"
        f"主要语言：{repo['lang'] or '未知'}；星标数：{repo['stars']}\n"
        f"标签：{', '.join(repo['topics'][:8]) or '无'}\n\n"
        "请用一句中文（不超过 45 字）点评它的核心价值与典型用途，"
        "口吻像向朋友安利，直接输出句子本身，不要引号、不要任何前缀。"
    )
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "你是资深开发者，擅长用一句话讲清一个开源项目值不值得关注、能用来做什么。",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.7,
        "max_tokens": 120,
    }
    try:
        data = http_post_json(
            endpoint,
            payload,
            token=token,
            extra_headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        content = data["choices"][0]["message"]["content"]
        return content.strip().strip('"“”').replace("\n", " ").strip()
    except Exception as e:  # noqa: BLE001
        log(f"点评生成失败 {repo['full_name']}: {e}")
        return ""


# ---------------------------------------------------------------- 渲染

def humanize(num):
    if num >= 1000:
        return f"{num / 1000:.1f}k"
    return str(num)


def render(repos, now_cst):
    day = now_cst.strftime("%Y-%m-%d")
    slot = {0: "早餐", 1: "午餐", 2: "晚餐"}.get(now_cst.hour // 6, "")
    lines = [f"## 🧭 GitHub 精选 · {day} {now_cst.strftime('%H:%M')} {slot}".rstrip(), ""]
    for i, r in enumerate(repos, 1):
        age = days_since(r["created"])
        age_txt = "今天" if age <= 0 else (f"{age}天前" if age < 60 else f"{age // 30}个月前")
        meta = f"⭐ {humanize(r['stars'])}"
        if r["lang"]:
            meta += f" · {r['lang']}"
        meta += f" · {age_txt}创建"
        lines.append(f"**{i}. [{r['full_name']}]({r['url']})**")
        lines.append(meta)
        lines.append(f"> {r['desc']}")
        if r.get("comment"):
            lines.append(f"💡 {r['comment']}")
        if r["topics"]:
            lines.append("`" + "` `".join(r["topics"][:5]) + "`")
        lines.append("")
    lines.append("---")
    lines.append("_由 github-digest 自动生成，帮你发现值得关注的开源项目。_")
    return "\n".join(lines)


# ---------------------------------------------------------------- 投递

def send_serverchan(sendkey, title, body):
    data = urllib.parse.urlencode({"title": title, "desp": body}).encode("utf-8")
    req = urllib.request.Request(
        f"https://sctapi.ftqq.com/{sendkey}.send", data=data, headers={"User-Agent": UA}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def send_pushplus(token, title, body):
    payload = json.dumps(
        {"token": token, "title": title, "content": body, "template": "markdown"}
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://www.pushplus.plus/send",
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": UA},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def deliver(title, body):
    sendkey = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
    pushplus = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    if sendkey:
        log("通过 Server酱 推送")
        return "serverchan", send_serverchan(sendkey, title, body)
    if pushplus:
        log("通过 PushPlus 推送")
        return "pushplus", send_pushplus(pushplus, title, body)
    raise SystemExit("未配置推送渠道：请在仓库 Secrets 中设置 SERVERCHAN_SENDKEY 或 PUSHPLUS_TOKEN")


# ---------------------------------------------------------------- 主流程

def build_queries(cfg):
    today = datetime.now(timezone.utc)
    mapping = {
        "{d7}": (today - timedelta(days=7)).strftime("%Y-%m-%d"),
        "{d30}": (today - timedelta(days=30)).strftime("%Y-%m-%d"),
        "{d2}": (today - timedelta(days=2)).strftime("%Y-%m-%d"),
        "{d1}": (today - timedelta(days=1)).strftime("%Y-%m-%d"),
    }
    out = []
    for q in cfg.get("search_queries", []):
        for k, v in mapping.items():
            q = q.replace(k, v)
        out.append(q)
    return out


def main():
    cfg = load_json(CONFIG_PATH, {})
    state = load_json(STATE_PATH, {"sent": {}})
    sent = state.get("sent", {})
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if not token:
        log("警告：未提供 GITHUB_TOKEN，搜索接口速率极低，可能失败")

    # 1) 采集
    trending_map = fetch_trending(cfg.get("trending_since", "daily"), token)
    raw_items = fetch_search(build_queries(cfg), token)

    repos_by_name = {}
    for item in raw_items:
        r = normalize(item)
        if r["full_name"]:
            repos_by_name[r["full_name"]] = r

    # 2) 用 trending 补全/丰富仓库详情（并覆盖排序依据）
    for full_name in list(trending_map.keys()):
        if full_name not in repos_by_name:
            item = fetch_repo(full_name, token)
            if item:
                repos_by_name[full_name] = normalize(item)
            time.sleep(0.5)

    log(f"去重前候选 {len(repos_by_name)} 个")

    # 3) 打分选取
    now_cst = datetime.now(timezone.utc).astimezone(CST)
    today = now_cst.date()
    picked = pick(repos_by_name, trending_map, cfg, sent, today)
    log(f"最终选出 {len(picked)} 个")

    if not picked:
        log("没有可推送的新项目，跳过本次投递")
        return

    # 3.5) 生成一句话点评（可选）
    if (cfg.get("llm") or {}).get("enabled"):
        for r in picked:
            comment = llm_comment(r, cfg)
            if comment:
                r["comment"] = comment
            time.sleep(1)

    # 4) 渲染 + 投递
    day = now_cst.strftime("%Y-%m-%d")
    title = f"🧭 GitHub 精选 {day} {now_cst.strftime('%H:%M')}"
    body = render(picked, now_cst)

    if os.environ.get("DRY_RUN"):
        print(title)
        print(body)
        log("DRY_RUN 已开启，跳过推送与状态写入")
        return

    channel, resp = deliver(title, body)
    log(f"{channel} 返回: {resp}")

    # 5) 回写状态
    for r in picked:
        sent[r["full_name"]] = day
    # 清理过老记录，避免无限增长
    cutoff = (today - timedelta(days=365)).strftime("%Y-%m-%d")
    state["sent"] = {k: v for k, v in sent.items() if v >= cutoff}
    save_json(STATE_PATH, state)
    log("状态已更新，共记录", len(state["sent"]), "个仓库")


if __name__ == "__main__":
    main()
