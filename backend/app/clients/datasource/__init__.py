"""
数据源抽象层（CLAUDE.md §9.3 Interface First）

- protocols.py：DataSourceProtocol（接口就近放置）
- static_seed_source.py：当前实现，从 MySQL 读种子数据
- mock_source.py：单测用 mock 实现（M18+ 真正接 Taobao Adapter 时再做）

何时启用：
- 当前（M14 V3）：业务层仍走 ProductTool/OrderTool（保持 V14.x 兼容）
- V3.2 规划（M15+）：OrderService / ProfileService 通过 Depends 注入 DataSource
  → 启用后可低成本接入自更新（Taobao webhook → 同步到 MySQL → DataSource 透明读取）

禁止：
- 业务模块直接 `from app.clients.datasource import StaticSeedSource`
  → 必须依赖 Protocol，不能依赖具体实现（CLAUDE.md §9.2.2 跨模块侵入禁止）
"""
