# -*- coding: utf-8 -*-
"""umqtt.simple 桥接桩：接口对齐 MicroPython umqtt.simple，底层用 paho-mqtt。

与真机一致的关键行为（用于暴露固件的 bytes/str 问题）：
- subscribe/publish/set_last_will 主题与消息必须是 bytes（str 会抛 TypeError）。
- 回调收到 (topic: bytes, payload: bytes)。
- MQTTClient 构造参数允许 str（真机上 client_id/user 传 str 也会被 umqtt 内部报错，
  但为便于测试，这里做了兼容：str 自动转 bytes 内部处理）。

断线重联测试：
- 调用 sim_down()：模拟网络断开，后续 connect/publish/check_msg 抛 OSError。
- 调用 sim_up()：恢复。
- 固件每次 mqtt_connect 都会 new 一个 MQTTClient，这里按 client_id 维护注册表，
  新连接建立前先断开同 client_id 的旧连接，避免 broker 侧连接顶号/冲突。

qos=1 发布死锁仿真（v6.0.4 关键回归保护）：
  环回模式下 publish(qos=1) 不再“同步返回”，而是**忠实复刻 MicroPython
  umqtt.simple 的分包语义**——真机源码：
      publish(): if qos == 1: while 1: op = self.wait_msg(); if op == 0x40: ...
      wait_msg(): self.sock.setblocking(True); ... self.cb(topic, msg)
  即「publish 自旋等 PUBACK」与「回调在 wait_msg 内被派发」共用同一个读循环。
  于是：主循环 publish(qos=1) 等 PUBACK 时若来了下行 -> wait_msg 派发回调 ->
  回调里再 publish(qos=1) -> 嵌套的 wait_msg 把**外层那颗 PUBACK 读走并丢弃**
  （pid 不匹配 -> 继续循环）-> 外层回到阻塞读，永久卡死、不抛异常。
  真机表现 = 心跳照常、/api/info 全绿、属性流彻底停摆（2026-09-10 实板事故）。

  环回下无法真的“永久阻塞”（会让测试挂死），故超时后抛 MQTTPubackDeadlock，
  并把事件记入 sim_deadlock_events()。正确写法是出站一律 qos=0。
  注意：真实 broker 模式走 paho（异步、线程安全），不复现该缺陷；
  该仿真只在环回模式生效。
"""
from __future__ import annotations

import threading
import time as _t

import paho.mqtt.client as mqtt

_CB_API = getattr(mqtt, "CallbackAPIVersion", None)

# 环回模式 qos=1 publish 自旋等待 PUBACK 的上限；超时即认定死锁（真机此处永久阻塞）
PUBACK_WAIT_TIMEOUT_S = 2.0

# 当前是否模拟“断网”
_blocked = False
# 环回模式：broker 不可达时也在本地“连接成功”，上行消息记录到 _rec_msgs，
# 下行消息由 sim_downlink 注入。用于无真实 broker 的环境跑全链路断言。
_loopback = False
_rec_msgs = []          # (topic_str, payload_str, retain, qos)，固件→平台 上行
# 上行的附带元信息（与 _rec_msgs 同序）：{"topic","qos","retain","tid"}
# tid = 调用线程 ident，用于断言「HTTP 线程绝不直接 publish」（v6.0.4 修复项 2）
_rec_meta = []
_deadlock_events = []   # 环回模式下 publish(qos=1) 死锁事件（v6.0.4 回归保护）
_rec_lock = threading.Lock()


class MQTTPubackDeadlock(AssertionError):
    """环回模式复刻真机 umqtt.simple 缺陷：publish(qos=1) 的 PUBACK 被嵌套 wait_msg 吃掉。"""


def sim_recv_meta(from_index=0):
    """取上行元信息：[{"topic","qos","retain","tid"}, ...]"""
    with _rec_lock:
        return [dict(m) for m in _rec_meta[from_index:]]


def sim_outbound_qos():
    """取本次运行所有出站 publish 的 qos 列表（v6.0.4：应全为 0）。"""
    with _rec_lock:
        return [q for (_t_, _p, _r, q) in _rec_msgs]


def sim_deadlock_count():
    with _rec_lock:
        return len(_deadlock_events)


def sim_deadlock_events():
    with _rec_lock:
        return list(_deadlock_events)


