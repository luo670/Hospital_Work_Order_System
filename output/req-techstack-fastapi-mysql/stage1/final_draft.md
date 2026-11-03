# 项目技术文档 · 求职作品集

## 医院运维工单系统 · 技术栈与架构汇总

纯网页全栈工单管理系统（FastAPI + MySQL + 原生前端 + 千问大模型）

编制日期：2026 年 11 月

用途：技术选型与架构能力梳理，用于求职面试展示

### 一、项目定位

一套纯网页运行的医院 IT 运维工单管理系统，面向「管理员 / 主管 / 工程师」三角色协作。核心覆盖：工单全生命周期管理（电脑 / 打印机 / PDA 三类故障）、24 小时超时统计、经验标签沉淀复用、AI 员工评估（SSE 流式输出）、RAG 故障经验检索，以及 24 小时在岗排班。技术上以「轻量可移植 + 生产可部署（ASGI + MySQL 事务型存储）」为核心设计原则：后端采用现代异步框架 FastAPI，数据层采用生产级 MySQL，前端仍为零框架原生实现，整套可由内嵌绿色 Python 一键启动，也易于容器化部署，适合作为全栈能力与工程实践的展示样例。

### 二、技术栈总览（分层）

| 层级 | 技术 / 框架 | 选型要点 |
| --- | --- | --- |
| 后端框架 | FastAPI（Python，ASGI） | 现代异步 Web 框架，路由 + 依赖注入（Depends）+ 数据校验一体 |
| 模板引擎 | Jinja2（Starlette Jinja2Templates） | 服务端渲染（SSR），无前后端分离 |
| 前端样式 | 原生 HTML5 + CSS3 | 手写设计系统（CSS 变量 + Flexbox + Grid），无 UI 框架 |
| 前端图表 | 原生 SVG | 仪表盘柱状 / 条形图，零依赖、免联网 |
| 数据存储 | MySQL（SQLAlchemy ORM + PyMySQL 驱动） | 事务型关系数据库，生产级并发与一致性，UTF8MB4 |
| 密码安全 | Werkzeug Security | pbkdf2:sha256 哈希，不存明文密码 |
| AI 能力 | 通义千问（DashScope）OpenAI 兼容 API + text-embedding-v3 | 员工评估（支持 SSE 流式输出）、RAG 故障经验检索，默认 qwen-plus，支持模型回退 |
| 向量检索 | text-embedding-v3 + 余弦相似度（纯 Python） | 历史工单向量化，应用内召回 Top-K，无需外部向量库 |
| 流式传输 | SSE（text/event-stream） | 员工评估逐 token 推送，前端 EventSource 逐字显示 |
| 并发桥接 | Thread + asyncio.Queue | 同步 urllib 流式生成不阻塞 ASGI 事件循环 |
| HTTP 客户端 | Python 标准库 urllib | 直连大模型接口，不引入第三方 HTTP 库 |
| 配置管理 | .env + os.environ | API Key 外置，杜绝硬编码 |
| 包管理 | uv（pyproject.toml + uv.lock） | 依赖声明与版本锁定分离，dev 依赖独立分组；uv sync --frozen 保证本机与镜像装出同一套版本 |
| 运行环境 | 内嵌绿色版 Python | 免装环境，一键启动（uvicorn main:app） |
| 容器化部署 | Docker + Docker Compose | 应用镜像 python:3.13-slim、数据库 mysql:8.0；健康检查驱动启动顺序，数据卷持久化 |

### 三、各端技术细节

