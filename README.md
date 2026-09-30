# 市政饮用水污染响应

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8334`。

- `app.py`：服务生命周期和依赖组装。
- `src/domain.py`：水源、污染物、区域、来源校验，以及应急水源/调度/送水入参校验。
- `src/rules.py`：污染评分、通知去重、停水、切换水源、冲洗、消毒、复检和恢复状态机。
- `src/allocation.py`：应急水源分配账规则（先后排队、容量不足只落整块片区、失败按片区回滚、终态释放）。
- `src/repository.py`：SQLite、事务、重复保护、乐观版本、审计链和分配账表。
- `src/service.py`：身份、角色和用例编排。
- `src/http_api.py`：JSON 接口与静态首页。
- `src/audit.py`：审计哈希。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8334
python3 -m unittest discover -s tests -v
```

接口包括 `GET /health`、`GET /api/state`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions` 和审计查询。测试覆盖完整响应流程、重复事件、重复通知、复检阈值、权限和版本冲突。

### 应急水源分配账

- `POST /api/water-sources`：登记应急水源与总容量（coordinator/dispatcher）。
- `GET /api/water-sources`、`GET /api/water-sources/<code>`：查询水源余量 `available_qty`、占用量 `occupied_qty` 和占用去向 `allocations`。
- `POST /api/items/<id>/dispatch`：调度申请，带 `order_no`、`source_code`、`zones[{zone_id,quantity}]` 和 `expected_version`。申请按先后占用；容量不足时只整块落能执行的片区，未排上片区在 `unfulfilled` 中返回原因。`order_no` 重复时沿用首次调度单（`replayed:true`），容量不重复占用。
- `POST /api/items/<id>/dispatch/<order_no>/delivery`：片区送水回报 `delivered`（可带 `delivered_qty`）或 `failed`。失败只回滚该片区未送达的承诺量，已送达记录永久保留。
- `GET /api/zones/progress?item_id=&zone_id=`：片区送水进度。
- `GET /api/allocation-ledger?source_code=&item_id=`：只增的分配台账（registered/hold/reject/deliver/rollback/release）。
- 事件进入终态（`cancelled`、`restored`）时，未执行占用在同一事务内立即失效释放；已送达部分不能撤，终态后拒绝新调度。调度员并发修改同一事件时，后提交者收到 409，按最新 `version` 重读后重提。
- 旧数据只有一个备用源字段（`alternate_source_id`）时，升级后仍可在事件详情 `dispatch_view.legacy_alternate_source_id` 查看。

内置规则不替代真实水质模型、法定通报渠道或供水控制系统的联锁。
