import json, sys

with open('portal_page.html', 'r', encoding='utf-8') as f:
    PAGE = f.read()

sample_mb = {
    "enabled": True,
    "mode": "tcp",
    "timeout_ms": 500,
    "retries": 2,
    "retry_interval_ms": 500,
    "slaves": [
        {
            "enabled": True,
            "host": "192.168.20.59",
            "port": 5502,
            "unit_id": 4,
            "registers": [
                {"addr": 3, "func": 3, "key": "temperature", "product": "th-lfx", "period_ms": 2000, "scale": 1, "digits": 0, "signed": False, "writable": True},
                {"addr": 4, "func": 3, "key": "humidity", "product": "th-lfx", "period_ms": 2000, "scale": 1, "digits": 0, "signed": False, "writable": True},
            ]
        }
    ]
}

mb_json = json.dumps(sample_mb).replace('"', "&quot;")

html = PAGE.format(
    wifi_ssid="Office-WiFi",
    wifi_password="",
    mqtt_host="172.16.4.211",
    mqtt_port="9783",
    mqtt_user="test",
    mqtt_password="123456",
    product_id="relay4_lfx",
    device_id="7ce8b1c1a7fc",
    report_interval="5",
    sel_direct="selected",
    sel_sys="",
    modbus_json=mb_json,
    mac="7c:e8:b1:c1:a7:fc",
)

with open('portal_preview.html', 'w', encoding='utf-8') as f:
    f.write(html)

print("preview written, bytes:", len(html))