1. 后端（Server / Backend）
- FastAPI 路由与视图：所有页面与接口由 main.py 中的异步函数承载，使用 @app.get / @app.post / @app.api_route 声明路由，依赖注入（dependencies=[Depends(...)]）统一挂载鉴权与数据库会话，TemplateResponse 完成服务端渲染，RedirectResponse / url_for 处理跳转。
- 会话与鉴权（RBAC）：基于 Starlette SessionMiddleware 维护登录态；自研 login_required / role_required 依赖（Depends）做统一拦截，越权时通过异常处理器（AuthRedirect）统一返回 302 跳转；角色分三级——admin（管理员，含全部后台权限）、supervisor（主管，可下派工单与评估）、engineer（工程师，处理工单）。权限判断贯穿导航渲染与接口层。
- 业务与数据分离：main.py 只负责路由、权限、业务逻辑；ORM 模型独立为 models.py，数据库引擎/会话独立为 database.py，模板/闪现/鉴权辅助独立为 web.py，演示数据独立为 seed_data.py，排班算法独立为 schedules.py，配置独立为 config.py，职责清晰、便于维护。
- 数据库自动迁移：Base.metadata.create_all 按 ORM 模型建表；MySQL 场景下启动时自动 CREATE DATABASE（需连接账号具备建库权限），保证代码升级后库表自动就绪。
- 密码哈希：建账号时使用 werkzeug.security.generate_password_hash（默认 pbkdf2:sha256），登录时 check_password_hash 校验，全库不存明文。
- 大模型调用：call_qwen() 用标准库 urllib.request 直接 POST 到 DashScope 的 OpenAI 兼容端点，未引入 openai SDK，减少依赖体积；支持模型自动回退。call_qwen_stream() 以 stream=true 逐行解析 SSE 增量，供员工评估流式输出；call_embedding() 调用 text-embedding-v3 完成历史工单向量化。

2. 前端（Web Frontend）
- 模板引擎 Jinja2：页面均位于 templates/，以 base.html 为布局母板（侧边栏 + 顶栏 + 内容区），各页面通过 {% block %} 继承复用；渲染由 Starlette 的 Jinja2Templates 完成，并通过 web.py 注入 url_for / get_flashed_messages / g / endpoint 复刻 Flask 上下文，保持 UI 一致。
- 手写 CSS 设计系统：base.html 内定义一套以 CSS 变量驱动的视觉规范，使用 Flexbox 与 CSS Grid 完成卡片、表格、统计卡等布局；无任何第三方 UI 框架。
- 原生 SVG 图表：「工单情况」页的近 14 天工单量、超时量、类型分布等图表由服务端生成内联 SVG，不依赖 ECharts / Chart.js 等库，断网可用。
- 轻量交互：交互以原生表单提交、少量原生 JS 与点击芯片快速填充标签为主；无前端框架、无打包构建步骤。
- 响应式：通过 CSS 媒体查询在窄屏下隐藏侧边栏、压缩统计卡列数。

3. 数据层（Data / Persistence）
- MySQL 关系数据库：数据落在由 DATABASE_URL 指定的 MySQL 库（mysql+pymysql://...，UTF8MB4），具备事务、并发与一致性保障；需要安装数据库服务或使用云数据库，备份通过 mysqldump / 云快照完成。
- 核心表设计：users（含角色、分组、特殊岗位标记）、groups、departments、work_orders（含状态、报障 / 故障 / 解决字段、满意度、时间戳）、workorder_tags（故障原因 / 解决方法复用标签）、evaluations（AI 评估缓存）、workorder_knowledge（RAG 历史工单向量库）、schedules（排班）——均由 SQLAlchemy ORM 模型（models.py）定义，全部 SQL 走参数化查询防注入。
- 演示数据外置：50 条工单、17 名工程师、3 分组、14 科室等测试数据由 seed_data.py 写入数据库，而非硬编码在业务源码中，符合「数据即配置」的实践。
- 标签沉淀：工程师完成工单时填写的「故障原因 / 解决方法」自动入库为可复用标签，后续可一键回填，体现数据闭环设计。

