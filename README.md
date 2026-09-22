# 院内配送机器人洁污通行

院内配送系统用事件描述任务、区域清洁状态、门禁结果与机器人动作。本仓库固定现场交换字段，并提供一套可独立运行的配送调度后端：药品、标本与污染物任务依照载荷类型、消毒状态、临时封控和电梯能力选择合法路线，在共享窄道与电梯上做不会超卖的时段预留。

## 资料约定

- fixtures/incident.json 保存一组可公开的事故事件，时间均带 UTC 偏移。
- fixtures/layout.json 描述楼层拓扑：区域洁污等级、走廊容量、电梯承运能力。
- fixtures/zoning_rules.v1.json 是带版本号的洁污分区规则（`rules_version`）。
- fixtures/night_shift.json 是一段乱序的夜班事件，覆盖封控、重复回执、过期凭证、离线补传与人工接管。
- src/hospital_route/contracts.py 定义最小事件信封与严格校验入口。
- 未识别的业务字段保留在 attributes 中，接入方不得静默丢弃。
- event_id 标识现场事实，occurred_at 与 received_at 分别表示发生和接收时间。

## 运行调度后端

项目要求 Python 3.11 或更高版本，不依赖外部服务。以下命令在仓库根目录执行（也可 `pip install -e .` 后直接使用 `hospital-route`）：

    # 首次接入：同时采用拓扑与规则版本，状态写入 var/state
    PYTHONPATH=src python3 -m hospital_route --state var/state ingest fixtures/night_shift.json \
        --layout fixtures/layout.json --rules fixtures/zoning_rules.v1.json

    # 值班视图：每台机器人下一项合法动作、阻断依据、污染传播与预留
    PYTHONPATH=src python3 -m hospital_route --state var/state status
    PYTHONPATH=src python3 -m hospital_route --state var/state status --json

    # 人工接管暂停任务（每次接管都会入日志）
    PYTHONPATH=src python3 -m hospital_route --state var/state override \
        --mission medbot-07#1 --action resume --operator 王护士 --reason "重新消毒完成"

    # 交付记录：追溯签认时使用的门禁、清场与温控证据
    PYTHONPATH=src python3 -m hospital_route --state var/state record --mission medbot-11#1

## 行为约定

- 事件按 (received_at, event_id) 顺序应用；乱序提交不影响最终状态。重复 event_id 幂等忽略，同一门禁回执（receipt_id）重送不会推进两次。
- 机器人离线后补传的到达或清洁事件不倒改已签认任务，只作为证据归档；任务签认后到达的事件一律记入 late_events。
- 载荷温控超时、门禁拒绝、消毒凭证过期时，任务进入可人工处置的暂停状态（paused），系统不另行绕过；人工接管（resume/cancel）后按当前事实重新评估。
- 门禁回执缺少明确结论时按拒绝处理（fail-closed）。
- 状态目录中的 journal.jsonl 是追加式日志，记录采用的拓扑、规则版本与全部事件；服务重启后据此重放，任务、预留、规则版本与每次人工接管都完整保留。
- 引擎不读取系统时钟：当前时间取已应用事件的最大 received_at；`override --at` 缺省时取最新事件时间 +1 秒。

## 本地校验

    python3 -m unittest discover -s tests -v
    python3 -m compileall -q src

领域逻辑应放在独立模块中，协议解析不得隐式读取系统时间。持久化文件、临时缓存与本地配置不进入版本库。