def sim_reset_deadlock():
    with _rec_lock:
        del _deadlock_events[:]


def sim_reset_records():
    """清空上行记录与死锁事件（测试自检阶段用，避免污染后续断言）。"""
    with _rec_lock:
        del _rec_msgs[:]
        del _rec_meta[:]
        del _deadlock_events[:]
# 注意：connect() 会在持有锁的情况下调用 old.disconnect()，而 disconnect() 也要取锁，
# 因此必须用可重入的 RLock，否则同 client_id 重建连接时会死锁。
_lock = threading.RLock()
_instances = []
_by_cid = {}
_conn_count = 0


def sim_set_loopback(v):
    """True：本地环回模式（不需要真实 broker）。"""
    global _loopback
    _loopback = bool(v)


def sim_loopback_on():
    return _loopback


def sim_recv_msgs(from_index=0):
    """取固件发布的上行消息（环回模式）：[(topic_str, payload_str), ...]"""
    with _rec_lock:
        return [(t, p) for (t, p, _r, _q) in _rec_msgs[from_index:]]


def sim_msg_count():
    with _rec_lock:
        return len(_rec_msgs)


def sim_downlink(topic_str, payload_str):
    """模拟平台下行：投递给已订阅该 topic 的固件实例。返回是否投递成功。"""
    payload_b = payload_str.encode("utf-8")
    topic_b = topic_str.encode("utf-8")
    with _lock:
        for inst in list(_by_cid.values()):
            subs = getattr(inst, "_loop_subs", None)
            if subs is not None and topic_str in subs:
                inst._dl_q.append((topic_b, payload_b))
                return True
    return False


def sim_down():
    global _blocked
    _blocked = True


def sim_up():
    global _blocked
    _blocked = False


def sim_conn_count():
    """返回已成功建立的底层连接次数（run_sim 用于断言重连发生）。"""
    return _conn_count


def _broker_reachable(host, port, timeout=3):
    """paho connect 前快速探测，避免对黑洞地址长时间阻塞主循环。"""
    import socket as _s
    if not host:
        return False
    try:
        _s.create_connection((host, int(port)), timeout=timeout).close()
        return True
    except Exception:
        return False


class MQTTError(OSError):
    pass