4. AI 集成（大模型应用）
- 通义千问 OpenAI 兼容接口：通过 .env 中的 DASHSCOPE_BASE_URL（兼容模式 /compatible-mode/v1）对接 DashScope（或私有 MaaS 部署），模型默认 qwen-plus（低成本可用），可在 .env 用 QWEN_MODEL 覆盖。
- 业务落地：主管 / 管理员进入「员工评估」模块，系统汇总某工程师的已完结工单（数量、平均处理时长、超时数、满意度），调用大模型生成工作表现总结（时效、积极性、改进建议）。详情页支持 SSE 流式输出，前端逐字显示生成过程，完成后自动落库。
- RAG 故障经验检索：管理员把「已完成且填写故障原因 / 解决方法」的历史工单用 text-embedding-v3 向量化写入 workorder_knowledge 表；工程师输入新故障描述后，系统在应用内做余弦相似度召回 Top-3 工单，再交给千问生成针对性建议。无需外部向量数据库，百~千级数据量下性价比最高。
- 安全与成本：API Key 经 os.environ 读取，绝不出现在源码；评估结果写入 evaluations 表缓存，可「重新生成」，避免重复消耗 token。

4.1 本次新增技术细节：RAG 与 SSE 流式输出
- 离线索引（RAG Indexing）：只索引「已完成 + 有故障原因 + 有解决方法」的工单，把「类型 / 科室 / 报障内容 / 原因 / 解决方法」拼成一段文本，调用 text-embedding-v3 生成向量，以 JSON 字符串存入 workorder_knowledge.embedding（MySQL TEXT），不引入向量数据库，避免额外运维成本。
- 在线检索（Retrieval）：工程师输入新故障描述后先向量化，再与应用内全部工单向量做余弦相似度计算，召回 Top-3；余弦相似度用纯 Python 实现（不依赖 numpy），百~千级数据量下为毫秒级。
- 生成增强（Generation）：把 Top-3 历史工单作为上下文拼入 prompt，调用 qwen-plus 生成不超过 300 字的排查与解决建议；同时把相似度、故障原因、解决方法原样返回前端，避免「黑盒」结论。
- SSE 流式链路：call_qwen_stream() 以 stream=true 请求模型，逐行解析 `data:` 增量；服务端用 StreamingResponse 输出 text/event-stream；由于 urllib 为同步阻塞，实际生成放在后台线程，经 asyncio.Queue 把 token 回传事件循环，避免阻塞其它请求；前端用 EventSource 监听 token / done / error 事件实现逐字显示。
- 结果一致性：流式结束后统一做「空内容 / 超 500 字」校验，并用独立 SessionLocal 落库 evaluations，保证与原有非流式接口同一口径、同样可缓存复用。
- 成本与稳健性：索引按 20 条分批 commit 的批量重建；日常每次提问仅 1 次 query embedding + 1 次对话调用；embedding 端点支持 EMBEDDING_BASE_URL → DASHSCOPE_BASE_URL 自动回退，兼容「私有 MaaS 网关 + 公共 DashScope」混合部署。

5. 工程化与运行（Tooling / DevOps）
- 依赖清晰：uv 管理依赖——pyproject.toml 的 [project.dependencies] 声明 fastapi / uvicorn / sqlalchemy / pymysql / cryptography / itsdangerous / python-dotenv / python-multipart / werkzeug / jinja2，pytest 独立放在 [dependency-groups].dev；uv.lock 锁定精确版本与哈希，uv sync 一键还原环境、uv run 直接在项目虚拟环境内执行。
- 内嵌绿色 Python：项目自带 python/ 目录的免安装 Python（含 pip 与依赖），配合 run.bat（Windows）/ run.sh（Linux / macOS）以 uvicorn main:app 一键启动，摆脱环境问题。
- 排班算法：schedules.py 以约束求解方式生成排班——保证每日 24 小时在岗、工作日白班 2 人 / 夜班 1 人、非工作日白班 1 人 / 夜班 1 人、夜班次日休息、单周工时 ≤ 48 小时标红，并按分组均衡分配，结果写入 schedules 表（SQLAlchemy 参数化查询）。
- 密钥外置：.env 管理 AI 密钥与可切换模型，运行期注入环境变量。

