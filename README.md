# 点跨（Clickspan）

点击流装载 → 按访客空闲超时切会话 → 脏事件隔离 → 会话证明互证。

**不是** 运单轨迹时间线（途迹）、变更投影流（changecast）、CDC Sink、券核销钉扎、日结封账、双边对齐、改价价签、证据包放行。  
去皮后是：**事件装载 + 会话窗物化 + 闸/隔离 + 证明**。

## 启动

```powershell
# 后端
cd backend
pip install -r requirements.txt
$env:DATA_DIR = "$PWD\data"
$env:FIXTURES_DIR = "$PWD\..\fixtures"
uvicorn app.main:app --host 127.0.0.1 --port 10900

# 前端
cd frontend
npm install
npm run dev
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://127.0.0.1:5900 |
| 接口 | http://127.0.0.1:10900 |

## UI

**泳道织机**：顶栏品牌 + 顶栏菜单（织机 / 收流 / 搁置 / 核证 / 切窗）+ 横向会话串珠。  
不用底栏导航、不用三栏测绘板、不用左轨编号、不用流程 ribbon。

## 改切窗不回溯（timeout_change_no_rewrite）

改 `idle_timeout_sec` 只影响**之后**的收流裁决，历史不回溯。

- **落库形态**：采用 `config_versions` 配置版本表（只追加），不采用「会话绑定 `timeout_sec_snapshot`」。
- **收流裁决**：只对 `open` 会话按当前版本的新超时与末击点间隙裁决；继续或切开。
  切开时前序落 `closed` 并封印最新证明，同时 append-only 记一条 `session_cuts` 切分边
  （含 `gap_sec` / `timeout_sec_used` / `config_version`）。
- **失败闭**：配置变更在单事务内写新版本并更新设置，任一失败整笔回滚；
  `POST /api/resplit-history` 固定拒绝（409），不在 `closed` 上搬成员或改切分边。
  库级触发器兜底：`session_cuts` / `config_versions` 不可 UPDATE / DELETE。
- **核证**：`GET /api/config-audit` 只读，校验 closed 成员与其最新证明一致、
  切分边与织机同源一致，并列出偏差；`GET /api/sessions/{id}` 返回关联切分边。

| 接口 | 说明 |
| --- | --- |
| `GET /api/settings` | 当前超时、配置版本、下限 |
| `PUT /api/settings/idle-timeout` | 单事务写新配置版本（同值不产生新版本） |
| `GET /api/config-audit` | 配置版本 + 切分边 + 只读核证 |
| `POST /api/resplit-history` | 固定拒绝历史重切（409） |
| `POST /api/ingest/run` | `{"fixture": "..."}` 收流；幂等回放不重算 |

