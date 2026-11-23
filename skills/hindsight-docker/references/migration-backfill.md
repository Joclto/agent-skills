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

## B. 多设备历史会话回灌（feed_sessions.py）

把每台设备上 Hermes 的**完整对话历史**一次性投喂进中央库——会话细节比插件在会话结束时
retain 的摘要多得多，等于给记忆库补全"过程记忆"。

数据源：各设备 `%LOCALAPPDATA%\hermes\state.db` 的 `messages` 表（完整对话记录）。
脚本以**只读模式**读库，Hermes 运行中可安全执行；仅保留 user/assistant 正文，
剥掉系统注入与压缩标记，单条截 3000 字、单会话截 80k 字。

在每台设备上运行一次：

```powershell
python feed_sessions.py http://<中央机Tailscale IP>:8888
```

- 投喂模式 `async=true`：立即入队，服务端后台慢慢提炼（GLM 提取 + 嵌入），
  进度看官方 UI「操作」页或 `GET .../operations`。
- Hindsight 有 content_hash 去重（delta retain），与已有记忆重叠的内容不会重复入库。
- 重复运行安全（同内容去重），但会多一次 GLM 提取调用，一般无需重跑。

## C. 验收探针（迁移/回灌后）

拿已知历史问题各召回一遍，确认首条命中：

```
POST .../banks/<bank>/memories/recall  {"query": "<某个你确定聊过的老问题>", "top_k": 5}
```

对比迁移前后首条命中一致性；再对比 recall 延迟（嵌入模型变大后每轮 +0.5~2s 属预期）。
