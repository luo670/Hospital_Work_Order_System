# 医院运维工单系统

一个纯网页运行的医院运维工单管理系统，使用 **Python + FastAPI（ASGI）+ MySQL** 实现，前端为零框架原生 HTML/CSS/SVG，适合作为贴近生产环境的全栈能力与工程实践展示样例。

## 技术栈

- **Web 框架**：FastAPI（异步 ASGI），服务端模板渲染沿用 Jinja2（Starlette `Jinja2Templates`）
- **数据库**：MySQL（通过 SQLAlchemy ORM + PyMySQL 驱动），UTF8MB4；本地冒烟可使用 SQLite
- **鉴权**：基于会话（Starlette `SessionMiddleware`）+ Werkzeug 密码哈希；依赖注入（`Depends`）实现 `login_required` / `role_required`
- **AI**：通义千问（OpenAI 兼容接口，标准库 `urllib` 直连）；员工评估支持 **SSE 流式输出**；智能助手使用 **text-embedding-v3** 向量化历史工单实现 RAG 检索
- **部署**：Docker + Docker Compose（`python:3.13-slim` 应用镜像 + `mysql:8.0` 数据库服务，单 worker 运行）

## 功能概览

### 三种角色
| 角色 | 权限 |
| --- | --- |
| **管理员 (admin)** | 拥有主管与工程师的全部功能；可对**科室**、**用户**、**工程师分组**进行增删改查（含创建账号、分配角色权限、启用/禁用） |
| **主管 (supervisor)** | 报障、**下派工单**给工程师、查看工单情况 |
| **工程师 (engineer)** | 报障、**接受工单**、**完成工单**（填写故障原因与解决方法） |

> 三个角色都可以报障（提交工单）。管理员默认拥有另外两个角色的全部操作权限。

### 工单负责人规则
- 每张工单最终都落到**某位工程师名下（负责人）**：
  - 主管 / 管理员发起的报障，必须在新建时**下派给指定工程师**，由其负责处理；
  - 工程师本人发起的报障，负责人即发起人自己。
- 管理员 / 主管可在工单未完结时**改派**负责人。

### 工单类型
- 电脑故障
- 打印机故障
- PDA 故障

### 工单字段
- **报障科室**、**报障内容**：由报障人（任一角色）填写
- **故障原因**、**解决方法**：由负责人工程师在处理完成后填写
- 状态流转：`待接受 → 处理中 → 已完成`
- **超时规则**：工单提交后 **24 小时**内未完成即视为超时（含延迟完结，在「工单情况」中单独统计）

### 工程师分组
- 工程师可按专业划分为若干**分组**（如网络运维组、硬件运维组、系统软件组）；
- 分组由管理员在「工程师分组」中维护，并在创建 / 编辑工程师账号时指定所属分组；
- 工单按负责人归属分组，便于统计各团队工作量。

### 工单情况（原仪表盘）
- 工单总数 / 已完成 / 处理中 / 超时工单 概览卡片
- 近 14 天「每日工单数量」与「超时工单数量」柱状图（内置 SVG，无需联网）
- 工单类型分布、各分组工单量横向条形图

### 经验标签复用
- 工程师**完成工单**时填写的「故障原因」「解决方法」，会自动沉淀为**可复用标签**；
- 下次完成工单时，详情页会展示历史常用标签，**点击即可快速填入**，减少重复录入。

### 满意度评定
- 工单满意度**默认满分（满意）**；
- **管理员**可在工单详情将特殊工单评定为「不满意」，且**必须填写不满意原因**；
- 满意度纳入「员工评估」的量化统计。

### 员工评估（AI 大模型）
- **主管 / 管理员**导航栏新增「员工评估」模块；
- 针对每位工程师，汇总其**已完结工单**的：完成数量、平均处理时长（从派发到完结的耗时）、超时数、满意度，并接入**千问大模型**自动生成工作表现总结（含工作积极性、处理时效、满意度与改进建议）；
- 评估结果写入库缓存，可随时「生成 / 重新生成」，避免重复消耗 token；
- **流式输出（SSE）**：详情页点击「生成评估（流式）」后，后端通过 `text/event-stream` 逐 token 推送，前端逐字显示，生成完成即自动落库；同时保留「非流式生成」作为兜底（流式失败或浏览器不支持 SSE 时可用）；
- 处理时长口径：= 工单「派发给该工程师」之时 至 「工程师提交完结」之时。

