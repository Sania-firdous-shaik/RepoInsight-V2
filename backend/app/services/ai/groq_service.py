from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import logging
from groq import Groq
from fastapi import HTTPException
from app.core.config import settings

logger = logging.getLogger(__name__)

class GroqRateLimitError(Exception):
    def __init__(self, retry_after: float | None = None):
        super().__init__("Groq rate limit reached.")
        self.retry_after = retry_after

def _retry_after_seconds(response) -> float | None:
    headers=getattr(response,"headers",{}) or {}
    value=headers.get("retry-after") or headers.get("Retry-After")
    if value is None: return None
    try: return max(0.0,float(value))
    except (TypeError,ValueError):
        try:
            deadline=parsedate_to_datetime(str(value))
            if deadline.tzinfo is None: deadline=deadline.replace(tzinfo=timezone.utc)
            return max(0.0,(deadline-datetime.now(timezone.utc)).total_seconds())
        except (TypeError,ValueError,OverflowError): return None

def complete_with_metadata(system: str, user: str, *, max_retries: int = 2, max_tokens: int | None = None,
                           raise_rate_limit: bool = False, json_mode: bool = False,
                           include_usage: bool = False):
    if not settings.groq_api_key or not settings.groq_model: raise HTTPException(503,"Configure GROQ_API_KEY and GROQ_MODEL in backend .env to enable AI answers.")
    try:
        client=Groq(api_key=settings.groq_api_key, timeout=45, max_retries=max_retries)
        completion_limit=min(settings.groq_max_tokens,max_tokens) if max_tokens else settings.groq_max_tokens
        params={"model":settings.groq_model,"temperature":settings.groq_temperature,
            "max_tokens":completion_limit,
            "messages":[{"role":"system","content":system},{"role":"user","content":user}]}
        if json_mode:params['response_format']={"type":"json_object"}
        prompt_estimate=max(1,(len(system)+len(user)+3)//4)
        logger.info("groq_completion_start model=%s max_completion_tokens=%s estimated_prompt_tokens=%s json_mode=%s",
                    settings.groq_model,completion_limit,prompt_estimate,json_mode)
        try:result=client.chat.completions.create(**params)
        except Exception as exc:
            # Some Groq compatible models do not implement JSON mode. Fall back to the same prompt/model.
            if not json_mode or getattr(exc,"status_code",None)!=400:raise
            logger.warning("groq_json_mode_fallback model=%s status=400 error_code=%s",
                           settings.groq_model,_safe_error_code(exc))
            params.pop('response_format',None)
            result=client.chat.completions.create(**params)
        choice=result.choices[0]
        reason=getattr(choice,"finish_reason",None)
        usage=getattr(result,"usage",None)
        usage_info={key:getattr(usage,key,None) for key in ("prompt_tokens","completion_tokens","total_tokens")} if usage else {}
        logger.info("groq_completion_finish model=%s max_completion_tokens=%s finish_reason=%s prompt_tokens=%s completion_tokens=%s total_tokens=%s",
                    settings.groq_model,completion_limit,reason,usage_info.get("prompt_tokens"),
                    usage_info.get("completion_tokens"),usage_info.get("total_tokens"))
        if include_usage:return choice.message.content or "",reason,usage_info
        return choice.message.content or "",reason
    except Exception as exc:
        status=getattr(exc,"status_code",None)
        logger.error("groq_completion_error model=%s status=%s error_code=%s error_type=%s",
                     settings.groq_model,status,_safe_error_code(exc),type(exc).__name__)
        if status in (401,400): raise HTTPException(502,"Groq rejected the configured credentials or model. Check GROQ_API_KEY and GROQ_MODEL.") from exc
        if status==429:
            if raise_rate_limit:raise GroqRateLimitError(_retry_after_seconds(getattr(exc,"response",None))) from exc
            raise HTTPException(503,"Groq rate limit reached. Try again later.") from exc
        raise HTTPException(502,"Groq request failed. Check service availability and configuration.") from exc


def _safe_error_code(exc) -> str | None:
    """Extract only provider error code metadata; never log raw provider bodies or prompts."""
    body=getattr(exc,"body",None)
    if isinstance(body,dict):
        error=body.get("error")
        if isinstance(error,dict) and isinstance(error.get("code"),str):return error["code"][:80]
    return None

def complete(system: str, user: str, *, max_retries: int = 2, max_tokens: int | None = None,
             raise_rate_limit: bool = False, json_mode: bool = False) -> str:
    """Return generated text while keeping the existing caller contract."""
    content, _ = complete_with_metadata(system,user,max_retries=max_retries,max_tokens=max_tokens,
                                        raise_rate_limit=raise_rate_limit,json_mode=json_mode)
    return content
