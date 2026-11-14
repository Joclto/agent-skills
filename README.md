# agent-skills

可被 AI agent 直接加载的运维 skill 集（Agent Skills 通用格式，Hermes、Claude Code、Codex 等均兼容）。

## Skill 列表

| Skill | 触发场景 | 版本 |
|---|---|---|
| [`hermes-update-network-fix`](skills/hermes-update-network-fix/SKILL.md) | `hermes update` 报网络错误；github.com:443 HTTPS 被干扰（ping 通但 curl 超时 / 重置 / 空回复） | v0.2.0 · Windows 实测 |

## 给 AI agent 的加载指令

命中某 skill 的触发场景，或需主动核查所在环境时：

- **通用（任何 agent）**：直接读取 `skills/<name>/SKILL.md`（raw 地址见下），按其 Procedure 执行；涉及修改系统的动作（git remote / 环境变量 / SSH 配置）先征得用户同意。
- **Hermes 便利路径**：`hermes skills inspect <raw-url>` 只读预览即用；`hermes skills install <raw-url>` 常驻安装——涉及 SSH / git 配置类命令的 skill 会被安全扫描 caution 门控，复核内容（尤其 `references/`）后加 `--force`。这是预期行为，不是 skill 有问题。

raw 地址：`https://raw.githubusercontent.com/Joclto/agent-skills/main/skills/hermes-update-network-fix/SKILL.md`

若 raw.githubusercontent.com 也不可达：`git clone git@github.com:Joclto/agent-skills.git` 后读取对应 `skills/*/SKILL.md`（本仓库首个 skill 正是为「github.com HTTPS 被掐但 SSH 可用」的网络环境而生，克隆不受影响）。

## 收录约定

- 每个 skill 一个目录：`skills/<name>/SKILL.md`（frontmatter 遵循 Agent Skills 规范：description ≤60 字符、一句陈述能力）+ 可选 `references/`。
- 只收本人实测过根因与修复路径的运维 skill，不收转发拼凑。