5.1 容器化部署（Docker / Compose）
- 镜像构建：单阶段 Dockerfile，基础镜像 python:3.13-slim；先 pip install --no-cache-dir uv 安装 uv，再 COPY pyproject.toml uv.lock 并执行 uv sync --frozen --no-dev（只装运行时依赖），最后 COPY . . 并通过 ENV PATH="/app/.venv/bin:$PATH" 指向虚拟环境内的 uvicorn；依赖层与代码层分离，业务代码变更不会触发重新装依赖；不采用多阶段构建，保持简单可读。
- 构建上下文瘦身：.dockerignore 排除 .env（密钥绝不进镜像）、*.db、python/（内置绿色 Python，镜像内用官方解释器）、tests/、README.md、.git/、__pycache__/、.pytest_cache/、.venv/ 等，缩小传输体积并避免本地状态污染镜像。
- 服务编排：docker-compose.yml 定义 db（mysql:8.0，utf8mb4 / utf8mb4_unicode_ci 与应用建库语句一致，mysql_data 数据卷持久化，mysqladmin ping 健康检查 interval 5s / start_period 60s）与 app（build .、5000:5000、restart: unless-stopped）两个服务；app 用 depends_on 长格式 condition: service_healthy，确保 MySQL 真正可接受连接后才启动，避免「端口已监听但仍在初始化」导致的首次连接失败。
- 配置注入（12-Factor）：DATABASE_URL / SECRET_KEY / DASHSCOPE_API_KEY / QWEN_MODEL / EMBEDDING_MODEL 等全部由 compose 环境变量注入，其中 DATABASE_URL 指向容器内服务名 db；本地直接 uvicorn 运行时仍走 config.py 的 127.0.0.1 默认值，两种运行方式互不干扰。
- 连接池调优：create_engine 显式 pool_pre_ping=True、pool_size=10、max_overflow=20，避免容器内「多并发请求 + SSE 流式任务」同时占用连接耗尽默认池（5 + 10）；文件型 SQLite 同样使用 QueuePool，接受这些参数，本地冒烟行为不变。
- 单 worker 约束：SSE 流式的生成任务经线程回调「本进程」的事件循环，镜像 CMD 固定单 worker；若要扩多 worker，必须在反向代理层做会话粘性，否则同一会话的请求与流式任务会落到不同进程。
- 认证依赖补齐：MySQL 8 默认认证插件 caching_sha2_password 在非 TLS 的 TCP 连接上首次认证需 RSA 公钥加密，PyMySQL 依赖 cryptography 包，已在 pyproject.toml 显式声明，避免容器内首次连接即报错。

5.2 依赖管理迁移：由 requirements.txt 切换为 uv
- 项目元数据：uv init 生成 pyproject.toml——name = ticket-system、version = 0.1.0、description、readme = README.md、requires-python = ">=3.13"；作为「应用」而非可分发包，不声明 build-system，uv sync 只安装依赖、不构建项目自身。
- 依赖分层：[project.dependencies] 只放运行时依赖（含 cryptography 与 itsdangerous）；pytest 放入 [dependency-groups].dev（PEP 735 依赖组），生产/镜像用 uv sync --no-dev 直接跳过，避免测试依赖进入运行环境。
- 锁定与复现：uv.lock 记录每个包的精确版本、下载地址与哈希，uv sync --frozen 严格按锁文件安装、不做二次解析；本机、CI 与镜像三处环境完全一致，杜绝「本机能跑、容器报错」。
- 迁移方式：uv add -r requirements.txt 批量导入原有依赖，随后 uv remove pytest + uv add --dev pytest 完成主依赖与开发依赖的拆层；迁移完成后删除 requirements.txt，避免两套依赖清单并存造成漂移。
- 真实收益：迁移后用锁文件版本（fastapi 0.162 / starlette 1.10 / sqlalchemy 2.1.6）起服务做冒烟，暴露出 starlette 的 SessionMiddleware 依赖 itsdangerous，而原 requirements.txt 从未声明它（此前只是靠 Flask 时代遗留的间接安装才没报错）——已在 pyproject.toml 补声明，属于「锁定依赖后才会暴露的隐性缺失」。


### 四、架构亮点（可用于面试阐述）

