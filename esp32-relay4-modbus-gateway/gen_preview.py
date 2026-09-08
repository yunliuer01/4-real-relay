import re, json, sys

with open('main.py', 'r', encoding='utf-8') as f:
    src = f.read()

# 提取 PAGE 多行字符串
m = re.search(r'PAGE = """(.*?)"""', src, re.DOTALL)
if not m:
    print("PAGE not found")
    sys.exit(1)

PAGE = m.group(1)

sample_mb = {
    "enabled": True,
    "uart_id": 1, "baudrate": 9600, "tx_pin": 20, "rx_pin": 21, "dir_pin": 8,
    "timeout_ms": 500, "retries": 2, "retry_interval_ms": 500,
    "slaves": [
        {
            "slave_id": 1, "enabled": True,
            "registers": [
                {"addr": 0, "func": 3, "key": "temperature", "scale": 0.1, "period_ms": 1000, "signed": False, "digits": 2},
                {"addr": 1, "func": 3, "key": "humidity", "scale": 0.1, "period_ms": 1000, "signed": False, "digits": 2},
            ]
        },
        {
            "slave_id": 2, "enabled": True,
            "registers": [
                {"addr": 5, "func": 4, "key": "soil_moisture", "scale": 1, "period_ms": 3000, "signed": False, "digits": 0},
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