### 智能助手（RAG 故障经验检索）
- **全部角色**（尤其一线工程师）导航栏新增「智能助手」入口：输入新故障的文字描述，系统检索**相似历史工单**并给出解决建议；
- **离线建库**：管理员在页面点击「构建 / 重建知识库」，系统把「已完成且填写了故障原因 / 解决方法」的历史工单拼接为文本，调用 `text-embedding-v3` 向量化后存入 `workorder_knowledge` 表；
- **在线检索**：提交描述后先将其向量化，在应用内用**余弦相似度**召回最相似的 Top-3 工单（无需外部向量数据库，百~千级数据量下性价比最高），再把召回结果作为上下文交给千问生成针对性建议；
- 页面同时展示每条历史工单的**相似度、故障原因与解决方法**，便于工程师自行判断，而不是盲信模型；
- 成本控制：embedding 只在「重建知识库」时批量调用；日常每次提问仅产生 1 次 query embedding + 1 次对话调用。知识库为空时会明确提示先建库。

### 排班表
- **全部角色**导航栏均有「排班表」入口，可按**分组**与**月份**切换查看；
- 各分组每日 24 小时均需有人在岗：工作日白班 09:00–17:00 需 **2 人**、夜班 17:00–次日 09:00 需 **1 人**；非工作日（周末 / 节假日）全天 00:00–24:00 仅需 **1 人**；
- 值夜班（或全天班）的工程师次日休息至少一天；系统按「均衡工时」自动生成 10 月、11 月排班并写入数据库；
- 页面按自然周统计每位工程师工时，**超出单周 48h 上限自动标红**提示（受班组编制限制，3–4 人小组在 24h 在岗 + 夜班休息规则下无法严格满足 48h，详见页面说明）。

### 演示 / 测试数据说明
- 测试数据（50 条工单、17 名工程师、1 名主管、3 个分组、14 个科室等）**不直接写在 `main.py` 主源码中**，而是放在独立模块 `seed_data.py`，
  通过 `main.py` 启动时（库为空）自动**写入数据库**；
- 排班数据由独立模块 `schedules.py` 生成并写入 `schedules` 表；
- 如需重置 / 重写全部演示数据与排班：清空数据库后重新启动应用即可（`Base.metadata.create_all` 重建表结构，空库时自动写入演示数据与排班）。

#### AI 配置（`.env`，密钥绝不硬编码进代码）
项目根目录的 `.env` 中已提供：
```
DASHSCOPE_API_KEY=你的密钥
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```
程序启动时通过 `os.environ` 读取，**密钥不会出现在源码中**。默认使用成本最低的可用千问模型 `qwen-plus`，
如需更换可在 `.env` 增加 `QWEN_MODEL=模型名`（如 `qwen-max`、`qwen-long`）。若首选模型在账号下不可用，会自动回退到其它千问模型。

RAG 向量化默认走公共 DashScope 的 embeddings 接口（与对话共用同一个 `DASHSCOPE_API_KEY`），也可在 `.env` 覆盖：
```
EMBEDDING_MODEL=text-embedding-v3
EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```
若自建的 MaaS 网关同时代理了 embeddings，可将 `EMBEDDING_BASE_URL` 指向该网关；否则保持默认即可。代码在 `EMBEDDING_BASE_URL` 调用失败时会自动回退到 `DASHSCOPE_BASE_URL` 对应的 `/embeddings`，兼容「对话走私有网关、向量走公共接口」的混合部署。

## 数据库（MySQL）

生产环境在项目根目录 `.env` 中配置连接串：
```
DATABASE_URL=mysql+pymysql://用户名:密码@主机:3306/workorder?charset=utf8mb4
```
- 首次启动若数据库不存在，程序会自动执行 `CREATE DATABASE`（需连接账号具备建库权限）；否则请先手动建库：
  ```sql
  CREATE DATABASE workorder CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
  ```
- 表结构由 `models.py`（SQLAlchemy ORM）定义，启动时 `Base.metadata.create_all` 自动建表；
- 本地冒烟测试可临时用 SQLite 覆盖：`DATABASE_URL="sqlite:///./dev.db" ./python/python.exe -m uvicorn main:app`。

## 运行方式

