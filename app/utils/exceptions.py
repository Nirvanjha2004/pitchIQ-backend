"""Custom exceptions"""

class PitchIQException(Exception):
    """Base exception for PitchIQ"""
    pass

class ValidationError(PitchIQException):
    """Invalid input"""
    pass

class RateLimitError(PitchIQException):
    """Rate limit exceeded"""
    pass

class LLMError(PitchIQException):
    """LLM API error"""
    pass

class NotFoundError(PitchIQException):
    """Resource not found"""
    pass

class AuthenticationError(PitchIQException):
    """Authentication failed"""
    pass
