# Spec2Repo — formerly Paper2Repo Agent

本仓库已从论文复现 Agent 演进为 **规格驱动的 Coding Agent 后端**。新版位于 [`spec2repo/`](spec2repo/README.md)，包含自主工具循环、任务记忆、异步任务服务、故障恢复、Docker 隔离和独立验收。

**从这里开始：[新版介绍与运行指南](spec2repo/README.md)**

- [Agent 启动、计划审批与规格澄清](spec2repo/docs/RUN_AGENT.md)
- [独立验收、修复与成果下载](spec2repo/docs/RUN_VERIFICATION.md)
- [记忆与上下文管理](spec2repo/docs/MEMORY_CONTEXT.md)
- [轨迹评估与停滞干预](spec2repo/docs/AGENT_EVALUATION.md)
- [真实 Agent 实验](spec2repo/docs/AGENT_BENCHMARK.md)

旧 Paper2Repo 实现继续保留在仓库根目录的 `src/` 等目录，说明见 [旧版 README](README.paper2repo.md)。两个版本各有独立依赖与运行方式；运行新版请先进入 `spec2repo` 目录。

实验口径：16 Worker 固定任务批次为 11/16 成功、4.74 个验收通过任务/分钟；上下文小样本实验尚未证明 token 节省。完整条件和原始报告见新版文档。
