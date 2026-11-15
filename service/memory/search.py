"""Keyword retrieval with an optional vector fallback.

FTS5's unicode61 tokenizer treats a run of CJK characters as one token, so the
index text is expanded with per-character and bigram tokens. Queries are
expanded the same way. When the FTS index is available, candidates are ranked
by bm25 (which carries IDF, so common filler words cannot outrank distinctive
terms) and that relevance order is preserved through Python scoring; the LIKE
fallback keeps the Python hit-count ordering.

Keyword hits are returned unchanged. Only when the keyword path finds nothing
and ``ai.embedding`` is enabled does the caller-supplied embedder get used, and
then at most once per search. Any embedding failure, timeout or exhausted
budget falls back to the (empty) keyword result, so retrieval never fails
because the vector index is unavailable.

``search_for_context`` reuses the same keyword candidates, re-ranks them with
strength, recall and recency, and merges one vector query when keyword hits are
sparse. It returns plain texts under a strict character budget for prompt
injection and counts the returned memories as recalled.
"""

import re
import sqlite3
import time
import unicodedata

from service.ai.budget import BudgetLedger
from service.ai.embedding import EmbeddingClient
from service.database import SUBJECT_USER_ID
from service.memory import embeddings as memory_embeddings
from service.memory import policy
from service.memory.store import MemoryStore, memory_summary

DEFAULT_LIMIT = 5
MAX_LIMIT = 50
MAX_QUERY_CHARS = 500
CANDIDATE_FACTOR = 10
MAX_CANDIDATES = 200
DEFAULT_CONTEXT_CHARS = 500
CONTEXT_CANDIDATE_LIMIT = 200
MIN_KEYWORD_HITS = 2

# Context ranking blends lexical relevance with the signals that say whether a
# memory has been useful before, so a single hit on a strong, often-recalled
# memory can outrank several shallow hits on a weak one.
RANK_RELEVANCE_WEIGHT = 0.45
RANK_STRENGTH_WEIGHT = 0.30
RANK_RECALL_WEIGHT = 0.15
RANK_RECENCY_WEIGHT = 0.10
RECENCY_HALF_LIFE_SECONDS = 7 * 24 * 60 * 60

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
    scored, _terms = _keyword_candidates(
        store, query, min(MAX_CANDIDATES, limit * CANDIDATE_FACTOR), subject_user_id)
    results = []
    for score, row in scored[:limit]:
        summary = memory_summary(row)
        summary['snippet'] = _snippet(row.get('content', ''))
        summary['score'] = score
        results.append(summary)
    return results


def _keyword_candidates(store, query, candidate_limit, subject_user_id):
    """Score keyword candidates in Python; returns (scored rows, query terms)."""
    terms = query_terms(query)
    if not terms:
        return [], []
    candidates = store.search_candidates(
        query, terms, candidate_limit, subject_user_id)
    scored = []
    for row in candidates:
        score = _score(row.get('content', ''), query, terms)
        if score <= 0:
            continue
        scored.append((score, row))
    if scored and 'fts_rank' in scored[0][1]:
        # FTS 候选已按 bm25 相关性升序给出；保持这一相关性次序，
        # Python 分只在 bm25 同分时作次级裁决。LIKE 兜底路径无
        # fts_rank，沿用原有的词命中排序。
        scored.sort(key=lambda item: (
            float(item[1]['fts_rank']),
            -item[0],
            -int(item[1].get('mention_count') or 0),
            -policy.timestamp_value(item[1].get('updated_at')),
        ))
    else:
        scored.sort(key=lambda item: (
            -item[0],
            -float(item[1].get('importance') or 0.0),
            -int(item[1].get('mention_count') or 0),
            -policy.timestamp_value(item[1].get('last_reinforced_at')),
            -policy.timestamp_value(item[1].get('updated_at')),
        ))
    return scored, terms


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


# -- context retrieval ---------------------------------------------------


def _context_store(database):
    store = getattr(database, 'memory_store_v3', None)
    if store is None:
        store = MemoryStore(database)
        setattr(database, 'memory_store_v3', store)
    return store


def _settings_config(settings):
    """Return ``(config dict, settings source)`` for a dict or SettingsStore."""
    if isinstance(settings, dict):
        return settings, None
    config = getattr(settings, 'settings', None)
    if isinstance(config, dict):
        return config, settings
    return {}, settings


