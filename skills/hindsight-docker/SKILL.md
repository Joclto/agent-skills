---
name: hindsight-docker
description: Deploy and operate a centralized Hindsight memory bank with Docker Desktop (vchord variant, full-dimension embeddings, multi-device session backfill).
version: 1.5.3
author: "刘飞天 (Joclto), Hermes Agent"
license: MIT
platforms: [windows]
metadata:
  hermes:
    tags: [hindsight, memory, docker, vchord, embedding, xedge, overlay-network]
    category: productivity
---

# Hindsight 中央记忆库 · Docker Desktop 部署与运维

一台 Windows 主机常驻 Hindsight（API 8888 + 官方 Control Plane 9999 + vchord PostgreSQL），
多台设备的 Hermes 经内网组网（xEdge，网段 100.64.0.0/10）指向同一记忆库，共享一个 bank。

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
- 多设备历史会话一次性回灌：`assets/feed_sessions.py`（v3）在每台设备上运行一次，
  把该机 Hermes 的完整对话历史（state.db）投喂进中央库，服务端后台慢慢提炼。
  每片带固定 `document_id`，重跑零新增；**已在实时镜像的机器必须加 `--before`
  <接入时刻，精确到分>**，否则接入之后的会话镜像、回灌各进一份。

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
6. **防火墙**：裸机模式靠端口级放行规则把 8888/9999 限在组网网段（xEdge `100.64.0.0/10`）。Docker Desktop
   模式下这两条放行规则是冗余的（监听进程是 Docker 自带的 `com.docker.backend.exe`，由它自带的任意地址
   入站放行规则放行），而「仅组网可达」**不自动成立**：实测非组网来源（WSL 网段）也能访问 8888，
   而 API 无鉴权。Docker 模式的正确形态 = 不建端口级放行规则 + 两条 Block 规则（已在中央主机落地并
   三向验证通过：回环正常、成员机可访问、WSL 来源超时；原有的端口级放行规则 2026-11-26 删除，
   删前删后非组网对照一致，组网方向删后也观察到成员机实连与入库），见 deploy-guide 第五节。判断「某条规则还要不要」是独立问题：读实时规则表
   加监听进程归属，别按规则名或历史推断。
7. **整合队列停滞（思考模型 × 单次超时）**：`/health` 全绿但 pending 不动、日志刷
   `Connection error (HTTP None)`——GLM 强制思考默认 max 档，重的整合调用超过 120s
   单次尝试上限。保 max 的终案是思考时间不设上限：
   `HINDSIGHT_API_CONSOLIDATION_LLM_TIMEOUT` 取 24h（该链路无「无上限」哨兵，删键/填 0
   都回退 120s）+ 显式钉 `…_REASONING_EFFORT: "max"`，批大小保持 8，唯一兜底是 7200s
   空闲上限。同一事实簇单条就能想 238s、8 条批 546s——按分位数定档（240/300s）或
   缩批到 4 都兜不住。failed 项不会自动重试，需 SQL 重排。详见 deploy-guide 第七节。
8. **固定 `HINDSIGHT_API_WORKER_ID`**：默认取容器 hostname、重建即变，硬崩溃/断电后
   遗留在 processing 的 op 永不回收，整合队列无声卡死（启动日志有同名告警）。
9. **retain 也要放开超时**：retain 提炼默认沿用全局 `HINDSIGHT_API_LLM_TIMEOUT`（120s），
   长文档单次思考超时后空烧重试梯子，重试耗尽**整篇不落库**（回灌 13 个会话丢了 2 个）。
   同样无哨兵：`HINDSIGHT_API_RETAIN_LLM_TIMEOUT: "86400"`；单篇实际封顶是 retain 墙钟
   `HINDSIGHT_API_RETAIN_WALL_TIMEOUT`（默认 3600s，**不随进度延长**，从认领起算）是防卡死兜底，但大批量回灌期间会误杀：
   全局 LLM 并发只有 2 且与整理共用，单片光排队就超过一小时（实测一次 181 片死 33 片），
   回灌期一并设为 `"86400"`（重建容器才生效，在跑的 op 自动回到 pending 再认领；回灌结束后
   改回）。慢在 LLM 提炼而不是嵌入向量化（`[retain-timing]` 行里嵌入只占 2–6%）。
   failed 的 retain 不会自动重试：优先用官方重试端点
   `POST /v1/default/banks/<bank>/operations/<id>/retry` 从已提交分块续跑（不重复提炼），
   回灌脚本直接重跑也行（固定 id，已入库的片是空操作），手工补投
   必须沿用原 `document_id`。详见 deploy-guide 第八节。
10. **后台看不出记忆来自哪台机器**：Control Plane 的列表/详情没有来源列，写入时也不传主机名。
    只有库里的 `memory_units.metadata->>'user_id'`（Hermes 桌面端 = `sid:<Windows 账户 SID>`）
    能区分设备，须查库。见 deploy-guide 第四节「来源设备」。
11. **回灌去重只在同一个 `document_id` 内**（块 `content_hash`，只含正文）：同一段正文不带 id
    或换个 id 再投就是第二份（临时库实测 +41 单元，文档 1→2），整合层 0.97 的语义去重只是兜底。
    回灌必须带稳定 id，同一台机器不与 v2 混跑；去重类结论一律在临时库里验证
    （`assert BANK != 'hermes'`，跑完删库）。见 `references/migration-backfill.md` §B。
