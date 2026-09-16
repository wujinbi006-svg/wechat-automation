class WeChatError(Exception): pass
class WeChatNotRunning(WeChatError): pass
class AccessibilityUnavailable(WeChatError): pass
class AccessibilityActivationFailed(WeChatError): pass
class ForegroundRequired(WeChatError): pass
class ChatNotFound(WeChatError): pass
class ProviderUnavailable(WeChatError): pass
class DatabaseUnavailable(ProviderUnavailable): pass
class OperationTimeout(WeChatError): pass
class ForegroundRestoreFailed(WeChatError): pass
class UIAOperationTimeout(WeChatError):
    def __init__(self, operation, timeout):
        self.operation, self.timeout = operation, timeout
        super().__init__(f"{operation} 超时（{timeout}s）")
