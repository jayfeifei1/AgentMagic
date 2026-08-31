# 待修复问题

## P1：复合问题未触发 Multi-Agent 协同

- 状态：待修复
- 发现时间：2026-08-31
- 复现请求：`我登录后台提示 500，同时订单 ORD-20260831-002 被重复扣款 99 元，帮我同时排查技术和账单问题。`
- 实际结果：意图识别为 `refund`，仅 Billing Agent 执行；Trace `080dcb88` 的 `supporting_agents` 为空。
- 预期结果：Technical Agent 与 Billing Agent 并行执行，由 ResponseComposer 合并回答，并在 Trace 中记录两类 Agent 和 Composer 的 LLM 调用。
- 定位：`_route_decision()` 仅依据 `_domain_scores()` 的辅助 Agent 阈值筛选；本例 Technical 得分为 `0.38`，低于 `0.45`。代码中已有 `_collaboration_targets()` 的复合关键词识别逻辑，但当前没有被路由流程调用。
- 修复方向：在路由决策中显式合并复合关键词候选 Agent，并保留分数阈值作为降级保护；补充真实场景回归测试，断言 Trace 包含 `technical`、`billing`、`composer` 三类 LLM 调用。

后续迭代计划：
1.系统中假的工具 可以结合mysql落成真是工具
2.转人工 调研下当前系统当用户明确转人工后的行为逻辑，是否有后续
3.模型无法闭环需要转人工 可以前端弹弹窗按钮，用户点击后 转人工，这里也可以生成工单落库来模拟转人工的行为