def _keyword_relevance(score, terms):
    # A full match hits every term and may add the two-point phrase bonus.
    return policy.clamp(score / (len(terms) + 2.0))


def _keyword_entries(scored, terms):
    # bm25 次序本身就是相关性信号：按位次给出 (0,1] 的单调衰减相关度，
    # 让上下文融合（strength/recall/recency）建立在真实的词频相关性上，
    # 而不是无 IDF 的词命中计数。LIKE 兜底路径仍用原有关键词相关度。
    by_rank = bool(scored) and 'fts_rank' in scored[0][1]
    entries = []
    for position, (score, row) in enumerate(scored):
        relevance = (1.0 / (1.0 + position)) if by_rank \
            else _keyword_relevance(score, terms)
        summary = memory_summary(row)
        summary['snippet'] = _snippet(row.get('content', ''))
        summary['score'] = score
        entries.append((relevance, summary))
    return entries


def _recall_weight(count):
    try:
        count = max(0, int(count))
    except (TypeError, ValueError):
        return 0.0
    return 1.0 - 1.0 / (1.0 + count)


def _recency_weight(summary, now):
    # 事实发生时间优先（双时态检索侧最小子集）：「我下周三去上海」过期后
    # recency 应随事实时间轴衰减，而不是随被再次提起的 updated_at 回春。
    # 存量行与解析不出时间的行没有 occurred_at（写入侧落 NULL），回退
    # updated_at（Mem0 decay 的 event_date→updated_at 回退链）。
    stamp = policy.timestamp_value(summary.get('occurred_at')
                                   or summary.get('updated_at'))
    if stamp <= 0:
        return 0.0
    age = max(0.0, now - stamp)
    return 0.5 ** (age / RECENCY_HALF_LIFE_SECONDS)


def _rank_score(summary, relevance, now):
    strength = policy.clamp(summary.get('strength') or 0.0)
    recall = _recall_weight(summary.get('recall_count') or 0)
    recency = _recency_weight(summary, now)
    return (RANK_RELEVANCE_WEIGHT * policy.clamp(relevance)
            + RANK_STRENGTH_WEIGHT * strength
            + RANK_RECALL_WEIGHT * recall
            + RANK_RECENCY_WEIGHT * recency)


def _merged_entries(keyword_entries, vector_entries):
    """Deduplicate by memory id, keeping the stronger relevance signal."""
    merged = {}
    for relevance, summary in list(keyword_entries) + list(vector_entries):
        memory_id = summary.get('id')
        if type(memory_id) is not str or not memory_id:
            continue
        current = merged.get(memory_id)
        if current is None or relevance > current[0]:
            merged[memory_id] = (relevance, summary)
    return list(merged.values())


def _ranked_entries(entries, now):
    ranked = []
    for relevance, summary in entries:
        ranked.append((_rank_score(summary, relevance, now), relevance, summary))
    ranked.sort(key=lambda item: (
        -item[0],
        -item[1],
        -float(item[2].get('strength') or 0.0),
        -int(item[2].get('recall_count') or 0),
        # 平局裁决同样以事实发生时间优先，无则回退 updated_at。
        -policy.timestamp_value(item[2].get('occurred_at')
                                or item[2].get('updated_at')),
    ))
    return [item[2] for item in ranked]


def _budget_selection(ranked, total_chars, entry_chars=None):
    """Greedily take texts in rank order under a per-entry and total cap."""
    cap = total_chars if entry_chars is None else max(1, int(entry_chars))
    texts = []
    ids = []
    used = 0
    for summary in ranked:
        remaining = total_chars - used
        if remaining <= 0:
            break
        text = summary.get('text')
        if type(text) is not str or not text:
            text = summary.get('snippet') or ''
        if not text:
            continue
        piece = text[:min(cap, remaining)]
        if not piece:
            continue
        texts.append(piece)
        ids.append(summary.get('id'))
        used += len(piece)
    return texts, ids


