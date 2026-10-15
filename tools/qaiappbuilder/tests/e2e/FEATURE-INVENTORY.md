# E2E 功能清单(草稿,待评审)

> 状态:**草稿**。由代码梳理得出,**尚未与产品/开发确认**,优先级是建议值。
> 来源:`interfaces/http/routes/**`、`frontend/src/router/routes.ts`、
> `frontend/src/components/**` 的 `data-testid`、`factory/chat_features/**`、`QUICK-START.md`。
> 本文档只列"要测什么",不含用例设计和代码。

## 1. 分层与标记

| 层 | 含义 | 运行环境 | pytest marker |
|---|---|---|---|
| **L1 API** | 拉起真实后端子进程,用 HTTP/SSE/WS 调用 | 任意 Windows/Linux,不要 NPU | `e2e` |
| **L2 UI** | Playwright 驱动浏览器,打的是 `frontend/dist` 编译产物 | 需 Chromium | `e2e` + `ui` |
| **L3 NPU** | 需要真实 NPU / `qai_appbuilder` / context bin | Snapdragon X 真机 | `e2e` + `npu` |
| **L4 Install** | 跑 `Setup.bat`/`Build.bat`/`Start.bat`(Linux 为 `setup.sh`/`start.sh`) | 慢、依赖网络,建议夜间 | `e2e` + `install` |

每个用例还要标注**适用平台**:`win-arm64`(首批)、`win-x64`、`linux-arm64`,见 §2.3 和 §5.1。

依赖标记:`NET` 需联网;`NPU` 需真实 NPU;`QAIRT` 需 QAIRT SDK + x64 转换 venv;
`LLM` 需可用的模型服务/云端模型(账号、额度);`AUTH` 受 Okta 登录门影响。

> **marker 注册**:`pyproject.toml` 里目前只有 `e2e`、`subprocess`、`qairt`、`network`、`slow`,
> **没有** `ui`、`install`、`npu`。项目开了 `--strict-markers`,直接使用未注册的 marker 会报错。
> (`tests/system/model_builder/conftest.py` 里的 `npu` 是该目录自己注册的,不是全局的。)
> 落地时需要在 `pyproject.toml` 注册新增 marker。

优先级:**P0** 核心主流程,必须有;**P1** 重要;**P2** 有则更好。

## 2. 前提

- 后端用 `python -m apps.api --port N` 启动(`N` 可为任意端口),配置可用 `QAI_` 前缀环境变量覆盖。
- **登录门**:已由负责人确认"登录问题已解决",E2E 不再把它当风险。
  具体怎么在测试里处理(关闭,或使用已有会话)在设计用例时按实际方案定。
  登录相关的现状见 §2.1,仅作背景。
- **端口**:E2E 约定**测试时本机没有其它 AppBuilder 在运行**,直接用 **8989**。
  端口被占用就**直接报错退出**,不回退到备用端口。实现要点见 §2.2。
- 后端直接托管 `frontend/dist`(SPA catch-all);dist 缺失时返回 503 维护页。
  因此 UI 测试打的是**编译后的产物**,不需要 vite dev server。
- 数据目录:可用 `QAI_DATA__DATA_DIR` 隔离数据文件;keyring 不隔离(见 §2.1.4)。

### 2.1 登录/令牌现状(背景,只读代码得出)

代码里有**三套互不相同的令牌**,不要混淆:

| 令牌 | 作用 | 有效期 | 谁产生 |
|---|---|---|---|
| Okta `id_token` | 只在 `/callback` 验签一次,验完即丢弃 | Okta 决定 | Okta SSO |
| 本地 session cookie(`qai_session`,HMAC 签名) | 网页门禁的唯一依据 | `session_ttl_seconds` 默认 **8 小时**(每次请求会顺延) | 后端,在 SSO 成功或 device-flow 引导后签发 |
| QAI Service JWT | 调用 QAI Service(LLM 中转)的 bearer | 约 **8 小时**(`expires_in`,默认 28800 秒,并提前几十秒作废) | QAI Service,用 `id_token` 换取 |

结论(针对"JWT 7 天免登录"的优化设想):
- **目前 JWT 不能让用户跳过 SSO**。QAI Service JWT 只能在 `/callback` 里,凭刚验证过的 `id_token`
  换取;代码里没有"JWT 仍有效就跳过登录"的路径。网页门禁认的是 session cookie,不是这个 JWT。
- JWT 会写入系统 keyring,后端**重启后仍可读回**(到期则丢弃)。这只让重启后"不丢令牌",
  不等于免登录。
