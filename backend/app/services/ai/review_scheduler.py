import random
import threading
import time

from app.core.config import settings
from app.services.ai.groq_service import GroqRateLimitError


class ReviewRateLimitExhausted(Exception):
    def __init__(self, retry_after: float | None = None):
        super().__init__("Groq rate limit retry limit reached.")
        self.retry_after = retry_after


class GroqReviewScheduler:
    """Process-wide serialized request gate with bounded 429 retries."""
    def __init__(self, concurrency=1, min_interval=2.5, max_retries=3, backoff_base=2.0,
                 max_retry_wait=120.0, jitter=0.8, *, sleep=time.sleep,
                 random_uniform=random.uniform, monotonic=time.monotonic):
        self._slots=threading.BoundedSemaphore(max(1,int(concurrency)))
        self.min_interval=max(0.0,float(min_interval))
        self.max_retries=max(0,int(max_retries))
        self.backoff_base=max(0.01,float(backoff_base))
        self.max_retry_wait=max(0.0,float(max_retry_wait))
        self.jitter=max(0.0,float(jitter))
        self._sleep=sleep;self._uniform=random_uniform;self._monotonic=monotonic
        self._spacing_lock=threading.Lock();self._last_start=0.0

    def _pace(self):
        # Serialize start-time checks, even when deployments opt into >1 in-flight call.
        with self._spacing_lock:
            delay=self.min_interval-(self._monotonic()-self._last_start)
            if delay>0:self._sleep(delay)
            self._last_start=self._monotonic()

    def run(self, request):
        with self._slots:
            for attempt in range(self.max_retries+1):
                self._pace()
                try:return request()
                except GroqRateLimitError as exc:
                    if attempt>=self.max_retries:raise ReviewRateLimitExhausted(exc.retry_after) from exc
                    delay=exc.retry_after
                    if delay is None:
                        delay=self.backoff_base*(2**attempt)+self._uniform(0.0,self.jitter)
                    if delay>self.max_retry_wait:
                        raise ReviewRateLimitExhausted(delay) from exc
                    self._sleep(max(0.0,delay))


review_scheduler=GroqReviewScheduler(
    concurrency=settings.review_ai_concurrency,
    min_interval=settings.review_ai_min_interval_seconds,
    max_retries=settings.review_ai_max_retries,
    backoff_base=settings.review_ai_backoff_base_seconds,
    max_retry_wait=settings.review_ai_max_retry_wait_seconds,
    jitter=settings.review_ai_retry_jitter_seconds,
)
