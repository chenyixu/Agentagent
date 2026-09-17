"""知识检索：中文分词、切块与 BM25 近似打分。"""

from .index import CHUNK_CHAR_LIMIT, ScoredChunk, bm25_scores, build_chunks
from .tokenizer import DOMAIN_LEXICON, STOPWORDS, token_coverage, tokenize, unique_tokens

__all__ = [
    "CHUNK_CHAR_LIMIT",
    "DOMAIN_LEXICON",
    "STOPWORDS",
    "ScoredChunk",
    "bm25_scores",
    "build_chunks",
    "token_coverage",
    "tokenize",
    "unique_tokens",
]
