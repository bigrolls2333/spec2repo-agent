# 运行投递服务与故障实验

本指南帮助你复现第三阶段的数据库、Redis 和假 worker 实验。前提是本机 Docker Desktop 已运行，PowerShell 当前目录为 spec2repo，且已安装 uv。机制解释见 [第三阶段学习记录](PHASE_03_WALKTHROUGH.md)。

## 1. 运行完整测试与 Redis 停机实验

使用专用测试库和 Redis，避免测试清理普通开发数据：

```powershell
uv --cache-dir .cache/uv sync --locked --extra dev
.venv/Scripts/python.exe scripts/init_test_env.py
docker compose --env-file .env.test -f compose.test.yaml up -d --wait
.venv/Scripts/python.exe scripts/run_mysql_tests.py
.venv/Scripts/python.exe scripts/redis_outage_experiment.py
.venv/Scripts/ruff.exe check src tests migrations scripts
```

第三阶段交付时完整测试为 74 passed、2 skipped；第四阶段新增容器与恢复用例，最新运行方式见 [执行恢复指南](RUN_EXECUTION.md)。停机实验输出两行确认信息，详见学习记录。run_mysql_tests.py 默认连接 `redis://127.0.0.1:16380/15`，需要两个测试服务均启动。测试凭证仍由 init_test_env.py 写入被忽略的 .env.test，不需要修改既有文件。

测试使用随机 Stream 名，只删除本测试创建的键，不执行 FLUSHDB。数据库测试仍会清理 spec2repo_test 中业务表，不要并行运行两份完整套件。停机实验会短暂停止共享的 redis-test 服务，也应单独执行。它在 finally 中尝试恢复 Redis，未操作开发 Redis 服务。

## 2. 单步检查 relay 和 worker

运行上一节后，可以在同一 PowerShell 中加载专用测试连接，检查 CLI 入口：

```powershell
$testLine = Get-Content .env.test | Where-Object { $_.StartsWith('SPEC2REPO_TEST_MYSQL_URL=') }
$env:SPEC2REPO_DATABASE_URL = $testLine.Split('=', 2)[1]
$env:SPEC2REPO_REDIS_URL = 'redis://127.0.0.1:16380/15'
$env:SPEC2REPO_STREAM = 'spec2repo:test:manual'
.venv/Scripts/python.exe -m spec2repo.runtime relay --simulate --once
.venv/Scripts/python.exe -m spec2repo.runtime worker --simulate --consumer manual-a --once
```

`--once` 只执行一轮，空队列正常返回 0，有 Redis/数据库错误返回 1。假 worker 必须显式加 `--simulate`；第四阶段执行 worker 则使用 `--execute-fixture`。模拟实验的 relay 也加 `--simulate`，启用第三阶段基于回执的对账规则。

测试已由两个独立子进程验证持续 worker 模式。手动长期运行时去掉 `--once`，另一个 PowerShell 加载同样的环境后使用不同 consumer 名，如 manual-b。两个 worker 必须使用相同 Stream 和消费组；本版本固定组名为 dispatch-simulation。

## 3. 接到自己的开发 API

先按照 [第二阶段启动说明](PHASE_02_WALKTHROUGH.md) 配置 .env 和 API，并确认 .env 包含 `SPEC2REPO_REDIS_URL=redis://127.0.0.1:16379/0`。重跑 `docker compose up -d --wait` 启动开发 Redis，执行 `alembic upgrade head` 应用最新迁移。

在启动 relay 和 worker 的每个窗口加载 .env 中的进程环境，运行上节同样的角色命令；开发环境不要设置 SPEC2REPO_STREAM，使用默认 `spec2repo:dispatch`。通过既有 API 提交规格后，查询任务 events，预期新增 `dispatch.simulated`，任务状态仍为 QUEUED。

不得在同一个 Stream 上混用不同数据库的 relay/worker。消息中的 ID 只在对应数据库内有意义，混用会导致拒收。模拟模式回执会阻止同一事件继续补发；切换执行模式按 [第四阶段迁移说明](RUN_EXECUTION.md) 操作，执行对账忽略模拟回执。

## 4. 停机与排查

持续服务使用 Ctrl+C 退出，释放数据库连接和 Redis 连接。结束测试容器：

```powershell
docker compose --env-file .env.test -f compose.test.yaml stop
```

如果反复看到 `dispatch retry: ConnectionError`，先确认相应 Redis 服务运行、端口与环境一致；如果为 SQLAlchemy 异常，检查 MySQL 连通和迁移是否完成。日志只显示类型，故障排查需结合容器状态及专用实验，不能靠打印数据库密码定位。

完成运行后，用题单 Q035～Q044 复述投递、ACK、去重和对账边界，再进入租约与执行恢复阶段。
