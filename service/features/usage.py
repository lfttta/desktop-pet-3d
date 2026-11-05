"""Token usage and cost reporting for the usage panel.

The report aggregates ``usage_ledger`` rows already recorded by the AI budget
code. Limits and the ok/warning/hard_limit state reuse the same budget policy,
so the panel and request gating never disagree. No prompt or response text is
read here; only counts, tokens and costs.
"""

from service.ai.budget import BudgetLedger

NAME = 'usage'
CAPABILITY = {
    'available': True,
    'reason': '',
    'commands': ['usage.get'],
}


def _ai_settings(store):
    settings = getattr(store, 'settings', None)
    if isinstance(settings, dict) and isinstance(settings.get('ai'), dict):
        return settings['ai']
    return {}


def _price_configured(ai_settings):
    try:
        total = float(ai_settings.get('input_price_per_million') or 0.0) \
            + float(ai_settings.get('output_price_per_million') or 0.0)
    except (TypeError, ValueError):
        return False
    return total > 0.0


def get(request, store, ctx):
    ai_settings = _ai_settings(store)
    ledger = BudgetLedger(getattr(ctx, 'database', None))
    budget = ledger.status(ai_settings)
    if budget['local_only']:
        status = 'hard_limit'
    elif budget['warning']:
        status = 'warning'
    else:
        status = 'ok'
    return 'result', {
        'today': ledger.today(),
        'month': ledger.month(),
        'limits': {
            'daily_tokens': budget['daily_token_budget'],
            'monthly_cost': budget['monthly_cost_budget'],
            'currency': ai_settings.get('currency') or 'USD',
        },
        'ratio': {'tokens': budget['token_ratio'], 'cost': budget['cost_ratio']},
        'status': status,
        'by_model': ledger.by_model(),
        'by_day': ledger.by_day(),
        'price_configured': _price_configured(ai_settings),
    }


COMMANDS = {'usage.get': get}
