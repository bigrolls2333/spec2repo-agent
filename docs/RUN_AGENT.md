# 运行 Agent、审批计划与提交澄清

本指南用于第五阶段模型驱动运行。先按 [第四阶段运行指南](RUN_EXECUTION.md) 准备 MySQL、Redis、固定运行镜像和共享工作区。机制说明见 [第五阶段学习记录](PHASE_05_WALKTHROUGH.md)。

第六阶段已接入正式验收与两轮修复。默认实验保留第五阶段停在 WAITING_VERIFICATION 的行为；完整验收入口、快照复验与下载见 [验收运行指南](RUN_VERIFICATION.md)。持续运行时 recovery 进程会自动调度等待验收的任务。

## 1. 先运行不调用模型的测试

PowerShell 当前目录为 spec2repo。测试使用专用数据库；不要与真实模型实验同时运行，因为测试会清理测试表。

```powershell
uv --cache-dir .cache/uv sync --locked --extra dev
.venv/Scripts/python.exe scripts/init_test_env.py
docker compose --env-file .env.test -f compose.test.yaml up -d --wait
docker build -t spec2repo-runtime:phase4 runtime-image
$env:SPEC2REPO_TEST_DOCKER = '1'
$phase5Temp = Join-Path (Get-Location) ('.runtime-tests-' + [guid]::NewGuid().ToString('N'))
.venv/Scripts/python.exe scripts/run_mysql_tests.py --basetemp $phase5Temp -p no:cacheprovider -q
.venv/Scripts/ruff.exe check src tests migrations scripts
```

测试不需要模型密钥，不发送外部模型请求。Docker 反馈实验使用脚本模型控制输入，检查真实容器输出如何返回模型上下文。

2026-10-21 完整结果为 116 passed、2 skipped。跳过项属于 SQLite 不适用的并发测试，全部 6 项 Docker 实验已运行；保留一条既有 TestClient 弃用提示。

## 2. 配置开发环境与运行角色

在 .env 中配置三个宿主变量：

| 变量 | 含义 |
|---|---|
| SPEC2REPO_MODEL_BASE_URL | 供应商 HTTPS 基地址，以兼容服务要求的 /v1 路径结束；适配器追加 /chat/completions |
| SPEC2REPO_MODEL_API_KEY | 对应服务的模型密钥 |
| SPEC2REPO_MODEL | 该服务支持的准确模型名 |

不自动扫描或加载其他项目凭证。开发时按 [第二阶段说明](PHASE_02_WALKTHROUGH.md) 将 .env 载入各宿主进程环境；不要把它挂载给容器。API 本身不需要调用模型，模型变量只需提供给 agent worker。

执行 `.venv/Scripts/alembic.exe upgrade head` 应用 0005_agent；启动 API 的命令仍为：

```powershell
.venv/Scripts/python.exe -m uvicorn spec2repo.api.app:create_app --factory --host 127.0.0.1 --port 8000
```

relay 与 recovery 命令保持第四阶段方式；worker 改为：

```powershell
.venv/Scripts/python.exe -m spec2repo.runtime worker --agent --consumer agent-a
```

同一个执行部署不要混用 `--agent` 与 `--execute-fixture` worker，它们共享 execution-v1 消费组，后者只用于第四阶段实验。第二个 agent worker 使用不同 consumer 名，但应连接相同数据库、Stream 和工作区根目录。

第四阶段 fixture 的 WAITING_APPROVAL 只是固定操作的等待出口，没有可审批的实现计划。切换 Agent 时提交新任务，不把旧 fixture 任务当成已规划的任务继续审批。

## 3. 提交任务与审批

以下命令使用已加载的开发环境身份，并提交仓库内公开的 notes 示例：

```powershell
$identities = $env:SPEC2REPO_API_KEYS | ConvertFrom-Json
$headers = @{ 'X-API-Key' = $identities.alice; 'Idempotency-Key' = [guid]::NewGuid().ToString('N') }
$specBody = Get-Content examples/notes_api.spec.json -Raw -Encoding UTF8
$run = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/runs -Headers $headers -ContentType 'application/json' -Body ([Text.Encoding]::UTF8.GetBytes($specBody))
$reviewUrl = "http://127.0.0.1:8000/api/runs/$($run.id)/review"
$review = Invoke-RestMethod -Uri $reviewUrl -Headers $headers
$review
```

任务处于 RUNNING 时稍后再次查询 review。只有 WAITING_APPROVAL 且存在 PENDING 计划时才审批；先阅读计划内容、需求和验收关联。批准请求示例：

