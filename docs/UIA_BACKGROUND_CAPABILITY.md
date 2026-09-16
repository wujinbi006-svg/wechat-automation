# UIA Background Capability Matrix

> WeChat 4.1.13.12 | Windows 11 Home | Accessibility Gate: Activated
> Last Updated: 2026-09-02

## Summary

| Operation | Background | Foreground Required | Intrusive Step | Evidence | Result |
|-----------|:----------:|:-------------------:|:--------------:|----------|:------:|
| ControlFromHandle | YES | NO | - | focus_v5 | ✅ PASS |
| GetChildren | YES | NO | - | focus_v5 | ✅ PASS |
| Find control by ClassName | YES | NO | - | focus_v5 | ✅ PASS |
| Read control properties | YES | NO | - | focus_v5 | ✅ PASS |
| GetPattern(ValuePattern) | YES | NO | - | focus_v5 | ✅ PASS |
| GetPattern(TextPattern) | YES | NO | - | input_pattern_audit | ✅ PASS |
| GetPattern(InvokePattern) | YES | NO | - | input_pattern_audit | ✅ PASS |
| GetPattern(SelectionItemPattern) | YES | NO | - | input_pattern_audit | ✅ PASS |
| Select(cell) / open_chat | **NO** | **YES** | Select() | dynamic_validation | 🚫 CONFIRMED |
| SetValue (search field) | **NO** | **YES** | SetValue | focus_v5 | 🚫 CONFIRMED |
| LegacyIAccessible | N/A | N/A | - | - | ❌ NOT_AVAILABLE |
| WM_SETTEXT | N/A | N/A | - | - | ❌ NO_HWND |

## Accessibility Gate

| Property | Value |
|----------|-------|
| Weixin.dll Base | 0x7FF928430000 |
| Gate RVA | 0xAD19668 |
| Cold State | 0 |
| Activated | 1 |
| UIA Before | 2 nodes, 1 mmui |
| UIA After | 88 nodes, 82 mmui |

## Control Inventory

### Accessible via UIA (gate activated)

| Control | ClassName | Pattern | Background Read |
|---------|-----------|---------|:---------------:|
| Search Field | mmui::XSearchField | - | ✅ |
| Text Input (Search) | mmui::XValidatorTextEdit | Value, Text, Invoke | ✅ |
| Session List | mmui::ChatSessionList | - | ✅ |
| Session Cell | mmui::ChatSessionCell | SelectionItem, Invoke | ✅ |
| Chat Detail View | mmui::ChatDetailView | - | ✅ |
| Chat Master View | mmui::ChatMasterView | - | ✅ |
| Chat Page | mmui::ChatPage | - | ✅ |
| Table View | mmui::XTableView | - | ✅ |
| Text View | mmui::XTextView | Text | ✅ |

### NOT accessible via UIA

| Control | Notes |
|---------|-------|
| Chat Input Field | Not exposed as UIA element. Rendered inside MMUIRenderSubWindowHW. |
| Contact List | May not be exposed |
| Message List | May not be exposed |

## Intrusive Operations

### SetValue (search field)
- **Foreground BEFORE**: ChatGPT
- **Foreground AFTER**: Weixin
- **First intrusive step**: S08 (SetValue)
- **Status**: FOREGROUND_REQUIRED

### Select(cell) / open_chat
- **Foreground BEFORE**: ChatGPT
- **Foreground AFTER**: Weixin
- **Status**: FOREGROUND_REQUIRED

## Background Capable Operations

All read-only UIA operations are safe to execute from background:
- ControlFromHandle
- FindAll / FindFirst (all scopes)
- GetCurrentPattern (all pattern types)
- GetSupportedPatterns
- Read properties (Name, ClassName, IsEnabled, etc.)

## Architecture

```
WeChatService
├── AccessibilityActivator  (gate 0→1)
├── UIAAdapter              (control: search, navigation → FOREGROUND_REQUIRED)
├── DatabaseAdapter         (read: contacts, chats, messages → BACKGROUND)
└── MinimalForegroundInput  (manages foreground for SetValue operations)
```