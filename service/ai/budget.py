"""Token usage accounting, cost estimation and local-only budget limits.

Usage is written to ``usage_ledger``. Daily token and monthly cost limits warn
at 80 percent and switch the next request to a local-only reply once reached.
A limit of zero disables that limit.

聚合查询使用 ``created_at`` 的范围边界（可走索引），不再对整表做
``date(created_at)`` 全表扫描。并发请求通过 ``reserve``/``release`` 原子地
预留最坏用量，避免多个请求同时通过预算检查而超支。
"""

from datetime import datetime, timedelta, timezone
import sqlite3
import threading

WARNING_RATIO = 0.8
BY_DAY_DEFAULT = 14
# 账本保留天数：覆盖 by_day 的最大 366 天窗口，再留一点余量。
LEDGER_RETENTION_DAYS = 400


def _day_bounds(moment=None):
    """当天 UTC 的 [起始, 次日) 边界，格式与 CURRENT_TIMESTAMP 可字典序比较。"""
    moment = moment or datetime.now(timezone.utc)
    start = moment.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.strftime('%Y-%m-%d %H:%M:%S'), end.strftime('%Y-%m-%d %H:%M:%S')


def _month_bounds(moment=None):
    moment = (moment or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start.strftime('%Y-%m-%d %H:%M:%S'), end.strftime('%Y-%m-%d %H:%M:%S')


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
        # 在途请求的最坏用量预留；status() 会把它计入投影，保证并发请求
        # 的检查-预留是原子的，不会一起通过预算闸门。
        self._reservations = {}
        self._reservation_lock = threading.Lock()

    def reserve(self, request_id, reserve):
        """原子地登记一个在途请求的最坏用量。"""
        reserve = reserve if isinstance(reserve, dict) else {}
        tokens = _token(reserve.get('tokens'))
        try:
            cost = max(0.0, float(reserve.get('cost') or 0.0))
        except (TypeError, ValueError):
            cost = 0.0
        with self._reservation_lock:
            self._reservations[str(request_id)] = {'tokens': tokens, 'cost': cost}

    def release(self, request_id):
        with self._reservation_lock:
            self._reservations.pop(str(request_id), None)

    def _reserved_totals(self):
        with self._reservation_lock:
            tokens = sum(item['tokens'] for item in self._reservations.values())
            cost = sum(item['cost'] for item in self._reservations.values())
        return tokens, cost

    def _connection(self):
        database = self.database
        return None if database is None else database.connection

    def _sum(self, sql, parameters=()):
        connection = self._connection()
        if connection is None:
            return 0
        try:
            row = connection.execute(sql, parameters).fetchone()
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
        day_start, day_end = _day_bounds()
        month_start, month_end = _month_bounds()
        tokens = self._sum(
            'SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) '
            'FROM usage_ledger WHERE created_at >= ? AND created_at < ?',
            (day_start, day_end))
        cost = self._sum(
            'SELECT COALESCE(SUM(cost), 0) FROM usage_ledger '
            'WHERE created_at >= ? AND created_at < ?',
            (month_start, month_end))
        reserve = reserve if isinstance(reserve, dict) else {}
        reserved_tokens = _token(reserve.get('tokens'))
        try:
            reserved_cost = max(0.0, float(reserve.get('cost') or 0.0))
        except (TypeError, ValueError):
            reserved_cost = 0.0
        inflight_tokens, inflight_cost = self._reserved_totals()
        reserved_tokens += inflight_tokens
        reserved_cost += inflight_cost
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

    def _totals(self, where, parameters=()):
        rows = self._rows(
            'SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0), '
            'COALESCE(SUM(cost), 0), COUNT(*) FROM usage_ledger WHERE ' + where,
            parameters)
        if not rows:
            return {'tokens': 0, 'cost': 0.0, 'calls': 0}
        row = rows[0]
        return {'tokens': _token(row[0]), 'cost': _cost(row[1]),
                'calls': _token(row[2])}

    def today(self):
        """Aggregate usage recorded since 00:00 UTC today."""
        start, end = _day_bounds()
        return self._totals('created_at >= ? AND created_at < ?', (start, end))

    def month(self):
        """Aggregate usage recorded since the start of the current UTC month."""
        start, end = _month_bounds()
        return self._totals('created_at >= ? AND created_at < ?', (start, end))

    def by_model(self):
        """Current-month totals grouped by model, largest token share first."""
        start, end = _month_bounds()
        rows = self._rows(
            'SELECT model, COALESCE(SUM(prompt_tokens + completion_tokens), 0), '
            'COALESCE(SUM(cost), 0), COUNT(*) FROM usage_ledger '
            'WHERE created_at >= ? AND created_at < ? '
            'GROUP BY model ORDER BY 2 DESC, 1 ASC', (start, end))
        return [{'model': str(row[0] or ''), 'tokens': _token(row[1]),
                 'cost': _cost(row[2]), 'calls': _token(row[3])}
                for row in rows]

    def by_day(self, days=BY_DAY_DEFAULT):
        """Daily totals for the last ``days`` UTC days, zero-filled and ascending."""
        days = days if type(days) is int and 1 <= days <= 366 else BY_DAY_DEFAULT
        today = datetime.now(timezone.utc).date()
        window_start = (today - timedelta(days=days - 1)).strftime('%Y-%m-%d 00:00:00')
        rows = self._rows(
            'SELECT date(created_at) AS day, '
            'COALESCE(SUM(prompt_tokens + completion_tokens), 0), '
            'COALESCE(SUM(cost), 0), COUNT(*) FROM usage_ledger '
            'WHERE created_at >= ? GROUP BY day', (window_start,))
        totals = {str(row[0]): row for row in rows if row[0]}
        result = []
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

    def cleanup(self, retention_days=LEDGER_RETENTION_DAYS):
        """删除超过保留期的账本行；返回删除行数，失败时返回 0。"""
        if type(retention_days) is not int or retention_days < 1:
            retention_days = LEDGER_RETENTION_DAYS
        connection = self._connection()
        if connection is None:
            return 0
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=retention_days)).strftime('%Y-%m-%d %H:%M:%S')
        try:
            with self.database.transaction() as transaction:
                cursor = transaction.execute(
                    'DELETE FROM usage_ledger WHERE created_at < ?', (cutoff,))
                return cursor.rowcount if cursor.rowcount > 0 else 0
        except sqlite3.Error:
            return 0

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
