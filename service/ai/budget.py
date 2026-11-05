"""Token usage accounting, cost estimation and local-only budget limits.

Usage is written to ``usage_ledger``. Daily token and monthly cost limits warn
at 80 percent and switch the next request to a local-only reply once reached.
A limit of zero disables that limit.
"""

from datetime import datetime, timedelta, timezone
import sqlite3

WARNING_RATIO = 0.8
BY_DAY_DEFAULT = 14


def _token(value):
    if type(value) is int and value >= 0:
        return value
    return 0


def _cost(value):
    try:
        return round(max(0.0, float(value or 0.0)), 10)
    except (TypeError, ValueError):
        return 0.0


def parse_usage(payload):
    """Normalize ``usage.prompt_tokens`` / ``completion_tokens`` to integers."""
    usage = payload if isinstance(payload, dict) else {}
    prompt = _token(usage.get('prompt_tokens'))
    completion = _token(usage.get('completion_tokens'))
    total = _token(usage.get('total_tokens'))
    if total == 0:
        total = prompt + completion
    return {
        'prompt_tokens': prompt,
        'completion_tokens': completion,
        'total_tokens': total,
    }


def estimate_cost(usage, ai_settings):
    """Estimate a request cost from per-million token prices."""
    try:
        input_price = float(ai_settings.get('input_price_per_million') or 0.0)
        output_price = float(ai_settings.get('output_price_per_million') or 0.0)
    except (TypeError, ValueError):
        return 0.0
    prompt = _token(usage.get('prompt_tokens'))
    completion = _token(usage.get('completion_tokens'))
    return round(prompt / 1_000_000.0 * input_price
                 + completion / 1_000_000.0 * output_price, 10)


def estimate_text_tokens(text):
    """Approximate one text token count without a provider tokenizer."""
    if type(text) is not str or not text:
        return 0
    ascii_chars = sum(1 for char in text if ord(char) < 128)
    return (ascii_chars + 3) // 4 + (len(text) - ascii_chars)


def estimate_prompt_tokens(messages):
    """Approximate the prompt tokens for a provider message list."""
    total = 0
    for item in messages if isinstance(messages, list) else []:
        if isinstance(item, dict):
            total += estimate_text_tokens(item.get('content'))
    return total


def reservation(messages, ai_settings):
    """Return the tokens and cost this request may consume at most."""
    ai_settings = ai_settings if isinstance(ai_settings, dict) else {}
    prompt = estimate_prompt_tokens(messages)
    completion = _token(ai_settings.get('max_tokens'))
    try:
        input_price = float(ai_settings.get('input_price_per_million') or 0.0)
        output_price = float(ai_settings.get('output_price_per_million') or 0.0)
    except (TypeError, ValueError):
        input_price = output_price = 0.0
    cost = prompt / 1_000_000.0 * input_price + completion / 1_000_000.0 * output_price
    return {'tokens': prompt + completion, 'cost': round(cost, 10)}


