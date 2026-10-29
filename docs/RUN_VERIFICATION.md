# 运行独立验收、修复与成果下载

本指南帮助你复现第六阶段的验收与修复测试，以及验收已有真实模型快照。原理见 [第六阶段讲解](PHASE_06_WALKTHROUGH.md)。命令在 PowerShell 中执行，当前目录为 spec2repo。

## 1. 准备专用测试服务

先完成 [Agent 运行指南](RUN_AGENT.md) 的虚拟环境、.env.test 和运行镜像准备。测试会清理 spec2repo_test 数据表，不与真实实验同时运行。

```powershell
docker compose --env-file .env.test -f compose.test.yaml up -d --wait
$env:SPEC2REPO_TEST_DOCKER = '1'
$verifyTemp = Join-Path (Get-Location) ('.runtime-tests-' + [guid]::NewGuid().ToString('N'))
.venv/Scripts/python.exe scripts/run_mysql_tests.py tests/test_verification.py -q --basetemp $verifyTemp -p no:cacheprovider
.venv/Scripts/python.exe -m ruff check src tests migrations scripts
```

使用全新临时目录避免 Windows 系统临时目录权限冲突。测试不调用模型。检查内容包括事务回滚、重复调度、两轮修复上限、取消与代次校验、两个业务套件和下载鉴权。

全量命令将上述 pytest 文件参数去掉即可。本次第六阶段的 9 项专用测试全部通过，完整测试集合的结果见第五节。

## 2. 验收已有真实生成成果

保留第五阶段的 .runtime/live-agent/snapshots 目录时，执行：

```powershell
.venv/Scripts/python.exe scripts/verify_saved_generation.py --report docs/experiments/phase05-a08e3859-362b-445d-9270-36b76fe0e545.json
```

脚本校验已有快照，在专用数据库创建带来源记录的验收 run，保留原模型预算，执行正式验收和产物下载检查。脚本没有模型接口调用；失败时只记录与安排修复，不代替用户发起新的模型实验。报告写入 docs/experiments/phase06-<run_id>.json。

历史报告若缺少 spec 正文，需要明确提供与该成果对应的规格；脚本默认使用笔记 API 示例。它不从生成代码猜测业务契约。报告和快照都可读不代表来源可信，导入入口属于本地实验维护工具，不是开放给模型的 API。

## 3. 持续运行正式流水线

使用 [已有运行配置](RUN_AGENT.md)，先升级 Alembic，再分别启动 API、relay、recovery 与 agent worker。recovery 角色负责把 WAITING_VERIFICATION 推进入队；不要只启动 worker 后期待等待状态自行变化。

在已批准的真实模型实验中，run_agent_experiment.py 增加 --verify 会推进正式验收和最多两轮修复；--spec 支持 notes 或 task。该选项会在修复时继续调用配置的外部模型，延用整条任务的预算。本轮执行的是第二节的无模型快照验收，没有额外执行这一付费入口。

API 和 worker 的 SPEC2REPO_WORKSPACE_ROOT 必须指向同一快照存储根目录。应用入口为 app:app；任务 API v2 使用 DATABASE_PATH，笔记 API v1 使用 NOTES_DB；正式验收注入独立数据库路径。

## 4. 查询和交付接口

所有接口使用现有 X-API-Key 身份鉴权，run_id 必须属于该身份。

| 方法与路径 | 返回内容 |
|---|---|
| GET /api/runs/{run_id}/verifications | 按序号排列的验收报告，含 outcome、检查项与反馈 |
| GET /api/runs/{run_id}/artifacts | 成功产物的快照、规格、套件 hash 与验收序号 |
| GET /api/runs/{run_id}/artifacts/repository.zip | 通过 hash 校验的源码 ZIP |

业务用例不匹配时 outcome=FAILED；缺少注册套件或验证基础设施故障时 outcome=ERROR；只有完整检查证据通过才是 PASSED。无成功产物时产物接口返回 409；跨身份返回 404；快照缺失或损坏返回 503。

## 5. 本次交付记录

最终测试集合共 127 项，125 项已验证通过、2 项跳过，包含 10 项真实 Docker 用例。准确运行记录：首次全量 124 passed、2 skipped；新增一项边界测试并升级任务规格后，全量 123 passed、2 failed、2 skipped。失败来自旧澄清测试把下一版写死为 2；修正为当前版本加一后，对这两项和全部第六阶段用例定向回归，11 passed。没有把多次运行的重复用例累计为新测试。

两项跳过来自 SQLite 不适用的并发用例，对应 MySQL 用例已执行。Ruff 检查通过。已知 Starlette TestClient 的 httpx 弃用提示不影响测试结果。

真实成果复验记录：[最终验收器的通过报告](experiments/phase06-f580825a-ba25-4373-92c6-b8d39a9e9c08.json)，15 项通过，SUCCEEDED，新增模型调用为 0。此前的 [初次通过记录](experiments/phase06-5ae35a37-01d5-4e0f-839b-aa3f457cd07d.json) 继续保留。两次均针对同一个历史生成快照，不是两次独立模型样本。

结束测试服务：

```powershell
docker compose --env-file .env.test -f compose.test.yaml stop
```

测试数据库位于 tmpfs，重启后不依赖它保留旧 run。长期证据是版本化 JSON 报告与本地快照；开发环境使用 compose.yaml 中的持久数据库卷。
