"""Provider 抽象接口层 — 未来突破接入点。

本包定义所有底层实现的可替换接口。当前生产实现 (src/providers.py,
src/uia_service.py, src/database_adapter.py) 保持不变；未来新实现
只需实现这些接口并注册到 ProviderRegistry，无需修改 OpenClaw 核心。

不包含任何具体实现逻辑；仅为契约定义。
"""
from __future__ import annotations

from .interfaces import (
    # Core interfaces
    IUIAutomationProvider,
    IDatabaseProvider,
    IMessageStore,
    IConversationStore,
    IContactStore,
    IEventSource,
    IMediaTransport,
    ITextTransport,
    IImageTransport,
    IFileTransport,
    ITargetResolver,
    IOCRProvider,
    ILifecycleProvider,
    # Credential
    ICredentialResolver,
    DatabaseCredential,
    CredentialSource,
    CredentialStatus,
    # Identity
    TargetIdentity,
    TargetResolution,
    ChatContext,
    # Events
    WeChatEvent,
    EventSequence,
    # Lifecycle
    RuntimeState,
    CapabilityLevel,
    CapabilityRecord,
    # Version
    VersionProfile,
    CapabilityProfile,
)
from .registry import ProviderRegistry, get_registry

__all__ = [
    "IUIAutomationProvider", "IDatabaseProvider", "IMessageStore",
    "IConversationStore", "IContactStore", "IEventSource",
    "IMediaTransport", "ITextTransport", "IImageTransport", "IFileTransport",
    "ITargetResolver", "IOCRProvider", "ILifecycleProvider",
    "ICredentialResolver", "DatabaseCredential", "CredentialSource",
    "CredentialStatus",
    "TargetIdentity", "TargetResolution", "ChatContext",
    "WeChatEvent", "EventSequence",
    "RuntimeState", "CapabilityLevel", "CapabilityRecord",
    "VersionProfile", "CapabilityProfile",
    "ProviderRegistry", "get_registry",
]
