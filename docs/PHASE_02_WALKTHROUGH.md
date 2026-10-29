# 第二阶段：持久任务 API

本阶段让规格成为可跨进程查询的任务，并把重复提交、身份隔离和状态竞争交给数据库约束。2026-10-19 在 Windows、Python 3.13、Docker Linux 容器 MySQL 8.4 上验证：63 个测试通过，2 个跳过。两个跳过项是 SQLite 下的并发实验，其 MySQL 版本均通过。测试没有调用模型。

本文保留第二阶段交付时的结果。第三阶段现已接入 Redis 和模拟消费；最新能力与运行步骤见 [第三阶段记录](PHASE_03_WALKTHROUGH.md) 和 [投递运行说明](RUN_DISPATCH.md)。

## 1. 已实现的行为

| 能力 | 当前行为 | 实现入口 |
|---|---|---|
| 持久任务 | 保存完整 CoreSpec、摘要、身份、状态、版本、UTC 时间 | [models.py](../src/spec2repo/storage/models.py) |
| 幂等创建 | 同身份同键同规格返回同一 run；内容变化返回 409 | [runs.py](../src/spec2repo/storage/runs.py) |
| 基础鉴权 | API key 在服务端映射身份；查询、列表、事件和取消都限制 owner | [app.py](../src/spec2repo/api/app.py) |
| 原子状态更新 | 状态和版本均满足条件才更新，事件与状态同事务提交 | RunRepository.change_status |
| 取消 | 等待/排队任务直接取消；运行任务进入 CANCELLING；重复取消不重复记事件 | RunRepository.cancel |
| 投递基础 | 就绪任务与 outbox 记录在同一个 MySQL 事务保存 | RunRepository.create |
| 数据库演进 | Alembic 显式迁移；启动 API 不自动改表 | [migrations](../migrations) |

当前没有 relay、Redis、worker、模型调用、审批服务、执行沙箱或独立验收器。新任务停留在 QUEUED，outbox 的 published_at 保持空；存在阻塞问题则进入 WAITING_CLARIFICATION，不创建调度消息。CANCELLING 目前只是状态，停止容器和工具调用由后续阶段完成。

## 2. 先跑真实 MySQL 测试

在 spec2repo 目录打开 PowerShell，启动 Docker Desktop，执行：

```powershell
uv --cache-dir .cache/uv sync --locked --extra dev
.venv/Scripts/python.exe scripts/init_test_env.py
docker compose --env-file .env.test -f compose.test.yaml up -d --wait
.venv/Scripts/python.exe scripts/run_mysql_tests.py
.venv/Scripts/ruff.exe check src tests migrations scripts
docker compose --env-file .env.test -f compose.test.yaml stop
```

init_test_env.py 只在文件不存在时生成随机测试凭证，保存在被 Git 忽略的 .env.test 中，不打印密码。测试数据库使用本机 13307 端口、spec2repo_test 库和 tmpfs，停止容器后不保证保留数据。测试会清空该专用库中的业务表；fixture 拒绝其他库名，避免把普通开发库误作测试库。

直接运行 `.venv/Scripts/python.exe -m pytest -q` 时，未配置真实数据库会跳过 MySQL 用例。这不能用于宣称 MySQL 验证通过。提供测试连接但数据库不可用时，测试会失败。

## 3. 启动自己的持久化开发服务

开发数据库使用 13306 端口和命名卷，与测试数据库分离。复制配置示例并填写自己的密码及 API key：

```powershell
Copy-Item .env.example .env
notepad .env
```

将 YOUR_DATABASE_PASSWORD 替换为同一个随机十六进制密码，同时写入 MYSQL_PASSWORD 和连接 URL。另设 MYSQL_ROOT_PASSWORD。SPEC2REPO_API_KEYS 是身份到密钥的 JSON 对象，密钥须不同且至少 24 个 ASCII 字符。不要照用占位符。下面加载器接受示例文件中直接写值的格式，不支持 shell 插值或外层引号：

```powershell
Get-Content .env | ForEach-Object {
    if ($_ -match '^([A-Z][A-Z0-9_]*)=(.*)$') {
        [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
    }
}
docker compose up -d --wait
.venv/Scripts/python.exe -m alembic upgrade head
.venv/Scripts/python.exe -m uvicorn spec2repo.api.app:create_app --factory --host 127.0.0.1 --port 8000
```

API 和 Alembic 从进程环境读取配置，不自动读取 .env；Docker Compose 才会自动加载它。另开一个 PowerShell 窗口发请求时，先切换到同一目录并执行上面的环境加载片段：

```powershell
$identities = $env:SPEC2REPO_API_KEYS | ConvertFrom-Json
$apiKey = ($identities.PSObject.Properties | Select-Object -First 1).Value
$headers = @{ 'X-API-Key' = $apiKey; 'Idempotency-Key' = 'learning-001' }
$body = Get-Content examples/task_api.spec.json -Raw -Encoding UTF8
$run = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/runs -Headers $headers -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/runs/$($run.id)" -Headers $headers
```

重复提交相同内容得到同一任务；修改 title 后复用同一键返回 409。关闭并重启 API 后仍能按 ID 查询。完成后 Ctrl+C 停止 API，`docker compose stop` 停止开发数据库；命名卷保留。不要使用删除卷的命令来日常停机。

本轮实际运行的是专用测试库和真实 HTTP 子进程；普通开发库命名卷的重启持久性未单独做破坏性实验。

## 4. API 约定

所有业务路由都需要 X-API-Key。身份由服务端决定，请求体不接受 owner_id。

