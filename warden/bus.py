import ssl

import paho.mqtt.client as mqtt


class Bus:
    def __init__(self, config, client_id, will_topic):
        api = getattr(mqtt, "CallbackAPIVersion", None)
        if api is not None:
            self._client = mqtt.Client(api.VERSION2, client_id=client_id, clean_session=True)
            self._client.on_connect = lambda _c, _u, _f, reason, _p: not reason.is_failure and self.on_connect()
        else:
            self._client = mqtt.Client(client_id=client_id, clean_session=True)
            self._client.on_connect = lambda _c, _u, _f, rc: rc == 0 and self.on_connect()
        self._client.on_message = lambda _c, _u, msg: self.on_message(msg.payload)
        self._client.username_pw_set(config.user, config.password)
        self._client.tls_set_context(ssl.create_default_context())
        self._client.will_set(will_topic, "offline", qos=1, retain=True)
        self._client.reconnect_delay_set(min_delay=2, max_delay=30)
        self._client.max_queued_messages_set(100)
        self._host, self._port = config.host, config.port
        self._last = None
        self.on_connect = lambda: None
        self.on_message = lambda payload: None

    def start(self):
        self._client.connect_async(self._host, self._port, keepalive=30)
        self._client.loop_start()

    def publish(self, topic, payload, retain=False):
        self._last = self._client.publish(topic, payload, qos=1, retain=retain)

    def subscribe(self, topic):
        self._client.subscribe(topic, qos=1)

    def close(self, timeout=3):
        if self._last is not None and self._client.is_connected():
            self._last.wait_for_publish(timeout)
        self._client.disconnect()
        self._client.loop_stop()
