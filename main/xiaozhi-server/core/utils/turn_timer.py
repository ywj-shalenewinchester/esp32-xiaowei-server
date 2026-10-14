"""一轮语音交互的分段耗时（TTFT）埋点。

一轮 = 用户说完话 → 设备出声。计时从 VAD 判定"说话结束"开始，横跨三个
执行上下文：主事件循环（VAD/ASR）、chat() 线程池（LLM）、TTS 与音频下发
线程。因此有三条硬约束：

1. 统一用 time.monotonic()（进程级单调钟，跨线程一致，不受系统时间调整
   影响）。不要用 time.time()——现有代码拿它存墙钟时间戳
   （last_activity_time 等），混用会互相污染。
2. mark() 必须幂等：同一个阶段重复标记返回 None，这样打点可以无脑放在
   每帧的热路径上，不必自己判重。
3. 存"相对 T0 的绝对秒数"而不是段耗时。某阶段可能根本不触发（被打断、
   走了工具调用分支），存绝对值才能在汇总时跳过缺失阶段，而各段之和仍
   恒等于总时长。

汇总只在 device_first_frame 首次标记成功时输出一次，INFO 级——项目里现有
的耗时日志都是 debug，而 config.yaml 是 log_level: INFO，等于没打印。
"""
import threading
import time
from typing import Optional

# 阶段顺序即汇总时的相减顺序
STAGES = (
    "asr_done",
    "mem_done",
    "llm_req",
    "llm_sent",
    "llm_first_chunk",
    "llm_first_token",
    "tts_first_audio",
    "device_first_frame",
)

LABELS = {
    "asr_done": "ASR识别",
    "mem_done": "记忆检索",
    "llm_req": "请求准备",
    "llm_sent": "组装对话体",
    "llm_first_chunk": "网络首字节",
    "llm_first_token": "首个可播",
    "tts_first_audio": "TTS首帧",
    "device_first_frame": "下发设备",
}


class TurnTimer:
    """一轮交互的埋点容器，线程安全。"""

    def __init__(self, stale_key: Optional[str] = None):
        self._t0 = time.monotonic()
        self._marks: dict = {}
        self._turn_key: Optional[str] = None
        self._stale_key = stale_key
        self._finished = False
        self._lock = threading.Lock()

    @property
    def finished(self) -> bool:
        return self._finished

    def mark(self, stage: str, turn_key: Optional[str] = None) -> Optional[float]:
        """首次标记返回相对 T0 的秒数；重复标记或不属于本轮则返回 None。

        turn_key 传 sentence_id：本 timer 尚未绑定时就绑定它，之后凡是带不同
        key 的标记一律丢弃，避免上一轮残留的音频帧被记到新一轮。

        stale_key 是建表那一刻仍在生效的旧 sentence_id（见 start_turn）。它必须
        在绑定之前就被拒掉：上一轮的音频帧带的是旧 id，若让它抢先绑定，本轮所有
        标记都会因 key 不符被丢弃，这一轮就没有汇总行了——埋点静默失效。
        """
        with self._lock:
            if self._finished:
                return None
            if turn_key is not None:
                if self._stale_key is not None and turn_key == self._stale_key:
                    return None
                if self._turn_key is None:
                    self._turn_key = turn_key
                elif self._turn_key != turn_key:
                    return None
            if stage in self._marks:
                return None
            elapsed = time.monotonic() - self._t0
            self._marks[stage] = elapsed
            return elapsed

    def summary(self) -> str:
        """一行汇总；各段 = 相邻已记录标记之差，缺失阶段自动跳过。"""
        with self._lock:
            marks = dict(self._marks)
            key = self._turn_key
        if not marks:
            return ""
        parts = []
        prev = 0.0
        total_ms = 0
        for stage in STAGES:
            if stage not in marks:
                continue
            cur = marks[stage]
            ms = int(round((cur - prev) * 1000))
            total_ms += ms
            parts.append(f"{LABELS[stage]}={ms}ms")
            prev = cur
        head = f"轮次={key[:6]}" if key else "轮次=-"
        # 总计取各段取整后之和，保证"分段之和 == 总计"精确成立：各段独立四舍五入
        # 会让总和漂移几 ms，读日志的人会把它当成 bug。
        return f"[TTFT] {head} | " + " | ".join(parts) + f" || 说完话→设备出声={total_ms}ms"

    def finish(self) -> None:
        """本轮结束，之后所有 mark 一律忽略（防延迟到达的帧重复触发汇总）。"""
        with self._lock:
            self._finished = True


def start_turn(conn, force: bool = False) -> TurnTimer:
    """开始新一轮计时。

    force=True 用于 VAD 判定说话结束、手动模式收到 stop —— 这两个时刻确定
    是新一轮开始，必须重置 T0。不能省这个 force：上一轮若因 ASR 空文本等原因
    没产出音频，它的 timer 不会被 finish，复用会把 T0 留在上一轮，让"ASR识别"
    这一段虚高。

    force=False 是兜底（startToChat / chat），只在没有 timer 或已结束时才建，
    避免把 VAD 刚记下的 T0 冲掉。

    stale_key 只在 force 时传：此刻 chat() 还没给新一轮分配 sentence_id，
    conn.sentence_id 仍是上一轮的，正好用来把上一轮的残留帧挡在绑定之外。
    非 force 路径下 conn.sentence_id 可能已经是新的，传进去会误杀本轮标记。
    """
    timer = getattr(conn, "turn_timer", None)
    if force or timer is None or timer.finished:
        stale_key = getattr(conn, "sentence_id", None) if force else None
        timer = TurnTimer(stale_key=stale_key)
        conn.turn_timer = timer
    return timer


def mark(conn, stage: str, turn_key: Optional[str] = None) -> Optional[float]:
    """给当前轮打点；无 timer 时（非语音路径）静默返回 None。"""
    timer = getattr(conn, "turn_timer", None)
    if timer is None:
        return None
    return timer.mark(stage, turn_key)