- 现有唯一不经浏览器的登录路径是 **device-flow**(RFC 8628):`qai auth device-login` 把
  Okta `refresh_token` 存进 SecretStore,之后无 cookie 的请求会用它静默换出 session。
  该开关 `device_flow_enabled` **默认关闭**。
- 因此"7 天内免登录"需要新增逻辑(例如把有效期改成 7 天,并允许凭它直接签发 session),
  这会改动认证行为。**这是设想中的后续优化,当前代码里没有。**
  实现后应补充用例:有效期内免登录、过期后回到登录、登出后令牌清除。

### 2.1.1 服务端(qai-service)怎么换取和校验 JWT

来源:`C:\work\code\qai-service`(`routers/auth.py`、`services/auth_service.py`、`api_key_auth.py`、`ceflow_verify.py`、`device_auth.py`),只读代码得出,未连真实服务实测。

**换取 `POST /api/auth/exchange { id_token }`**,按顺序:
1. 用 Okta JWKS 验 `id_token`(RS256,校验 iss/aud/exp);`iss` 只能路由到已配置的 provider,否则 401。
2. 邮箱域名白名单(`ALLOWED_EMAIL_DOMAINS`),不通过 403;没有 email claim 400。
3. 可选的"已注册邮箱"白名单(`registered_users`),未注册 403。
4. 创建或更新用户,默认角色 `user`(groups 里有 admin 才是 admin),并确保有额度行。
5. 用服务端的 `JWT_SECRET` 自签 **HS256** JWT,载荷 `sub/email/role/app_cid/exp`。
   `exp = now + JWT_EXPIRE_HOURS`,环境变量,默认 **8 小时**。响应 `expires_in = JWT_EXPIRE_HOURS*3600`。

**校验(每次 `/v1` 请求)**:
- 只验 HS256 签名和 `exp`,**不再访问 Okta**。JWT 是自包含的。
- Bearer JWT 的角色以 JWT 里的 `role` 为准;但每次都会重新检查注册白名单,并把 `user_id` 按邮箱重新解析成本实例的身份。
  停用用户或从白名单删除会让已发出的 JWT 立刻失效(401/403)。
- **没有 refresh 接口。** 过期后(401)只能重新拿 Okta `id_token` 再换一次,即重新 SSO。

**另外两种凭据,直接当 api_key 用,不用换取、不过期**(服务端默认都关):

| 路径 | 形态 | 开关 | 角色 |
|---|---|---|---|
| B:CEFlow App API Key | HS256 JWT,`kid`=我们的 clientId,无 `exp` | `CEFLOW_AUTH_ENABLED` 等 4 项齐全 | 封顶 `user` |
| C:设备口令 | `<device_id>:<passphrase>` | `DEVICE_AUTH_ENABLED` + `DEVICE_SHARED_SECRET` | 固定 `user` |

服务端文档称开发服 `fsm-sh01-lnx` 上 C 是开的、B 是关的(该文档写作时的状态,现在是否仍如此未核对)。

**对客户端和 "7 天免登录" 的含义**:
- 服务端要把 JWT 做成 7 天,**只需把 `JWT_EXPIRE_HOURS` 设为 168**,客户端按 `expires_in` 计时,会自动跟随。
- 但客户端网页门禁认的是 session cookie(8 小时),不是这个 JWT;要免 SSO 仍需客户端新增逻辑(见 §2.1)。
- 客户端取 JWT 只有一个入口:从 SecretStore(`qai.service.session` / `jwt`)读 `{"jwt","deadline"}`。
  客户端**没有**为 B、C 路径写任何专门代码;把 B/C 凭据写进这个位置是否能用,**未验证**。

### 2.1.2 E2E 登录方案(已选:复用真实 SSO 会话)

做法:人工 SSO 登录一次,之后的 E2E 复用这份登录态。

1. **登录一次**:用有头 Playwright(或 `launch_persistent_context` 指定固定用户目录)走一遍 SSO,
   保存 `storage_state`。这一步后端同时把 JWT 写进 keyring。
2. **固定 cookie 签名密钥**:E2E 后端要与登录时用同一把 `auth_session_secret`。
   办法是同一个数据目录,或显式设 `QAI_AUTH__SESSION_SECRET`。**该变量名已实测生效**
   (`Settings()` 加载后 `auth.session_secret` 取到了环境变量的值),否则 cookie 会被判无效。
