"""Qdrant collection access and repository-scoped hybrid retrieval."""
from __future__ import annotations

from collections import OrderedDict
from threading import RLock
from qdrant_client import QdrantClient, models
from app.core.config import settings
from app.services.embeddings.service import dimension
from app.services.retrieval.query_analysis import analyze_query
from app.services.retrieval.pipeline import SearchHit, filter_repository_points, retrieve_results, scroll_all_pages

COLLECTION = "repo_code"
client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key or None, timeout=15)
_CACHE_MAX_REPOS = 8
_POINT_CACHE: OrderedDict[tuple[int, str | None], list] = OrderedDict()
_CACHE_LOCK = RLock()

def ensure_collection():
    if not client.collection_exists(COLLECTION):
        client.create_collection(COLLECTION, vectors_config=models.VectorParams(size=dimension(), distance=models.Distance.COSINE))
        for field in ("repository_id", "language", "file_path", "symbol_type", "branch"):
            client.create_payload_index(COLLECTION, field, field_schema=models.PayloadSchemaType.KEYWORD)

def upsert(points):
    ensure_collection()
    if points:
        client.upsert(COLLECTION, points=points, wait=True)

def delete_repository(repository_id: int):
    """Delete all points for a repository; reserved for explicit repository removal."""
    if client.collection_exists(COLLECTION):
        client.delete(COLLECTION, points_selector=models.FilterSelector(filter=models.Filter(must=[models.FieldCondition(key="repository_id", match=models.MatchValue(value=repository_id))])), wait=True)
    clear_repository_cache(repository_id)

def delete_generation(repository_id: int, generation: str):
    if client.collection_exists(COLLECTION):
        selector = models.FilterSelector(filter=models.Filter(must=[
            models.FieldCondition(key="repository_id", match=models.MatchValue(value=repository_id)),
            models.FieldCondition(key="index_generation", match=models.MatchValue(value=generation)),
        ]))
        client.delete(COLLECTION, points_selector=selector, wait=True)
    clear_repository_cache(repository_id)

def delete_stale_generations(repository_id: int, active_generation: str):
    """Remove prior (including legacy, untagged) chunks after the active pointer commits."""
    if client.collection_exists(COLLECTION):
        selector = models.FilterSelector(filter=models.Filter(
            must=[models.FieldCondition(key="repository_id", match=models.MatchValue(value=repository_id))],
            must_not=[models.FieldCondition(key="index_generation", match=models.MatchValue(value=active_generation))],
        ))
        client.delete(COLLECTION, points_selector=selector, wait=True)
    clear_repository_cache(repository_id)

def clear_repository_cache(repository_id: int | None = None):
    with _CACHE_LOCK:
        if repository_id is None:
            _POINT_CACHE.clear()
        else:
            for key in [k for k in _POINT_CACHE if k[0] == repository_id]:
                _POINT_CACHE.pop(key, None)

def _load_repository_points(repository_id: int, generation: str | None) -> list:
    key = (repository_id, generation)
    with _CACHE_LOCK:
        if key in _POINT_CACHE:
            _POINT_CACHE.move_to_end(key)
            return _POINT_CACHE[key]
    repository_filter = models.Filter(must=[models.FieldCondition(key="repository_id", match=models.MatchValue(value=repository_id))])
    def fetch(offset):
        return client.scroll(COLLECTION, scroll_filter=repository_filter, limit=512, offset=offset, with_payload=True, with_vectors=False)
    points = scroll_all_pages(fetch, page_size=512)
    # Local validation is intentional defense-in-depth, and preserves legacy untagged points.
    points = filter_repository_points(points, repository_id, generation)
    with _CACHE_LOCK:
        _POINT_CACHE[key] = points
        _POINT_CACHE.move_to_end(key)
        while len(_POINT_CACHE) > _CACHE_MAX_REPOS:
            _POINT_CACHE.popitem(last=False)
    return points

def repository_points(repository_id: int, generation: str | None = None) -> list:
    ensure_collection()
    return _load_repository_points(repository_id, generation)

def file_points(repository_id: int, file_path: str, generation: str | None = None) -> list:
    ensure_collection()
    must = [models.FieldCondition(key="repository_id", match=models.MatchValue(value=repository_id)),
            models.FieldCondition(key="file_path", match=models.MatchValue(value=file_path))]
    if generation:
        must.append(models.FieldCondition(key="index_generation", match=models.MatchValue(value=generation)))
    file_filter = models.Filter(must=must)
    def fetch(offset):
        return client.scroll(COLLECTION, scroll_filter=file_filter, limit=256, offset=offset, with_payload=True, with_vectors=False)
    points = scroll_all_pages(fetch, page_size=256)
    return [p for p in points if p.payload.get("repository_id") == repository_id
            and p.payload.get("file_path") == file_path
            and (not generation and not p.payload.get("index_generation") or generation and p.payload.get("index_generation") == generation)]

def repository_files(repository_id: int, generation: str | None = None) -> list[str]:
    return sorted({str(p.payload.get("file_path", "")) for p in repository_points(repository_id, generation)
                   if p.payload.get("file_path")})

def _repository_filter(repository_id: int, generation: str | None):
    must = [models.FieldCondition(key="repository_id", match=models.MatchValue(value=repository_id))]
    if generation:
        must.append(models.FieldCondition(key="index_generation", match=models.MatchValue(value=generation)))
    return models.Filter(must=must)

def search(repository_id: int, vector: list[float], query: str, limit: int | None = None,
           index_generation: str | None = None) -> list[SearchHit]:
    """Run dense and full-corpus lexical retrieval, then RRF + diversity selection."""
    ensure_collection()
    profile = analyze_query(query)
    requested = limit or profile.top_k
    configured_limit = max(1, settings.rag_max_context_chunks)
    if limit is None and any(intent in profile.intents for intent in ("workflow", "cross_file", "frontend_backend")):
        # Multi-file questions need a few more candidate files, but remain bounded
        # by both the profile and the configured context-token budget.
        budget_limit = max(1, settings.rag_context_token_budget // 500)
        result_limit = min(max(configured_limit, profile.top_k), budget_limit, 24)
    else:
        result_limit = min(requested, configured_limit)
    dense_limit = min(max(result_limit * 12, 96), 240)
    result = client.query_points(COLLECTION, query=vector, query_filter=_repository_filter(repository_id, index_generation),
                                limit=dense_limit, with_payload=True).points
    dense = filter_repository_points(result, repository_id, index_generation)
    all_points = _load_repository_points(repository_id, index_generation)
    return retrieve_results(dense, all_points, profile, result_limit)
