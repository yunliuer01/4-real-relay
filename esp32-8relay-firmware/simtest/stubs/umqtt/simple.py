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
"""
from __future__ import annotations

import threading
import time as _t

import paho.mqtt.client as mqtt

_CB_API = getattr(mqtt, "CallbackAPIVersion", None)

# 当前是否模拟“断网”
_blocked = False
# 注意：connect() 会在持有锁的情况下调用 old.disconnect()，而 disconnect() 也要取锁，
# 因此必须用可重入的 RLock，否则同 client_id 重建连接时会死锁。
_lock = threading.RLock()
_instances = []
_by_cid = {}
_conn_count = 0


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
        if not _broker_reachable(self.server, self.port):
            raise OSError("simulated connect failed (broker unreachable: %s:%s)" % (self.server, self.port))

        cid = self._to_str(self.client_id)
        # 断开同 client_id 的旧连接（固件重连时会 new 新对象）
        with _lock:
            old = _by_cid.get(cid)
            if old is not None and old is not self:
                old.disconnect()
            _by_cid[cid] = self

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
        pass

    def _on_connect_v1(self, client, userdata, flags, rc):
        pass

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
        if self._paho is None:
            raise OSError("not connected")
        self._paho.publish(self._to_str(topic), msg, qos=qos, retain=retain)

    def check_msg(self):
        """paho 后台线程已投递消息，此处主要模拟断线检测。"""
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
