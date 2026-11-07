# courseware-api

课程平台服务端：FastAPI、SQLAlchemy、Alembic、PostgreSQL，包含独立更新 Worker。首版已实现账号/会话、课程权限、元数据入库、中文检索、发布回退和受保护静态文件服务。

## 开发

推荐从 `courseware-dev` 工作区执行 `python3 scripts/dev.py setup` 和 `python3 scripts/dev.py start`。独立运行时先提供环境变量：

- `CW_DATABASE_URL`：PostgreSQL 的 `postgresql+psycopg://...` 连接字符串。
- `CW_SECRET_KEY`：至少 32 字符的随机服务端密钥。
- `CW_PUBLIC_ORIGIN`：浏览器的完整同源地址，无路径。
- `CW_STORAGE_PATH`：发布文件和临时工作区的绝对目录。
- `CW_SECURE_COOKIES`：生产默认 true；仅本机 HTTP 开发设为 false。
- `CW_X_ACCEL`：生产 Nginx 授权文件服务设为 true；本地直发默认为 false。

```bash
uv sync --frozen
uv run alembic upgrade head
uv run courseware create-admin admin
uv run uvicorn courseware.main:app --host 127.0.0.1 --port 8008
# 另一终端，使用同样的环境配置：
uv run courseware worker
```

启动前创建 `$CW_STORAGE_PATH/releases`，API 对其只读，Worker 对整个存储目录可写。命令行创建管理员使用交互式密码，也可从 `CW_ADMIN_PASSWORD` 读取，不接受明文密码命令行参数。

## 课程与契约

课程标准唯一维护在 `courseware-skill`。本仓库打包支持的 Schema，`src/courseware/schemas/provenance.json` 记录来源及 SHA-256，导入不依赖运行时联网获取规范。更新契约时一并更新 Schema、来源记录和验证测试。

```bash
uv run courseware validate /path/to/course
uv run courseware import-local /path/to/trusted/course --username admin --list
uv run courseware export-openapi openapi.json
```

`import-local` 是显式的开发/管理员预览入口，记录 local-preview 与内容哈希，允许未提交文件。HTTP 接口只登记部署白名单内的 HTTPS Git 地址，Worker 发布确定提交里的文件，不执行课程仓库脚本。私有仓库凭据仅由 Worker 环境提供，见根工作区开发说明。

API 业务入口 `/api/v1/`，课程文件 `/courses/`。接口返回明确 DTO，不暴露数据库模型、原始清单或磁盘路径。`openapi.json` 为导出产物，前端据此生成类型；生产不公开交互式文档。

## 测试

```bash
uv run ruff check src tests
uv run pytest -q
uv run alembic check
```

pytest 必须指向名为 `courseware_test` 的独立数据库，否则拒绝执行；测试会清空这个数据库的业务表。测试样例使用同一工作区的 `courseware-demo`。覆盖会话撤销、CSRF、权限、静态资源、中文搜索、Git 提交导出、失败保留旧版本、回退和任务恢复。生产文件代理另由根工作区 `scripts/check-gateway.py` 验证。

## 安装后的服务命令

发布包 `courseware-api` 提供 `courseware-api` 命令，并保留 `courseware` 别名。

```bash
courseware-api init                  # 首次生成配置；编辑 PostgreSQL 连接后再运行以迁移
courseware-api create-admin admin
courseware-api server start          # 同时管理 API（8008）与同步 Worker
courseware-api server status
courseware-api server stop
courseware-api --version
```

配置为 `~/.farfarfun/courseware/backend/config.json`，可由 `--config` 或 `CW_CONFIG_FILE` 覆盖；环境变量优先。默认公开入口为新安装前端的 `http://127.0.0.1:8817`，现有 frpc 环境填写其实际入口。数据库由部署方提供，`init` 不清空数据库；迁移和 Schema 均打入 wheel。日志在配置旁的 `run/server.log`，升级保留配置与存储。`server run` 用于前台诊断；`start/stop/restart/status` 管理整组进程。
