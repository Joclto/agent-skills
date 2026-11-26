# Hindsight Docker 部署指南（vchord 变体 · Windows Docker Desktop）

> 在一台 Windows 主机上部署多设备共享的 Hindsight 中央记忆库。
> 所有密钥用 `<占位符>` 表示，实际值放 `.env`（不入库不入仓）。

## 前置条件

1. **Docker Desktop**（WSL2 后端）已安装并运行
2. 一个 LLM API key（提取用；国内智谱 Coding Plan 走 `https://open.bigmodel.cn/api/coding/paas/v4`，
   key 必须带 `/api/coding/` 路径）
3. 内网组网（可选，多设备远程接入时；下文以 xEdge 为例，其网段为 `100.64.0.0/10`）

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
   API 填 `http://<host xEdge IP>:8888`，bank 填 `hermes`，无需 key。

**来源设备**：Control Plane 的列表和详情都没有「来源机器」一栏（写入时不传主机名），
要知道某条记忆来自哪台设备只能查库——`memory_units.metadata->>'user_id'`（Hermes 桌面端
写入的是 `sid:<Windows 账户 SID>`，每台 Windows 安装不同），会话 id 见
`metadata->>'session_id'` 或标签 `session:<id>`：

```sql
SELECT metadata->>'user_id' AS device, count(*) FROM memory_units
WHERE bank_id='hermes' GROUP BY 1;
```

## 五、防火墙（远程可达性）

```powershell
# 管理员（裸机模式才需要；Docker 模式下是冗余的，见下）：8888/9999 仅放行组网网段（xEdge：100.64.0.0/10）
New-NetFirewallRule -DisplayName "Hindsight API (xEdge only)" -Direction Inbound -Protocol TCP -LocalPort 8888 -RemoteAddress 100.64.0.0/10 -Action Allow
New-NetFirewallRule -DisplayName "Hindsight UI (xEdge only)" -Direction Inbound -Protocol TCP -LocalPort 9999 -RemoteAddress 100.64.0.0/10 -Action Allow
```
Docker 发布端口走宿主端口规则，同规则对容器映射生效。
**Docker Desktop 模式下这两条端口级放行规则是冗余的**：8888/9999 的监听进程是 Docker 自带的
`com.docker.backend.exe`，组网流量靠它自带的入站放行规则（见下）放行。中央主机上原有的端口级放行规则
已作为冗余删除（2026-11-26；只删这一条，按显示名 + 规则 ID 双匹配，防误删同名新规则），删前删后
实测一致：回环正常、非组网来源仍超时。组网方向未单独复测——放行依据是 Docker 自带规则，它没有改动。
Docker 模式只需要下面的 Block 规则；两条放行规则留着也不出错，只是不起作用。

**Docker Desktop 下「仅组网可达」并不自动成立**（实测）：Docker Desktop 自带一条
`com.docker.backend.exe` 的入站放行规则（任意地址，Private/Public 配置文件），它管的是
转发进程而不是端口，会架空上面两条端口规则——非组网来源（实测：WSL 虚拟网段）同样能访问
8888，而 API 没有鉴权。要真正收紧，显式阻断组网网段以外的来源（Block 规则优先于 Allow）。
下面两条已在中央主机实机落地并验证（2026-11-24），在其它机器上加完同样必须做三向验证：

```powershell
# 管理员：8888/9999 拒绝来自组网网段以外的所有地址（IPv4 两段 + IPv6 全量）
$deny = "0.0.0.0-100.63.255.255","100.128.0.0-255.255.255.255","::-ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff"
New-NetFirewallRule -DisplayName "Hindsight API (block non-overlay)" -Direction Inbound -Action Block -Protocol TCP -LocalPort 8888 -RemoteAddress $deny
New-NetFirewallRule -DisplayName "Hindsight UI (block non-overlay)" -Direction Inbound -Action Block -Protocol TCP -LocalPort 9999 -RemoteAddress $deny
```

