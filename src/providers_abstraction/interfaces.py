"""Provider 接口契约定义。

每个接口只定义方法签名和返回类型，不包含实现。
未来任何新方法成功后：new implementation → provider → validation →
capability registry → live test，无需重构 OpenClaw。
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional, Protocol, runtime_checkable


# ══════════════════════════════════════════════════════════════
# Enums & Dataclasses
# ══════════════════════════════════════════════════════════════

class RuntimeState(str, Enum):
    STARTING = "STARTING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    RECOVERING = "RECOVERING"
    FAILED = "FAILED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    # Special states
    WAITING_FOR_WECHAT = "WAITING_FOR_WECHAT"
    WAITING_FOR_CREDENTIAL = "WAITING_FOR_CREDENTIAL"
    FOREGROUND_SAFETY_BLOCKED = "FOREGROUND_SAFETY_BLOCKED"


class CapabilityLevel(str, Enum):
    """Six-layer capability model."""
    IMPLEMENTED = "IMPLEMENTED"
    WIRED = "WIRED"
    RUNTIME_READY = "RUNTIME_READY"
    REAL_E2E = "REAL_E2E"
    RECOVERABLE = "RECOVERABLE"
    PRODUCTION_READY = "PRODUCTION_READY"


class CredentialSource(str, Enum):
    USER_PROVIDED = "USER_PROVIDED"
    CONFIGURED = "CONFIGURED"
    ENVIRONMENT = "ENVIRONMENT"
    OS_SUPPORTED = "OS_SUPPORTED"
    UNAVAILABLE = "UNAVAILABLE"


class CredentialStatus(str, Enum):
    PENDING = "PENDING"
    VALIDATING = "VALIDATING"
    READY = "READY"
    INVALID = "INVALID"
    UNAVAILABLE = "UNAVAILABLE"


class DatabaseLifecycleState(str, Enum):
    DISCOVERING = "DISCOVERING"
    DISCOVERED = "DISCOVERED"
    WAITING_FOR_CREDENTIAL = "WAITING_FOR_CREDENTIAL"
    VALIDATING = "VALIDATING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    RECOVERING = "RECOVERING"
    FAILED = "FAILED"


@dataclass
class CapabilityRecord:
    """Evidence-first capability record."""
    capability: str
    status: CapabilityLevel
    evidence_type: str
    timestamp: str
    details: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class TargetIdentity:
    """Unified target model — who is the action for."""
    wxid: Optional[str] = None
    conversation_id: Optional[str] = None
    nickname: Optional[str] = None
    remark: Optional[str] = None
    display_name: Optional[str] = None
    group_id: Optional[str] = None
    raw_query: Optional[str] = None

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class TargetResolution:
    resolved_target: TargetIdentity
    confidence: float
    source: str
    ambiguity: str  # "unique" | "ambiguous" | "not_found"
    timestamp: str
    candidates: list[TargetIdentity] = field(default_factory=list)


@dataclass
class ChatContext:
    """Current chat context — avoid repeated UIA searches."""
    current_target: Optional[TargetIdentity] = None
    current_conversation: Optional[str] = None
    current_chat_verified: bool = False
    last_verified_at: Optional[str] = None
    ui_generation: Optional[int] = None
    uia_generation: Optional[int] = None


@dataclass
class DatabaseCredential:
    source: CredentialSource
    type: str  # "sqlcipher4_passphrase"
    fingerprint: str  # hash of db signature, NOT the key
    database_identity: str  # db path / account
    created_at: str
    validated_at: Optional[str] = None
    status: CredentialStatus = CredentialStatus.PENDING


@dataclass
class WeChatEvent:
    """Unified event model."""
    event_id: str
    event_type: str  # "message.new" | "connection.changed" | ...
    source: str  # "uia" | "database" | "polling" | "synthetic"
    conversation_id: Optional[str] = None
    sender_id: Optional[str] = None
    timestamp: Optional[str] = None
    payload: dict[str, Any] = field(default_factory=dict)
    sequence: int = 0
    dedupe_key: Optional[str] = None


@dataclass
class EventSequence:
    """Ordered event sequence with checkpoint support."""
    events: list[WeChatEvent]
    checkpoint: int  # last consumed sequence
    source: str


@dataclass
class VersionProfile:
    """Version-specific behavior profile — no hardcoded if/else."""
    wechat_version: str
    process_name: str
    main_module: str
    data_layout: str
    sqlcipher_version: int
    key_derivation: str
    uia_class_root: str
    uia_qt_gate: bool
    child_process_types: list[str]
    supported_features: list[str]
    known_limitations: list[str]


@dataclass
class CapabilityProfile:
    """What a version supports — derived from VersionProfile."""
    version: str
    uia_read_only: bool
    uia_foreground_safety: bool
    database_discovery: bool
    database_key_required: bool
    event_broadcast: bool
    send_text: bool
    send_image: bool
    send_file: bool
    group_info: bool


# ══════════════════════════════════════════════════════════════
# Interfaces (Protocols)
# ══════════════════════════════════════════════════════════════

@runtime_checkable
class IUIAutomationProvider(Protocol):
    """UIA provider — can be replaced without touching OpenClaw."""

    def initialize(self) -> dict[str, Any]:
        """Active initialization (Qt gate, etc.)."""
        ...

    def ready(self) -> bool:
        """Is the UIA tree connected and ready for passive reads?"""
        ...

    def get_chats(self) -> list[dict]:
        ...

    def get_current_chat(self) -> dict | None:
        ...

    def search_chat(self, name: str) -> list[dict]:
        ...

    def open_chat(self, name: str) -> dict:
        ...

    def read_messages(self, limit: int = 20) -> dict:
        ...

    def send_text(self, text: str) -> dict:
        ...

    def send_image(self, path: str) -> dict:
        ...

    def send_file(self, path: str) -> dict:
        ...

    def diagnostics(self) -> dict:
        ...

    def recover(self) -> bool:
        """Attempt to rebuild connection."""
        ...

    def shutdown(self) -> None:
        ...


@runtime_checkable
class IDatabaseProvider(Protocol):
    """Database provider — future key resolver plugs in here."""

    def discover(self) -> dict[str, Any]:
        """Discover databases, accounts, layout."""
        ...

    def status(self) -> dict[str, Any]:
        """Current database state (key, layout, read_plane)."""
        ...

    def open(self, credentials: Optional[DatabaseCredential] = None) -> bool:
        """Open databases with optional credentials."""
        ...

    def validate(self) -> bool:
        """Validate that databases are readable."""
        ...

    def query(self, query: str, params: tuple = ()) -> list[dict]:
        """Execute a read-only SQL query."""
        ...

    def subscribe(self) -> Any:
        """Subscribe to database change events (future)."""
        ...

    def close(self) -> None:
        ...


@runtime_checkable
class IMessageStore(Protocol):
    def get_messages(self, chat_id: str, limit: int = 20,
                     before: str | None = None) -> list[dict]:
        ...

    def search_messages(self, query: str,
                        chat_id: str | None = None) -> list[dict]:
        ...


@runtime_checkable
class IConversationStore(Protocol):
    def get_chats(self) -> list[dict]:
        ...

    def get_current_chat(self) -> dict | None:
        ...


@runtime_checkable
class IContactStore(Protocol):
    def get_contacts(self) -> list[dict]:
        ...

    def search_contact(self, name: str) -> list[dict]:
        ...


@runtime_checkable
class IEventSource(Protocol):
    """Event source — can be DB, UIA, polling, or other."""

    def start(self) -> None:
        ...

    def stop(self) -> None:
        ...

    def is_active(self) -> bool:
        ...

    def state(self) -> str:
        """primary | fallback | disabled | degraded"""
        ...

    def last_event(self) -> WeChatEvent | None:
        ...


@runtime_checkable
class IMediaTransport(Protocol):
    def send_text(self, target: TargetIdentity, text: str) -> dict:
        ...

    def send_image(self, target: TargetIdentity, path: str) -> dict:
        ...

    def send_file(self, target: TargetIdentity, path: str) -> dict:
        ...


class ITextTransport(Protocol):
    def send(self, target: TargetIdentity, text: str) -> dict:
        ...


class IImageTransport(Protocol):
    def send(self, target: TargetIdentity, path: str) -> dict:
        ...


class IFileTransport(Protocol):
    def send(self, target: TargetIdentity, path: str) -> dict:
        ...


@runtime_checkable
class ITargetResolver(Protocol):
    def resolve(self, query: str) -> TargetResolution:
        ...

    def current_context(self) -> ChatContext:
        ...

    def set_context(self, context: ChatContext) -> None:
        ...


@runtime_checkable
class IOCRProvider(Protocol):
    def recognize(self, image_path: str) -> dict:
        ...


@runtime_checkable
class ILifecycleProvider(Protocol):
    def state(self) -> RuntimeState:
        ...

    def health(self) -> dict[str, Any]:
        ...

    def recover(self) -> bool:
        ...

    def shutdown(self) -> None:
        ...


@runtime_checkable
class ICredentialResolver(Protocol):
    """The key breakthrough interface.

    Future: when a new合法 database access method is found,
    implement this interface and register it. The entire message
    system becomes available without rewriting anything.
    """

    def resolve(self, database_identity: str) -> DatabaseCredential:
        ...

    def status(self) -> CredentialStatus:
        ...

    def source(self) -> CredentialSource:
        ...