方式一：使用内置绿色 Python（仅适用于原始工作目录，免装环境）
---------------------------------------------
本目录已内置一个绿色版 Python（`python\python.exe`，含 pip 与 FastAPI 等依赖），
不依赖系统里的任何 Python，双击即可运行：

- Windows：直接双击 `run.bat`
- 命令行：`.\python\python.exe -m uvicorn main:app --host 0.0.0.0 --port 5000`

> ⚠️ `python/` 目录体积大，**不随仓库发布**（已在 `.gitignore` 中排除）。
> 从 GitHub 克隆本项目后没有这个目录，请使用方法二（uv）。

方式二：使用本机 Python + uv（推荐，克隆后首选）
--------------------------------
```bash
# 1. 准备环境变量（复制模板后填入 DASHSCOPE_API_KEY 等，.env 不会入库）
cp .env.example .env

# 2. 安装依赖（仅需一次；uv 会自动创建 .venv 并按 uv.lock 精确安装）
uv sync

# 3. 启动（首次启动会自动建表、写入演示数据与排班）
uv run uvicorn main:app --host 0.0.0.0 --port 5000
```

启动后浏览器访问：<http://localhost:5000>

## Docker 运行

**前置**：安装 [Docker](https://docs.docker.com/get-docker/) 与 Docker Compose（Docker Desktop 已自带，`docker compose version` 可验证）。

**1. 准备 `.env`**（项目根目录，至少包含大模型密钥）：

```bash
DASHSCOPE_API_KEY=你的密钥
# 以下可选，不填则用默认值
# DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
# QWEN_MODEL=qwen-plus
# EMBEDDING_MODEL=text-embedding-v3
# EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```

> `.env` 只用于 compose 的变量替换与注入，已被 `.dockerignore` 排除，**不会打进镜像**。

**2. 启动**（首次会自动构建镜像、初始化 MySQL 并写入演示数据与排班）：

```bash
docker compose up --build
```

**3. 访问**：<http://localhost:5000>（账号见下方「默认账号」）

**4. 停止 / 清空数据**：

```bash
docker compose down       # 停止容器，数据保留在 mysql_data 卷中
docker compose down -v    # 停止并删除数据卷，下次启动重新初始化演示数据
```

### 容器化说明

- **数据库地址**：compose 里 `DATABASE_URL` 已被覆盖为 `mysql+pymysql://root:root@db:3306/workorder?charset=utf8mb4`（容器内用服务名 `db` 解析）。本地直接 `python -m uvicorn main:app` 运行时不受影响，仍用 `config.py` 中的 `127.0.0.1` 默认值或 `.env` 里的配置。
- **启动顺序**：`app` 通过 `depends_on: db: condition: service_healthy` 等 MySQL 健康检查通过后才启动，避免「端口已监听但仍在初始化」导致首个连接失败。
- **RAG 知识库**：镜像不含向量数据，需登录管理员账号后在「智能助手」页首次点击**构建 / 重建知识库**才可用（该操作会调用 `text-embedding-v3`，需要有效的 `DASHSCOPE_API_KEY`）。数据存在 MySQL 中，容器重启不会丢失。
- **SSE 流式评估**：默认**单 worker** 启动，流式输出与后台线程落库均工作正常。**不要**贸然改成 `--workers N`：流式任务与登录会话都依附于单个进程，多 worker 需要在反向代理层做会话粘性（sticky session）。
- **依赖安装**：镜像内先 `pip install uv`，再 `COPY pyproject.toml uv.lock` 后执行 `uv sync --frozen --no-dev` —— `--frozen` 严格按锁文件安装（不重新解析版本），`--no-dev` 跳过 pytest 等测试依赖；`.venv` 由构建生成，不来自宿主机（`.dockerignore` 已排除）。

## 依赖管理与测试

项目使用 **uv** 管理依赖（`pyproject.toml` 声明 + `uv.lock` 锁定版本，要求 Python ≥ 3.13）：

```bash
uv sync               # 首次安装（默认包含 dev 组，即 pytest）
uv sync --no-dev      # 仅装运行时依赖（镜像构建即用此方式）
uv run uvicorn main:app   # 在 uv 创建的 .venv 中运行
uv add 包名           # 添加运行时依赖（写入 [project.dependencies]）
uv add --dev 包名     # 添加开发依赖（写入 [dependency-groups].dev）
```

- 运行时依赖在 `[project.dependencies]`，开发 / 测试依赖（`pytest`）在 `[dependency-groups].dev`，两者分离，生产环境用 `uv sync --no-dev` 即可跳过测试依赖。
- `uv.lock` 必须提交，保证本机与镜像装出完全一致的版本。
- 运行单元测试：
  ```bash
  uv run pytest tests/ -q
  ```

## 默认账号（首次启动自动写入演示数据）

系统账号（密码均为 `admin123` / `sup123` / `eng123`）：

| 用户名 | 密码 | 角色 | 备注 |
| --- | --- | --- | --- |
| admin | admin123 | 管理员 | 系统管理员 |
| supervisor | sup123 | 主管 | 王主管 |

17 名工程师（密码均为 `eng123`，已分组）：

| 用户名 | 姓名 | 所属分组 |
| --- | --- | --- |
| xugong | 徐工 | 网络运维组 |
| baogong | 包工 | 网络运维组 |
| wugong | 吴工 | 网络运维组 |
| xiagong | 夏工 | 网络运维组 |
| fenggong | 冯工 | 网络运维组 |
| chenggong | 陈工 | 网络运维组 |
| lianggong | 梁工 | 网络运维组（服务台：仅工作日 08:00–17:30 值班） |
| zhaogong | 赵工 | 硬件运维组 |
| qiangong | 钱工 | 硬件运维组 |
| sungong | 孙工 | 硬件运维组 |
| hegong | 何工 | 硬件运维组 |
| gaogong | 高工 | 硬件运维组 |
| liugong | 刘工 | 硬件运维组 |
| ligong | 李工 | 系统软件组 |
| zhougong | 周工 | 系统软件组 |
| zhenggong | 郑工 | 系统软件组 |
| wanggong | 王工 | 系统软件组 |

> 演示数据包含 **50 条工单**（近 1 个月内，网络运维组占大头，其中 2 条为超时工单且分配给网络运维组之外的工程师），用于功能体验与演示。
> ⚠️ 演示数据仅用于快速体验，生产环境请务必修改默认密码、删除演示账号，并在 `.env` / `config.py` 中替换 `SECRET_KEY`。

## 目录结构
```
工单系统/
├── main.py              # 主程序（FastAPI 路由、依赖注入鉴权、业务逻辑）
├── config.py            # 配置中心（DATABASE_URL / SECRET_KEY / 千问模型，读 .env）
├── database.py          # 引擎 / 会话 / 声明基类（含 MySQL 自动建库）
├── models.py            # SQLAlchemy ORM 模型（8 张表，含 RAG 知识库表）
├── web.py               # 模板渲染 / 闪现消息 / 鉴权依赖 / 异常跳转
├── schedules.py         # 排班算法（约束求解，SQLAlchemy 参数化）
├── seed_data.py         # 演示数据（写入数据库，非硬编码）
├── tests/               # 单元测试（pytest：核心纯函数 + RAG 检索 + SSE 帧）
├── pyproject.toml       # 项目与依赖声明（uv 管理：[project.dependencies] + dev 组）
├── uv.lock              # 依赖锁文件（精确版本与哈希，必须提交，保证环境可复现）
├── Dockerfile           # 应用镜像（python:3.13-slim，单 worker 运行）
├── docker-compose.yml   # 一键起 app + mysql:8.0（含健康检查与数据卷）
├── .dockerignore        # 排除 .env / *.db / python/ 等，避免密钥与本地文件进镜像
├── .gitignore           # 排除 .env / *.db / .venv / python/ / 日志等
├── .env                 # 数据库连接 / AI 密钥（本地私有，不入库、不打进镜像）
├── .env.example         # 环境变量模板（入库，供克隆者复制后填写）
├── .python-version      # uv 使用的 Python 版本（3.13，入库）
├── python/              # 内置绿色版 Python（不随仓库发布，已在 .gitignore 排除）
├── run.bat / run.sh     # 一键启动脚本（uvicorn main:app，依赖 python/ 目录）
├── templates/           # 页面模板（base.html 为母板）
└── static/              # 静态资源
```

## 说明
- 账号体系：所有账号必须由管理员在「用户管理」中创建并分配角色，不存在自助注册；账号被禁用后无法登录。
- 删除科室时，若该科室下仍有工单则禁止删除，需先处理相关工单。
- 数据全部保存在配置的 MySQL 数据库中（生产）或 SQLite 文件（本地冒烟），方便备份与迁移。
