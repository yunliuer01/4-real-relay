-- ============================================================
-- EMQX 5.x 规则引擎 SQL —— 将模拟终端原始报文转换为 JetLinks 物模型格式
-- （适配路线 1：不改终端，由 EMQX 规则转换）
--
-- 终端原始上报（terminal/{deviceId}/th）:
--   {"device_id":"FILE-TERM-01","temperature":25.5,"humidity":60.2,"timestamp":"..."}
--
-- 转换后发布到 JetLinks 属性上报主题:
--   /mqtt-iot/FILE-TERM-01/properties/report
--   {"properties":{"temperature":25.5,"humidity":60.2}}
--
-- 使用方法（EMQX Dashboard -> 规则引擎 -> 规则 -> 创建）:
--   1. 在 "SQL 编辑" 中粘贴本文件第一段 SELECT 语句
--   2. 添加动作 -> 消息重新发布 (Republish)
--       主题: /mqtt-iot/${topic(2)}/properties/report
--       QoS: 1
--       负载: 保持默认（即使用 SELECT 输出）
--   3. 保存并启用规则
--   提示: 若 EMQX Dashboard(18083) 无法访问，需联系老师开通，
--         或使用 EMQX REST API / 管理端配置
-- ============================================================

-- ---------- 规则 SQL（复制这一段到 EMQX 规则编辑器） ----------
SELECT
  payload.temperature AS "properties.temperature",
  payload.humidity   AS "properties.humidity"
FROM "terminal/+/th"
WHERE topic(2) = 'FILE-TERM-01'
-- -------------------------------------------------------------

-- 多设备版本：每台设备建一条规则，把 topic(2) 换成对应设备ID，
-- 若设备属于同一产品 mqtt-iot，Republish 主题可统一为 /mqtt-iot/${topic(2)}/properties/report

-- 调试技巧：先在 EMQX 里订阅 /mqtt-iot/+/properties/report，
-- 再修改 data/sensor_data.json，应能看到转换后的 JetLinks 报文。