class BudgetLedger:
    def __init__(self, database=None):
        self.database = database

    def _connection(self):
        database = self.database
        return None if database is None else database.connection

    def _sum(self, sql):
        connection = self._connection()
        if connection is None:
            return 0
        try:
            row = connection.execute(sql).fetchone()
        except sqlite3.Error:
            return 0
        return row[0] if row and isinstance(row[0], (int, float)) else 0

    def status(self, ai_settings, reserve=None):
        ai_settings = ai_settings if isinstance(ai_settings, dict) else {}
        daily_limit = _token(ai_settings.get('daily_token_budget'))
        try:
            monthly_limit = float(ai_settings.get('monthly_cost_budget') or 0.0)
        except (TypeError, ValueError):
            monthly_limit = 0.0
        tokens = self._sum("SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) "
                           "FROM usage_ledger WHERE date(created_at) = date('now')")
        cost = self._sum("SELECT COALESCE(SUM(cost), 0) FROM usage_ledger "
                         "WHERE strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')")
        reserve = reserve if isinstance(reserve, dict) else {}
        reserved_tokens = _token(reserve.get('tokens'))
        try:
            reserved_cost = max(0.0, float(reserve.get('cost') or 0.0))
        except (TypeError, ValueError):
            reserved_cost = 0.0
        # A request is only started when its worst-case usage still fits.
        projected_tokens = tokens + reserved_tokens
        projected_cost = cost + reserved_cost
        token_ratio = projected_tokens / daily_limit if daily_limit > 0 else 0.0
        cost_ratio = projected_cost / monthly_limit if monthly_limit > 0 else 0.0
        reason = ''
        if daily_limit > 0 and token_ratio >= 1.0:
            reason = 'daily_token_budget'
        elif monthly_limit > 0 and cost_ratio >= 1.0:
            reason = 'monthly_cost_budget'
        warning = (daily_limit > 0 and token_ratio >= WARNING_RATIO) or \
                  (monthly_limit > 0 and cost_ratio >= WARNING_RATIO)
        return {
            'local_only': bool(reason),
            'warning': bool(warning) and not reason,
            'reason': reason,
            'daily_tokens': tokens,
            'projected_tokens': projected_tokens,
            'reserved_tokens': reserved_tokens,
            'daily_token_budget': daily_limit,
            'monthly_cost': float(cost),
            'projected_cost': float(projected_cost),
            'reserved_cost': reserved_cost,
            'monthly_cost_budget': monthly_limit,
            'token_ratio': round(token_ratio, 4),
            'cost_ratio': round(cost_ratio, 4),
        }

    def _rows(self, sql, parameters=()):
        connection = self._connection()
        if connection is None:
            return []
        try:
            return connection.execute(sql, parameters).fetchall()
        except sqlite3.Error:
            return []

    def _totals(self, where):
        rows = self._rows(
            'SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0), '
            'COALESCE(SUM(cost), 0), COUNT(*) FROM usage_ledger WHERE ' + where)
        if not rows:
            return {'tokens': 0, 'cost': 0.0, 'calls': 0}
        row = rows[0]
        return {'tokens': _token(row[0]), 'cost': _cost(row[1]),
                'calls': _token(row[2])}

    def today(self):
        """Aggregate usage recorded since 00:00 UTC today."""
        return self._totals("date(created_at) = date('now')")

    def month(self):
        """Aggregate usage recorded since the start of the current UTC month."""
        return self._totals(
            "strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')")

    def by_model(self):
        """Current-month totals grouped by model, largest token share first."""
        rows = self._rows(
            'SELECT model, COALESCE(SUM(prompt_tokens + completion_tokens), 0), '
            'COALESCE(SUM(cost), 0), COUNT(*) FROM usage_ledger '
            "WHERE strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now') "
            'GROUP BY model ORDER BY 2 DESC, 1 ASC')
        return [{'model': str(row[0] or ''), 'tokens': _token(row[1]),
                 'cost': _cost(row[2]), 'calls': _token(row[3])}
                for row in rows]

    def by_day(self, days=BY_DAY_DEFAULT):
        """Daily totals for the last ``days`` UTC days, zero-filled and ascending."""
        days = days if type(days) is int and 1 <= days <= 366 else BY_DAY_DEFAULT
        rows = self._rows(
            'SELECT date(created_at) AS day, '
            'COALESCE(SUM(prompt_tokens + completion_tokens), 0), '
            'COALESCE(SUM(cost), 0), COUNT(*) FROM usage_ledger '
            "WHERE date(created_at) >= date('now', ?) GROUP BY day",
            (f'-{days - 1} days',))
        totals = {str(row[0]): row for row in rows if row[0]}
        result = []
        today = datetime.now(timezone.utc).date()
        for offset in range(days - 1, -1, -1):
            day = (today - timedelta(days=offset)).isoformat()
            row = totals.get(day)
            result.append({
                'date': day,
                'tokens': _token(row[1]) if row else 0,
                'cost': _cost(row[2]) if row else 0.0,
                'calls': _token(row[3]) if row else 0,
            })
        return result

    def record(self, request_id, model, usage, ai_settings):
        normalized = parse_usage(usage)
        cost = estimate_cost(normalized, ai_settings)
        currency = ai_settings.get('currency') or 'USD'
        recorded = False
        connection = self._connection()
        if connection is not None:
            try:
                with self.database.transaction() as transaction:
                    transaction.execute(
                        'INSERT INTO usage_ledger(request_id, model, prompt_tokens, '
                        'completion_tokens, total_tokens, cost, currency) '
                        'VALUES(?,?,?,?,?,?,?)',
                        (request_id or '', str(model or ''),
                         normalized['prompt_tokens'], normalized['completion_tokens'],
                         normalized['total_tokens'], cost, str(currency)))
                recorded = True
            except sqlite3.Error:
                recorded = False
        return {
            'prompt_tokens': normalized['prompt_tokens'],
            'completion_tokens': normalized['completion_tokens'],
            'total_tokens': normalized['total_tokens'],
            'cost': cost,
            'currency': str(currency),
            'recorded': recorded,
        }