3. **JWT**:E2E 起的新后端进程从 keyring 读回。**读不到就判测试失败**(负责人已定),不要 skip、不要静默降级。
   实测(本机 ARM64 Windows,`WinVaultKeyring`):用全新进程 + 全新临时数据目录构造 `SecretStore`,
   `get_service_jwt()` 读回了真实 JWT(长度 277),即**当前这台机器上能读到**。偶发的读取失败(代码注释提到 ARM64 上有过)没有复现。
   注意"读到"是指"存在且未过期":JWT 已过期(8 小时)时同样读不到,所以 E2E 前先做一次"JWT 可读"的前置检查,
   失败时报明确原因(无记录 / 已过期 / keyring 异常),而不是笼统的"测试失败"。
4. **时效陷阱**:session cookie 是滑动续期,JWT 是固定 8 小时。登录 8 小时后,界面看起来仍是已登录,但调用 QAI Service 会 401。
   所以涉及 QAI Service 的用例前,先检查 JWT 是否还有足够剩余时间,不够就 **skip 并写明原因**,不要当成失败。
5. **只有少数用例真用 JWT**(聊天走 QAI Service 模型、额度显示)。它们消耗真实账号额度,默认不跑,需显式开启。
6. **不要提交登录态**:`storage_state` 文件、用户目录、密钥文件都要加进 `.gitignore`。
7. **登出类用例**会清掉 cookie 和 JWT,会破坏上面这份登录态。这类用例不要和其它用例共用同一份登录态。

自动化的边界:这个方案**依赖人工先登录**,所以不适合无人值守的 CI。需要无人值守时,另一条路是在本地起一个测试用的
qai-service,自己设 `JWT_SECRET` 并自签 JWT(服务端只验签名和 `exp`),这样不消耗真实额度。这条路这次没有选,先记在这里。

### 2.1.3 配置环境变量(已实测)

用 `Settings()` 加载验证过,以下变量都能覆盖对应字段(`env_prefix=QAI_`,嵌套分隔符 `__`):

| 变量 | 对应字段 | 实测结果 |
|---|---|---|
| `QAI_AUTH__SESSION_SECRET` | `auth.session_secret` | 生效 |
| `QAI_AUTH__ENABLED` | `auth.enabled` | 生效(`false` 可关登录门) |
| `QAI_DATA__DATA_DIR` | `data.data_dir` | 生效 |

注意:只验证了配置对象能读到值,**没有**验证整个后端用这个数据目录能否正常启动并隔离所有数据
(密钥文件、数据库、keyring 条目是否都落在该目录下,仍在 §6 第 2 项)。

### 2.1.4 数据目录隔离(已实测)

