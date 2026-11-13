"""单工作线程后台任务：网络调用离开服务主线程。

与聊天编排器相同的模式：工作线程只做网络 IO，结果通过队列交回服务线程，
由 ``poll`` 在 tick 中排空并执行回调（SQLite 写入与协议帧都属于服务线程）。
每个运行器同一时刻至多一个任务；忙时 ``submit`` 返回 False，由调用方决定
如何向客户端报告。``close`` 只做限时 join，绝不阻塞退出。
"""

import queue
import threading


class TaskRunner:
    def __init__(self, name):
        self.name = name
        self._results = queue.Queue()
        self._callbacks = {}
        self._lock = threading.Lock()
        self._worker = None
        self._closed = False

    def busy(self):
        with self._lock:
            return self._worker is not None and self._worker.is_alive()

    def submit(self, key, work, on_done, on_error=None, on_progress=None):
        """排队一个任务；``work(post)`` 在工作线程执行，回调在服务线程执行。"""
        with self._lock:
            if self._closed or (self._worker is not None and self._worker.is_alive()):
                return False
            self._callbacks[key] = (on_done, on_error, on_progress)
            worker = threading.Thread(target=self._run, args=(key, work),
                                      name=self.name, daemon=True)
            self._worker = worker
        try:
            worker.start()
        except RuntimeError:
            with self._lock:
                self._callbacks.pop(key, None)
                self._worker = None
            return False
        return True

    def _run(self, key, work):
        def post(data):
            self._results.put(('progress', key, data))

        try:
            value = work(post)
        except BaseException as exc:  # noqa: BLE001 - 异常原样交回服务线程
            self._results.put(('error', key, exc))
            return
        self._results.put(('done', key, value))

    def poll(self):
        """在服务线程排空结果并调用回调；回调异常不会中断后续结果。"""
        while True:
            try:
                kind, key, data = self._results.get_nowait()
            except queue.Empty:
                return
            with self._lock:
                callbacks = self._callbacks.get(key)
                if kind != 'progress':
                    self._callbacks.pop(key, None)
            if callbacks is None:
                continue
            on_done, on_error, on_progress = callbacks
            try:
                if kind == 'progress':
                    if on_progress is not None:
                        on_progress(data)
                elif kind == 'done':
                    on_done(data)
                elif on_error is not None:
                    on_error(data)
            except Exception:
                # 回调失败不允许拖垮 tick；调用方自行记录日志。
                continue

    def close(self, timeout=0.5):
        with self._lock:
            self._closed = True
            worker = self._worker
            self._callbacks.clear()
        if worker is not None and worker.is_alive():
            try:
                worker.join(timeout=timeout)
            except RuntimeError:
                pass