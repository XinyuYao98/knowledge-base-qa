# 青岚智造 API 使用说明

本文档介绍设备接入 API 的调用方式。

## 认证方式

所有请求需在 Header 中携带 `Authorization: Bearer <token>`。

## 上报数据

示例请求：

```json
POST /api/v1/telemetry
{"device_id": "GW-001", "metric": "power", "value": 12.5}
```

### 字段说明

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| device_id | string | 是 | 设备唯一编号 |
| metric | string | 是 | 指标名称 |
| value | number | 是 | 指标数值 |

## 错误码

- 401：token 无效或过期
- 429：请求频率超限，请退避重试
- 500：服务端异常，请联系技术支持
