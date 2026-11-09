"""Keyword retrieval with an optional vector fallback.

FTS5's unicode61 tokenizer treats a run of CJK characters as one token, so the
index text is expanded with per-character and bigram tokens. Queries are
expanded the same way and scored in Python, which keeps ordering independent of
the tokenizer and makes the LIKE fallback behave like the FTS path.

Keyword hits are returned unchanged. Only when the keyword path finds nothing
and ``ai.embedding`` is enabled does the caller-supplied embedder get used, and
then at most once per search. Any embedding failure, timeout or exhausted
budget falls back to the (empty) keyword result, so retrieval never fails
because the vector index is unavailable.
"""

import re
import unicodedata

from service.ai.budget import BudgetLedger
from service.database import SUBJECT_USER_ID
from service.memory import embeddings as memory_embeddings
from service.memory import policy
from service.memory.store import memory_summary

DEFAULT_LIMIT = 5
MAX_LIMIT = 50
MAX_QUERY_CHARS = 500
CANDIDATE_FACTOR = 10
MAX_CANDIDATES = 200

_WORD = re.compile(r'[0-9a-z_]+')
_CJK = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff]+')


def _normalized(text):
    return unicodedata.normalize('NFKC', text).casefold()


def query_terms(text):
    """Latin words plus CJK unigrams/bigrams, de-duplicated in order."""
    normalized = _normalized(text)
    terms = []
    for match in _WORD.finditer(normalized):
        terms.append(match.group())
    for match in _CJK.finditer(normalized):
        run = match.group()
        if len(run) == 1:
            terms.append(run)
        else:
            terms.extend(run[index:index + 2] for index in range(len(run) - 1))
    seen = set()
    ordered = []
    for term in terms:
        if term and term not in seen:
            seen.add(term)
            ordered.append(term)
    return ordered


def index_text(text):
    """Content plus CJK unigram and bigram tokens for unicode61 indexing."""
    normalized = _normalized(text)
    parts = [normalized]
    for match in _CJK.finditer(normalized):
        run = match.group()
        parts.extend(run)
        if len(run) > 1:
            parts.extend(run[index:index + 2] for index in range(len(run) - 1))
    return ' '.join(parts)


def match_expression(query):
    terms = query_terms(query)
    if not terms:
        return None
    # Terms contain only word characters by construction, so quoting is safe.
    return ' OR '.join('"' + term + '"' for term in terms)


def _score(content, query, terms):
    normalized = _normalized(content)
    hits = sum(1 for term in terms if term in normalized)
    phrase = 2 if _normalized(query).strip() and _normalized(query).strip() in normalized else 0
    return hits + phrase


def _snippet(content, limit=160):
    text = content.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + '…'


def search(store, query, limit=DEFAULT_LIMIT, subject_user_id=SUBJECT_USER_ID,
           settings=None, embedder=None):
    """Return at most limit summaries ordered by hits, importance and recency.

    ``settings`` and ``embedder`` are optional so existing keyword-only callers
    keep their behaviour. Vector retrieval is attempted only when the keyword
    path returns nothing.
    """
    if type(query) is not str or not query.strip():
        return []
    limit = max(1, min(int(limit), MAX_LIMIT))
    results = _keyword_search(store, query, limit, subject_user_id)
    if results:
        return results
    return _vector_search(store, query, limit, subject_user_id, settings, embedder)


def _keyword_search(store, query, limit, subject_user_id):
    terms = query_terms(query)
    if not terms:
        return []
    candidates = store.search_candidates(
        query, terms, min(MAX_CANDIDATES, limit * CANDIDATE_FACTOR), subject_user_id)
    scored = []
    for row in candidates:
        score = _score(row.get('content', ''), query, terms)
        if score <= 0:
            continue
        scored.append((score, row))
    scored.sort(key=lambda item: (
        -item[0],
        -float(item[1].get('importance') or 0.0),
        -int(item[1].get('mention_count') or 0),
        -policy.timestamp_value(item[1].get('last_reinforced_at')),
        -policy.timestamp_value(item[1].get('updated_at')),
    ))
    results = []
    for score, row in scored[:limit]:
        summary = memory_summary(row)
        summary['snippet'] = _snippet(row.get('content', ''))
        summary['score'] = score
        results.append(summary)
    return results


def _embedding_config(settings):
    """Return (ai settings, model, dimensions) when vector retrieval is on."""
    if not isinstance(settings, dict):
        return {}, '', 0
    ai_settings = settings.get('ai') if isinstance(settings.get('ai'), dict) else {}
    embedding = ai_settings.get('embedding')
    if not isinstance(embedding, dict) or not embedding.get('enabled'):
        return {}, '', 0
    model = embedding.get('model')
    model = model if type(model) is str else ''
    dim = embedding.get('dimensions')
    dim = dim if type(dim) is int and not isinstance(dim, bool) else 0
    if not model or dim <= 0:
        return {}, '', 0
    return ai_settings, model, dim


def _budget_exhausted(store, ai_settings):
    database = getattr(store, 'database', None)
    if database is None:
        return False
    try:
        return bool(BudgetLedger(database).status(ai_settings).get('local_only'))
    except Exception:
        return False


def _invoke_embedder(embedder, texts):
    method = getattr(embedder, 'embed', None)
    return method(texts) if callable(method) else embedder(texts)


def _vector_search(store, query, limit, subject_user_id, settings, embedder):
    ai_settings, model, dim = _embedding_config(settings)
    if embedder is None or not model:
        return []
    if _budget_exhausted(store, ai_settings):
        return []
    try:
        # Exactly one embedding request per search.
        outcome = _invoke_embedder(embedder, [query])
        vectors = memory_embeddings.normalize_vectors(outcome, 1, dim)
        return memory_embeddings.rank(store, vectors[0], model, dim, limit,
                                      subject_user_id)
    except Exception as exc:
        code = getattr(exc, 'code', '') or type(exc).__name__
        memory_embeddings.record_error(store, str(code)[:64])
        return []

