-- ============================================================
-- EMQX 规则引擎 SQL —— 将模拟终端原始报文转换为 JetLinks 物模型格式
-- （适配路线 1：不改终端，由 EMQX 规则转换）
--
-- 同一产品 mqtt-iot 下两个设备（勿新建产品）：
--   设备1 FILE-TERM-01    文件监听终端   terminal/FILE-TERM-01/th
--   设备2 MODBUS-TERM-01  Modbus采集终端  terminal/MODBUS-TERM-01/th
--
-- 终端原始上报（terminal/{deviceId}/th）:
--   {"device_id":"FILE-TERM-01","temperature":25.5,"humidity":60.2,"timestamp":"..."}
--
-- 转换后发布到 JetLinks 属性上报主题:
--   /mqtt-iot/{deviceId}/properties/report
--   {"deviceId":"...","properties":{"temperature":25.5,"humidity":60.2}}
--
-- 使用方法（EMQX Dashboard -> 规则引擎 -> 规则 -> 创建）:
--   1. 在 "SQL 编辑" 中粘贴本文件对应设备的 SELECT 语句
--   2. 添加动作 -> 消息重新发布 (Republish)
--       主题: /mqtt-iot/{deviceId}/properties/report  (deviceId 换成对应设备)
--       QoS: 1
--       负载: 保持默认（即使用 SELECT 输出）
--   3. 保存并启用规则
-- ============================================================

-- ---------- 规则1：文件终端 FILE-TERM-01（rule_lfx，已建） ----------
SELECT
  payload.temperature AS "properties.temperature",
  payload.humidity   AS "properties.humidity"
FROM "terminal/+/th"
WHERE topic(2) = 'FILE-TERM-01'
-- 动作 Republish:
--   主题: /mqtt-iot/FILE-TERM-01/properties/report
--   负载: {"deviceId":"FILE-TERM-01","properties":{"temperature":${properties.temperature},"humidity":${properties.humidity}}}
-- -------------------------------------------------------------

-- ---------- 规则2：Modbus 采集终端 MODBUS-TERM-01（rule_lfx_modbus，已建） ----------
SELECT
  payload.temperature AS "properties.temperature",
  payload.humidity   AS "properties.humidity"
FROM "terminal/+/th"
WHERE topic(2) = 'MODBUS-TERM-01'
-- 动作 Republish:
--   主题: /mqtt-iot/MODBUS-TERM-01/properties/report
--   负载: {"deviceId":"MODBUS-TERM-01","properties":{"temperature":${properties.temperature},"humidity":${properties.humidity}}}
-- -------------------------------------------------------------

-- 注意：两条规则都加 WHERE topic(2) 精确匹配设备 ID，
-- 避免某台终端的消息被错误转发成另一台设备的数据。

-- 调试技巧：先在 EMQX 里订阅 /mqtt-iot/+/properties/report，
-- 再修改 data/sensor_data.json 或写入 Modbus 寄存器，应能看到转换后的 JetLinks 报文。
