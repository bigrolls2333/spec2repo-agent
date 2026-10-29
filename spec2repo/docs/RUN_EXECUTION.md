# 运行第四阶段与复现执行恢复

本指南使用专用 MySQL 测试库、Redis 和真实 Docker 容器，复现租约恢复、取消与隔离实验。PowerShell 当前目录应为 spec2repo，Docker Desktop 使用 Linux 容器模式。原理解释见 [第四阶段学习记录](PHASE_04_WALKTHROUGH.md)。

## 1. 准备与完整测试

测试会清理 spec2repo_test 的业务表，不要连接开发数据库，也不要并行启动两份测试套件。Redis 使用随机 Stream 名和专用 DB 15。

```powershell
uv --cache-dir .cache/uv sync --locked --extra dev
.venv/Scripts/python.exe scripts/init_test_env.py
docker compose --env-file .env.test -f compose.test.yaml up -d --wait
docker build -t spec2repo-runtime:phase4 runtime-image
$env:SPEC2REPO_TEST_DOCKER = '1'
$phase4Temp = Join-Path (Get-Location) ('.runtime-tests-' + [guid]::NewGuid().ToString('N'))
.venv/Scripts/python.exe scripts/run_mysql_tests.py --basetemp $phase4Temp -p no:cacheprovider -q
.venv/Scripts/ruff.exe check src tests migrations scripts
```

临时目录使用新 UUID，避免 Windows 不同进程身份共用 pytest 临时目录造成访问错误。pytest 会管理指定目录，只传入新生成的测试目录，不能填项目目录。测试产生的工作区留在该目录，可检查 attempts 与 snapshots；目录已加入 .gitignore。

Docker 测试需要显式设置 SPEC2REPO_TEST_DOCKER=1；否则会跳过，不能把跳过当成沙箱验证通过。只跑第四阶段真实容器实验：

```powershell
$phase4Temp = Join-Path (Get-Location) ('.runtime-tests-' + [guid]::NewGuid().ToString('N'))
.venv/Scripts/python.exe scripts/run_mysql_tests.py tests/test_execution.py -m docker --basetemp $phase4Temp -p no:cacheprovider -v
```

2026-10-20 实测：完整套件 93 passed、2 skipped；上述 Docker 子集 5 passed。两个跳过项属于 SQLite 不适用的并发测试。完整套件还有一条已有的 Starlette TestClient 弃用提示；具体实验记录见第四阶段学习文档。

kill 测试启动独立 worker，等待第一阶段检查点与第二阶段开始标志，强制终止进程，等待真实租约到期，再运行接管 worker。测试租约为 6 秒、心跳为 1 秒；这组参数用于加快故障实验，服务默认值仍为 60 秒与 10 秒。

## 2. 接到开发 API

按照 [第二阶段说明](PHASE_02_WALKTHROUGH.md) 启动开发 MySQL、Redis 与 API，并加载 .env 配置。执行 `.venv/Scripts/alembic.exe upgrade head`，新增迁移为 0004_execution。所有角色必须连接同一 MySQL、Redis，并使用同一 SPEC2REPO_WORKSPACE_ROOT 绝对路径。

先停止第三阶段的模拟 worker，将 relay 从 `--simulate` 切回默认执行对账。在各自终端加载相同环境，分别长期运行以下三个角色：

```powershell
.venv/Scripts/python.exe -m spec2repo.runtime relay
```

```powershell
.venv/Scripts/python.exe -m spec2repo.runtime worker --execute-fixture --consumer executor-a
```

```powershell
.venv/Scripts/python.exe -m spec2repo.runtime recovery
```

另一个 worker 使用不同 consumer 名 executor-b。执行组固定为 execution-v1，第三阶段模拟组仍为 dispatch-simulation。历史模拟回执不影响执行模式对账；第三阶段 QUEUED 任务仍可以领取，旧消息缺失 generation 时按 0 处理。

提交示例 CoreSpec 后，预期事件依次包含 execution.claimed、两个 execution.checkpoint、execution.fixture_completed。任务进入 WAITING_APPROVAL，generation 为 1；此处是 fixture 的持久等待出口，不代表已生成实现计划或验收成功。

配置参考：

| 配置 | 默认值 / 用途 |
|---|---|
| SPEC2REPO_WORKSPACE_ROOT | .runtime；多进程建议设置相同绝对路径 |
| SPEC2REPO_RUNTIME_IMAGE | spec2repo-runtime:phase4；必须已在本地构建 |
| --lease-seconds | 60；由数据库时间判断有效期 |
| --heartbeat-seconds | 10；必须小于租约时长的一半 |
| --fixture-delay | 0；人为延长阶段二，范围 0～300 秒 |
| --idle-ms | 30000；Redis pending 扫描阈值，不是租约时长 |
| --once | 一轮消费/发布/恢复；worker 可能执行一个完整 fixture |

recovery 每轮后等待 1 秒；每次最多恢复 100 个过期任务，清理历史结束或正在取消的 attempt，再进行排队对账。relay 每 10 秒做一次排队对账。扫描与 Docker 操作本身也耗时，轮询间隔不等于恢复耗时承诺。

## 3. 查看故障与结束实验

通过 API events 查询 execution.requeued、execution.cancelled、execution.failed、execution.exhausted。快照只接受数据库已提交的引用；不能看到旧目录里有文件，就认为阶段已经完成。

```powershell
docker ps -a --filter label=spec2repo.run
docker image inspect spec2repo-runtime:phase4 --format '{{.Id}}'
docker compose --env-file .env.test -f compose.test.yaml stop
```

正常测试会清理自身容器。测试被外部强制终止时，应保留相同测试数据库与工作区连接，运行 recovery 清理对应容器，再停止测试服务。不要直接删除所有 Docker 容器。

排查顺序：确认迁移完成 → 确认镜像存在 → 确认路径可绑定且各角色一致 → 查看任务事件与 attempt → 检查 MySQL / Redis / Docker 连通性。程序控制台隐藏数据库凭证与任意工具输出，基础设施重试日志只显示异常类型。