class MQTTClient:
    def __init__(self, client_id, server, port=0, user=None, password=None,
                 keepalive=0, ssl=None, ssl_params=None):
        self.client_id = client_id
        self.server = server
        self.port = port or 1883
        self.user = user
        self.password = password
        self.keepalive = keepalive or 60
        self.ssl = ssl
        self.cb = None
        self._paho = None
        self._stop_evt = threading.Event()
        self._loop_thread = None
        self._connack_rc = None   # 真实模式：connect() 后记录 CONNACK 返回码，非 0 即认证/授权失败
        self._loop_ok = False       # 环回模式：无需真实 paho 连接
        self._loop_subs = None      # 环回模式：订阅的 topic 集合(str)
        self._dl_q = []             # 环回模式：平台下行消息队列
        self._mid = 0               # 环回模式：出站 packet id 计数（对齐 umqtt self.pid）
        self._pubacks = []          # 环回模式：broker 已回、等待被读走的 PUBACK mid 队列
        with _lock:
            _instances.append(self)

    # ---------- 桥接实现 ----------
    def set_callback(self, f):
        self.cb = f

    def set_last_will(self, topic, msg, retain=False, qos=0):
        if isinstance(topic, str):
            raise TypeError("umqtt topic must be bytes, got str")
        if isinstance(msg, str):
            raise TypeError("umqtt msg must be bytes, got str")
        # 暂存，paho 连接时再设置
        self._will = (topic, msg, retain, qos)

    def connect(self, clean_session=True):
        global _conn_count
        if _blocked:
            raise OSError("simulated network down (connect)")
        cid = self._to_str(self.client_id)
        # 断开同 client_id 的旧连接（固件重连时会 new 新对象）
        with _lock:
            old = _by_cid.get(cid)
            if old is not None and old is not self:
                old.disconnect()
            _by_cid[cid] = self
        if _loopback:
            # 环回模式：不探测、不真连，直接“在线”
            self._paho = None
            self._loop_ok = True
            self._loop_subs = set()
            self._dl_q = []
            self._pubacks = []
            with _lock:
                _conn_count += 1
            return
        if not _broker_reachable(self.server, self.port):
            raise OSError("simulated connect failed (broker unreachable: %s:%s)" % (self.server, self.port))

        kwargs = {}
        if _CB_API:
            kwargs["callback_api_version"] = mqtt.CallbackAPIVersion.VERSION2
        self._paho = mqtt.Client(client_id=cid, clean_session=clean_session,
                                 protocol=mqtt.MQTTv311, **kwargs)
        if self.user:
            self._paho.username_pw_set(self._to_str(self.user), self._to_str(self.password or ""))
        will = getattr(self, "_will", None)
        if will:
            topic, msg, retain, qos = will
            self._paho.will_set(self._to_str(topic), msg, qos=qos, retain=retain)
        if self.ssl:
            import ssl as _ssl
            self._paho.tls_set_context(self.ssl)
        if _CB_API:
            self._paho.on_message = self._on_message_v2
            self._paho.on_disconnect = self._on_disconnect_v2
            self._paho.on_connect = self._on_connect_v2
        else:
            self._paho.on_message = self._on_message_v1
            self._paho.on_disconnect = self._on_disconnect_v1
            self._paho.on_connect = self._on_connect_v1
        self._paho.connect(self.server, self.port, self.keepalive)
        # paho 同步 connect() 返回前已处理完 CONNACK；rc!=0 表示账号/密码被拒。
        # 不在此显式检查的话，固件会误以为“已连接”，导致 broker 消息级断言全部超时。
        if self._connack_rc not in (None, 0):
            try:
                self._paho.disconnect()
            except Exception:
                pass
            self._paho = None
            raise OSError("broker refused connection (CONNACK rc=%r)" % (self._connack_rc,))
        # 不用 paho.loop_start：其 loop_stop 会 join 一个阻塞在 recv 上的线程，
        # 在断线场景下最坏要等一个 keepalive(60s)。这里用自管的 0.2s 超时投递线程，
        # 保证 disconnect/旧连接顶替时能立即退出。
        self._stop_evt = threading.Event()
        self._loop_thread = threading.Thread(target=self._loop_worker, daemon=True)
        self._loop_thread.start()
        with _lock:
            _conn_count += 1

    def _loop_worker(self):
        while not self._stop_evt.is_set():
            if self._paho is None:
                break
            try:
                self._paho.loop(timeout=0.2)
            except Exception:
                if self._stop_evt.is_set():
                    break
                _t.sleep(0.2)
            if not self._stop_evt.is_set():
                _t.sleep(0.01)

    # ---- paho 回调 ----
    def _on_connect_v2(self, client, userdata, flags, reason_code, properties=None):
        # paho 2.x: reason_code 为 ReasonCode 对象，rc=0 成功
        rc = getattr(reason_code, "value", reason_code)
        self._connack_rc = rc

    def _on_connect_v1(self, client, userdata, flags, rc):
        self._connack_rc = rc

    def _on_message_v2(self, client, userdata, msg):
        # 与真机一致：回调收到 bytes
        if self.cb:
            self.cb(msg.topic.encode("utf-8"), msg.payload)

    def _on_message_v1(self, client, userdata, msg):
        if self.cb:
            self.cb(msg.topic.encode("utf-8"), msg.payload)

    def _on_disconnect_v2(self, client, userdata, flags, reason_code, properties=None):
        pass

    def _on_disconnect_v1(self, client, userdata, rc):
        pass

    # ---------- 基础 API ----------
    def subscribe(self, topic, qos=0):
        if _blocked:
            raise OSError("simulated network down (subscribe)")
        if isinstance(topic, str):
            raise TypeError("umqtt topic must be bytes, got str")
        if self._loop_ok:
            self._loop_subs.add(self._to_str(topic))
            return
        if self._paho is None:
            raise OSError("not connected")
        self._paho.subscribe(self._to_str(topic), qos)

    def publish(self, topic, msg, retain=False, qos=0):
        if _blocked:
            raise OSError("simulated network down (publish)")
        if isinstance(topic, str):
            raise TypeError("umqtt topic must be bytes, got str")
        if isinstance(msg, str):
            raise TypeError("umqtt msg must be bytes, got str")
        tid = threading.get_ident()
        with _rec_lock:
            _rec_msgs.append((self._to_str(topic), self._to_str(msg), bool(retain), qos))
            _rec_meta.append({"topic": self._to_str(topic), "qos": qos,
                              "retain": bool(retain), "tid": tid})
        if self._loop_ok:
            if qos:
                self._spin_puback(qos)
            return
        if self._paho is None:
            raise OSError("not connected")
        # 真实 broker 走 paho：异步、线程安全，天然没有 PUBACK 争用问题
        self._paho.publish(self._to_str(topic), msg, qos=qos, retain=retain)

    def _spin_puback(self, qos, topic=""):
        """环回模式复刻 umqtt.simple 的 publish(qos=1) 自旋：

            while 1:
                op = self.wait_msg()      # 读一个包；若为下行 PUBLISH 则派发回调
                if op == 0x40: ...        # 只有 pid 匹配才 return，不匹配直接丢弃

        因此回调内再 publish(qos=1) 时，嵌套的 wait_msg 会把外层的 PUBACK 读走丢弃，
        外层永远等不到 —— 真机表现为无异常的永久阻塞。
        """
        self._mid += 1
        mid = self._mid
        self._pubacks.append(mid)        # 环回无网络延迟：broker 立即回 PUBACK
        deadline = _t.monotonic() + PUBACK_WAIT_TIMEOUT_S
        while True:
            # 1) 有下行就派发（对齐 wait_msg(): 读到 PUBLISH 即进回调），可能递归 publish
            if self._dl_q:
                t_b, p_b = self._dl_q.pop(0)
                if self.cb:
                    self.cb(t_b, p_b)
                continue
            # 2) 读走一颗 PUBACK；非本 mid 的会被直接丢弃（真机 bug 核心）
            if self._pubacks:
                got = self._pubacks.pop(0)
                if got == mid:
                    return
                continue
            # 3) 真机在此处 sock.read(1) 永久阻塞。仿真里超时即报错，避免测试挂死。
            if _t.monotonic() >= deadline:
                ev = ("publish(qos=%d) 等不到自己的 PUBACK(mid=%s)：已被嵌套的 "
                      "wait_msg 消费掉 -> 真机永久阻塞。出站 publish 必须用 qos=0。"
                      % (qos, mid))
                with _rec_lock:
                    _deadlock_events.append(ev)
                raise MQTTPubackDeadlock(ev)
            _t.sleep(0.005)

    def check_msg(self):
        """paho 后台线程已投递消息；环回模式由本方法从 _dl_q 拉取并回调。"""
        if _blocked:
            raise OSError("simulated network down (check_msg)")
        if self._loop_ok:
            while self._dl_q:
                t_b, p_b = self._dl_q.pop(0)
                if self.cb:
                    self.cb(t_b, p_b)
            return
        # 非环回：paho 后台线程已投递消息，此处主要模拟断线检测。
        if _blocked:
            raise OSError("simulated network down (check_msg)")

    def wait_msg(self):
        if _blocked:
            raise OSError("simulated network down (wait_msg)")
        try:
            if self._paho is not None:
                self._paho.loop(timeout=0.5)
        except Exception:
            pass

    def ping(self):
        if _blocked:
            raise OSError("simulated network down (ping)")

    def _stop_loop(self):
        self._stop_evt.set()
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=3)
            self._loop_thread = None

    def disconnect(self):
        self._stop_loop()
        if self._loop_ok:
            self._loop_ok = False
            self._loop_subs = None
            self._dl_q = []
            self._pubacks = []
            with _lock:
                if _by_cid.get(self._to_str(self.client_id)) is self:
                    _by_cid.pop(self._to_str(self.client_id), None)
            return
        if self._paho is not None:
            try:
                self._paho.disconnect()
            except Exception:
                pass
            self._paho = None
        with _lock:
            if _by_cid.get(self._to_str(self.client_id)) is self:
                _by_cid.pop(self._to_str(self.client_id), None)

    def sim_force_disconnect(self):
        """强制断开底层连接，但保留实例（模拟 broker 掉线）。"""
        self._stop_loop()
        if self._paho is not None:
            try:
                self._paho.disconnect()
            except Exception:
                pass

    # ---------- 工具 ----------
    @staticmethod
    def _to_str(b):
        if isinstance(b, bytes):
            return b.decode("utf-8")
        if isinstance(b, bytearray):
            return bytes(b).decode("utf-8")
        return str(b)
