# 15 · NAS 自动化部署方案（Docker Hub + Watchtower）

> 状态：**决策已确认，待所有者批准执行**
> 前置阅读：`docs/11-部署运维.md`（项目构建/发布现状事实梳理）
> 日期：2026-07-22（v2，按所有者决策重写；v1 的备选方案比较已删除）

## 一、已确认的决策

| # | 决策点 | 结论 |
|---|---|---|
| D1 | 镜像仓库 | **Docker Hub**（所有者自己的账号，替换原硬编码 `sunxiao0721/panwatch`） |
| D2 | 远程访问 | **所有者自行配置**，本方案不涉及 |
| D3 | 自动部署 | **Watchtower 轮询**：NAS 上跑 watchtower 容器，发现新镜像自动 pull + recreate |
| D4 | NAS 环境 | **群晖，IP 192.168.1.124**，SSH 用户 jonah，部署操作需 root 权限（sudo -i / sudo 执行） |
| D5 | 总体策略 | **全自动化部署**：测试优先在本地完成；**NAS 上只运行最新可用（已验证）的版本**，即追 `latest` |
| D6 | 认证 | 公网访问由所有者自配；容器侧预设 `AUTH_USERNAME` / `AUTH_PASSWORD`（存于 NAS 本地 `.env`，不进仓库） |

> 安全约定：NAS 密码、Docker Hub token、登录密码等机密**一律不写入仓库文件**。NAS 侧机密放 `~/panwatch/.env`（或群晖 Container Manager 的环境变量配置），GitHub 侧机密放仓库 Secrets。

## 二、目标工作流（全自动闭环）

```
本地开发 (macOS)
   │  ① 本地测试：make test + make dev-api / dev-web 自验
   │  ② 本地通过后：git push + git tag x.y.z && git push --tags
   ▼
GitHub Actions（release.yml，已存在，需改镜像名）
   │  ③ pytest 两套测试 → ④ 构建 amd64+arm64 镜像 → ⑤ 推 Docker Hub（tag + latest）
   ▼
Docker Hub（所有者账号）
   │  ⑥ Watchtower 每 N 分钟轮询发现新 latest
   ▼
群晖 NAS (192.168.1.124)
   │  ⑦ watchtower 自动 pull + recreate panwatch 容器（带 rolling 清理旧镜像）
   │  ⑧ 容器健康检查（/api/health，Dockerfile 已内置 HEALTHCHECK）
   ▼
可用服务 :8000（数据持久化在 NAS 卷，升级无损）
```

设计要点：

- **"最新可用"的保障在 CI 门禁**：测试不过 → 镜像不推 → NAS 永远只拿到通过测试的 latest。这就是"测试在本地 + CI 双重把关，NAS 无脑追新"的含义。
- **NAS 侧零操作**：部署后日常只需保证 NAS 开机；版本升级完全由 tag 驱动。
- **回滚**：Docker Hub 保留历史版本 tag，需要时在 NAS 上把 compose 的 image 改成指定版本号即可。

## 三、执行阶段

### 阶段 1 · 发布链路修正（本地改 3 个文件 + GitHub 配置）

变更：

1. `build.sh`：镜像名 `sunxiao0721/panwatch` → 所有者的 Docker Hub 仓库名（**待确认：你的 Docker Hub 用户名**）。
2. `.github/workflows/release.yml`：同上镜像名。
3. `src/core/update_checker.py`：默认检查仓库改为新镜像名（否则更新检查指向原作者）。

GitHub 侧配置（所有者手动或我指导）：仓库 Settings → Secrets 添加 `DOCKERHUB_USERNAME`、`DOCKERHUB_TOKEN`（Docker Hub → Account Settings → Security 生成 Access Token）。

验收：手动 `workflow_dispatch` 触发 release.yml，确认镜像出现在你的 Docker Hub。

产出：3 个文件的 diff 清单 + CI 运行链接。

### 阶段 2 · NAS 部署基线（新增 `deploy/` 目录 + SSH 到群晖操作）

仓库新增（提交进 git）：

- `deploy/docker-compose.yml`：

