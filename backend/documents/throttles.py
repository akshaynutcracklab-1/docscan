from rest_framework.throttling import UserRateThrottle


class UploadRateThrottle(UserRateThrottle):
    """60 uploads per minute per user (settings: DEFAULT_THROTTLE_RATES['uploads'])."""

    scope = "uploads"
