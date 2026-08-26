"""Public Beta Local Bridge 的低强度执行约束。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PublicExecutionProfile:
    instruction: str
    reasoning_effort: str = "low"
    expected_seconds: float = 180.0

    def __post_init__(self) -> None:
        if self.reasoning_effort != "low":
            raise ValueError("Public Beta 只能使用 low reasoning effort。")
        if self.expected_seconds <= 0:
            raise ValueError("预计时间必须大于零。")


def public_beta_profile(workflow: str) -> PublicExecutionProfile:
    boundary = "ADVANCED_CASE_NOT_SUPPORTED"
    if workflow == "outfit_transfer":
        instruction = (
            "执行公开 Beta 简单换装路线：Studio 已完成第二张图的尺寸归一化和服装主体预处理，"
            "附加的第二张图就是可直接使用的服装输入。不要再次处理图片。只做快速参考分析、"
            "一次 ImageGen 和一次基础结果观察；通过来源、安全、人体、身份与严格交付检查后立即 Early Exit。"
            f"遇到复杂风险、检查失败或无法判断时返回 {boundary}，不继续生成，也不返回候选图。"
        )
    else:
        instruction = (
            "执行公开 Beta 延迟优先路线：只做必要预处理、快速参考分析、一次 ImageGen 和一次基础结果观察；"
            "通过来源、安全、人体、身份与严格交付检查后立即 Early Exit。"
            f"遇到复杂风险、检查失败或无法判断时返回 {boundary}，不继续生成，也不返回候选图。"
        )
    return PublicExecutionProfile(instruction=instruction)
