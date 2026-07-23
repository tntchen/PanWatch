# NAS 自动化部署通用 Playbook（Docker Hub + GitHub Actions + Watchtower + 群晖）

> 用途：把任意"单容器自托管 Web 应用"部署到群晖 NAS，并建立 `代码推送 → CI 构建镜像 → NAS 自动更新` 的零手工 DevOps 闭环。
> 本文档由 PanWatch 项目（2026-07-22）实战沉淀，写给其他项目的 AI/开发者：**照做即可，不要重新探索已解决的坑**。
> 阅读对象假设：你（AI）能访问项目仓库、拥有 GitHub CLI（gh）、NAS SSH 凭据、Docker Hub 账号。

---

## 0. 方案一句话

本地测试通过 → 手动触发（或 tag 触发）GitHub Actions 跑测试+构建多架构镜像推 Docker Hub → NAS 上 Watchtower 每 5 分钟轮询，发现新 `latest` 自动 pull + recreate → 容器自带健康检查兜底。

**核心设计原则**：CI 测试是门禁——测试不过镜像不推，NAS 追的 `latest` 必然是通过全部测试的版本。这就是"NAS 只跑最新可用版本"的实现方式。

---

## 1. 前置条件清单（动手前逐项确认）

| 项 | 说明 |
|---|---|
| 项目已有可用 Dockerfile | 多阶段构建、EXPOSE 端口、HEALTHCHECK、数据卷路径明确 |
| GitHub 仓库 + `gh` CLI 已认证 | `gh auth status` 确认，scopes 需含 `repo, workflow` |
| Docker Hub 账号 + **Read & Write** 权限的 Access Token | ⚠️ 见坑 #1 |
| NAS SSH 可达 | 确认 IP、端口（可能非 22）、用户可免 sudo 跑 docker |
| NAS 出网正常 | `curl -sI https://registry-1.docker.io/v2/` 返回 401 即通 |

---

## 2. NAS（群晖）环境侦察要点与已知坑

### 2.1 必查项（只读命令）

```bash
sshpass -p '<密码>' ssh -p <SSH端口> -o StrictHostKeyChecking=accept-new <用户>@<NAS_IP> '<命令>'
```

- 架构：`uname -m`（x86_64 → amd64 镜像；arm64 需 CI 出 arm 镜像）
- Docker：`/usr/local/bin/docker version`、`docker compose version`
- ⚠️ **坑 #2：群晖上 docker CLI 在 `/usr/local/bin/docker`，普通用户默认 PATH 没有**——所有脚本/命令用全路径
- 权限：用户若在 `administrators` 组可免 sudo 直接跑 docker
- 端口占用：`netstat -tlnp | grep :<端口>`
- 资源：`free -h`、`df -h /volume1`
- 同名残留容器/数据卷：`docker ps -a`、`docker volume ls`（部署前先决策：复用数据 or 删除）

### 2.2 群晖网络坑

- ⚠️ **坑 #3：可能存在定时断流**（如凌晨路由管控）。部署前实测出网；watchtower 依赖持续出网
- ⚠️ **坑 #4：群晖缺系统 CA 证书**（`/etc/ssl/certs/ca-certificates.crt` 不存在），裸 `curl` 报证书错，用 `curl -k` 测试即可；`docker pull` 走 daemon 自身证书链，不受影响
- ⚠️ **坑 #5：scp/sftp 可能不可用**（subsystem request failed）。用 SSH 管道写文件：
  ```bash
  sshpass -p '<密码>' ssh -p <端口> <用户>@<IP> 'cat > ~/app/docker-compose.yml' < local/docker-compose.yml
  ```
- ⚠️ **坑 #6：长时间 pull 会被本地 SSH 超时杀掉**。后台拉取再轮询：
  ```bash
  ssh ... 'cd ~/app && nohup /usr/local/bin/docker compose pull > /tmp/pull.log 2>&1 & echo started'
  # 之后反复 tail /tmp/pull.log 直到出现 Pulled
  ```
- 高频 SSH 可能触发临时拒绝（Permission denied）——sleep 20s 重试即可

---

## 3. 仓库侧改动（一次性）

### 3.1 镜像名迁移（fork 项目必做）

全局 `grep -rn '<原镜像名>'`，典型需要改的位置：

1. `build.sh` — `IMAGE_NAME=`
2. `.github/workflows/release.yml` — `env.IMAGE_NAME`
3. 应用内**更新检查器**的默认仓库（如 `update_checker.py`）——⚠️ 坑 #7：漏改则更新检查永远指向原作者镜像
4. README 安装示例、前端"发现新版本"跳转 URL——⚠️ 注意区分 **Docker Hub 用户名** 与 **GitHub 用户名**（实战案例：Docker Hub 是 `chentnt`，GitHub 是 `tntchen`，两者不同！前端 releases 链接要指向 GitHub 仓库）

### 3.2 GitHub Secrets

```bash
gh secret set DOCKERHUB_USERNAME -b '<dockerhub用户名>'
gh secret set DOCKERHUB_TOKEN  -b '<Read&Write的PAT>'
```

⚠️ **坑 #1（最高发）**：Docker Hub Personal Access Token 默认可能是 **Read-only**，推镜像时报 `401: access token has insufficient scopes`。必须建 **Read & Write** token。验证 token 写权限的方法：调 `POST /v2/repositories/` 建仓库，返回 `insufficient scope` 即只读。

### 3.3 发布触发方式

- `workflow_dispatch` 手动触发（最可靠）：
  ```bash
  gh workflow run release.yml -f version=x.y.z -f push_latest=true
  gh run list --workflow=release.yml --limit 1   # 确认已启动
  ```
