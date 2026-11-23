---
name: hindsight-docker
description: Deploy and operate a centralized Hindsight memory bank with Docker Desktop (vchord variant, full-dimension embeddings, multi-device session backfill).
version: 1.1.0
author: "刘飞天 (Joclto), Hermes Agent"
license: MIT
platforms: [windows]
metadata:
  hermes:
    tags: [hindsight, memory, docker, vchord, embedding, tailscale]
    category: productivity
---

# Hindsight 中央记忆库 · Docker Desktop 部署与运维

一台 Windows 主机常驻 Hindsight（API 8888 + 官方 Control Plane 9999 + vchord PostgreSQL），
多台设备的 Hermes 通过 Tailscale 指向同一记忆库，共享一个 bank。

## 适用场景

- 多设备 AI 助手需要共享长期记忆（跨设备召回同一知识库）
- 想用 4B 级嵌入模型的原生全维度（2560 维）——pgvector HNSW 有 2000 维硬限，vchord 没有，
  而 vchord 无 Windows 二进制，**Docker 是 Windows 上唯一路径**
- 想要官方全家桶管理界面（Next.js Control Plane）且与 API 版本同步更新

## 架构（一份 compose = 全部）

```
容器 hindsight-app（ghcr.io/vectorize-io/hindsight 一体镜像）
  ├─ 8888 记忆 API（recall / retain / transfer）
  └─ 9999 官方 Control Plane（中文 UI：记忆星座图/实体/操作流水/知识页）
容器 hindsight-db（tensorchord/vchord-suite:pg18-latest，仅内网可达）
一次性 vectorchord-init（建 vchord/pg_tokenizer/vchord_bm25 扩展后自动退出）
卷：pg_data（库）+ hf_cache（嵌入模型，镜像更新不重下）
```

日常只有一条管理命令：`docker compose pull && docker compose up -d`（API/UI/DB 版本永远同步）。

## 快速部署

见 `references/deploy-guide.md`（含国内网络两个关键点：Docker Hub 拉取需给 Docker Desktop
配代理、模型下载需 HF 镜像端点）。compose 文件在 `assets/docker-compose.yaml`。

## 数据迁移与会话回灌

- 裸机/旧库 → Docker：官方 transfer/export → import（merge），免费零 LLM token，
  全量记忆自动用新嵌入模型重嵌入。见 `references/migration-backfill.md`。
- 多设备历史会话一次性回灌：`assets/feed_sessions.py` 在每台设备上运行一次，
  把该机 Hermes 的完整对话历史（state.db）投喂进中央库，服务端后台慢慢提炼。

## 运维速查

```powershell
docker logs hindsight-app --tail 50                        # 日志
docker compose -f <目录>\docker-compose.yaml restart      # 重启
docker compose pull; docker compose up -d                 # 更新（API+UI+DB 同步）
# 备份（热备，3 秒导出整库为便携 zip，可跨版本恢复）：
#   POST http://127.0.0.1:8888/v1/default/banks/<bank>/transfer/export
#   → 轮询 operations → GET result_metadata.download_url 下载
```

## 关键坑（实战验证）

1. **容器内监听 9999 不是 3000**：`ghcr.io/vectorize-io/hindsight` 的 Control Plane 起在 9999，
   端口映射写 `9999:3000` 会"连接即断"。
2. **llmlingua2 tokenizer 建失败是竞态**：init 容器的 `create_tokenizer` 失败被
   `2>/dev/null` 吞掉，症状是 retain 报 500 `Tokenizer not found: llmlingua2`；
   手动重跑一次 `SELECT create_tokenizer('llmlingua2', ...)` 即好。
3. **Docker Hub（docker.io）国内直连不通**：给 Docker Desktop 配 HTTP(S) 代理
   （settings-store.json: `ProxyHttpMode=manual` + `OverrideProxyHttp/Https`），ghcr 可直连。
4. **Coding Plan 的 GLM key 必须带 `/api/coding/` 路径**，标准路径报 1113。
5. **导入前先建库**：transfer/import 的 URL bank 必须已存在（先 retain 一条种子）。
6. **防火墙**：8888/9999 入站规则按端口生效，对 Docker 发布端口同样有效，限 Tailscale 网段即可。
7. **整合队列停滞（思考模型 × 默认超时）**：`/health` 全绿但 pending 不动、日志刷
   `Connection error (HTTP None)`——GLM 强制思考默认 max 档，重的整合调用超过 120s
   单次尝试上限。两条路：降 `HINDSIGHT_API_CONSOLIDATION_LLM_REASONING_EFFORT`，
   或保 max 放宽 `HINDSIGHT_API_CONSOLIDATION_LLM_TIMEOUT`（实测 300s 兜住、240s 不够）；
   failed 项不会自动重试，需 SQL 重排。详见 deploy-guide 第七节。
