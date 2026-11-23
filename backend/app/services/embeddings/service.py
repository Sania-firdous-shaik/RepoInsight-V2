from functools import lru_cache
from sentence_transformers import SentenceTransformer
from app.core.config import settings

@lru_cache(maxsize=1)
def model(): return SentenceTransformer(settings.embedding_model, device="cpu")

def embed_texts(texts: list[str]) -> list[list[float]]:
    vectors = model().encode(texts, batch_size=32, normalize_embeddings=True, show_progress_bar=False)
    return vectors.tolist()

def dimension() -> int: return model().get_sentence_embedding_dimension()
