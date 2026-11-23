"""Safe, staged repository indexing. New points are activated only after upload completes."""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from fastapi import HTTPException
from git import Repo
from qdrant_client import models

from app.core.config import settings
from app.services.parsing.chunker import CHUNKER_VERSION, SKIP_DIRS, chunks, is_sensitive_file, language_for_path
from app.services.embeddings.service import embed_texts
from app.services.vector_store.qdrant import delete_generation, upsert

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class IndexResult:
    owner: str
    name: str
    branch: str
    commit_sha: str
    generation: str
    file_count: int
    chunk_count: int
    summary: str

def parse_github_url(url: str):
    p = urlparse(url)
    parts = [x for x in p.path.strip("/").split("/") if x]
    if p.scheme != "https" or p.hostname != "github.com" or p.username or p.password or len(parts) != 2:
        raise HTTPException(422, "Use a public GitHub URL like https://github.com/owner/repository")
    owner, repo = parts
    if not all(x and x not in (".", "..") and x.replace("-", "").replace("_", "").isalnum() for x in (owner, repo)):
        raise HTTPException(422, "Invalid GitHub repository URL")
    return owner, repo.removesuffix(".git")

def _point_id(repository_id: int, generation: str, chunk: dict) -> str:
    raw = f"{repository_id}:{generation}:{chunk['file_path']}:{chunk['symbol_name']}:{chunk['content_hash']}"
    return str(uuid.UUID(hex=hashlib.sha256(raw.encode()).hexdigest()[:32]))


def _repository_file_inventory(root: Path, candidates: list[Path]) -> list[str]:
    """Return safe repository paths for distinguishing present files from code references."""
    inventory = []
    for path in candidates:
        relative = path.relative_to(root)
        if (path.is_symlink() or not path.is_file() or any(part in SKIP_DIRS for part in relative.parts)
                or is_sensitive_file(path)):
            continue
        inventory.append(relative.as_posix())
        if len(inventory) >= settings.max_files:
            break
    return inventory

def index_repository(repository) -> IndexResult:
    """Build a new generation in Qdrant without deleting the repository's active points."""
    owner, name = parse_github_url(repository.url)
    temp = tempfile.mkdtemp(prefix="repoinsight-")
    generation = ""
    try:
        path = os.path.join(temp, "repo")
        git = Repo.clone_from(repository.url, path, depth=1, branch=repository.branch or None,
                              multi_options=["--filter=blob:none"],
                              env={"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.followRedirects",
                                   "GIT_CONFIG_VALUE_0": "false"})
        commit_sha = git.head.commit.hexsha
        generation = hashlib.sha256(f"{commit_sha}:{CHUNKER_VERSION}".encode()).hexdigest()
        branch = git.active_branch.name if not git.head.is_detached else repository.branch
        root = Path(path)
        all_chunks: list[dict] = []
        total_bytes = 0
        processed_files = 0
        candidates = sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix().lower())
        repository_file_inventory = _repository_file_inventory(root, candidates)
        for p in candidates:
            rel = p.relative_to(root)
            if p.is_symlink() or not p.is_file() or any(part in SKIP_DIRS for part in rel.parts):
                continue
            if is_sensitive_file(p) or not language_for_path(rel.as_posix()):
                continue
            if processed_files >= settings.max_files:
                break
            try:
                file_size = p.stat().st_size
                if file_size > settings.max_file_size_mb * 1024 * 1024:
                    continue
                if total_bytes + file_size > settings.max_repository_size_mb * 1024 * 1024:
                    break
                raw = p.read_bytes()
                if b"\0" in raw:
                    continue
                content = raw.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            file_chunks = chunks(rel.as_posix(), content)
            processed_files += 1
            total_bytes += len(raw)
            all_chunks.extend(file_chunks)

        # A single bounded manifest lets architecture analysis verify that a
        # referenced model/dataset file exists without embedding binary/data
        # contents or adding a database schema change.
        if all_chunks:
            all_chunks[0]["repository_file_inventory"] = repository_file_inventory

        # Stage vectors in bounded batches; IDs include commit and chunker version for repeatability.
        for offset in range(0, len(all_chunks), 32):
            batch_chunks = all_chunks[offset:offset + 32]
            vectors = embed_texts([c["content"] for c in batch_chunks])
            points = []
            for chunk, vector in zip(batch_chunks, vectors):
                uid = _point_id(repository.id, generation, chunk)
                payload = {**chunk, "repository_id": repository.id, "repository_name": name,
                           "repository_url": repository.url, "owner": owner, "branch": branch,
                           "commit_sha": commit_sha, "index_generation": generation,
                           "embedding_model": settings.embedding_model, "chunk_id": uid}
                points.append(models.PointStruct(id=uid, vector=vector, payload=payload))
            if points:
                upsert(points)

        return IndexResult(owner, name, branch, commit_sha, generation, processed_files, len(all_chunks),
                           f"Indexed {processed_files} source files and {len(all_chunks)} chunks at {commit_sha[:10]}.")
    except Exception:
        # Keep the previously active generation. Remove only this run's staged data when safe.
        if generation and generation != getattr(repository, "index_generation", None):
            try:
                delete_generation(repository.id, generation)
            except Exception:
                logger.exception("Could not clean incomplete Qdrant generation for repository %s", repository.id)
        raise
    finally:
        shutil.rmtree(temp, ignore_errors=True)
