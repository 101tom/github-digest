# github-digest 🧭

每天三餐时间，自动从 GitHub 挑选 5 个**有趣、有价值**的开源项目，推送到你的微信。

- **采集**：GitHub Trending + Search API（近期新建高星、近期活跃高星）
- **排序**：绝对热度 + 星标增速 + 新鲜度 + 今日趋势，四项加权，不限领域
- **去重**：已推送过的仓库进入 90 天冷却期，绝不重复打扰
- **投递**：微信（Server酱 或 PushPlus，二选一）
- **运行**：GitHub Actions 云端定时，不依赖你的电脑开机

## 目录结构

```
github-digest/
├── .github/workflows/digest.yml   # 定时任务（三餐时间）
├── scripts/digest.py              # 主程序，仅用标准库
├── config.json                    # 筛选与打分配置
└── state/sent.json                # 去重记录（由 Actions 自动回写）
```

## 部署三步

### 1. 推到 GitHub

在 GitHub 新建一个仓库（例如 `github-digest`），然后把**本目录里的内容**作为仓库根目录推上去：

```bash
cd github-digest
git init -b main
git add .
git commit -m "init: github-digest"
git remote add origin https://github.com/<你的用户名>/github-digest.git
git push -u origin main
```

### 2. 配置推送密钥（Secrets）

仓库 → **Settings → Secrets and variables → Actions → New repository secret**，二选一：

| 名称 | 说明 |
| --- | --- |
| `SERVERCHAN_SENDKEY` | [Server酱](https://sct.ftqq.com/) 的 SendKey（推荐，微信扫码即用） |
| `PUSHPLUS_TOKEN` | [PushPlus](https://www.pushplus.plus/) 的 token |

> 若两个都配，优先走 Server酱。

### 3. 允许并测试 Actions

- 仓库 → **Actions** 标签，若提示则点击允许工作流运行。
- 选择 `github-digest` → **Run workflow** 手动跑一次，几秒后微信应收到推送。

## 定时时间

`.github/workflows/digest.yml` 使用 UTC，北京时间 = UTC + 8：

| 推送 | 北京 | cron |
| --- | --- | --- |
| 早餐 | 07:30 | `30 23 * * *` |
| 午餐 | 12:00 | `0 4 * * *` |
| 晚餐 | 18:30 | `30 10 * * *` |

改时间就改这三行。注意 GitHub 定时任务在高峰期可能延迟几分钟到十几分钟，属正常。

## 配置项 `config.json`

| 字段 | 作用 |
| --- | --- |
| `top_n` | 每次推送几个项目（默认 5） |
| `language` | 限定语言，如 `"Python"`；留空 = 不限 |
| `min_stars` | 星标下限 |
| `include_topics` | 只看这些话题；留空 = 不限领域（广撒网） |
| `exclude_topics` | 排除这些话题（默认剔除 awesome/roadmap 等清单类） |
| `exclude_repos` | 永久拉黑某些仓库，如 `["torvalds/linux"]` |
| `resend_cooldown_days` | 同一仓库多久内不重复推 |
| `search_queries` | 搜索语句，`{d7}`/`{d30}`/`{d2}` 会自动替换成日期 |
| `llm.enabled` | 是否生成「一句话中文点评」 |
| `llm.model` | 点评用的模型，默认 `openai/gpt-4o-mini` |

## 中文简介 + 一句话点评（可选）

开启后（`config.json` 里 `llm.enabled = true`），每个项目会直接附上 **🇨🇳 中文简介**（它是什么、解决什么问题）和 **💡 点评**（亮点、适合谁用），无需点开链接就能看懂。若模型不可用则自动回退到英文官方简介。

默认走 **DeepSeek**（OpenAI 兼容接口，中文好、便宜）。需要：

1. 去 https://platform.deepseek.com/ 注册 → 创建 API Key
2. 在仓库 **Secrets** 里新增 `LLM_API_KEY`，填入该 Key

想换别家（OpenAI / 硅基流动 / Moonshot / 通义 等）只改 `config.json`：

```json
"llm": {
  "enabled": true,
  "base_url": "https://api.openai.com/v1",   // 或任何 OpenAI 兼容 base_url
  "model": "gpt-4o-mini",
  "api_key_env": "LLM_API_KEY"
}
```

> 进阶：若把 `base_url` 留空，会回退到用 `GITHUB_TOKEN` 调 GitHub Models（`models: read` 权限已配好），无需额外密钥——但该接口在部分环境下不可用。


## 本地演练（不推送、不改状态）

```powershell
$env:DRY_RUN="1"; python scripts/digest.py
```

## 注意事项

- **公开仓库**的 Actions 分钟数免费；私有仓库会消耗额度，且公开仓库若 **60 天无提交活动**，定时任务会被 GitHub 自动暂停（本项目每次推送都会回写 `state/sent.json`，相当于有活动，一般不受影响）。
- 搜索接口有独立速率限制，脚本已内置退避与间隔，无需干预。
- 想加"中文一句话点评"？可以再接入一个 LLM Key 生成摘要，需要的话告诉我们。