```yaml
services:
  panwatch:
    image: <你的dockerhub>/panwatch:latest
    container_name: panwatch
    ports:
      - "8000:8000"          # 如 8000 被占改宿主机侧，如 "18000:8000"
    volumes:
      - panwatch_data:/app/data
    env_file: .env           # NAS 本地文件，不进仓库
    restart: unless-stopped

  watchtower:
    image: containrrr/watchtower:latest
    container_name: watchtower
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    environment:
      WATCHTOWER_POLL_INTERVAL: 300        # 5 分钟轮询
      WATCHTOWER_CLEANUP: "true"           # 自动删旧镜像
      WATCHTOWER_INCLUDE_STOPPED: "false"
      WATCHTOWER_MONITOR_ONLY: "false"
      # 私有仓库时需挂 ~/.docker/config.json；Docker Hub 公开仓库可免
    command: panwatch                       # 只盯 panwatch 容器
    restart: unless-stopped

volumes:
  panwatch_data:
```

- `deploy/.env.example`：`AUTH_USERNAME=` / `AUTH_PASSWORD=` / `TZ=Asia/Shanghai` 样例。
- `deploy/README.md`：NAS 侧操作步骤（SSH、建目录、放 .env、compose up）。

NAS 侧操作（通过 SSH jonah@192.168.1.124，sudo 执行，部署时进行）：

1. 确认群晖已安装 Container Manager（或 docker + docker-compose 可用），确认 CPU 架构（`uname -m`，决定拉 amd64 还是 arm64 镜像，CI 双架构均会产出）。
2. `mkdir -p ~/panwatch && cd ~/panwatch`，放 `docker-compose.yml` 和 `.env`（真实密码只存在这里）。
3. `docker compose up -d`，验证：`docker compose ps` 两个容器 healthy、`curl localhost:8000/api/health` 返回正常、局域网访问 `http://192.168.1.124:8000` 可登录。
4. 首次启动后台下载 Chromium（数百 MB），观察日志确认完成。

产出：新增文件清单 + NAS 验证结果（健康检查、登录页截图或 curl 输出）。

### 阶段 3 · 端到端自动化验证

1. 本地跑 `make test` 通过后，打一个真实 tag（如 `0.3.0`）push。
2. 观察 release.yml 全绿、Docker Hub 出现新 latest。
3. 5 分钟内观察 NAS：`docker logs watchtower` 应显示发现新镜像并 recreate；`docker exec panwatch cat /app/VERSION` 应等于新 tag。
4. 验收标准：**从 push tag 到 NAS 跑上新版本，全程零手工操作**。

产出：端到端验证记录（CI 链接 + watchtower 日志摘录 + 版本号对比）。

### 阶段 4 · 运维加固（可选，后置）

- 数据备份：群晖 Hyper Backup 或 cron 定期打包 `panwatch_data` 卷。
- 部署通知：release.yml 已有 Telegram 通知半成品（`TELEGRAM_*` secrets），需要则启用。
- Watchtower 升级失败告警（可接 shoutrrr 通知，后续需要再加）。

## 四、待所有者确认的剩余事项

1. **你的 Docker Hub 用户名**（阶段 1 改镜像名必需）。
2. 阶段 1 的 3 处镜像名修改是否批准（改完即触发门禁提交变更清单）。
3. NAS 侧执行时点：阶段 2 需要我通过 SSH 在群晖上执行 docker 命令（使用你提供的 jonah/root 凭据，sudo 提权），请确认授权我直接操作。
4. NAS 上 8000 端口是否空闲（若被占，告诉我改用哪个宿主机端口）。

## 四点五 · 验证记录（2026-07-22 01:05，子代理实测）

### NAS 环境侦察（192.168.1.124，只读）

