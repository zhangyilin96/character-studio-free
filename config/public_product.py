"""Character Studio Public Beta 的独立产品配置。"""

from __future__ import annotations

from character_workflow.versioning import PRODUCT_VERSION


PRODUCT_NAME = "Character Studio"
PRODUCT_DISPLAY_VERSION = "Beta 0.1"
DISTRIBUTION_PUBLIC_BETA = "public_beta"


def require_public_beta(value: str | None) -> str:
    """只接受公开发行模式，避免公开入口回落到其他产品分支。"""

    normalized = (value or DISTRIBUTION_PUBLIC_BETA).strip().casefold()
    if normalized != DISTRIBUTION_PUBLIC_BETA:
        raise ValueError("公开安装包只支持 public_beta 发行模式。")
    return DISTRIBUTION_PUBLIC_BETA


PUBLIC_PRODUCT_INFO = {
    "name": PRODUCT_NAME,
    "display_version": PRODUCT_DISPLAY_VERSION,
    "product_version": PRODUCT_VERSION,
    "distribution_mode": DISTRIBUTION_PUBLIC_BETA,
}