```powershell
$plan = $review.plans[-1]
$decision = @{
    revision = $plan.revision
    spec_hash = $plan.spec_hash
    plan_hash = $plan.plan_hash
    approve = $true
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/runs/$($run.id)/approval" -Headers $headers -ContentType 'application/json' -Body $decision
```

拒绝将 approve 改成 false，任务变为 CANCELLED。相同决定可以重放；旧规格、旧修订或相反决定返回 409。其他 owner 访问返回 404。

实现完成后状态为 WAITING_VERIFICATION，review 返回 snapshot_hash。这表示已提交生成产物，正式验收与最终成功由第六阶段负责，不能把它当成 SUCCEEDED。

## 4. 回答澄清

任务进入 WAITING_CLARIFICATION 时，review 的 questions 给出问题。根据回答修改完整 CoreSpec，使 version 恰好加 1，并解决阻塞问题。提交：

```json
{
  "expected_version": 4,
  "reason": "明确了资源冲突时返回 409 的规则，并更新相关需求和验收项",
  "spec": {
    "...": "完整且通过 CoreSpec 校验的下一版本规格"
  }
}
```

这是请求结构示意，不能直接发送带省略号的 JSON。目标为 `POST /api/runs/{id}/clarification`；expected_version 使用最新 review.run.version，不是 spec.version。提交后保存历史规格、重新入队规划，并要求对新计划重新审批。

## 5. 真实模型实验

脚本只处理自己创建的测试任务，保留既有测试数据；仍不能与会清表的 pytest 并行。模型请求会发送 notes 示例规格、生成代码与沙箱反馈，使用对应服务额度。

明确选择一个模型配置文件后运行：

```powershell
.venv/Scripts/python.exe scripts/run_agent_experiment.py --model-env ../paper2repo_agent/.env
```

脚本兼容原项目的 OPENAI_BASE_URL、OPENAI_API_KEY、MODEL_NAME，也接受第五阶段三个 SPEC2REPO_MODEL 变量。只读取配置，不复制密钥到新项目。此路径是当前仓库布局下的示例；其他部署应传入自己的配置路径。

供应商升级端点时，可显式传入 `--base-url` 覆盖本次实验的基地址；脚本不会自动更换服务或修改原配置。OpenCode Go 官方文档要求自己的 User-Agent 与稳定会话 ID，适配器使用 spec2repo/0.5，并在该域名上发送 run_id 作为会话标识；参见 [官方接入说明](https://opencode.ai/v2/docs/console/go/)。HTTP 401 表示鉴权没有通过，应核对本地凭证和服务权限，不盲目重试。

实验 owner 由脚本创建并执行审批，目的是验证完整协议链路。开发环境的审批仍通过带身份认证的 HTTP API。报告写入 docs/experiments/phase05-{run_id}.json，工作区和快照保留在 .runtime/live-agent。报告包含调用、工具事件、预算和快照引用，不保存密钥或模型思考内容。

状态为 FAILED 或 WAITING_CLARIFICATION 时，脚本也写报告并返回非零退出码。模型 usage 缺失或无法计算费用时保持 null，不用零元代替。不要把一次结果描述为统计成功率。

从本次已提交快照恢复新目录，使用报告中的相同 image ID 重跑开发测试：

```powershell
.venv/Scripts/python.exe scripts/check_agent_snapshot.py docs/experiments/phase05-a08e3859-362b-445d-9270-36b76fe0e545.json
```

此命令依赖本机 .runtime/live-agent/snapshots 中对应文件仍存在。重新克隆仓库只有报告，没有被 .gitignore 排除的运行产物，需要先运行自己的生成实验，再替换报告路径。复测结果是 6 passed；它是模型生成的开发测试，不是第六阶段独立验收。

## 6. 收尾

默认运行启用停滞干预；轨迹、干预规则及三组对照入口见 [Agent 评估](AGENT_EVALUATION.md)。

任务记忆、输出分页与快照恢复规则见 [记忆与上下文管理](MEMORY_CONTEXT.md)。部署前将数据库迁移到 `0007_memory`。

工具批量调用与依赖规则见 [并行工具设计](PARALLEL_TOOLS.md)，真实模型 1/2/4 及扩展并发实验见 [吞吐实验记录](AGENT_BENCHMARK.md)。这些实验会产生模型费用，接口 RPS 与 Agent 验收交付吞吐须分别解读。

先退出 worker、relay、recovery 和 API，再停止测试服务：

```powershell
docker ps -a --filter label=spec2repo.run
docker compose --env-file .env.test -f compose.test.yaml stop
```

审批、规格与预算保存在 MySQL；生成快照在工作区根目录。专用测试容器使用临时存储，停止后不要期待数据库证据仍持久存在，需以已导出的实验报告复查。
