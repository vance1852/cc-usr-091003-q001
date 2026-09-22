# 院内配送机器人洁污通行

院内配送系统用事件描述任务、区域清洁状态、门禁结果与机器人动作。本仓库固定现场交换字段，并交付一套可独立运行的配送调度后端：药品、标本与污染物任务依照载荷类型、消毒状态、临时封控和电梯能力选择合法路线，在共享窄道与电梯上做不会超卖的时段预留。

## 资料约定

- fixtures/incident.json 保存一组可公开的事故事件，时间均带 UTC 偏移。
- fixtures/night_shift_part1/2/3.json 是隔离病区启用新消毒通道后第一晚的乱序事件流。
- scenarios/night_isolation/ 保存楼层拓扑（topology.json）、洁污分区规则（rules.json）与机器人档案（robots.json）。
- src/hospital_route/contracts.py 定义最小事件信封与严格校验入口。
- 未识别的业务字段保留在 attributes 中，接入方不得静默丢弃。
- event_id 标识现场事实，occurred_at 与 received_at 分别表示发生和接收时间。

## 调度语义

- **事件重放**：事件按 (received_at, sequence, event_id) 全序重放，乱序上报得到确定性结果；同一 event_id 只应用一次。
- **终态保护**：已签认（送达/取消）的任务、已确认的消毒凭证、已解除的封控、已回执的门禁请求，不会被离线补传或重送事件倒改；同一门禁请求的回执只生效一次。
- **路线合法性**：逐边检查临时封控、区域/电梯污染档位与载荷耐受、消毒凭证有效期、门禁回执、电梯载荷白名单与机器人权限、温控时限（按最快路线 ETA 预估）。
- **人工处置暂停**：门禁拒绝/故障、温控超时、消毒凭证缺失或过期时，任务进入 suspended，只能由值班员 takeover 放行或取消，系统不另行绕过。
- **时段预留**：窄道按行驶方向、电梯按轿厢登记 [start, end) 预留，任意时刻占用不超过容量，重新规划先作废旧预留。
- **污染传播**：污染档位沿邻接边与电梯外溢（confirmed → suspected）；敞开运送的污染物（tier ≥ 2）通行时污染途经区域与电梯，密封载荷不污染；消毒事件将目标重置为 clean 并签发有效期凭证。
- **持久化**：任务、预留、封控、凭证、门禁回执、污染档位、规则版本与每次人工接管都写入 state.json（不进入版本库），重启后完整恢复。

## 本地校验

项目要求 Python 3.11 或更高版本，不依赖外部服务。运行下列命令可检查协议样例和源码：

    python -m unittest discover -s tests -v
    python -m compileall -q src

领域逻辑应放在独立模块中，协议解析不得隐式读取系统时间。持久化文件、临时缓存与本地配置不进入版本库。

## 值班操作

```bash
export PYTHONPATH=src

# 重演整个夜班（临时目录，含重启恢复演示）
python -m hospital_route run-demo

# 分步操作（状态持久化在 scenarios/night_isolation/state.json）
python -m hospital_route ingest fixtures/night_shift_part1.json
python -m hospital_route plan                       # 规划全部待办任务
python -m hospital_route plan --mission M-0901 --at 2026-09-22T20:44:00+08:00
python -m hospital_route takeover --operator duty-officer --action resume --mission M-0901
python -m hospital_route next                       # 每台机器人下一项合法动作
python -m hospital_route blockers                   # 路线被阻断的具体依据
python -m hospital_route risk                       # 污染如何传播到关联区域
python -m hospital_route record M-0901              # 交付记录：门禁/清场/温控证据
python -m hospital_route takeovers                  # 全部人工接管记录
```

## 模块结构

- contracts.py — 事件信封与严格校验（不读系统时间）
- model.py — 拓扑/规则/机器人静态资料模型
- state.py — 运行时状态、事件处理器、终态保护与污染传播
- engine.py — 路线搜索、合法性判定、容量预留
- service.py — 服务外观：接入、规划、接管、持久化
- report.py — 值班视图与交付记录
- persistence.py — JSON 快照（原子写入）