用 `QAI_DATA__DATA_DIR=C:\Temp\qai-e2e-data`、端口 18989 真起了一次后端,并对比启动前后的文件快照
(`%LOCALAPPDATA%\QAIModelBuilder`、`%TEMP%\QAIModelBuilder`、`~\.qai`、仓库 `data\`、仓库 `.qai\`,共约 3.8 万个文件):

- **后端能用临时数据目录正常启动**:`/api/system/health` 返回 200,2 秒内就绪;开启登录门后未登录请求返回 401。
- **文件隔离成立**:被监控的上述位置**0 个新增、0 个修改**。启动时写入临时目录的只有
  `db\qai.db`、`runtime\server.endpoint.json`、`secrets\_master.key`,数据库迁移也在这个目录里完成。
- **keyring 不隔离**:Windows 凭据管理器按 `service/key` 存取,不带数据目录。用全新临时数据目录构造的 `SecretStore`
  仍然读到了你日常的真实 JWT(`qai.service.session/jwt`)。含义:
  - E2E 能复用你的真实登录态(这正是已选方案要的),不需要重新登录;
  - 但 E2E 里**写入或删除** keyring 的用例(登出、改密钥、改 API Key)会直接改到你的真实凭据,需要避免;
  - 文件密钥(`_master.key`)和 keyring 条目的分裂:临时目录里新生成的主密钥解不开旧目录里用旧主密钥加密的文件密钥,
    只有存 keyring 的条目才跨目录可见。
- 没覆盖到的位置:`%LOCALAPPDATA%\QAIModelBuilder\envs`(虚拟环境,只读使用)、`%APPDATA%`、`~\.config`、浏览器缓存、
  以及 AI Coding 相关的 `edit_trash` 等。这些路径在代码里是写死的(`LOCALAPPDATA` / `Path.home()`),本次启动没触发,
  但跑到相应功能时可能写到数据目录之外。

结论:**数据文件可以隔离**,keyring 不行。对"不能隔离也没关系"的前提,这个程度已足够,但要把 keyring 写操作列入禁区。

启动后端时必须自己设 `PYTHONPATH=<repo>\src;<repo>`:这个 venv 里没有安装 `qai` 包,直接 `python -m apps.api` 会
`ModuleNotFoundError: No module named 'qai'`。`Start.bat` 的启动路径不同,E2E 用直连入口时要补上这一项。

### 2.2 端口与启动入口

| 入口 | 可指定端口 | 端口被占用时 |
|---|---|---|
| `python -m apps.api --port N` | 任意端口 | **已实测**:8989 被占用时进程自行退出,**退出码 1**,日志含 `[Errno 10048] error while attempting to bind on address ('127.0.0.1', 8989)`;用户实例不受影响。但退出前已完成大半启动流程(连库、跑迁移、装钩子),不是启动最早期就失败 |
| `python -m apps.cli.serve --port N`(supervisor) | 任意端口 | 显式 `--port` 时只试这一个端口,**不回退**;被占用或落入 Windows 保留范围就抛 `RuntimeError`,提示后以退出码 **1** 结束 |
| `Start.bat --port N` | **仅 8989 或 28688**,其它值直接报错 | 同 supervisor:显式端口不回退 |
| `Start.bat`(不带 `--port`) | - | 从 `ports.json` 的 `fallbacks` `[8989, 28688]` 依次试,**会回退** |

对 E2E 的含义:
- 用 `--port 8989` 启动即可满足"被占用就报错退出"。supervisor 路径(`apps.cli.serve` 或 `Start.bat --port 8989`)
  行为已从代码确认;`python -m apps.api` 直连路径已实测(见上表),两条路径都是被占用即退出码 1。
- 直连路径是在绑定端口**之前**就连库、跑迁移,所以被占用时已经对数据目录做过写入。想避免,用例应先检查 8989 是否空闲(见下)。
- 用例前置检查建议:先确认 8989 空闲,否则直接 fail 并给出占用进程信息,避免误杀用户实例。
- 本机当前确实有实例占着 8989(调研时看到 `python -m apps.api --port 8989`),
  跑 E2E 前必须先停掉。

### 2.3 测试平台

E2E 需要区分三个平台,每个用例都要标明适用平台。**首批只做 Windows ARM64。**

| 平台 | 代号 | 运行时 venv | 登录门默认 | 推理后端 | 转换器环境 | 备注 |
|---|---|---|---|---|---|---|
| Windows ARM64 | `win-arm64` | `.venv_arm64_313`(Python 3.13) | 开 | NPU(HTP),`onnxruntime-qnn` | x64 Python 3.10 的 `.venv_x64_310`(模拟运行) | **首批目标**;本机即此平台(X Elite) |
| Windows x64 | `win-x64` | x64 Python 3.13 | 开 | 无本机 NPU,CPU:标准 `onnxruntime`、x64 `qai-appbuilder` | 同一环境 | 无 NPU,L3 用例不适用 |
| Linux ARM64 | `linux-arm64` | `envs/venv`(Python 3.12),由 `setup.sh` 创建 | 有桌面会话(`DISPLAY`/`WAYLAND_DISPLAY`)才开,无头服务器关 | 需 FastRPC(`libcdsprpc.so`) | 同一 venv(无单独的 x64 转换 venv) | 入口是 `setup.sh`/`start.sh`,不是 `.bat` |

要点:
- L4 安装类用例的入口按平台不同:Windows 用 `Setup.bat`/`Start.bat`,Linux 用 `setup.sh`/`start.sh`。
- `INST-02`(x64 3.10 转换 venv)只适用 Windows ARM64;x64 与 Linux 没有这一步。
- L3 NPU 用例只适用 `win-arm64`(以及有 FastRPC 的 `linux-arm64`,是否可行**未验证**)。
- 目前测试里没有任何平台 marker(`pyproject.toml` 里有 `windows_only`、`linux_only`,没有 ARM64/x64 区分)。
  落地时需要一个平台 marker 或选择器,例如按 `platform.system()` 与 `platform.machine()` 判断。
- 以上平台差异来自读代码,`win-x64` 与 `linux-arm64` 我没有实机核对。

## 3. 功能清单

### 3.1 系统与启动(后端 `/api/system/*`)

| ID | 功能 | 层 | 依赖 | 优先级 |
|---|---|---|---|---|
| SYS-01 | 后端启动成功,`/api/system/health` 健康 | L1 | - | P0 |
| SYS-02 | `build-info` / `edition` / `platform` / `python-info` 返回合理 | L1 | - | P1 |
| SYS-03 | `features`、`config/summary` 反映实际启用的功能 | L1 | - | P2 |
| SYS-04 | `qairt-versions` 列出已装 SDK;QAIRT 热切换(`/api/system` qairt-switch) | L1 | QAIRT | P2 |
| SYS-05 | 启动写 `data/runtime/server.endpoint.json`,内容与实际端口一致 | L1 | - | P1 |
| SYS-06 | SPA:`/` 返回 index.html,未知路径回落 SPA,`/assets/*` 正确 MIME | L1 | dist | P0 |
| SYS-07 | `dist` 缺失时返回 503 维护页(不崩溃) | L1 | - | P2 |
| SYS-08 | 退出码 75 触发 supervisor 重启 | L1 | - | P2 |
| SYS-09 | Start.bat 在端口被占用时回退到备用端口(8989 → 28688) | L4 | - | P2 |

### 3.2 登录与访问控制

| ID | 功能 | 层 | 依赖 | 优先级 |
|---|---|---|---|---|
| AUTH-01 | 启用登录门时,未登录:浏览器请求 303 到 `/auth/login`,API 请求 401 | L1 | AUTH | P1 |
| AUTH-02 | 公共路径(`/api/system/health`、build-info、edition、`/auth/*`、`/callback`、OpenAPI 文档、SPA 静态)免登录 | L1 | AUTH | P1 |
| AUTH-03 | 登录门关闭或已有有效会话时,受保护接口可访问(登录门的处理方式见 §2) | L1 | - | P0 |
| AUTH-04 | 真实 Okta 登录流程 | - | AUTH, NET | **不自动化**(需真实账号/SSO) |
| AUTH-05 | **(后续优化,当前代码没有)** JWT 7 天有效期内免登录;过期回到登录;登出清除令牌 | L1 | AUTH | 待优化落地后再定(见 §2.1) |
| SEC-01 | CSRF:缺少 cookie/头的写请求被拒,携带后通过 | L1 | - | P1 |

### 3.3 聊天(`/chat`,`/api/chat/*`,`/v1/*`)

| ID | 功能 | 层 | 依赖 | 优先级 |
|---|---|---|---|---|
| CHAT-01 | 创建/列出/重命名/删除会话(REST) | L1 | - | P0 |
| CHAT-02 | 发消息并收到流式回复(SSE / WS) | L1 | LLM | P0 |
| CHAT-03 | 多标签会话(`/chat/:tabId`)切换与深链 | L2 | - | P1 |
| CHAT-04 | 会话搜索(conversations search) | L1 | - | P1 |
| CHAT-05 | 中断生成(interrupt)、权限审批弹窗 | L1/L2 | LLM | P1 |
| CHAT-06 | OpenAI 兼容接口 `/v1/*`(chat completions、models) | L1 | LLM | P1 |
| CHAT-07 | 附件/图片上传(`/api/uploads`)并在消息中引用 | L1 | - | P1 |
| CHAT-08 | MCP 配置(`/api/chat/mcp`)增删与列出 | L1 | - | P2 |
| CHAT-09 | 聊天模式切换(chat mode)、工具模式(composer 工具栏) | L2 | - | P1 |
| CHAT-10 | 无 LLM 可用时给出明确提示而不是挂起 | L1/L2 | - | P1 |

> 说明:聊天核心功能依赖模型服务。无账号/额度的环境下,CHAT-02/05/06 应使用**可控的替身模型服务**,
> 或标记为需要凭据的 opt-in 用例。**替身怎么接入尚未设计。**

### 3.4 App Builder(模型运行/应用生成)

内置 Pack:`whisper-base`(ASR)、`zipformer-zh`(ASR)、`melotts-zh`(TTS)、`real-esrgan-x4plus`(超分)。

| ID | 功能 | 层 | 依赖 | 优先级 |
|---|---|---|---|---|
| AB-01 | 列出模型 `/models`,详情 `/models/{id}`,Pack manifest 合法 | L1 | - | P0 |
| AB-02 | 依赖状态 `deps-status`、`deps-status/packs` | L1 | - | P1 |
| AB-03 | 权重自动下载:缺权重时首次使用触发下载,失败返回 `WEIGHTS_NOT_INSTALLED` 而不崩溃 | L1 | NET | P1 |
| AB-04 | 运行记录:创建 run、`/runs/{id}`、SSE `stream`、取消、历史 | L1 | NPU | P0 |
| AB-05 | real-esrgan 推理(图片 → 超分图片,产物可下载) | L3 | NPU | P0 |
| AB-06 | whisper / zipformer 语音识别(音频 → 文本) | L3 | NPU, 权重 | P0 |
| AB-07 | melotts-zh 文本转语音(文本 → 音频) | L3 | NPU, 权重 | P1 |
| AB-08 | 批量运行 `/batch` | L3 | NPU | P2 |
| AB-09 | 语音偏好 `voice-preference` 读写;启动预热(默认 Zipformer) | L1 | - | P1 |
| AB-10 | 驻留 worker 状态 `worker/status`、缓存 `cache/status`、清缓存 | L1 | - | P1 |
| AB-11 | 应用(App)生成/运行/日志/打包:`/apps/*`、package 进度 | L1 | - | P1 |
| AB-12 | 分享链接 `/share/{token}` | L1 | - | P2 |
| AB-13 | 导入:`import/dry-run`、scan-bins、rollback | L1 | - | P1 |
| AB-14 | 无 NPU 的机器上优雅降级(不崩溃,明确报错) | L1 | - | P1 |

### 3.5 Model Builder(模型转换)

| ID | 功能 | 层 | 依赖 | 优先级 |
|---|---|---|---|---|
| MB-01 | 工作区初始化、plan/REPORT 解析、精度归一化、任务分类 | 已有 system test | - | 已覆盖 |
| MB-02 | `POST /api/app-builder/import/auto-export` 及 `/status` | 已有 system test(最小 app) | - | 已覆盖 |
| MB-03 | auto-export 在**完整后端**上的行为(含 DB、DI 装配) | L1 | - | P1 |
| MB-04 | ONNX → QNN 转换 → context binary 全流程 | L3 | QAIRT, NPU | P1(夜间) |
| MB-05 | 量化(INT8/W8A16)、敏感度分析 | L3 | QAIRT | P2 |
| MB-06 | CLI:`qai pack export/validate`、`qai build` | L1 | - | P1 |
| MB-07 | 转换环境就绪检查:`qairt_env.json` 与 SDK、x64 venv 一致 | L1 | QAIRT | P1 |

### 3.6 Model Hub / 模型目录 / 下载

| ID | 功能 | 层 | 依赖 | 优先级 |
|---|---|---|---|---|
| MH-01 | 模型目录 `entries` 列表/详情 | L1 | - | P1 |
| MH-02 | 下载任务:创建、`progress`、取消、校验(`verify`) | L1 | NET | P1 |
| MH-03 | 云端模型/Provider 配置读写(`providers`、`cloud-models`) | L1 | - | P1 |
| MH-04 | 下载页 `/downloads` 展示任务与进度 | L2 | 需先补 testid | P1 |
| MH-05 | aria2c 管理(`/api/aria2c`) | L1 | - | P2 |

### 3.7 本地模型服务(Service,`/api/service/*`、`/api/versions/*`)

| ID | 功能 | 层 | 依赖 | 优先级 |
|---|---|---|---|---|
| SVC-01 | 服务状态/启动/停止/探测(`status`、`start`、`stop`、`probe`) | L3 | NPU, 服务二进制 | P1 |
| SVC-02 | 服务日志(SSE)、清日志 | L1 | - | P1 |
| SVC-03 | 服务配置读写 | L1 | - | P1 |
| SVC-04 | 版本管理:列出/下载/安装/删除(`/api/versions`) | L1 | NET | P2 |
| SVC-05 | 服务页 `/service` | L2 | 需先补 testid | P1 |

### 3.8 安全与权限(`/api/security/*`,`/security`)

| ID | 功能 | 层 | 依赖 | 优先级 |
|---|---|---|---|---|
| SECU-01 | 路径白名单/授权(paths、grants)增删 | L1 | - | P0 |
| SECU-02 | 危险命令策略(dangerous commands)、自动审批(auto-approve) | L1 | - | P0 |
| SECU-03 | 权限请求/审批流(permission) | L1/L2 | - | P0 |
| SECU-04 | 运行时配置、Skills 安全设置 | L1 | - | P1 |
| SECU-05 | 安全页 `/security` | L2 | - | P1 |

### 3.9 通道、定时任务、技能、AI Coding、其他

| ID | 功能 | 层 | 依赖 | 优先级 |
|---|---|---|---|---|
| CH-01 | 通道(微信/飞书等)配置读写;页面 `/channels` | L1/L2 | - | P2 |
| CH-02 | 通道真实收发 | - | 外部账号 | **不自动化** |
| TASK-01 | 定时任务 CRUD、`catalog`、执行记录 | L1 | - | P1 |
| SKILL-01 | 技能列表/启用/禁用;页面 `/skills` | L1/L2 | UI 需先补 testid | P1 |
| SKILL-02 | 内置技能可加载(browser-automation、weather、read-arxiv-paper 等) | L1 | 部分 NET | P2 |
| CODE-01 | AI Coding(Claude Code / OpenCode)面板:配置、会话、权限弹窗 | L2 | 外部 CLI | P2 |
| SET-01 | 设置页:读写偏好 `user_prefs`、语言切换(中/英) | L1/L2 | - | P0 |
| SET-02 | 全局代理配置,下载走代理 | L1 | - | P1 |
| SET-03 | TLS 校验开关热生效 | L1 | - | P2 |
| SRCH-01 | 搜索引擎配置 `/api/search/engines`;浏览器引擎 | L1 | NET | P2 |
| REM-01 | 远程部署(`/api/remote-deploy`) | L1 | 开发板 | P2(多半不做) |
| GALL-01 | Gallery 提交 | L1 | NET | P2 |
| EVT-01 | 事件流 `/events` | L1 | - | P1 |
| GO-01 | GoMaster / MB-Pro 会话 | - | 内部服务 | **内部,不纳入** |

### 3.10 安装、构建、启动脚本

| ID | 功能 | 层 | 依赖 | 优先级 |
|---|---|---|---|---|
| INST-01 | `Setup.bat` 默认安装:创建 venv,装依赖,初始化 `data/` | L4 | NET | P1(夜间) |
| INST-02 | `Setup.bat --builder` 创建 x64 3.10 venv(回归:ARM64 上不应找 aarch64) | L4 | NET | P1 |
| INST-03 | `--builder` 步骤失败时中止并给出 `[ERROR]` | L4 | - | P1 |
| INST-04 | `Setup.bat --help`、未知参数的处理 | L4 | - | P2 |
| INST-05 | `Build.bat` 产出 `frontend/dist` | L4 | Node | P1 |
| INST-06 | `Start.bat` 启动并写 endpoint 文件;`--port` 仅接受 8989/28688 | L4 | - | P1 |
| INST-07 | `Uninstall.bat` 不删 `data/` | L4 | - | P2 |
| INST-08 | `qai.bat --help` / `qai config provider list` | L1 | - | P1 |

## 4. UI(L2)的范围与维护成本

编译产物可直接测:后端托管 `frontend/dist`,Playwright 打的就是编译后的页面。
但需要保证 dist 与源码同步(先 `Build.bat`)。

**侧边栏共 7 个视图**:chat、channels、skills、downloads、service、security、settings。
(App Builder / Model Builder / 代码 / 翻译 / PPT 不是独立页面,在聊天输入框的工具栏里。)

**`data-testid` 现状**(静态扫描,未在运行页面上验证):

| 区域 | 行数 |
|---|---|
| components/chat | 647 |
| components/security | 68 |
| components/channels | 66 |
| components/ai-coding | 55 |
| components/layout | 47 |
| components/app-builder | 36 |
| views/* | 27(Chat 5、ClaudeCode 10、OpenCode 8、Channels 4) |
| components/downloads、service、common、pipeline | **0** |
| **合计** | **947 行 / 105 个文件**(其中 182 行是动态 id) |

观察:
- 聊天、安全、通道、布局、AI Coding 已有较多标识,适合写 UI 用例。
- `DownloadsView`、`ServiceView`、`SecurityView`、`SettingsView`、`SkillsView` 这 5 个视图文件本身都是 0 个 testid。
  其中 Security 的子组件有 68 个;Settings 复用 `components/chat` 下的面板(如 `AgentSettingsPanel` 10 个、
  `SearchSettingsPanel` 6 个,但 `ChatHooksSettings` 为 0);**Downloads(10 个组件)和 Service(6 个组件)整体为 0**。
  要测 Downloads / Service / Skills,需要**先在前端补 `data-testid`**(会改前端代码)。
- 动态 id(如 `coding-panel-${kind}`)的拼接规则要写进页面对象,变更时需同步。
- 以上都是静态文本扫描:不代表这些元素在页面上真的可见、可点,也没确认 id 是否唯一。

**WebUI 改动的影响**:按 `data-testid` 定位的用例对样式和文案改动不敏感,但改 testid 命名、
删除/合并组件、改交互步骤仍需同步修改用例。建议:
1. UI 层只保留 P0/P1 的主流程(冒烟级),业务细节放 L1。
2. 用页面对象封装定位和步骤,界面变动只改封装层。
3. 约定:改动带 `data-testid` 的元素时,PR 需说明或更新 E2E。

## 5. 建议的首批范围(待评审)

**平台:仅 Windows ARM64(`win-arm64`)。** `win-x64`、`linux-arm64` 后续再扩展,
清单里的用例先按平台标注,实现时再决定是否复用。下面的"平台"列见 §5.1。

- **L1 先行**:SYS-01/05/06、AUTH-03、CHAT-01、AB-01/09/10/14、SECU-01/02、SET-01、TASK-01、MB-03/06。
  这批不需要 NPU、不需要联网,价值/成本比最高。
- **L2 小而精**:页面加载与 7 个视图可进入、聊天发消息(用替身模型)、安全页授权、设置页语言切换。
- **L3**:real-esrgan 一条推理(AB-05)、zipformer 一条(AB-06),在本机 X Elite 上 opt-in。
- **L4**:先做 INST-02/03(正好覆盖本次修复的回归),其余夜间。

### 5.1 各类用例的平台适用性(初判,未在 win-x64 / linux-arm64 上核对)

| 类别 | win-arm64 | win-x64 | linux-arm64 |
|---|---|---|---|
| L1 API(不依赖 NPU/联网) | ✅ 首批 | 预计可复用 | 预计可复用 |
| L2 UI(Playwright) | ✅ 首批 | 预计可复用 | 需桌面或 headless 浏览器,未核对 |
| L3 NPU(AB-05/06/07、SVC-01) | ✅ 首批 | ❌ 无 NPU | 需 FastRPC,未核对 |
| L4 `Setup.bat`/`Start.bat` | ✅ 首批 | 入口相同,INST-02 不适用 | 入口是 `setup.sh`/`start.sh`,需另写 |
| INST-02/03(x64 3.10 转换 venv) | ✅ | ❌ 不适用 | ❌ 不适用 |
| AUTH 登录门默认状态 | 开 | 开 | 随 `DISPLAY`/`WAYLAND_DISPLAY` |

## 6. 开工前必须验证的不确定项

已解决/已定:
- ~~关登录门~~:登录问题已解决,不再作为风险。
- ~~端口~~:E2E 固定用 8989,被占用就报错退出(见 §2.2)。直连入口的占用报错已实测:退出码 1。
- ~~数据隔离~~:数据文件可隔离,keyring 不隔离(见 §2.1.4);"不能隔离也没关系"的前提下已满足。
- ~~JWT 读不到~~:按负责人决定,读不到即判测试失败(见 §2.1.2 第 3 点)。

仍待验证:
1. **keyring 写操作的防护**:E2E 中哪些用例会写或删 keyring(登出、API Key、凭据管理),如何避免动到真实凭据。
2. **数据目录之外的写入**:AI Coding 的 `edit_trash`、浏览器缓存、`~\.config` 等写死路径(见 §2.1.4),
   跑到相应功能时是否会写出数据目录,需在对应用例上实测。
3. **启动预热**:启动时的语音预热会不会触发权重下载;测试中如何关闭。
4. **替身 LLM**:聊天流式回复在没有账号时如何稳定测试。
5. **dist 与源码同步**:UI 用例怎么确保测的是最新编译产物。
6. **CI**:`ci.yml` 对 `tools/**` 是 `paths-ignore`;E2E 要自动跑需另加 workflow,
   且 GitHub 托管机器无 NPU,L3 只能在自托管 Snapdragon 机器上跑。
7. **平台 marker**:怎么在 pytest 里表达"适用平台"(见 §2.3)。
8. **JWT 7 天免登录**:不在当前项目里(负责人确认之前说的 7 天 JWT 不在这个仓库;我已查 `origin/main`
   及全部远端分支,认证相关文件无差异)。**E2E 按当前代码的 8 小时 session / 8 小时 JWT 设计。**
   等该优化真正落地再补用例(见 §2.1)。

## 7. 明确不自动化的内容

真实 Okta 登录、微信/飞书真实收发、内部 GoMaster/MB-Pro 服务、远程开发板部署、
需要付费账号的云端模型调用。
