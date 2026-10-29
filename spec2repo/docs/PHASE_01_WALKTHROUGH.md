# 第一阶段：验证规格与任务状态规则

本阶段建立 agent 执行前的契约：规格中的需求能够关联到验收条件，关键问题未回答时不会被误认为可以直接推进，任务不能在取消后又宣告成功。你可以运行离线 CLI 和测试，沿调用链理解这些规则；此时还没有接入模型、数据库或队列。

## 1. 本次实现与环境边界

本次只新增 Spec2Repo 基础文件并更新计划，没有修改旧 Paper2Repo 代码。

| 项目 | 当前状态 |
|---|---|
| 独立 Python 包、虚拟环境和 uv.lock | 已建立 |
| CoreSpec、需求与验收关联、规格指纹 | 已实现 |
| 未决问题与规划就绪判断 | 已实现领域判断，尚无持久化澄清 API |
| 生命周期规则与版本检查 | 已实现纯函数，尚无数据库原子更新 |
| 离线 validate-spec CLI | 已实现 |
| 任务管理规格 | 5 条需求、8 条验收条件；尚未生成业务仓库 |
| 验收执行器 | 未实现；当前验收项是结构化行为描述 |
| MySQL、Redis、worker、agent loop | 下一阶段逐步实现 |
| Docker | 客户端可用；本次无法连接默认 Docker 服务端 |

旧仓库基线 HEAD 为 `372dcdafc3d96901255397e164ec4abaa4df10a2`。开始时旧项目存在多项未提交修改，因此这个 commit 不能代表当时所有工作区内容；本阶段没有重置或覆盖它们。

环境检查发现 Python 3.13.5 可用。Docker 默认命名管道不存在，并出现读取用户 Docker 配置被拒绝的警告；只据此记录“当前连接未验证”，不推断所有 Docker context 都不可用。没有运行不可信生成代码，也没有调用付费模型。

## 2. 运行本阶段

在 Windows PowerShell 中进入项目目录。下面使用项目内缓存，避免依赖全局 uv 缓存的写权限；不需要激活虚拟环境。

```powershell
Set-Location 'D:\FuckingCode\开发代码demo\ProjectDemo2\spec2repo'
uv --cache-dir .cache/uv sync --locked --extra dev
.venv/Scripts/spec2repo.exe validate-spec examples/task_api.spec.json
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/ruff.exe check src tests
.venv/Scripts/ruff.exe format --check src tests
```

依赖已经锁定在 `uv.lock`。首次安装需要访问包源；若实际依赖升级，必须重新验证测试，而不是只修改锁文件。

本次 CLI 输出的关键字段如下：

```json
{
  "valid": true,
  "spec_id": "task-api",
  "version": 1,
  "ready_for_planning": true,
  "blocking_question_ids": [],
  "requirements": 5,
  "acceptance_criteria": 8
}
```

实际输出还包含完整规格的 SHA-256 指纹。`ready_for_planning` 仅表示结构校验通过且没有未回答的阻塞问题，不表示规格已被人批准，也不表示代码已通过验收。

CLI 的退出码区分三种情况：

| 退出码 | 含义 | 处理方式 |
|---|---|---|
| 0 | 规格结构有效，可进入规划 | 下一阶段才会提交任务并规划 |
| 1 | 文件不可读或规格非法 | 检查 stderr 中的结构化 errors，修正规格 |
| 2 | 结构有效，但有未回答的阻塞问题 | 根据 blocking_question_ids 补充业务决定 |

## 3. 阅读规格校验调用链

先读 [cli.py](../src/spec2repo/cli.py)，再读 [domain/spec.py](../src/spec2repo/domain/spec.py)，最后对照 [任务规格](../examples/task_api.spec.json)。入口保持很薄，领域规则不依赖 CLI、FastAPI、MySQL 或模型供应商。

```text
validate-spec 路径
    → 读取 UTF-8 JSON
    → CoreSpec.model_validate_json
        → 字段与未知字段检查
        → ID 唯一性检查
        → requirement_ids 引用检查
        → 每条需求有验收项的覆盖检查
        → method/path 重复检查
    → 判断未解决的阻塞问题
    → 输出规范化指纹、就绪状态与退出码
```