三向验证：本机 `curl http://127.0.0.1:8888/health` 仍 healthy（回环不受防火墙管）、
组网设备仍可访问、非组网来源连接超时。任一项不符就 `Remove-NetFirewallRule -DisplayName
"Hindsight*block non-overlay*"` 回退。

实测记录（中央主机，2026-11-24）：回环 200；同一 WSL 来源对 8888/9999 超时，而对宿主 7680 端口
（有监听且被放行）秒回——路径本身是通的，丢包来自 Block 规则。**别拿无监听端口当对照**：
Windows 防火墙隐身模式对没人监听的端口同样超时。组网方向随后在另一台成员机上实测通过
（`curl http://<host xEdge IP>:8888/health` 返回 healthy），主机侧 TCP 表也观察到成员机到 9999
的实连（`Get-NetTCPConnection -LocalPort 8888,9999` 里来源为 `100.x`、状态 Established）——三向
全部成立，规则保留。一次性请求不会在主机侧留下 TCP 痕迹（先关连接的是客户端），要在对端
执行的那一刻轮询才抓得到。

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

## 七、整合队列停滞：GLM 思考模型 × 单次超时（终案：思考不设上限）

**症状**：`/health` 全绿、嵌入调用秒回，但 `pending_consolidation` 小时级不动，
日志持续刷 `Connection error (HTTP None), attempt N`。根因：GLM-5.3 系**强制思考不能关**
（`thinking.type=disabled` 被端点静默降为 ≈low、`enable_thinking=false` 直接 400/1210、
`reasoning_effort` 只有 low/high/max 且默认 **max**），而整合单次尝试默认超时只有 120s——
max 档重的整合调用动辄数百秒，死在超时上的尝试整次作废，重试梯子（4 次尝试 × 3 轮外层）
再把空耗放大十几倍。

**实测**（GLM-5.3-flash @ bigmodel coding 端点，effort=max；探针为生产同形只读——真实待整合
事实、k=1/2/4/8 条/批、无 max_tokens、temperature 0、共享观察池）：

| 场景 | 单次耗时 |
|---|---|
| 21 组真实待整合批（21/21 HTTP 200 零 429） | p50 59s / p90 143s / p95 198s / max 254s |
| 同会话事实簇 4 条同批（生产日志） | >300s（300s 处连败） |
| 同簇**单条事实**（文本仅 146 字符） | **238s**（13.9k 思考 token @60 tok/s） |
| 终案上线后首个生产批（8 条） | **546s**，正常提交 |

要点：思考耗时跟「事实条数 × 观察池交叉比对」走，与事实文本长短无关；同簇多条同批只会更长。
**任何有限的单次超时都兜不住合法的长思考**——按分位数定档（120→240→300s）只是把失败点
往后挪，批缩到 4 条同样不够。

**终案（质量优先：保 max、思考时间不设上限）**——compose 的 `environment` 加四行：

```yaml
    environment:
      # 24h = 单次调用超时实际永不触发。该链路没有「无上限」哨兵：删键回退全局
      # HINDSIGHT_API_LLM_TIMEOUT(120s)，填 0 因 falsy 判断同样回退
      HINDSIGHT_API_CONSOLIDATION_LLM_TIMEOUT: "86400"
      # 显式钉死：端点默认就是 max，钉住是防上游改默认值
      HINDSIGHT_API_CONSOLIDATION_LLM_REASONING_EFFORT: "max"
      # 批大小保持默认 8：超时不再是约束后，缩批只换来 LLM 调用翻倍
      HINDSIGHT_API_CONSOLIDATION_LLM_BATCH_SIZE: "8"
      # 默认取容器 hostname、每次重建都变 → 硬崩溃后 processing 的 op 永不回收
      HINDSIGHT_API_WORKER_ID: hindsight-app
```

- **唯一兜底**是整合空闲上限 `HINDSIGHT_API_CONSOLIDATION_WALL_TIMEOUT`（默认 7200s）：连续 2h
  没有**已提交批次**才判停滞并取消。它是卡死探测器，不是慢批上限；无超时调用遇到死连接时
  它是唯一出口，不要关。