- 生产级可部署 + 零外部服务依赖并存：后端 FastAPI（ASGI）具备高并发能力，数据层 MySQL 提供事务型存储，可单机部署也可容器化；同时保留 SQLite 作为本地冒烟 / 演示的可选项，整套仍可由内嵌 Python 一键启动，体现可移植性。
- 关注点分离：main.py（路由 / 权限 / 业务）、models.py（ORM）、database.py（引擎会话）、web.py（模板·闪现·鉴权）、schedules.py（算法）、seed_data.py（数据）、config.py（配置）解耦，模块边界清晰，利于测试与演进。
- 安全实践落地：密码哈希、API Key 外置、基于依赖注入的 RBAC 拦截、全部 SQL 走 SQLAlchemy 参数化（防注入），并非玩具项目。
- AI 解决真实业务问题：将大模型用于「员工评估」这类非结构化总结任务，并配套结果缓存与成本意识；进一步以 RAG 把历史工单经验转化为可检索的解决建议，展示 AI 工程化应用能力。
- 约束求解能力：排班模块把人力规则转化为可计算约束并自动均衡，展示算法建模与工程实现能力。
- 容器化交付：单阶段 Dockerfile + compose 双服务，健康检查驱动启动顺序、数据卷持久化、密钥全部环境变量注入，一条 docker compose up --build 即可复现整套环境；同时保留内嵌 Python 一键启动路径，兼顾「可复现部署」与「零环境演示」。

### 五、项目目录结构

```
工单系统/
├── main.py                 # 主程序：FastAPI 路由 / 依赖注入鉴权 / 业务逻辑
├── config.py               # 配置中心（DATABASE_URL / SECRET_KEY / 千问模型，读 .env）
├── database.py             # 引擎 / 会话 / 声明基类（含 MySQL 自动建库）
├── models.py               # SQLAlchemy ORM 模型（8 张表，含 RAG 知识库表）
├── web.py                  # 模板渲染 / 闪现消息 / 鉴权依赖 / 异常跳转
├── schedules.py            # 排班算法（约束求解，SQLAlchemy 参数化）
├── seed_data.py            # 演示数据（写入数据库，非硬编码）
├── tests/                  # 单元测试（pytest：核心纯函数 + RAG 检索 + SSE 帧）
├── pyproject.toml          # 项目与依赖声明（uv：[project.dependencies] + dev 组）
├── uv.lock                 # 依赖锁文件（精确版本与哈希，保证环境可复现）
├── Dockerfile              # 应用镜像（python:3.13-slim，单 worker，SSE 兼容）
├── docker-compose.yml      # app + mysql:8.0 编排（健康检查驱动启动顺序 / 数据卷）
├── .dockerignore           # 排除 .env、*.db、python/ 等，密钥与本地文件不进镜像
├── .env                    # 数据库连接 / AI 密钥（不入库）
├── python/                 # 内嵌绿色版 Python（免装即用）
├── run.bat / run.sh        # 一键启动（uvicorn main:app）
├── templates/              # Jinja2 页面模板（base.html 为母板）
└── static/                 # 静态资源
```

### 六、可迁移能力总结

通过本项目可展示的能力栈：Python 全栈开发（FastAPI + Jinja2 SSR + ASGI）、关系型数据建模与 MySQL / SQLAlchemy ORM 实战、基于依赖注入的 RBAC 权限系统设计、原生前端（HTML / CSS / SVG）实现能力、大模型 API 集成与工程化处理（密钥管理 / 结果缓存 / 模型回退 / RAG 检索增强 / 向量化与余弦召回 / SSE 流式输出与同步异步桥接 / 成本控制）、Docker 容器化交付（镜像分层缓存 / 编排与健康检查 / 配置注入 / 连接池调优）、uv 依赖管理（声明与锁定分离 / dev 依赖分组 / --frozen 可复现安装）、约束求解类算法落地，以及生产级可部署交付与零依赖可移植交付的双重工程意识。这些能力可平滑迁移到中后台系统、内部工具、SaaS 管理端等常见岗位场景。