def _embed_for_context(store, ai_settings, source, query, model):
    """One embedding request through the injected client or EmbeddingClient."""
    injected = None
    if source is not None:
        injected = getattr(source, 'embedding_client', None)
        if injected is None:
            injected = getattr(source, 'embedding_provider', None)
    if injected is not None:
        method = getattr(injected, 'embed', None)
        outcome = method([query]) if callable(method) else injected([query])
    else:
        transport = getattr(source, 'ai_transport', None) if source is not None else None
        key = getattr(source, 'ai_embedding_key', None) if source is not None else None
        if key is None and source is not None:
            from service.secrets import load_key_for
            key = load_key_for(source)
        outcome = EmbeddingClient(ai_settings, transport=transport).embed([query], key=key)
    _record_embedding_usage(store, ai_settings, outcome, model)
    return outcome


def _record_embedding_usage(store, ai_settings, outcome, model):
    usage = getattr(outcome, 'usage', None)
    if not isinstance(usage, dict):
        return
    try:
        BudgetLedger(store.database).record(
            'embedding', getattr(outcome, 'model', '') or model, usage, ai_settings)
    except Exception:
        # Usage accounting must never break retrieval.
        return


def _context_vector_entries(store, query, settings, limit, subject_user_id):
    config, source = _settings_config(settings)
    ai_settings, model, dim = _embedding_config(config)
    if not model or dim <= 0:
        return []
    if source is None:
        # A bare settings dict carries no transport or key. Calling the
        # embedding client would fail with missing_key and overwrite the real
        # diagnostic error, so keyword retrieval stays authoritative.
        return []
    if _budget_exhausted(store, ai_settings):
        return []
    try:
        outcome = _embed_for_context(store, ai_settings, source, query, model)
        vectors = memory_embeddings.normalize_vectors(outcome, 1, dim)
        summaries = memory_embeddings.rank(
            store, vectors[0], model, dim, limit, subject_user_id)
    except Exception as exc:
        code = getattr(exc, 'code', '') or type(exc).__name__
        memory_embeddings.record_error(store, str(code)[:64])
        return []
    return [(policy.clamp(item.get('score') or 0.0), item) for item in summaries]


def search_for_context(database, query, settings, limit=DEFAULT_LIMIT,
                       total_chars=DEFAULT_CONTEXT_CHARS, entry_chars=None,
                       with_ids=False):
    """Return ranked memory texts that fit a strict character budget.

    ``total_chars`` bounds the injected text as a whole while ``entry_chars``
    caps each memory; they are separate because a per-entry setting must not
    silently become the whole prompt's memory allowance. Keyword retrieval
    supplies the candidates; when it surfaces fewer than two memories and
    embeddings are configured, one vector query is merged in by memory id. Any
    vector failure falls back to the keyword result.

    Recalled memories get ``recall_count += 1`` and a recomputed strength, but
    a failed counter update never fails retrieval. With ``with_ids`` the caller
    receives ``(texts, ids)`` and is responsible for calling
    :func:`record_recall` after deciding what actually reaches the prompt.
    """
    if database is None or type(query) is not str or not query.strip():
        return ([], []) if with_ids else []
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    limit = max(1, min(limit, MAX_LIMIT))
    try:
        total_chars = int(total_chars)
    except (TypeError, ValueError):
        total_chars = DEFAULT_CONTEXT_CHARS
    if total_chars <= 0:
        return ([], []) if with_ids else []
    try:
        store = _context_store(database)
        scored, terms = _keyword_candidates(
            store, query, CONTEXT_CANDIDATE_LIMIT, SUBJECT_USER_ID)
        now = time.time()
        entries = _keyword_entries(scored, terms)
        if len(scored) < MIN_KEYWORD_HITS:
            entries = _merged_entries(
                entries,
                _context_vector_entries(store, query, settings,
                                        CONTEXT_CANDIDATE_LIMIT, SUBJECT_USER_ID))
        ranked = _ranked_entries(entries, now)
        texts, ids = _budget_selection(ranked[:limit], total_chars, entry_chars)
        if ids and not with_ids:
            try:
                store.record_recall(ids, SUBJECT_USER_ID)
            except Exception:
                pass
        if with_ids:
            return texts, ids
        return texts
    except sqlite3.Error:
        return ([], []) if with_ids else []


def record_recall(database, memory_ids):
    """Count only the memories that actually reached the prompt."""
    if database is None or not memory_ids:
        return 0
    try:
        return _context_store(database).record_recall(
            list(memory_ids), SUBJECT_USER_ID)
    except Exception:
        return 0

