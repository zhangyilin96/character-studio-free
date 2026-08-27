# Public Beta 验证与交付策略

公开 Beta 运行 quick analysis、一次 Renderer 生成和 quick observation。

通过后仍必须验证 Renderer receipt、候选来源、角色 rendering domain 和 strict delivery，才能复制为最终结果。

遇到复杂风险、FAIL、NOT_ASSESSABLE、receipt 不可信、domain 不一致或交付门失败时，不返回候选图，并返回中性状态 `ADVANCED_CASE_NOT_SUPPORTED`。
