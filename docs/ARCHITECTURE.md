# 技术架构与边界

系统分为三个独立逻辑部分：经营分析（只读指标与 SQL）、模型调用（受限工具及执行轨迹）和运营状态机（人工审批、业务写入、持久化恢复）。

- **前端：** React + TypeScript + Vite + ECharts，包含决策总览、Agent 工作台、运营处置中心与任务历史。
- **后端：** FastAPI REST/SSE；模型只使用受约束的查询工具；运营写入通过独立 API、授权令牌、事务与状态校验进行。
- **数据：** 自生成 SQLite 经营数据、模拟运营与审计状态。三场景演示使用独立状态目录及 Compose 项目。
- **可靠性：** SQL 安全校验、模型建议快照检查、分阶段人工关口、幂等操作与恢复处理；并非生产级认证、跨系统分布式事务或实时 ERP 集成。

可验证代码见 `backend/app/`、`backend/app/ops/`、`frontend/src/`、`backend/tests/` 与 `frontend/tests/`。