- 取舍：病态批要跑完模型愿意想的全部时间才出结果，期间占着该 bank 唯一的整合 op；排空时间
  不可预测，但不再空烧重试。
- 另一条路是降档 `HINDSIGHT_API_CONSOLIDATION_LLM_REASONING_EFFORT: "low"`（重的整合调用约
  20× 提速；质量有损需自测对比，retain 提取建议保持默认）。

`docker compose up -d` 重建生效，容器内读回验证：

```powershell
docker exec hindsight-app python -c "from hindsight_api.config import get_config; c=get_config(); print(c.consolidation_llm_timeout, c.consolidation_llm_reasoning_effort, c.worker_id)"
# → 86400.0 max hindsight-app
```

批大小是 **bank 可配字段**，`get_config()` 直接读会报 `ConfigFieldAccessError`，要按 bank 解析：

```python
import asyncio
from hindsight_api.config import get_config
from hindsight_api.config_resolver import ConfigResolver
async def main():
    c = await ConfigResolver(get_config()).resolve_full_config('hermes', 'consolidation')
    print(c.consolidation_llm_batch_size)      # → 8
asyncio.run(main())
```

**生效判据（看行为不看配置）**：`llm_batch #N (8 memories, 1 llm calls) | processed=X/Y | … llm=…s`
正常提交、`Connection error` 归零。`[WORKER_TASK]` 行的
`stage=llm.<provider>.consolidation.attempt=1/4` 越过旧超时点仍停在 attempt 1，即证明超时已不
触发。stage_age ≥300s 后该行会带 `[STUCK?]` 前缀，并按 300/600/1200s…翻倍阈值各打一次
`[STUCK_STACK]` 栈转储——纯诊断标签，无动作，长思考批出现属预期。

**配套运维点**：

- **改批大小/超时后要开新轮**：参数在整合轮次入口读取，处理中的 op 续跑旧形状的批。取消用
  `DELETE /v1/default/banks/<bank>/operations/<op_id>`（协作式：行立即标 cancelled，worker 在
  下一检查点停；带 `/delete` 后缀的路由只收终态 op），poller 随后自动以新参数建新 op。
- **retain 同理**：默认也沿用全局 120s，长文档写入会整篇失败——同样放开，见第八节；
  reflect 仍用默认值。

**失败项重排**：failed 行**永远不会自动重试**，也没有 HTTP 接口——根因修好后，用与引擎
`requeue_failed_consolidation` 等价的 SQL（先数、再清、后验证）：

```sql
SELECT count(*) FROM memory_units
 WHERE bank_id='hermes' AND consolidation_failed_at IS NOT NULL
   AND fact_type IN ('experience','world');          -- 修复前先记数

UPDATE memory_units SET consolidation_failed_at = NULL, consolidated_at = NULL
 WHERE bank_id='hermes' AND consolidation_failed_at IS NOT NULL
   AND fact_type IN ('experience','world');
```

## 八、retain 超时：长文档写入整篇失败（同样放开）

**症状**：回灌历史会话时个别分块最终 `failed`，错误
`Task exceeded the 3600s wall-clock limit for 'batch_retain'`，此前日志里零星
`retain_extract_facts` 超时告警。根因同第七节：retain 提炼走同一个强制思考的 GLM，而 retain
默认沿用全局 `HINDSIGHT_API_LLM_TIMEOUT`（120s）——长文档单次思考超时后空烧重试梯子，
重试耗尽、3600s 墙钟到期，**整篇不落库**。

**修复**：compose 的 `environment` 再加一行（同样无哨兵，删键/填 0 都回退 120s）：

```yaml
      HINDSIGHT_API_RETAIN_LLM_TIMEOUT: "86400"
```

- 单篇实际封顶是 retain 墙钟 `HINDSIGHT_API_RETAIN_WALL_TIMEOUT`（默认 3600s，**不随进度延长**，
  这点与整合的 7200s 空闲上限不同），从认领起算。它是防卡死兜底：卡死的 op 否则永远占着
  worker 槽位（保持 `processing`，既不能重试也不能取消）。小流量下留着即可，但大批量回灌
  期间必须一并放开，见下文「回灌期间的墙钟」。
