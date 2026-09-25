# 乡镇政务协同服务

这是一个面向乡镇综合服务中心的模块化后端，集中管理居民档案、政务事务、信访流转、公告、部门、用户、角色、权限、会话、审计和可恢复后台任务。项目使用 FastAPI 与 SQLite，所有运行数据保存在单个本地数据库文件中，不依赖另行部署的数据库、缓存或消息队列。

## 主要模块

- 居民档案：登记、查询、更新和关联事务。
- 事务办理：受理、分派、退回、办结和部门责任查询。
- 信访流转：签收、分派、办理、审核、复查、催办和流转记录。
- 公告与部门：公告置顶、分类检索、部门信息及关联业务查看。
- 身份与权限：用户、角色、细粒度权限、会话令牌、账号停用和会话撤销。
- 审计记录：关键身份操作留痕，并对口令和令牌等敏感字段做过滤。
- 后台任务：使用 SQLite 保存待执行任务，支持去重、租约、重试和完成回执。
- 数据保留与清理：按资源类型和状态配置保留策略，支持有原因、有期限的单条或关联链冻结；清理任务先生成候选清单，经授权确认后分批执行，被冻结或仍被有效业务引用的数据一律保留；支持逐条解释保留或清理原因，并可从清单、执行批次和审计记录逐项对账。

## 运行环境

- Python 3.11
- SQLite 3，由 Python 标准库提供
- Linux、macOS 或 Windows

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

配置项均以 `TOWNSHIP_` 开头。可以复制 `.env.example` 后按需设置，默认数据库位于 `./data/township.db`。

## 初始化与检查

```bash
python -m app.cli init-db
python -m app.cli check-db
```

## 启动服务

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

首次部署可创建唯一的初始管理员：

```bash
curl -sS -X POST http://127.0.0.1:8432/api/auth/bootstrap   -H 'Content-Type: application/json'   -d '{"username":"admin","password":"Admin!23456","client_label":"initial-setup"}'
```

之后通过 `/api/auth/login` 获取会话令牌，并在管理接口请求头中使用 `Authorization: Bearer <token>`。

## 测试

```bash
python -m pytest
```

测试覆盖身份初始化、登录、用户与角色维护、权限计算、账号停用后的会话撤销、审计脱敏、居民事务、信访状态流转、公告排序、后台任务去重与领取，以及数据库时间格式。

## 编译检查

```bash
python -m compileall -q app tests
```

## API 冒烟

```bash
python -m app.cli smoke
```

该命令在进程内启动应用并检查服务根路径与健康接口，适合部署前快速确认路由和数据库初始化是否正常。

## 目录结构

```text
app/
  api/             用户、角色、审计、认证和系统接口
  core/            时钟、安全、异常和分页能力
  repositories/    SQLite 查询与持久化读取
  routers/         居民、事务、公告、部门和信访业务接口
  schemas/         管理接口输入模型
  services/        身份、审计和后台任务领域服务
  cli.py           初始化、检查和冒烟入口
  database.py      SQLite 连接、事务、表结构与基础权限
tests/             核心、管理接口和原有业务回归测试
tools/             本地维护脚本
```

## 数据保留与清理

`/api/retention` 提供历史数据清理能力，覆盖居民档案、政务事务、信访件、公告和审计事件五类资源：

- **保留策略**：`PUT /api/retention/policies` 按资源类型和业务状态配置保留天数（如“已办结事务保留 365 天”），可停用或删除。
- **冻结**：`POST /api/retention/holds` 对单条记录（`record`）或关联链（`chain`，如居民及其事务、信访件及其审计事件）设置有原因、有期限的冻结；到期自动失效，也可提前 `release` 解除。复查、投诉或审计调查期间，相关证据不会因清理任务被删除。
- **候选清单**：`POST /api/retention/plans` 依据当前启用的策略生成候选清单（`draft`），已被其他未完成清单占用的记录不会重复计入。
- **授权确认**：`POST /api/retention/plans/{id}/confirm` 需要独立的 `retention.confirm` 权限；确认后清单条目固化了当时的策略快照，之后修改策略不追溯影响该清单。
- **分批执行**：`POST /api/retention/plans/{id}/execute` 需要 `retention.execute` 权限，每批一个事务提交（批大小由 `TOWNSHIP_CLEANUP_BATCH_SIZE` 控制，默认 50）；执行时重新校验冻结和有效业务引用，命中的记录跳过并记录原因。任务中断后再次调用即从已提交边界继续，重复执行不会重复计数。
- **解释**：`GET /api/retention/explain?resource_type=&resource_id=` 说明某条数据为何被保留（冻结、引用、未到期）或何时被哪个清单清理。
- **对账**：`GET /api/retention/plans/{id}/reconcile` 逐项核对清单条目、执行批次和审计记录是否一致。

## 数据一致性

SQLite 连接默认启用外键、WAL、busy timeout 与同步写入策略。需要跨多张表更新的管理操作在即时事务中执行，失败会整体回滚。会话令牌只保存摘要；用户停用会撤销仍有效的会话。审计事件保存操作者、动作、资源、结果和前后状态，但不会保存明文密码或令牌。
