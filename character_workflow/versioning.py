"""产品层的单一版本来源。

冻结核心、Pipeline schema 和 Provider 协议保持各自的兼容版本，
不把它们强行改成产品版本。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class VersionSet:
    product: str
    engine: str
    skill_interface: str
    pipeline_schema: str
    frozen_core: str
    provider_protocol: str
    studio_interface: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


VERSIONS = VersionSet(
    product="0.1.0-beta.2",
    engine="1.8.1",
    skill_interface="2.0",
    pipeline_schema="1.8.1",
    frozen_core="v1.7-stable+v1.5-selection",
    provider_protocol="1.0",
    studio_interface="1.2.0",
)

PRODUCT_VERSION = VERSIONS.product
ENGINE_VERSION = VERSIONS.engine
SKILL_INTERFACE_VERSION = VERSIONS.skill_interface
PIPELINE_SCHEMA_VERSION = VERSIONS.pipeline_schema
FROZEN_CORE_VERSION = VERSIONS.frozen_core
PROVIDER_PROTOCOL_VERSION = VERSIONS.provider_protocol
STUDIO_INTERFACE_VERSION = VERSIONS.studio_interface
