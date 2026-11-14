"""Local quick commands that never call a remote model.

The router answers simple social and memory-request phrases immediately. The
optional local-model backend is reserved and disabled by default; when enabled
without an implementation the orchestrator reports that honestly.
"""

import re


_GREETINGS = frozenset({
    '你好', '您好', '你好呀', '您好呀', '嗨', '哈喽', 'hi', 'hello',
    '早上好', '晚上好', '早安', '晚安',
})

# Accepts the spacing the user actually types: "5分钟后提醒我",
# "5 分钟后提醒我" and an optional detail after the phrase.
_REMIND = re.compile(r'^(\d{1,5})\s*分钟后提醒我(.*)$')
_MAX_REMIND_MINUTES = 43200


class LocalRoute:
    __slots__ = ('name', 'reply', 'action', 'memory_text',
                 'timer_minutes', 'timer_text')

    def __init__(self, name, reply, action='', memory_text=None,
                 timer_minutes=None, timer_text=None):
        self.name = name
        self.reply = reply
        self.action = action
        self.memory_text = memory_text
        self.timer_minutes = timer_minutes
        self.timer_text = timer_text


class Router:
    def __init__(self, local_model=False):
        self.local_model = bool(local_model)

    def route(self, text):
        value = text.strip() if isinstance(text, str) else ''
        if not value:
            return None
        if value.startswith('记住'):
            target = _remainder(value, '记住')
            if target:
                return LocalRoute('remember',
                                  '我记住了。', '', target)
            return LocalRoute('remember_empty', '要记住什么呢？')
        if value.startswith('忘掉') or value.startswith('忘记'):
            return LocalRoute('forget',
                              '删除记忆需要在记忆列表里确认，我这还不能直接删。')
        if value.startswith('取消提醒'):
            return LocalRoute('timer_cancel', '好，取消提醒。', 'timer.cancel')
        remind = _REMIND.match(value)
        if remind is not None:
            minutes = int(remind.group(1))
            if 0 < minutes <= _MAX_REMIND_MINUTES:
                detail = remind.group(2).lstrip('：:，,。. 　!！')
                return LocalRoute('timer_set',
                                  f'好，{minutes} 分钟后提醒你。', 'timer.set',
                                  timer_minutes=minutes,
                                  timer_text=detail or None)
        if _plain(value) in _GREETINGS:
            return LocalRoute('hello', '你好呀。')
        if value.startswith('挥挥手') or value.startswith('挥手'):
            return LocalRoute('wave', '嗨～', 'wave')
        if value.startswith('过来'):
            return LocalRoute('come', '我过来啦。', 'walk')
        if value.startswith('安静'):
            return LocalRoute('quiet', '好，我安静一会儿。')
        if value.startswith('隐藏'):
            return LocalRoute('hide', '隐藏桌宠请用托盘菜单里的显示/隐藏。')
        return None


def _remainder(value, prefix):
    rest = value[len(prefix):]
    return rest.lstrip('：:，,。. 　')


def _plain(value):
    return value.strip().strip('，。！？!?.,~～、 　').lower()
