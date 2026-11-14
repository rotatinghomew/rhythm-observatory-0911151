# 业务资料契约

readings.cumulative 是设备原始累计计数，reset 表示重置后首个读数，不是瞬时流量。raw_hour 相对 2024-01-01 起算，interval_hours 是名义读取间隔，实际间隔由相邻原始时间决定。clocks 的区间左闭右开，重叠区间按公开日期和 revision 选择更晚规则；不同 id 也可能是校正补丁。outages 是明确停测区间，没有观测不等于没有阅读。

## 资料表

| 表名 | 原始记录数 | 字段 |
| --- | ---: | --- |
| readings | 527 | clock_version, cumulative, device, id, interval_hours, platform, raw_hour, released_at, reset, revision |
| clocks | 4 | clock_version, correction_hours, device, id, raw_from, raw_to, released_at, revision |
| outages | 1 | from_hour, id, platform, released_at, revision, to_hour |

记录均为本地合成资料，只表达业务结构，不是公开报道所述人群或机构的实测资料。日期是本地资料日历；所有版本以资料服务的 as_of 为准。字段值和自然缺失保留原义。金额、评分、频率和权重不可跨表不加判断地直接合计。