- ⚠️ **坑 #8：`git push origin x.y.z` 推 tag 可能不触发流水线**（fork 仓库遇到过，原因未查明）。推完 tag 务必 `gh run list` 确认有 run 启动；没有就改手动触发。
- CI 构建耗时参考：pytest + amd64/arm64 双架构 QEMU 构建 ≈ 15-25 分钟。轮询：
  ```bash
  gh run view <run_id> --json status,conclusion --jq '"\(.status) \(.conclusion)"'
  ```

---

## 4. NAS 部署文件（标准模板）

`docker-compose.yml`（放 NAS `~/app/` 目录，同时提交一份到仓库 `deploy/`）：

```yaml
services:
  app:
    image: <dockerhub用户>/<镜像名>:latest
    container_name: app
    ports:
      - "8000:8000"          # 容器内端口不变，只调宿主机侧
    volumes:
      - app_data:/app/data   # 数据卷 = 全部状态（SQLite/配置/浏览器等）
    env_file: .env
    restart: unless-stopped

  watchtower:
    image: containrrr/watchtower:latest
    container_name: watchtower
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    environment:
      WATCHTOWER_POLL_INTERVAL: 300   # 5 分钟轮询
      WATCHTOWER_CLEANUP: "true"      # 自动删旧镜像
    command: app                       # 只盯 app 容器
    restart: unless-stopped

volumes:
  app_data:
```

`.env`（只存 NAS，**绝不进 git**；仓库只提交 `.env.example` 空模板）：

```bash
TZ=Asia/Shanghai
# AUTH_USERNAME= / AUTH_PASSWORD= 等按应用需要
```

⚠️ **坑 #9：compose 引用了 `env_file: .env` 则 .env 必须存在**，否则 up 报错。不需要预设变量时也要创建一个（可只含 TZ）。

⚠️ **坑 #10：机密不进仓库**。NAS 密码、Docker Hub token、应用密码只出现在 NAS 本地 `.env` 和 GitHub Secrets，计划文档/commit 里一律不写。

启动与验证：

```bash
cd ~/app && /usr/local/bin/docker compose up -d
docker compose ps                                  # 两容器 healthy
curl -s http://localhost:8000/api/health           # 按应用实际健康端点
docker exec app cat /app/VERSION                   # 按应用实际版本标识
```

---

## 5. 日常发布流程（部署完成后）

```
改代码 → 本地测试通过 → 提交推送 main
→ gh workflow run release.yml -f version=x.y.z -f push_latest=true
→ CI 测试+构建+推送（15-25 分钟）
→ watchtower 5 分钟内自动更新 NAS（docker logs watchtower 可见 Updated=1）
→ 验证：docker exec <容器> cat /app/VERSION == x.y.z 且 healthy
```

**回滚**：compose 里 `image:` 改成具体旧版本号（如 `:0.3.0`）→ `compose up -d`。注意：钉版本后容器跟踪该版本 tag，watchtower 不会再升级它；恢复追新改回 `:latest` 即可。

---

## 6. 事故案例库（最重要的避坑部分）

### 案例 A：只读 PAT 导致推送 401
症状：CI 测试、构建全过，push 报 `401 access token has insufficient scopes`。
处置：换 Read & Write PAT，更新 secret，重跑 workflow。

### 案例 B：数据迁移阻断存量库启动（0.4.0 真实事故）
症状：新版本在生产库启动即 crash-loop，日志显示迁移对账 raise。
根因：迁移脚本里写死了针对**开发验收库特定数据**的一致性锚点（某条具体持仓记录），触发条件是"表非空即强制核对"——任何数据不同的存量库都会被拒绝启动。
教训与规则：
1. **环境特定的数据锚点绝不能钉死在通用迁移里**，应改为环境变量显式开启（如 `MT_RECON_ANCHORS=1`），默认关闭；schema 级不变量（外键、NULL、孤儿行）保持无条件强制。
2. 涉及存量库迁移的改动，**发布前先在真实数据副本上预演**（把生产 db 拷到本地跑一遍启动）。
3. 上线后第一时间盯启动日志；失败立即回滚（钉旧版本 tag），恢复服务后再修复。
4. 迁移若有 checksum 机制（source hash 记账），修改已发布迁移会导致全库重跑——确保该迁移幂等/只读才安全。

### 案例 C：tag push 不触发流水线
症状：`git push origin 0.4.0` 成功但 `gh run list` 没有新 run。
处置：不纠结，直接 `gh workflow run` 手动触发（版本号参数等效）。后续可排查 fork 仓库 Actions 设置。

---

## 7. 给其他项目 AI 的执行顺序（checklist）

1. [ ] 读项目 Dockerfile/compose/CI，确认单容器自托管形态、端口、数据卷、健康端点
2. [ ] NAS 侦察（第 2 节）：架构、docker 全路径、免 sudo、端口空闲、出网、残留容器与卷
3. [ ] 仓库镜像名迁移（第 3.1 节，含更新检查器、README、前端链接；区分 Docker Hub vs GitHub 用户名）
4. [ ] 配 GitHub Secrets（确认 PAT 是 Read & Write）
5. [ ] 仓库新增 `deploy/`（compose + .env.example + README），提交推送
6. [ ] 手动触发首次发布，等 CI success（15-25 分钟）
7. [ ] NAS 放 compose + .env，后台 pull，`up -d`，验证 healthy + 版本号 + 健康端点
8. [ ] 做一次真实版本发布，验证 watchtower 自动更新闭环（Updated=1、版本号变化）
9. [ ] 盯启动日志至少一个升级周期；有迁移的先在数据副本预演
10. [ ] 收尾：通知所有者；提醒删除旧的只读 token；记录事故与结论到项目文档
