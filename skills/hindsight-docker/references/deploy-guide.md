# Hindsight Docker 部署指南（vchord 变体 · Windows Docker Desktop）

> 在一台 Windows 主机上部署多设备共享的 Hindsight 中央记忆库。
> 所有密钥用 `<占位符>` 表示，实际值放 `.env`（不入库不入仓）。

## 前置条件

1. **Docker Desktop**（WSL2 后端）已安装并运行
2. 一个 LLM API key（提取用；国内智谱 Coding Plan 走 `https://open.bigmodel.cn/api/coding/paas/v4`，
   key 必须带 `/api/coding/` 路径）
3. Tailscale（可选，多设备远程接入时）

## 一、目录与配置

建部署目录（例：`%USERPROFILE%\.hindsight\docker\`），放两个文件：

### docker-compose.yaml

见 `assets/docker-compose.yaml`（与本指南同版本）。要点：
- `db`：`tensorchord/vchord-suite:pg18-latest`，**不对外发布端口**
- `vectorchord-init`：一次性建扩展（vchord / pg_tokenizer / vchord_bm25 / llmlingua2 分词器）
- `hindsight`：`ghcr.io/vectorize-io/hindsight:latest` 一体镜像，映射 `8888:8888` 与 `9999:9999`
  （**容器内 Control Plane 监听 9999，不是 3000**）
- 卷：`pg_data`（数据库）+ `hf_cache`（嵌入模型缓存，镜像更新不重下）

### .env（真实密钥，只放本机）

```ini
HINDSIGHT_DB_USER=hindsight_user
HINDSIGHT_DB_NAME=hindsight_db
HINDSIGHT_DB_PASSWORD=<生成一个随机密码>
HINDSIGHT_API_LLM_API_KEY=<你的 LLM key>
```

## 二、国内网络两个关键点

1. **Docker Hub 拉取不通**（vchord-suite 在 docker.io）：给 Docker Desktop 配代理——
   关闭 Docker Desktop 后编辑 `%APPDATA%\Docker\settings-store.json`：
   ```json
   "ProxyHttpMode": "manual",
   "OverrideProxyHttp": "http://127.0.0.1:<代理端口>",
   "OverrideProxyHttps": "http://127.0.0.1:<代理端口>",
   "OverrideProxyExclude": "localhost,127.0.0.1,::1"
   ```
   再启动 Docker Desktop。ghcr.io 通常可直连，无需代理。
2. **嵌入模型下载**：compose 已设 `HF_ENDPOINT=https://hf-mirror.com`；首次启动下载
   模型（4B 约 8GB），`HINDSIGHT_API_MODEL_INIT_TIMEOUT=3600` 放宽初始化等待。

## 三、启动与验证

```powershell
cd <部署目录>
docker compose pull
docker compose up -d
# 等 1-3 分钟（首次含模型下载），然后：
curl http://127.0.0.1:8888/health          # {"status":"healthy","database":"connected"}
curl http://127.0.0.1:9999/api/health      # control-plane ok + dataplane connected
```

浏览器开 `http://127.0.0.1:9999` 即官方管理界面（跟随浏览器语言出中文）。

**维度自检**（用了 4B 的话应为 2560）：

```powershell
docker exec hindsight-db psql -U hindsight_user -d hindsight_db -t -c ^
 "SELECT atttypmod FROM pg_attribute a JOIN pg_type t ON a.atttypid=t.oid WHERE t.typname='vector' AND a.attrelid='memory_units'::regclass AND a.attname='embedding';"
```

**常见首启故障**：retain 报 500 `Tokenizer not found: llmlingua2` = init 竞态，手动补一刀：
```powershell
docker exec hindsight-db psql -U hindsight_user -d hindsight_db -c "SELECT create_tokenizer('llmlingua2', $$ model = 'llmlingua2' $$);"
```

## 四、建库与多设备接入

1. 中央机 retain 一条种子自动建库（bank_id 例 `hermes`）：
   `POST /v1/default/banks/hermes/memories` body `{"items":[{"content":"种子"}]}`
2. 各设备 Hermes：设置 → 插件 → 安装 hindsight → Memory 提供商选 local_external →
   API 填 `http://<中央机Tailscale IP>:8888`，bank 填 `hermes`，无需 key。

## 五、防火墙（远程可达性）

```powershell
# 管理员：8888/9999 仅放行 Tailscale 网段（100.64.0.0/10）
New-NetFirewallRule -DisplayName "Hindsight API (TS only)" -Direction Inbound -Protocol TCP -LocalPort 8888 -RemoteAddress 100.64.0.0/10 -Action Allow
New-NetFirewallRule -DisplayName "Hindsight UI (TS only)" -Direction Inbound -Protocol TCP -LocalPort 9999 -RemoteAddress 100.64.0.0/10 -Action Allow
```
Docker 发布端口走宿主端口规则，同规则对容器映射生效。

## 六、日常运维

| 操作 | 命令 |
|---|---|
| 看日志 | `docker logs hindsight-app --tail 50` |
| 重启 | `docker compose restart` |
| **更新（API+UI+DB 同步）** | `docker compose pull` → `docker compose up -d` |
| 热备份份 | `POST /v1/default/banks/<bank>/transfer/export` → 轮询 operations → 下载 zip |
| 冷备 | `docker compose down` 后备份卷，或直接 pg_dump |

开机自启 = Docker Desktop 设置 AutoStart（默认开）+ 容器 `restart: unless-stopped`。

## 回滚

数据都在 named volume（`pg_data`/`hf_cache`），`docker compose down` 不删卷；
换回旧版本：`image: ghcr.io/vectorize-io/hindsight:<旧tag>` 再 `up -d`。
彻底重来：`docker compose down -v`（**会删数据，先导 transfer.zip**）。
