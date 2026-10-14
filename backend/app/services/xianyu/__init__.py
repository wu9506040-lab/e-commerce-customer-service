"""
闲鱼业务层（V16.1）

模块组成：
- watcher: XianyuWatcher（subprocess + JSONL 解析 + 崩溃恢复）
- debounce_queue: DebounceQueue（30s 静默合并）
- buyer_profile: BuyerProfile（拉取/缓存）
- phase_detector: PhaseDetector（订单 + 意图两阶段）
- reply_scheduler: ReplyScheduler（随机延迟 + Agent 调 + send）

调用链路（自上而下）：
  XianyuWatcher (新事件)
    → DebounceQueue (30s 静默合并)
    → ReplyScheduler (随机延迟)
      → BuyerProfile (拉买家画像)
      → PhaseDetector (判定售前/售中/售后)
      → 智能客服 /api/chat (复用核心)
      → XianyuMCPClient.send_message (复用 V16)
"""
