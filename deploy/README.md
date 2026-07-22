# PanWatch 群晖 NAS 部署指南

本目录提供一份开箱即用的 `docker-compose.yml`，包含 PanWatch 本体和 Watchtower 自动更新服务，适合在群晖 NAS 上长期运行。

## 部署步骤

### 1. SSH 登录群晖

```bash
ssh <你的用户名>@<群晖IP>
```

> 提示：群晖上 docker CLI 的全路径为 `/usr/local/bin/docker`，以下命令均使用该全路径；如果你的环境已配置好 PATH，也可以直接用 `docker`。

### 2. 创建部署目录

```bash
mkdir -p ~/panwatch
cd ~/panwatch
```

### 3. 上传配置文件

把本目录下的两个文件上传到 `~/panwatch`：

- `docker-compose.yml`
- `.env`（由 `.env.example` 复制而来，填写好 `AUTH_USERNAME` 和 `AUTH_PASSWORD`）

本地可以先执行：

```bash
cp .env.example .env
# 编辑 .env 填入账号密码
```

再用 scp 或群晖 File Station 上传：

```bash
scp docker-compose.yml .env <你的用户名>@<群晖IP>:~/panwatch/
```

### 4. 启动服务

```bash
cd ~/panwatch
/usr/local/bin/docker compose up -d
```

### 5. 验证运行状态

```bash
# 查看容器状态（应看到 panwatch 和 watchtower 都在运行）
/usr/local/bin/docker compose ps

# 检查后端健康接口
curl http://localhost:8000/api/health
```

然后在浏览器访问 `http://<群晖IP>:8000`，用 `.env` 中设置的账号密码登录。

## 自动更新说明

compose 中的 Watchtower 服务每 5 分钟（`WATCHTOWER_POLL_INTERVAL: 300`）检查一次 `chentnt/panwatch:latest` 是否有新镜像；有新版本时会自动拉取并重建 panwatch 容器，旧镜像自动清理（`WATCHTOWER_CLEANUP: "true"`）。数据保存在 named volume `panwatch_data` 中，更新不会丢数据。

## 回滚方法

如果某个新版本有问题，需要固定回旧版本：

1. 编辑 `docker-compose.yml`，把镜像改为具体版本号，例如：

   ```yaml
   image: chentnt/panwatch:0.2.3
   ```

2. 重新启动：

   ```bash
   /usr/local/bin/docker compose up -d
   ```

3. （可选）为避免 Watchtower 又把容器升回 latest，回滚期间可暂停 watchtower：

   ```bash
   /usr/local/bin/docker compose stop watchtower
   ```