- `docker compose up -d` 重建后读回：`get_config().retain_llm_timeout` → `86400.0`。
- 实测：此前在 120s 下重试耗尽而失败的两个分块（3012 / 10806 token），放开后重新投喂
  94s / 265s 完成。

**回灌期间的墙钟（2026-11-26 改判）**：全局 LLM 并发 2（`HINDSIGHT_API_LLM_MAX_CONCURRENT`）
由 retain、整理、反思共用，worker 槽位数不是 LLM 并行度——8 个 op 同时 `processing`，真正
在发的请求只有 2 路。单片要发多次提炼调用（实测平均 4 次、最多 7 次），而且是**一起发出**排队，不是发完一个再发下一个：
单次调用单独跑只要 30–80 秒，排队后中位耗时 15–20 分钟；一片从认领到完成 20 分钟到 1.5 小时不等，吞吐约每小时 8–12 片；默认 3600s 墙钟从认领起算、不随进度延长，一次 181 片的回灌被
它杀掉 33 片。修复 = compose 再加一行并重建容器（在跑的 op 自动回到 pending 重新认领，不丢
数据；回灌结束后改回 3600，同样要重建）：

```yaml
      HINDSIGHT_API_RETAIN_WALL_TIMEOUT: "86400"
```

重建后读回 `get_config().retain_wall_timeout` → `86400.0`。验证看行为：重建前认领的 op 越过
3600s 仍是 `processing`，日志里 `wall-clock limit` 为 0 条。代价是防卡死兜底从 1 小时变成 24
小时——真卡死的 op 会占着槽位更久，所以只在回灌期开。

**慢在提炼，不在嵌入**：每个完成的 op 有一行 `[retain-timing]`，`wall=` 是总耗时、`embed=`
是本地嵌入耗时，实测 2010s/47s、3001s/195s、2727s/130s（嵌入占 2–6%）；按 `llm_requests`
的 scope 汇总，`retain_extract_facts` 约占 LLM 时间 98%，`embedding_document` 约 2%——换更
快的嵌入模型没有意义。`llm_requests.started_at` 在排队等并发许可之前就落库（14 条请求同一个
起始秒，而许可只有 2 个），拿它算并发会高估；反过来，同一片的多次调用起始时间几乎相同（实测 45 片里 38 片的全部调用在 2 秒内发出）正说明它们都在排队。判据：独占许可时单次提炼调用只要 30–80 秒——慢的是排队，不是模型。日志里成片的 `APIStatusError ... HTTP 429
{"code": "1302"}` 是排队中的首次尝试快速失败触发的供应商限流，默认退避（≤60s）后自动恢复，
真正的代价是退避完还要重新排队；stage 显示 `...backoff` 或 `...structured.queued` 都是在
排队，不是卡死。能提速的只有降提炼推理档位或调大全局并发，前者动质量、后者触发限流
（并发 32 时该供应商直接回 429），均未采纳。

**失败分块补投**：failed 的 retain op 不会自动重试。优先用官方重试端点续跑——
`POST /v1/default/banks/<bank>/operations/<operation_id>/retry`，返回 200 即从已提交分块续跑，
不重复提炼（对子 op `retain` 重试，`batch_retain` 外层行保持 `failed` 不用管）；回灌脚本（v3）
投的直接重跑同一条命令也行——
`document_id` 固定，已入库的片是空操作（0 token），实际只提炼失败的那片。手工补投：从
`async_operations.task_payload` 取出该分块正文**连同原 `document_id`** 重新 POST
（新 op id，文档仍是原来那个；不带 id 或换新 id 都会重复入库，旧 failed 记录留着不影响）：

```python
import requests
requests.post("http://127.0.0.1:8888/v1/default/banks/hermes/memories",
              json={"items": [{"content": content, "context": context, "tags": tags,
                               "document_id": document_id}],
                    "async": True})
```