- ✅ 群晖 DS923+，DSM 7.3.1，**x86_64**（用 amd64 镜像）
- ✅ Docker 24.0.2 + compose v2.20.1 可用；**docker CLI 在 `/usr/local/bin/docker`，jonah 默认 PATH 没有**，脚本需用全路径
- ✅ 内存 31Gi（可用 22Gi）、/volume1 剩余 8.2T、**8000 端口空闲**
- ✅ jonah ∈ administrators，**可免 sudo 直接跑 docker**
- ✅ `~/panwatch` 不存在，干净可建
- ⚠️ **遗留容器 `panwatch_pro`**（镜像 sunxiao0721/panwatch:latest，Exited 137）——部署前需决策：废弃删除 or 保留，避免数据卷混淆
- ❌ **【阻塞项】NAS 当前无外网连通**：Docker Hub / 百度 / 114 DNS 全部超时，DNS 疑似被污染，daemon.json 配置的加速域名 `docker.chentnt.com` 也不通。但 31 小时前曾成功拉取镜像，疑似凌晨时段断流/路由管控。**部署与 watchtower 均依赖出网，需在出网正常窗口执行或先修网络**

### 镜像名修改点验证（grep 全仓，共 16 处引用）

- 必须改 3 处（与已知一致）：`build.sh:12`、`release.yml:26`、`update_checker.py:178`（后者有 `UPDATE_CHECK_DOCKER_REPO` 环境变量可应急覆盖）
- 建议顺带改：`README.md` 4 处（badge + 安装示例）、`AGENTS.md:20`——否则用户照文档装到的是原作者镜像，与升级检测指向脱节
- 新发现：`frontend/src/App.tsx:103,296` 前端"发现新版本"fallback URL 硬编码上游 `github.com/sunxiao0721/PanWatch/releases`，**升级链路自洽性需要一并改**（待所有者批准，超出原三处范围）
- 不应改：`LICENSE`（原作者版权声明）、`docs/` 历史记录
- release.yml 需要的 secrets：`DOCKERHUB_USERNAME`、`DOCKERHUB_TOKEN`（必需），`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`（可选）
- 版本号策略待定：fork 首个 tag 若低于上游版本号，老容器升级检测会失效（建议延续上游编号）

## 六 · 部署完成记录（2026-07-22 12:40，阶段 1–3 全部验收通过）

- 提交 `4cd4a5a`（镜像名迁移 + deploy/ 目录），已推送 tntchen/PanWatch main。
- GitHub Secrets：`DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN`（Read & Write PAT）已配置。首个 PAT 为只读导致首次发布 push 401，换写权限 token 后成功（教训：Docker Hub PAT 必须选 Read & Write）。
- 镜像：`chentnt/panwatch:0.3.0` + `:latest`（amd64+arm64），CI run 29890762246 success，仓库由 API 创建为 public。
- NAS（192.168.1.124）`/volume1/homes/jonah/panwatch/`：docker-compose.yml + .env（仅 TZ）；**继承旧数据卷 `panwatch_data`**，登录账号/自选股等配置保留。
- 容器状态：`panwatch`（healthy，版本 0.3.0，首次启动自动安装 Chromium 约 3 分钟）+ `watchtower`（healthy，5 分钟轮询，只盯 panwatch）。
- 验证：`curl http://localhost:8000/api/health` → `{"code":0,"data":{"status":"ok"}}`；局域网 `http://192.168.1.124:8000/` → HTTP 200；股票列表缓存正常加载（A股 5540 / 港股 4694 / 美股 13672 / 北交所 6846）。
- 日常使用：改代码 → 本地测试 → `git tag x.y.z && git push origin x.y.z`（或 Actions 手动触发）→ CI 测试+构建+推送 → NAS 5 分钟内自动更新，全程零手工。

## 五、风险与注意事项

1. **latest 追新的代价**：CI 测试虽兜底，但"测试通过 ≠ 业务可用"。若某次想冻结版本，把 compose 的 `image:` 改为具体版本号即可脱离自动更新。
2. **首次启动慢**：容器内下载 Chromium，NAS 出网慢可在 `.env` 加 `HTTP_PROXY`。
3. **群晖架构**：新款群晖多为 x86_64（amd64 镜像），部分 ARM 机型用 arm64——阶段 2 第一步确认。
4. **SQLite 单容器**：不做高可用；数据即卷，备份即备份卷。
5. **端口约定**：容器内 8000 不可改，宿主机映射可调。
