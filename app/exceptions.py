class GatewayError(Exception):
    """可安全返回给 API 调用方的业务异常，包含 HTTP 状态和重试提示。"""

    def __init__(self, status_code: int, code: str, detail: str, retryable: bool = False):
        """保存结构化错误信息，供 FastAPI 全局异常处理器转换为 JSON。"""
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.retryable = retryable
        super().__init__(detail)


class TokenInvalid(GatewayError):
    """表示乐享 access_token 已失效的专用错误类型。"""

    def __init__(self) -> None:
        """构造可重试的 401 上游鉴权错误。"""
        super().__init__(401, "UPSTREAM_TOKEN_INVALID", "Lexiang access token is invalid", True)