这里特意拒绝未知字段。例如把 requirements 写成 requrements，不应静默忽略后继续运行。ID 与引用检查避免产生指向不存在需求的“追溯记录”。

规格使用不可变模型和 tuple 字段。业务修改需要形成新的规格对象；正式版本递增和历史保存将在持久化服务中实施。当前校验器只能检查 version 是正整数，不能凭一份输入判断调用者是否正确递增了历史版本。

指纹对模型规范化后的 JSON 计算哈希，键顺序和缩进变化不影响结果，业务描述和问题答案变化会影响结果。数组顺序仍被保留，因此调整需求顺序也可能改变指纹；它不是语义等价证明。

## 4. 阅读任务生命周期

阅读 [domain/lifecycle.py](../src/spec2repo/domain/lifecycle.py) 与 [生命周期测试](../tests/test_lifecycle.py)。状态规则只回答“当前状态允许怎样变化”，不负责 SQL、租约、消息确认或容器清理。

```text
QUEUED → RUNNING → WAITING_CLARIFICATION → QUEUED
                → WAITING_APPROVAL → QUEUED
                → RETRY_WAIT → QUEUED
                → SUCCEEDED（必须有独立验收通过信号）
                → FAILED → QUEUED（由未来服务处理显式重试）
                → CANCELLING → CANCELLED
```

排队、澄清等待、审批等待和重试等待状态可以直接取消。取消中的任务只能进入 CANCELLED；SUCCEEDED 与 CANCELLED 不允许重新进入 QUEUED。

`transition` 接收当前快照、目标状态和调用者看到的 expected_version。版本不一致时，调用者必须重新读取状态并决定如何处理；不能盲目重试旧决定。

关键区别是：两个线程可以同时拿到同一份 RUNNING 快照，并各自通过这个纯函数。未来数据库必须在更新条件中同时匹配旧状态与版本，并检查受影响行数，才能让只有一个竞争操作成功。当前“取消先发生”的测试模拟连续观察的结果，不是多进程竞态测试。

`verification_passed` 是未来可信验证服务提供的结果。它不会作为外部 API 可任意提交的成功开关；下一阶段仍需将其与当前 run、规格、产物和 attempt 关联。当前函数只是先定义成功门禁。

## 5. 用测试验证理解

测试覆盖规格引用错误、需求缺少验收、重复接口、未决问题、指纹变化、错误输入，以及任务状态冲突。运行时不需要网络或模型凭证。

本次完整测试结果为：

```text
30 passed
```

下面三组测试适合单独阅读并运行：

```powershell
.venv/Scripts/python.exe -m pytest -q tests/test_spec.py -k "blocking_question or without_acceptance"
.venv/Scripts/python.exe -m pytest -q tests/test_lifecycle.py -k "cancel_wins or completion_wins"
.venv/Scripts/python.exe -m pytest -q tests/test_spec.py -k fingerprint
```

阅读时先预测为什么应当拒绝输入或状态迁移，再看断言。通过测试只能证明这些局部规则满足当前用例，不代表规格语义完整、生成代码正确或系统已经具备分布式恢复。

## 6. 进入下一阶段前的复述

下一阶段将实现 MySQL 任务持久化、迁移和幂等 API。复述问题已统一归入同目录的 [学习与面试题单](INTERVIEW_QUESTION_BANK.md)，后续按改造进度在该文件中增补。

先回答 Q001～Q004，分别对应成功口径、验收覆盖、并发版本检查和规格指纹。再回答本阶段优先题 Q006、Q011、Q020、Q023，检查架构分工、取消冲突、测试证据和环境边界；本文件不再复制题目正文。

完整范围与后续阶段预算见 [改造计划](../IMPLEMENTATION_PLAN.md)。以上保留第一阶段的验证边界。第二阶段已完成 Docker/MySQL 连通、持久 API 与数据库并发验证，见 [第二阶段学习记录](PHASE_02_WALKTHROUGH.md)；受限执行实验仍需在真正运行生成代码之前完成。
