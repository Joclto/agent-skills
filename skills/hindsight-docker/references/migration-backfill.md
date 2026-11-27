# 数据迁移与会话回灌

## A. 旧库 → Docker 新栈（transfer 迁移）

适用：从裸机 hindsight-api / 旧实例 / 其他机器把已有记忆库搬进来。**免费、零 LLM token、
自动用新栈嵌入模型全量重嵌入**。

```
1. 旧库导出（异步操作）：
   POST http://<旧库>/v1/default/banks/<bank>/transfer/export
   → {"operation_id": "..."}
2. 轮询：GET .../banks/<bank>/operations/<operation_id>
   → completed 后取 result_metadata.download_url
3. 下载 zip（download_url 相对路径，GET http://<旧库><download_url>）
4. 新库必须先存在：retain 一条种子建库（POST .../banks/<bank>/memories）
5. 导入（multipart 上传 zip）：
   POST http://<新库>/v1/default/banks/<bank>/transfer/import?mode=merge&document_conflict=skip
   Content-Type: multipart/form-data; 字段名 file
6. 轮询 operations 至 completed，对账 stats：
   GET .../banks/<bank>/stats  → total_nodes / total_links / total_documents
```

注意：merge 后节点数可能略多于旧库（导入重新提取 + 种子），属正常。
回滚保险：zip 保留 ≥1 周再清。

## B. 多设备历史会话回灌（feed_sessions.py v3.1）

把每台设备上 Hermes 的**完整对话历史**一次性投喂进中央库——比插件在会话里 retain 的摘要细得多，
等于给记忆库补全"过程记忆"。

数据源：各设备 `%LOCALAPPDATA%\hermes\state.db` 的 `messages` 表。脚本**只读**打开（仅标准库，
无需装包），Hermes 开着关着都能跑；只保留 user/assistant 正文，单条截 3000 字，
长会话按 8 万字在消息边界分片（v2 是整篇截到 8 万字，一台机器的历史只进了 34.5%）。
滤掉压缩摘要、后台进程结束通知、失败轮套话和空消息；用户中途插话去包装后原话保留。
**定时任务（cron）与子代理（subagent）会话整段不投**（v3.1 起，`--exclude-sources` 可改），
与插件实时镜像同一口径：定时任务默认不入库（`retain_cron` 默认关），子代理会话从不入库；
旧版 Hermes 的库没有 `sessions.source` 列时不排除。插件「卸载又装回」留下的空窗期
（实时镜像不补历史）用 `--after <空窗起点>` 单独补投。

### 去重语义（决定命令怎么写，先读）

服务端**不跨文档去重**：去重只在同一个 `document_id` 内按块的 `content_hash`（只含块正文）进行。
v3 给每片固定 id（`backfill-<主机名>-<会话id>-p<序号>`），由此得到（临时库实测，2 块的文档）：

| 做法 | 结果 |
|---|---|
| 原样重跑 / 中断后重跑 | 0 新增、0 token（块哈希逐一相同） |
| 会话后面追加了新消息再投 | 前面的块哈希不变被复用，只有末块重提炼（8,337 token，首次的 49%） |
| 不带 document_id 重投（v2 就是这样） | 服务端每次新建文档全量提取，**必重复**（实测 +41 单元 / +18.4k token，文档 1→2） |
| 同一台机器 v2、v3 各跑一遍 | 两套 id，各进一份 |

整合层的语义去重（阈值 0.97）只是兜底，不能当作"不会重复"的依据。

### 在每台设备上运行

```powershell
# 没有系统 python 时用 Hermes 自带的：& "$env:LOCALAPPDATA\hermes\tools\python*\python.exe"
python feed_sessions.py http://<host xEdge IP>:8888 --dry-run   # 只打印计划与过滤统计，不发请求
python feed_sessions.py http://<host xEdge IP>:8888             # 正式投喂
```

- **必须在源机器上跑**：主机名（`COMPUTERNAME`）写进了文档 id、context、tags，是后台区分来源的唯一
  依据。拿不到原机、用拷来的 state.db 代跑时要加 `--host <原机主机名>`（`--db` 指定库路径）。
- **已经在实时镜像的机器必须加 `--before`**，否则接入之后的会话"镜像一份 + 回灌一份"。
  值 = 该机器在中央库出现第一条记忆的时刻，**精确到分**（只填到天会丢掉接入当天零点到接入时刻
  之间的历史，因为这段既没镜像也没回灌）。跨越接入时刻的那个会话切不开，它接入前的部分会与镜像
  重叠一次，属已知的小范围重叠。在主机上查每台机器的接入时刻：

  ```bash
  docker exec hindsight-db psql -U hindsight_user -d hindsight_db -At -F ' ' -c \
    "SELECT right(metadata->>'user_id', 8), to_char(min(created_at) AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM-DD\"T\"HH24:MI') FROM memory_units WHERE bank_id='hermes' GROUP BY 1"
  ```

  每行是一台机器（Windows 账户 SID 的末 8 位；SID 为空的那行是旧库迁入的记忆，忽略）。对端 PowerShell 里执行
  `[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value`，末 8 位对得上就是它，
  例如：`python feed_sessions.py http://<host xEdge IP>:8888 --before 2026-11-23T09:48`。
  已接入设备的镜像起点（2026-11-28 库内记录，单文件启动器已内置名单）：`2026-11-23T09:48`、`2026-11-23T09:56`
- 投喂是 `async=true`：立即入队，服务端后台慢慢提炼（GLM 提取 + 嵌入），进度看官方 UI「操作」页。
  速度参考：一次实测回灌 26.7 万字，服务端累计提炼约 240 分钟（约 1100 字/分钟，多片并行时墙钟更短），
  百万字级历史按十几个小时估。
- **中断了、个别分块 failed：重跑同一条命令**即可，已入库且内容没变的片是空操作。
- 投完在主机上核对，片数应等于 dry-run 打印的片数：

  ```sql
  SELECT count(*) FROM documents WHERE bank_id='hermes' AND id LIKE 'backfill-<主机名>-%';
  ```

## C. 验收探针（迁移/回灌后）

拿已知历史问题各召回一遍，确认首条命中：

```
POST .../banks/<bank>/memories/recall  {"query": "<某个你确定聊过的老问题>", "top_k": 5}
```

对比迁移前后首条命中一致性；再对比 recall 延迟（嵌入模型变大后每轮 +0.5~2s 属预期）。