| 方法与路径 | 参数 | 返回 |
|---|---|---|
| POST /api/runs | CoreSpec；Idempotency-Key 请求头 | 202，任务、Location 和 Idempotency-Replayed |
| GET /api/runs | limit 1～100，offset ≥ 0 | 当前身份的任务列表 |
| GET /api/runs/{id} | UUID | 任务详情 |
| POST /api/runs/{id}/cancel | 无请求体 | 取消后的任务状态 |
| GET /api/runs/{id}/events | after ≥ 0，limit 1～200 | 事件和 next_after 游标 |

未认证返回 401；其他身份的任务与不存在的任务均返回 404；幂等内容冲突或状态冲突返回 409；输入不合法返回 422；数据库异常返回 503，不回显连接参数。不存在任意改状态或客户端宣布验收通过的接口。

幂等键作用域是 `(owner_id, key)`，区分大小写；最长 128，字符限字母、数字、下划线、点、冒号、短横线。比较的是校验后的 CoreSpec 规范化摘要，不是 HTTP 原始字节。重放返回任务的**当前状态**，不是首次响应的永久副本。首版不自动过期或清理幂等记录。

## 5. 沿一条调用链读代码

按 app.create_run → RunRepository.create → models → 迁移 → test_repository 阅读。

一次创建事务包含：插入 runs → 插入 idempotency_keys → 写 run.created 事件 → 写 outbox（仅就绪任务）→ 提交。任何一步失败，都不能留下半个任务。HTTP 返回丢失时，客户端使用原幂等键重试，获得已经提交的 run。

“先查有没有，再插入”只能减少常见重试的工作量，不能阻止两个并发请求同时查到不存在。真正的裁决点是数据库复合主键。竞争失败的事务整体回滚后，使用新 session 读取赢家的记录并比较摘要；不会留下竞争失败时临时插入的孤立 run。新事务也避开旧的 REPEATABLE READ 快照。

状态变化使用类似下面的条件更新，检查影响行数恰好为 1，并在同一个事务写事件：

```sql
UPDATE runs SET status = :target, version = :next_version, updated_at = :now
WHERE id = :id AND owner_id = :owner
  AND version = :expected_version AND status = :previous_status;
```

Python transition 负责判断业务迁移是否合法，数据库条件更新负责拒绝竞争中的过期写入。version 还不是 worker 的租约或 generation，不能阻止旧 worker 写文件或继续调用模型。将来需要同时约束提交权限和工作区。

## 6. 数据与查询取舍

四张业务表分别保存任务、幂等决定、待投递事件、审计事件，Alembic 另有版本表。attempt、step、approval 表等到对应功能有消费者时再引入。

任务列表索引为 `(owner_id, created_at, id)`，按时间和 ID 降序稳定排序；事件索引为 `(run_id, id)`，按递增 ID 翻页。任务列表仍使用 offset，深分页会有扫描成本，而且并发插入时跨页可能移动；当前没有做大规模性能证明。事件 ID 是游标，不保证连续。不同事务的自增分配顺序也不等于提交顺序，当前事件写入受 run 行更新串行化约束；未来增加并发事件生产者时必须重新检查游标是否可能漏读。

应用只存 UTC；MySQL DATETIME(6) 本身不带时区。首次真实进程重启测试发现默认 DATETIME 丢失微秒，创建响应和查询响应不一致，因此新增 0002_timestamp_precision 迁移保留六位精度。迁移能修改已经建好的表，不能找回历史已丢失的小数。

API key 通过环境配置，认证比较固定长度摘要；这只是首版基础身份隔离，没有账号管理、动态吊销、限流或公网部署安全设施。同步 PyMySQL 路由使用 FastAPI 普通 def，在工作线程中执行；数据库连接池不能无限扩张来解决吞吐问题。

## 7. 测试证据与剩余边界

| 实验 | 观察结果 | 证据 |
|---|---|---|
| 同键顺序重试 10 次、不同内容复用 | 同一任务；冲突 409；四表各一条 | test_api.py |
| MySQL 同键并发 10 次 | 一个新建结果，唯一 run、事件、outbox | test_repository.py |
| Outbox 插入时抛异常 | 四表全部回滚 | test_repository.py |
| 两个连接同时提交成功和取消 | 一个成功、一个 StaleVersionError，版本只增加一次 | test_repository.py |
| 越权查询、取消、事件、列表 | 不暴露另一身份任务 | test_api.py |
| 停止真实 Uvicorn 进程并重启 | HTTP 查询完整任务一致，原键重放仍命中 | test_process_restart.py |
| 迁移后的元数据比较 | SQLite 和 MySQL 均与模型一致 | test_repository.py |

完整结果为 63 passed、2 skipped；另有一条 Starlette TestClient 提示 httpx 适配器将弃用的依赖告警，当前不影响通过。没有测数据库主机宕机、Redis 中断、worker 接管、分布式文件恢复或 agent 生成成功率。

Outbox 表只解决“任务和待发送意图原子保存”。下一阶段需要 relay 实际发布、重复投递处理和对账，才能讨论任务最终被调度。

## 8. 学习与资料

本阶段优先复述题单 Q025、Q026、Q028、Q029、Q032，再阅读其余新增问题；旧题 Q003、Q020、Q023 已补充新证据。题目统一维护在 [学习与面试题单](INTERVIEW_QUESTION_BANK.md)，代码完成不会自动标记掌握。

事务作用域参考 [SQLAlchemy 官方事务文档](https://docs.sqlalchemy.org/en/20/orm/session_transaction.html)，并发写入与唯一约束锁行为参考 [MySQL 8.4 InnoDB 文档](https://dev.mysql.com/doc/refman/8.4/en/innodb-locks-set.html)。面经仅用于选择提问方向，技术行为以测试和官方资料为准。
