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
  实现后应补充用例:有效期内免登录、过期后回到登录。(登出相关用例按负责人决定不测,见 §2.1.2 第 7 点。)

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
7. **不测试登出**(负责人已定):登出会调 `clear_service_jwt()`,删掉内存和 keyring 里的 JWT,之后连你日常使用的
   AppBuilder 也读不到它，得重新 SSO。**E2E 一律不写登出用例,也不调 `/auth/logout`。** 这样真实 JWT 就不会被删。

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
  - 不过 E2E 里**写入或删除** keyring 的操作会直接改到真实凭据。负责人已定的策略:
    - **JWT 读真实的**,E2E 用例**不测登出**(登出是唯一会删真实 JWT 的操作);
    - 其他会写 keyring 的功能(模型提供商/搜索引擎 API Key、MCP token、AI Coding 凭据、代理密码、
      QAI Service 配置)**直接改真实环境也可以,不做隔离、不做备份还原**。
      通道凭据不在此列:**通道不做自动化**(见 §3.9 CH-01)。
    - **自建自删**(负责人已定):写入类用例只创建自己的数据,并在结束时删掉。约定:
      1. 名字带 `e2e-` 前缀加随机串(如 `e2e-mcp-3f9a1c`),不与真实配置重名;
      2. 不覆盖已有条目——覆盖后再删等于把原配置也删了,不是还原;
      3. 删除放在 `finally`/fixture 拆除里,用例中途失败也要清理;
      4. 删除后断言:列表里没有它,keyring 里对应条目也没有;
      5. 没有删除接口的功能不写写入用例(已知:通道、QAI Service 配置、设备登录 refresh_token)。
    代价要说清楚:这类用例跑完后,你机器上对应的真实配置会被改成测试值,写用例时要在用例说明里标注它会改什么。
  - 文件密钥(`_master.key`)和 keyring 条目的分裂:临时目录里新生成的主密钥解不开旧目录里用旧主密钥加密的文件密钥,
    只有存 keyring 的条目才跨目录可见。
- 没覆盖到的位置:`%LOCALAPPDATA%\QAIModelBuilder\envs`(虚拟环境,只读使用)、`%APPDATA%`、`~\.config`、浏览器缓存、
  以及 AI Coding 相关的 `edit_trash` 等。这些路径在代码里是写死的(`LOCALAPPDATA` / `Path.home()`),本次启动没触发,
  但跑到相应功能时可能写到数据目录之外。

结论:**数据文件可以隔离**,keyring 不行。对"不能隔离也没关系"的前提,这个程度已足够。
keyring 的处理见上面:**只禁登出**,其它写操作直接改真实环境。

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
- L3 NPU 用例只适用 `win-arm64`(以及有 FastRPC 的 `linux-arm64`,是否可行**未验证**)。
- 目前测试里没有任何平台 marker(`pyproject.toml` 里有 `windows_only`、`linux_only`,没有 ARM64/x64 区分)。
  落地时需要一个平台 marker 或选择器,例如按 `platform.system()` 与 `platform.machine()` 判断。
- 以上平台差异来自读代码,`win-x64` 与 `linux-arm64` 我没有实机核对。

## 3. 功能清单

> **如何运行 E2E(win-arm64)**
>
> ```powershell
> # 推荐:统一启动脚本 tests\e2e\run_e2e.bat(即 run_e2e.py,只用标准库),按功能分类运行并生成报告
> .\tests\e2e\run_e2e.bat --list                 # 15 个分类、每类的文件和用例数(含 tests/system 的 mb_system)
> .\tests\e2e\run_e2e.bat                        # 全部分类,依次执行(整套约 1 小时 10 分)
> .\tests\e2e\run_e2e.bat -c chat,security       # 只跑指定分类;-x 排除分类
> .\tests\e2e\run_e2e.bat -c settings -- -k proxy # "--" 之后的参数原样交给 pytest
>
> # 也可以直接用 pytest(不分类、不出报告;此时后端不会自动用产品 venv,必须自己设 QAI_E2E_PYTHON):
> $env:QAI_E2E_PYTHON = "$env:LOCALAPPDATA\QAIModelBuilder\envs\.venv_arm64_313\Scripts\python.exe"   # 后端用
> <装有 pytest 的 python> -m pytest tests/e2e
> ```
>
> **`run_e2e` 的行为**
>
> - **解释器:测试进程和后端都跑在产品 venv 上**,这样测到的库版本与用户实际运行的一致,不会因测试环境多装或少装库而漏掉问题。产品 venv 里没有 pytest 且不应被改动,所以 pytest / pytest-asyncio 装在单独的 runner venv(`data/e2e-runner-venv`,首次运行自动创建,可用 `QAI_E2E_RUNNER_VENV` 改位置),并且只把它的 site-packages **追加**到 `sys.path` 末尾:产品 venv 已有的包永远优先,产品缺的库不会被 runner venv 的副本掩盖。实际借用了哪些包会在开跑时打印,并写入 `report.html` / `summary.json`(目前只有 pytest、pytest-asyncio、pluggy、iniconfig)。后端和测试启动的子进程拿不到 runner venv。
>   `--python` / `$QAI_E2E_TEST_PYTHON` 可指定别的测试端解释器(此时它自己要装全 pytest、pytest-asyncio、httpx、psutil、keyring、websockets);后端 = `--backend-python` > `$QAI_E2E_PYTHON` > 产品 venv(`.venv_arm64_313` / `.venv_x64_313`)。缺什么依赖会在开跑前说清楚,不会跑到一半才失败。
> - 功能分类只在 `run_e2e.py` 的 `CATEGORIES` 表里(文件名或 `tests/` 下的相对路径 → 分类),不改测试文件。**`tests/e2e` 或 `tests/system` 下有 `test_*.py` 没归类或同时归了两类,脚本直接退出**,新增测试文件要记得加进表。`tests/system/model_builder` 对应 `mb_system` 分类(不启动后端,不要求 8989/6810 空闲)。
> - 每个分类单独起一个 pytest 进程,**串行**(8989 和 6810 同一时间只能有一个后端)。一个分类超时(默认 5400 秒,杀整个进程树)或崩溃不影响其他分类。
> - **同一时间只允许一个 `run_e2e`:** 启动时对 `data/e2e-reports/.run_e2e.lock` 加操作系统级文件锁,第二个实例会立刻退出(退出码 2)并显示持有者的 pid 和开始时间。锁随进程消失(包括被强杀),不会留下陈旧锁。原因:两次运行会抢 8989/6810,互相造成大批假失败(曾因此误判过一次)。`--list` 和 `--dry-run` 不加锁。
> - **`--reverse`:** 按相反顺序跑所选分类。失败如果换了顺序就消失,是顺序/环境残留导致;换了顺序仍在同一分类,才是该分类自己的问题。
> - **残留进程的识别与清理由测试端解释器(产品 venv,有 psutil)上的小辅助进程完成**,启动器自己的 Python 不需要装 psutil。
> - 输出在 `data/e2e-reports/<时间戳>/`(已被 gitignore):`report.html`、`summary.json`、`junit/<分类>.xml`、`logs/<分类>.log`。退出码 0 = 无失败,1 = 有失败/出错/遗留进程,2 = 预检失败或参数错(此时什么都没跑)。`e2e_real_jwt` 用例默认跳过,`--real-jwt` 才会跑(消耗真实额度)。
> - **端口遗留检查:** 每个分类跑完后检查 8989、6810、18420–18423、18555,只报告"开跑时空闲、现在被占用"的端口;报告里该分类标 `+LEAK` 并写出占用者的 pid、命令行、工作目录,计入失败。默认会清理,**但只杀带 `PYTEST_CURRENT_TEST` 环境变量的进程**(证明是测试启动的)及其 reloader 父进程,别的一律不动;`--keep-leaks` 只报告不清理。
> - `test_app_builder_apps.py` 要求 18420–18423 和 18555 全部空闲,否则**整个模块跳过(48 条)**;选了 `app_builder` 时预检会提前报出占用者,不再静默跳过。
>
> - `QAI_E2E_PYTHON` 指定**后端**使用的解释器;不设就用跑 pytest 的那个,而它通常装不全后端依赖,起不来。直接用 pytest 且没设时会给出警告。
> - 8989 必须空闲,被占用时整个会话直接中止(不会去杀占用者)。
> - 测试端环境曾因 Microsoft Store 版 Python 被卸载而失效(venv 的 `pyvenv.cfg` 指向已不存在的解释器)。
>   当前使用 `%TEMP%\qai-e2e-venv`(基于 `Programs\Python\Python312-arm64`),依赖版本固定为
>   pytest 9.1.1、pytest-asyncio 1.4.0、httpx 0.28.1、psutil 7.2.2、keyring 25.7.0、websockets 16.1.1
>   (最后一个只有 EVT-01 / MH-02 的 WebSocket 用例需要;产品 venv 里是 15.0.1,没动)。
> - **完成状态(截至 2026-10-20):** `tests/e2e` 56 个模块、`tests/system/model_builder` 6 个模块(`run_e2e` 的 `mb_system` 分类),合计 **2009 条用例**(用 `run_e2e --list` 核对),分 15 个分类。下面第一条是 2026-10-18 当时的记录,保留不改;最新的整套结果见紧接着的“整套运行(2026-10-20)”。清单里原先标为待做的 SECU-03 / SECU-04 /
>   CHAT-07 / EVT-01 / INST-08 / MB-07 / MH-02,CHAT-02 / CHAT-05(L1)/ CHAT-06(部分)/ CHAT-10(L1),以及 SYS-03/04/07/08、SET-03、CHAT-08、MH-05、SKILL-01/02(L1)、AB-11..14 已实现并做过变异检查(各行里写明了数量和未覆盖项)。
>   **整套 `tests/e2e` 已在同一次会话里完整跑过一遍:1753 条中 1752 条通过,1 条超时后已修正并单独重跑通过(见 CHAT-ISS-10),耗时 57 分钟**(运行前后已检查:产品代码没有改动、仓库 `models/` 没变、`C:\WoS_AI` 文件数不变)。
>   整套回归之后只改了一处:`test_chat_mcp.py` 里该超时的那条(改用 120 秒客户端超时,且只断言结果不断言耗时),已单独重跑整个模块 55 条通过,整套在之后的 2026-10-18 运行中已重跑(见下面"整套运行"一条)。变异检查里补的两条用例(`test_skills.py`、`test_app_builder_apps.py`)在回归启动之前已写好,已包含在那次 1753 条里。
> - **整套运行(2026-10-20,`run_e2e` 串行 15 个分类,含 `tests/system`,1 小时 10 分):2009 条中 2008 通过、0 失败、0 错误、1 跳过,没有遗留进程。**
>   - 唯一跳过的是 `mb_system` 的 `test_real_bin_records_live_io_contract`(没设置 `MB_REAL_WORKDIR`,需要真实 NPU 工作目录)。
>   - 全程在产品 venv 上运行:pytest 进程和后端都用 `.venv_arm64_313`,pytest / pytest-asyncio 等 4 个包借自单独的 runner venv(`data/e2e-runner-venv`),只追加到 `sys.path` 末尾。
>   - 日志里没有任何连接重置 / 中断,失败用例的后端日志目录也没有生成。此前偶发的连接错误已定位为产品缺陷并修复,见 KNOWN-ISSUES.md 的 TEST-ISS-02。
>   - 报告:`data/e2e-reports/20261020-135045/report.html`。
> - **此前的几次完整运行(2026-10-19/20)里出现过的失败,结论如下,避免再被当成产品问题:**
>   - 10-19 的一次完整运行因**另一次完整运行同时启动**(重叠约 28 分钟,抢 8989/6810)而出现大批失败(`system` 被中止,`chat`、`app_builder`、`model_hub` 多条失败);把受影响的 4 个分类单独重跑后只剩 1 条偶发失败(即上面的连接重置)。现在 `run_e2e` 有**单实例锁**,第二个实例会被拒绝。
>   - 10-20 的逆序运行(`--reverse`)2009 条中 1 条失败:`test_sys_reboot.py` 的 `test_without_a_supervisor_reboot_ends_the_process_with_code_75`,子进程以 `0xC0000005` 退出,崩在 `vendor\bin\arm64\guard64.dll`。单独连跑 10 次全过,偶发;该 DLL 不在仓库里,崩溃根因没有查,见 KNOWN-ISSUES.md 的相关记录。
> - **2026-10-18 的整套运行(当时 `run_e2e` 串行 14 个分类,1 小时 15 分):1922 条中 1870 通过、4 失败、48 跳过。**(历史记录,不代表当前状态)
>   - 48 条跳过全在 `test_app_builder_apps.py`:当时 18420 被一对更早一次测试遗留的 uvicorn 占着,模块按设计整体跳过。清掉后单独重跑 `app_builder`:264 条全过。
>   - 遗留的原因:该模块的清理函数 `_procs_under` 只认命令行含 `backend.main:app` 的进程,而 `test_a_custom_app_module_in_the_yaml_is_what_gets_launched` 把入口改成了 `server.web:application`,所以它起的服务器没被清理、一直占着 18420(用例本身通过)。已把过滤改为"uvicorn 且工作目录在本次数据目录下";恢复旧过滤条件做对照,遗留复现并被 runner 报出和清理,新条件下整个模块重跑无遗留。
>   - 4 条失败(model_builder 的 `test_qairt_switch` preflight 1 条读超时;model_hub 的 `test_downloads`/`test_model_hub` 3 条连接被重置)和随后 `app_builder` 重跑里的 2 条(`test_app_builder_import` 的 manifest 用例)全是 `httpx` 连接错误(WinError 10053/10054)或读超时,**没有一条是断言失败**;单独重跑多数通过,但同一批重跑时又会换几条失败。10 次失败里有 9 次发生在断言"服务端返回 500"的用例上(对应 MH-ISS-03 / APP-ISS-14)。**当时根因未定,2026-10-20 已定位并修复**:未处理异常返回 500 后 Starlette 重新抛出异常,uvicorn 关闭连接(不是 `guard64.dll`),详见 KNOWN-ISSUES.md 的 TEST-ISS-02。
>   - `KNOWN GAP` 用例(全目录 107 处,分布在 28 个文件)把当前的缺陷行为固定成了断言,所以"通过"只表示行为没变,不表示产品正确;修缺陷后它们会失败,按用例里的说明改断言。
> - **状态图例与汇总(2026-10-18 核对;下表数字由脚本按本节各行标记统计,共 75 行;L4 安装/启动脚本类 8 条已按决定从清单删除):**
>   `✅ 已实现` = 有测试文件;`✅ 部分` = ✅ 后面紧跟"部分",已做一部分,行内写明没覆盖什么(已实现的行里也常有"没测"一栏,那只是范围说明,不算部分);`⬜ 待做` = 尚无测试,括号里只写依赖列/层级,**未做过探测,不含原因判断**;
>   `不自动化/不做` = 决定不做;`暂时不做` = 依赖 NPU,暂缓,以后可再评估;`待定` = 等决定后再定做不做;`已覆盖` = 文档原写已有 system test(不在本目录);另有 AUTH-05(当前代码没有该功能)、GO-01(内部,不纳入)沿用原说明。
>
>   | 状态 | 数量 | 用例 |
>   |---|---|---|
>   | ✅ 已实现 | 46 | (略,见各行) |
>   | ✅ 部分 | 8 | CHAT-06、AB-02、AB-04、AB-14、MH-03、SVC-02、SVC-04、GALL-01 |
>   | 不自动化/不做 | 5 | AUTH-04、AB-03、CH-01、CH-02、REM-01 |
>   | 已覆盖(既有 system test) | 2 | MB-01、MB-02 |
>   | 沿用原说明 | 2 | AUTH-05、GO-01 |
>   | 暂时不做(依赖 NPU) | 6 | AB-05、AB-06、AB-07、AB-08、MB-04、SVC-01 |
>   | 待定(L2 页面,等是否引入浏览器测试的决定) | 6 | CHAT-03、CHAT-09、MH-04、SVC-05、SECU-05、CODE-01 |
> - 端口:8989(后端)和 6810(模型下载的 aria2c RPC)必须空闲。含自管后端的模块不能和模块级后端同时存活,所以拆成了独立文件。
> - 用例对产品代码做过变异检查;判定"被抓住"必须以 pytest 退出码 1 为准,退出码非 0/1 是环境故障,不是结论。

### 3.1 系统与启动(后端 `/api/system/*`)

| ID | 功能 | 层 | 依赖 | 优先级 |
|---|---|---|---|---|
| SYS-01 | 后端启动成功,`/api/system/health` 健康 | L1 | - | P0 ✅ 已实现(`test_sys_basic.py`) |
| SYS-02 | `build-info` / `edition` / `platform` / `python-info` 返回合理 | L1 | - | P1 ✅ 已实现(`test_sys_basic.py`) |
| SYS-03 | `features`、`config/summary` 反映实际启用的功能 | L1 | - | P2 ✅ 已实现(`test_sys_features_config.py` 18 条):`config/summary` 跟随 `docs_enabled`/`csrf_enabled`/`host`、关文档会同时拿掉 `/docs` `/redoc` `/openapi.json`、关 CSRF 后裸 POST 可通且不发 cookie、非法布尔值让后端起不来。**清单里“反映实际启用的功能”不成立**:`features` 是写死的 6 项,没有任何设置能改它(SYS-ISS-14)。**没测**:`edition`(来自仓库根的 `build_info.json`,不改它)、真正监听非回环地址 |
| SYS-04 | `qairt-versions` 列出已装 SDK;QAIRT 热切换(`/api/system` qairt-switch) | L1 | QAIRT | P2 ✅ 已由 MB-07 覆盖(`test_qairt_env.py` / `test_qairt_switch.py` / `test_qairt_env_bad_file.py`);真正执行一次切换未测,理由见 MB-07 |
| SYS-05 | 启动写 `data/runtime/server.endpoint.json`,内容与实际端口一致 | L1 | - | P1 ✅ 已实现(`test_sys_spa_endpoint.py`) |
| SYS-06 | SPA:`/` 返回 index.html,未知路径回落 SPA,`/assets/*` 正确 MIME | L1 | dist | P0 ✅ 已实现(`test_sys_spa_endpoint.py`) |
| SYS-07 | `dist` 缺失时返回 503 维护页(不崩溃) | L1 | - | P2 ✅ 已实现(`test_sys_spa_maintenance.py` 24 条,用一个子进程在临时仓库根上建 app,不动真实 `frontend/dist`):所有非 API 路径一律 503 + 同一张静态页 + no-store、API 不受影响、有 `index.html` 时的前端路由与 404 边界。**缺口 SYS-ISS-13**:`dist` 在但没有 `index.html`(半个构建)时只得到裸 JSON 404,反而没有维护页。**没测**:开发模式(`is_production=false`)的行为 |
| SYS-08 | 退出码 75 触发 supervisor 重启 | L1 | - | P2 ✅ 已实现(`test_sys_reboot.py` 10 条,跑真实的 `apps.cli.serve`):重启换 PID、supervisor 自身不变、数据仍在、连续两次合并为一次、无 CSRF 令牌不重启、没有 supervisor 时进程以 75 退出、退出码可配置。**纠正**:`POST /exit` 返回 `exit_code: 0`(干净停止),supervisor 随之退出,不会重启。**没测**:连续重启上限 32、崩溃 5 次/300 秒的上限(各需几十次启动)、Ctrl+C 与 35 秒优雅停止窗口 |

### 3.2 登录与访问控制

| ID | 功能 | 层 | 依赖 | 优先级 |
|---|---|---|---|---|
| AUTH-01 | 启用登录门时,未登录:**API/JSON 请求返回 401 信封**(`code=auth.required`,带 `login_url`);**浏览器 HTML 导航不重定向**,放行让 SPA 先渲染再弹登录提示(2026-07-17 UX 约定);伪造/过期/错签 cookie 被拒 | L1 | AUTH | P1 ✅ 已实现(`test_auth_gate_on.py`) |
| AUTH-02 | 公共路径(`/api/system/health`、build-info、edition、`/api/auth/me`、`/auth/*`、OpenAPI 文档、SPA 静态)免登录;`/v1/` 不受 session 门禁影响;其余私有路径被挡 | L1 | AUTH | P1 ✅ 已实现(`test_auth_gate_on.py`) |
| AUTH-03 | 登录门关闭或已有有效会话时,受保护接口可访问(登录门的处理方式见 §2) | L1 | - | P0 ✅ 已实现(`test_auth_gate_off_csrf.py`) |
| AUTH-04 | 真实 Okta 登录流程 | - | AUTH, NET | **不自动化**(需真实账号/SSO) |
| AUTH-05 | **(后续优化,当前代码没有)** JWT 7 天有效期内免登录;过期回到登录 | L1 | AUTH | 待优化落地后再定(见 §2.1) |
| SEC-01 | CSRF:缺少 cookie/头、两者不一致的写请求被拒(403 `security.csrf.missing`),携带匹配的 cookie+头后通过;GET/HEAD/OPTIONS 不需要 | L1 | - | P1 ✅ 已实现(`test_auth_gate_off_csrf.py`) |

### 3.3 聊天(`/chat`,`/api/chat/*`,`/v1/*`)

| ID | 功能 | 层 | 依赖 | 优先级 |
|---|---|---|---|---|
| CHAT-01 | 创建/列出/重命名/删除会话(REST);另含分页、搜索、pin/favorite 过滤、输入校验(400)、不存在 404 | L1 | - | P0 ✅ 已实现(`test_chat_conversations.py`) |
| CHAT-02 | 发消息并收到流式回复(SSE / WS) | L1 | LLM→本地替身 | P0 ✅ 已实现(用可脚本化的假 OpenAI 服务 `_fake_llm.py`,按环境变量 `QAI_CHAT__LLM_BASE_URL` 接入,不写钥匙串;`test_chat_stream.py` 43 条 + `test_chat_no_llm.py` 10 条 + `test_chat_cloud_key.py` 7 条):帧序列、上游请求的内容、历史回放、持久化、中文/长文本、上游 400/401/404/429/5xx 的分类与重试、断流、云端路径的 key 处理。**没测**:真实模型(见下方说明)、WebSocket 数据面的实际收发(只测了缺参数时的 403)、采样参数到上游的转发(探测时 `temperature/top_p/max_tokens` 查询参数没有出现在上游请求里,原因没查清,见 CHAT-ISS-04)、不可达的云端端点(产品会重试最长 10 分钟,太慢) |
| CHAT-03 | 多标签会话(`/chat/:tabId`)切换与深链 | L2 | - | P1 待定(2026-10-18;L2 页面用例需先决定是否引入浏览器测试、是否允许给前端补 `data-testid`;依赖列=-) |
| CHAT-04 | 会话搜索(conversations search) | L1 | - | P1 ✅ 已并入 CHAT-01(`test_chat_conversations.py` 里有按标题查找、无匹配返回空两条搜索用例) |
| CHAT-05 | 中断生成(interrupt)、权限审批弹窗 | L1/L2 | LLM→本地替身 | P1 ✅ L1 已实现(在 `test_chat_stream.py` 里):`/stop` 的幂等性、上游确实停止被读取(用服务端计数)、部分回复被保存、`active-runs`、同标签并发被拒、不同标签互不影响、工具调用触发权限请求、拒绝/批准后结果回到模型、等待审批时被停止、重复调用的无进展保护。**L2(弹窗 UI)未做**,需要先补 testid |
| CHAT-06 | OpenAI 兼容接口 `/v1/*`(chat completions、models) | L1 | LLM | P1 ✅ 部分实现(`test_openai_compat.py` 61 条 + `test_openai_compat_open.py` 1 条):模型列表/详情的形状与顺序、bearer 守卫(三个路由、各种错误写法、先于校验)、请求体校验、无模型时的行为。**有意没做:带上游的完整回复(流式和非流式)**——`/v1` 要求模型有上下文长度,只能来自已注册的 provider;能真正回答的 provider 需要密钥,密钥只能写入系统钥匙串(`apps/api/di.py` 写死 `prefer="auto"`,没有环境变量可以选加密文件存储)。同一个用例已在 `/api/chat` 上端到端覆盖,缺的只是 `/v1` 的信封(`choices`、`usage`、`chunk` 增量、`tool_calls` 增量)。发现 CHAT-ISS-01..03 |
| CHAT-07 | 附件/图片上传(`/api/uploads`)并在消息中引用 | L1 | - | P1 ✅ 已实现(`test_uploads.py` 83 条、`test_uploads_restart.py` 6 条):六类上传、大小上限(10 MB 图片 / 旧代码路由 10 MB 精确边界)、文件名清洗、`conv_id` 隔离、数据集压缩包解压(zip/tar 穿越与符号链接拦截)、旧代码上传的扩展名白名单、列表/删除、重启后索引重建。**没测**:2 GB/500 MB/100 MB/50 MB 的大文件上限、并发上传、磁盘写满、"在消息中引用"(需要 LLM 替身,见 CHAT-02) |
| CHAT-08 | MCP 配置(`/api/chat/mcp`)增删与列出 | L1 | - | P2 ✅ 已实现(`test_chat_mcp.py` 55 条 + `test_chat_mcp_gate.py` 13 条):增/列/测试/开关/删除、upsert 语义、两种 400 形状、各项上限的包含边界、本地目录(只读部分)、开关打开后“尝试启动”(用不存在的命令观察 `spawn_failed`)、重启后持久化、手写配置文件、损坏文件不拦后端。**没测**:真能连上的 MCP 服务、`headers`/密钥环境变量(会写系统钥匙串,见 CHAT-ISS-05)、联网的目录浏览与安装。发现 CHAT-ISS-05..09 |
| CHAT-09 | 聊天模式切换(chat mode)、工具模式(composer 工具栏) | L2 | - | P1 待定(2026-10-18;L2 页面用例需先决定是否引入浏览器测试、是否允许给前端补 `data-testid`;依赖列=-) |
| CHAT-10 | 无 LLM 可用时给出明确提示而不是挂起 | L1/L2 | - | P1 ✅ L1 已实现(`test_chat_no_llm.py`):未配端点时立即以固定文本 `[no LLM endpoint configured]` 结束、本地端点没服务时给出说明并列出要检查什么、云端路径没有 key 时在联网前就拦住、“localhost”的各种写法算不算本地。**L2 未做** |

> **为什么用假 LLM 而不是真实环境(已实现,不再是"尚未设计")**:真实模型的回复不可复现、无法按需变慢来测中断、也无法按需报错。
> 假服务(`tests/e2e/_fake_llm.py`)由最后一条用户消息决定回复(`[echo]`、`[slow] n`、`[error 500]`、`[cut]`、`[tool 名 {json}]`、`[loop ...]`),并记录产品真正发给上游的请求。
> 接入方式:后端环境变量 `QAI_CHAT__LLM_BASE_URL` / `QAI_CHAT__LLM_DEFAULT_MODEL` / `QAI_CHAT__LLM_API_KEY`;回环地址走“本地”路径(不需要 key),`localhost.`(带尾点)走云端路径。
> **真实环境不做**:唯一候选是 `qai-service`,需要 SSO 登录(其 JWT 在登录时由 Okta id_token 换取,AUTH-04 已决定不自动化),且本机钥匙串里没有任何云端 key、也没装本地 GenieAPIService。
> 缺少的是“真实模型能不能回答、真实链路通不通”这一类冒烟。

### 3.4 App Builder(模型运行/应用生成)

内置 Pack:`whisper-base`(ASR)、`zipformer-zh`(ASR)、`melotts-zh`(TTS)、`real-esrgan-x4plus`(超分)。

| ID | 功能 | 层 | 依赖 | 优先级 |
|---|---|---|---|---|
| AB-01 | 列出模型 `/models`,详情 `/models/{id}`,Pack manifest 合法 | L1 | - | P0 ✅ 已实现(`test_app_builder.py`):含 manifest/schema 与列表一致、taxonomy 三处计数一致、`include_disabled` 过滤(在临时库里禁用一行再断言)。**未测内置模型的 DELETE 保护(403)**,见下 |
| AB-02 | 依赖状态 `deps-status`、`deps-status/packs` | L1 | - | P1 ✅ 部分(`test_app_builder.py`):`deps-status` 概览、以及**检查器关闭**时的 `packs`。检查器**开启**时首次轮询会后台 `pip install` 进后端真实 venv,未测 |
| AB-03 | 权重自动下载:缺权重时首次使用触发下载,失败返回 `WEIGHTS_NOT_INSTALLED` 而不崩溃 | L1 | NET | 不自动化(2026-10-18 决定不做;下载的是 whisper/zipformer/melotts 三个语音包的 QNN 权重,走外部 S3) |
| AB-04 | 运行记录:创建 run、`/runs/{id}`、SSE `stream`、取消、历史 | L1 | NPU | P0 ✅ 部分(已由 `test_app_builder_runs.py`、`test_app_builder_runs_restart.py` 覆盖不需要 NPU 的部分:创建 run、按 id 查询、列表过滤与分页、删除历史、查不存在的 run、失败路径的 SSE `stream`、重启后记录仍在)。**没测,暂时不做(依赖 NPU):** 一次成功的运行、运行中取消、产物、评分、WebSocket。 |
| AB-05 | real-esrgan 推理(图片 → 超分图片,产物可下载) | L3 | NPU | P0 暂时不做(2026-10-18 决定;依赖 NPU,待以后再评估) |
| AB-06 | whisper / zipformer 语音识别(音频 → 文本) | L3 | NPU, 权重 | P0 暂时不做(2026-10-18 决定;依赖 NPU,待以后再评估) |
| AB-07 | melotts-zh 文本转语音(文本 → 音频) | L3 | NPU, 权重 | P1 暂时不做(2026-10-18 决定;依赖 NPU,待以后再评估) |
| AB-08 | 批量运行 `/batch` | L3 | NPU | P2 暂时不做(2026-10-18 决定;依赖 NPU,待以后再评估) |
| AB-09 | 语音偏好 `voice-preference` 读写;启动预热(默认 Zipformer) | L1 | - | P1 ✅ 配置面已实现(`test_app_builder.py`):默认值、往返并落库、PUT 整体替换、校验 400/422、关闭时 preload 跳过。预热真正把模型加载进 NPU worker 未测(需 NPU) |
| AB-10 | 驻留 worker 状态 `worker/status`、缓存 `cache/status`、清缓存 | L1 | - | P1 ✅ 已实现(`test_app_builder.py`):worker 状态的结构与不变量、缓存递归计数/清除/幂等/目录缺失;用例先断言缓存目录在临时数据目录内才写入 |
| AB-11 | 应用(App)生成/运行/日志/打包:`/apps/*`、package 进度 | L1 | - | P1 ✅ 已实现(`test_app_builder_apps.py` 48 条 + `test_app_builder_package.py` 46 条):发现/详情/排序/损坏 yaml、运行(真起 uvicorn,只含 `/health`)、端口分配、日志、崩溃重试 4 个端口、删除;打包的阶段/内容/排除规则/清单/requirements/run.bat/并发。**“生成”那一步(由聊天里的 LLM 写文件)没测**,由测试直接写文件代替。**没测**:带模型的应用、打包真实模型。发现 APP-ISS-01..09(其中 01 是停止后服务器仍在运行) |
| AB-12 | 分享链接 `/share/{token}` | L1 | - | P2 ✅ 已实现(`test_app_builder_runs.py` 里的分享部分,加 `test_app_builder_runs_restart.py`):生成/查看/过期/TTL 边界/令牌格式/删除运行后失效/重启后仍有效。**没测**:浏览器里的 `/share/<token>` 页面。发现 APP-ISS-16、17、20(无法撤销、无上限、`true` 被当成 1 秒) |
| AB-13 | 导入:`import/dry-run`、scan-bins、rollback | L1 | - | P1 ✅ 已实现(`test_app_builder_import.py` 61 条 + `test_app_builder_import_restart.py` 1 条):用造出来的模型包(约 1 MB 的零填充权重 + 一行 `runner.py`)测 add/replace/skip、1 MiB 边界、commit 的各种形状、rollback、删除。**没测**:运行导入后的模型(需要 NPU)、多变体包、`scan-bins` 的实际检查。发现 APP-ISS-10..14(commit 一律 201、rollback 不删文件、rollback 一个 replace 后库和磁盘不一致、非对象 JSON 让整个 dry-run 500) |
| AB-14 | 无 NPU 的机器上优雅降级(不崩溃,明确报错) | L1 | - | P1 ✅ 部分实现(`test_app_builder_runs.py`):**本机有 NPU,没有也不能模拟“没有 NPU”**;验证的是运行在输入缺失/文件不存在时的失败路径(`INVALID_INPUT` + 可读信息、SSE 说明失败、之后 worker 仍可用、并发 4 个都正常结束、重启后仍在)。这是没有 NPU 的用户同样会遇到的路径,但不是同一条。发现 APP-ISS-15、18、19 |

> **App Builder 已知现状(`test_app_builder.py` 里已用例固定,改了行为需同步改用例):**
> - `PUT /voice-preference` **不校验模型是否存在**:格式合法但不存在的模型 id、或真实模型的不存在变体,都返回 200,之后预热才会静默失败。
> - 校验失败是 **422**(领域错误),请求体格式错误是 **400**;模型路由上的未知 id 是 404,但响应体是裸 `{"detail": ...}`,不是项目统一的错误信封。
> - `DELETE /cache` 只删文件,不删空子目录。
> - 内置模型的 DELETE 保护(403,`app_builder.app_model_builtin_protected`)**没有自动化**:权重在仓库 `models/` 下(被 git 忽略,约 2 GB,4 个包里 3 个没有下载地址),后端不隔离它;保护一旦失效,测试会真的删掉不可恢复的文件。负责人已决定不测。
> - AB-14(无 NPU 时优雅降级)**未覆盖**:本机有 NPU,无法在此验证。

### 3.5 Model Builder(模型转换)

| ID | 功能 | 层 | 依赖 | 优先级 |
|---|---|---|---|---|
| MB-01 | 工作区初始化、plan/REPORT 解析、精度归一化、任务分类 | 已有 system test | - | 已覆盖 |
| MB-02 | `POST /api/app-builder/import/auto-export` 及 `/status` | 已有 system test(最小 app) | - | 已覆盖 |
| MB-03 | auto-export 在**完整后端**上的行为(含 DB、DI 装配) | L1 | - | P1 ✅ 已实现(`test_model_builder.py`):完整包产物与 manifest/校验和一致、覆盖参数、多精度、重复导出、失败后状态回到 idle 且无残留、路径守卫(含目录联接逃逸)、`/status` 三态。合成 `.bin`,**不验证真实 io_contract**(见下) |
| MB-04 | ONNX → QNN 转换 → context binary 全流程 | L3 | QAIRT, NPU | P1(夜间) 暂时不做(2026-10-18 决定;依赖 NPU,待以后再评估) |
| MB-05 | 量化(INT8/W8A16 等)经 `run_pipeline.py` 到 DLC(**不含**"敏感度分析":产品里没有这个功能,只有一份参考文档 `references/quantization-sensitivity.md`) | L3(无 NPU) | QAIRT SDK + x64 3.10 转换 venv | P2 ✅ 已实现(`test_quantization.py` 28 条,约 2 分钟):用代码现造的极小模型(2 层卷积,1359 字节)+ 8 个随机校准样本,放临时目录,**加 `--skip_contextbin` 只产出 DLC,不碰 NPU**。15 个慢场景在一个模块级 fixture 里并行跑一次(串行约 258 秒,并行约 120 秒)。覆盖:W8A8/W8A16/FP16/BF16/自定义位宽(8/8/32)的产物(量化 DLC 与 FP DLC 并存且不同、FP 精度不跑量化器);各选项真正传给 SDK 的命令行(W8A16 的 `--restrict_quantization_steps`、`--cle`→`--algorithms cle`、`--per_channel`、percentile 的值、默认偏置保持 FP32、`--dump_encoding` 写出 json);脚本在启动 SDK 前就拒绝的情况(单独的 `--act_bw`/`--weight_bw`、缺校准列表、校准列表文件不存在、模型不存在,退出码 1;未知 `--precision`/`--calib_method`,退出码 2);只有量化器才拒绝的情况(空校准列表、校准样本不存在、`--bias_bw 7`、percentile 值 50,退出码 1,且只留下 FP 的 DLC)。变异检查 16 个(改脚本副本,以 pytest 退出码 1 为准)全部被抓住。**没测**:context binary(MB-04 的 NPU 部分)、量化后的数值精度、真实模型和其他算子(MatMul/注意力)、`--per_row`/`--io_config`/`--quant_overrides`/`--htp_version`/`--soc_optimized`、4 位精度、`qai_convert_int.py` 和 `run_pipeline_legacy.py`。**如实记录(未判对错)**:尺寸错误的校准样本(4 字节对 3072 字节)被接受并产出量化 DLC;旧的 `input:=路径` 列表格式被接受(带 `[WARN]`)。没有 SDK 或 x64 venv 时整个模块自动跳过。 |
| MB-06 | CLI:`qai pack export/validate`、`qai build` | L1 | - | P1 ✅ 已实现(`test_model_builder.py`):`pack export/validate/workspace-init` 以真实子进程运行(退出码+输出)、校验器 11 种破坏、路径守卫、`--precision` 合并。`qai build` **只测**参数解析和"无云端模型"提前退出(它是需要云端 LLM 的 REPL,无法无人值守) |
| MB-07 | 转换环境就绪检查:`qairt_env.json` 与 SDK、x64 venv 一致 | L1 | QAIRT | P1 ✅ 已实现(只读;`test_qairt_env.py` 8 条、`test_qairt_switch.py` 28 条、`test_qairt_env_bad_file.py` 3 条,约 5 分钟):`GET /api/system/qairt-versions`(严格版本目录、数值降序、`qairt_root` 优先于 `qairt_sdk_root`、17 种 env 文件形态)、QAIRT 热切换的 `preflight`(必需扩展版本、四个候选、是否需要安装)以及 `create`/`job` 的校验边缘。全部用一个**假的 SDK 目录**(空的版本文件夹)+ 一个自管后端,不碰真实 SDK 和产品自己的 `qairt_env.json`(每个模块起止都校验哈希)。**没测**:真正执行一次切换(会装 wheel、改 `qairt_env.json`、重启 worker)、job 轮询的真实形状、回滚、x64 转换 venv 是否真的可用(L3) |

> **Model Builder 已知现状(`test_model_builder.py` 里已用例固定,改了行为需同步改用例):**
> - **`pack_id_override` 不做格式校验**:DTO 文档写着必须 kebab-case,实际 `"UPPER"`、`"Not Kebab!"`、`"has space"` 都被接受,原样写进 `manifest.modelId`,导出的包还能通过 `qai pack validate`。(`AppModelId` 的规则只在之后的导入阶段才用。)
> - **`qai pack validate` 只做结构检查**:`_candidate.json` 里 `ready:false` 能通过;`variants: []` 能通过;把 `checksum` 清空后,被篡改的权重也能通过。
> - **垃圾 `.bin` 也能"导出成功"**:合成 `.bin` 让原生冒烟探针结果**不确定**(实测约 1/4 子进程访问冲突崩溃,导出降级为 `io_contract=null` 加一条软错误;其余情况"加载成功"并记录**空**的 io_contract 且标成 `validated_at_export:true`)。两种都 `success=true`。服务进程不受影响。真实 io_contract 需 NPU(MB-04)。
> - **减少精度后重新导出,旧 `.bin` 会残留在 `weights/`**(manifest 已不再列出它)。
> - **CLI 的 `--wos-ai-root` 默认指向真实 `C:/WoS_AI`**,而且是它唯一的路径守卫;`qai build` 的数据库在 `--repo-root/data` 下。所以 CLI 测试必须显式传 `--wos-ai-root` 并设 `QAI_DATA__DATA_DIR`。
> - `qai pack export` 里"`success==False` 就退出 1"这个分支**黑盒不可达**(`plan.md` 可省略,所有影响 `success` 的检查对能走到 `.bin` 查找之后的工作区都为真,缺失/过小的 `.bin` 是抛异常),所以没有对应测试。
> - 显示名按 `str.title()` 生成:`e2e_one` → `E2e One`。

### 3.6 Model Hub / 模型目录 / 下载

| ID | 功能 | 层 | 依赖 | 优先级 |
|---|---|---|---|---|
| MH-01 | 模型目录 `entries` 列表/详情 | L1 | - | P1 ✅ 已实现(`test_model_hub.py`):注册/读取/列表/删除往返、重复 409、输入校验与长度边界、排序、版本列表与"删当前版本清空 current"、级联删除、数据库约束。下载与校验只测不需网络/文件的边缘 |
| MH-02 | 下载任务:创建、`progress`、取消、校验(`verify`) | L1 | NET→本地替身 | P1 ✅ 已实现(`test_downloads.py` 34 条 + `test_downloads_no_aria2c.py` 1 条,约 1 分钟):用**本地 `http.server` 替身 + 仓库自带的 aria2c**,不联网。覆盖启动/校验、进度 SSE、完成、404 源、取消(用服务端计数证明传输真的停了)、列表、`verify` 的匹配/不匹配/空文件/非法 key、没有 aria2c 时的 503。**没测**:真实模型文件与真实目录 URL、重启后续传、多连接分块、代理、WebSocket 变体的实时帧(仅测了找不到任务的路径)、aria2c 守护进程崩溃恢复。发现 MH-ISS-01..05 |
| MH-03 | 云端模型/Provider 配置读写(`providers`、`cloud-models`) | L1 | - | P1 ✅ 部分已实现(`test_model_hub.py`):provider 读写与整体替换、cloud-models 投影/排序/容错、历史明文 api_key 读取时被掩码、外部版隐藏内部服务。**任何带真实 api_key 的用例都没写**(见下) |
| MH-04 | 下载页 `/downloads` 展示任务与进度 | L2 | 需先补 testid | P1 待定(2026-10-18;L2 页面用例需先决定是否引入浏览器测试、是否允许给前端补 `data-testid`;依赖列=需先补 testid) |
| MH-05 | aria2c 管理(`/api/aria2c`) | L1 | - | P2 ✅ 已实现(`test_aria2c.py` 5 条 + `test_aria2c_running.py` 7 条):状态、启动(用仓库自带的 aria2c)、幂等、停止、RPC 只监听回环、后端退出时 aria2c 不残留。**没测**:自动下载安装(联网 GitHub)、代理、真实任务的取消。发现 MH-ISS-06、07(`<data>/bin` 不存在时 start 失败;无二进制时 status 仍报 available) |

> **Model Hub 已知现状(`test_model_hub.py` 里已用例固定,改了行为需同步改用例):**
> - **领域 `ValueError` 没有映射成 4xx,返回 500**:`POST /entries` 的 `source_url` 不是 http(s)(`not a url`、`file://`、`ftp://`)或 `tags` 大小写重复,`POST /verify` 的 `storage_category`/`storage_name` 不合法,都是 `internal.unexpected`。客户端错误被报成服务器错误。
> - **provider id 上限名不符实**:校验允许 128,但行键带 23 字符前缀而 `kv_user_prefs.key` 上限 128,所以 106–128 字符的 id 返回 **503**。
> - **含 `/` 的 entry id 可以创建、不能寻址**:`POST` 返回 201,之后 `GET` 404、`DELETE` 405,只能直接改数据库。
> - **没有删除 provider 的 HTTP 接口**:`PUT {"config": {}}` 只是留下一个空行。
> - **`pinned` 用 `bool()` 读取**:字符串 `"false"` 会把 provider 置顶;`context_length: true` 被当成 1。
> - **一行损坏的 entry 会让整个 `GET /entries` 返回 503**(其他完好的条目也列不出来)。
> - 初始库里 `entries` 不一定为空(启动会播种 `qai-service::route-1`),所以用例一律按自己的 `e2e-` id 过滤,不假设列表为空。
> - **未覆盖(负责人已定,不写 keyring)**:真实 `api_key` 经 SecretStore 写入、`has_api_key` 变为 true、替换 key。原因:`PUT /providers` 带 key 会写 Windows 凭据管理器,而同一 service 下写新条目时,系统会把用户已有的"裸 target"条目重存为 `<用户名>@<service>`(值不变,但存放形式被改写)。最重要的两条安全行为仍有覆盖:配置文档中不含 `api_key`(写库前被剔除)、历史明文 key 在 `GET` 时被掩码。
> - **已知不可达的存活变异(已核实,非测试漏洞)**:`register` 的重复检查(仓储层主键冲突会映射成同一个 409)、`remove_version` 的 404 检查(实体 `get_version` 会抛同一个异常)、`/query-services` 的内部版门禁(外部版没有 `qai.platform.edition` 包,去掉门禁结果仍是空列表)。

### 3.7 本地模型服务(Service,`/api/service/*`、`/api/versions/*`)

| ID | 功能 | 层 | 依赖 | 优先级 |
|---|---|---|---|---|
| SVC-01 | 服务状态/启动/停止/探测(`status`、`start`、`stop`、`probe`) | L3 | NPU, 服务二进制 | P1 暂时不做(2026-10-18 决定;依赖 NPU,待以后再评估) |
| SVC-02 | 服务日志(SSE)、清日志 | L1 | - | P1 ✅ 部分已实现(`test_service.py`):只测**服务未运行**时的日志流(帧头、`[DONE]` 收尾、能自行关闭、`skip` 校验与越界)和 `logs/clear`。**真实日志内容没测**:`skip` 在非空缓冲上的偏移、环形缓冲回绕、运行中清空、崩溃后的日志流、停止运行中的守护进程(见下) |
| SVC-03 | 服务配置读写 | L1 | - | P1 ✅ 已实现(`test_service.py`,不起任何进程):未安装态(默认值、保存 412、无副作用)、读(合并默认值、BOM、坏文件回退、`api_key` 掩码)、写(深合并、列表整体替换、保留未知键、缩进 4、非 ASCII 原样)、配置文件定位(`root_path` 去空白、搜索深度 3 层、失效/相对/无配置/指向文件的根)。另含 `status`/`probe`/`models` 的只读部分 |
| SVC-04 | 版本管理:列出/下载/安装/删除(`/api/versions`) | L1 | NET | P2 ✅ 部分已实现(`test_service_versions.py`,98 条,**不联网、不起 aria2c**):目录用测试自己起的本地 HTTP 服务器代替 GitHub(JSON 清单过滤/去重/规范化、纯文本列表、各类错误码、超时设置生效)、`download-settings` 读写、安装(用测试自建的 zip:目录命名必带版本、单顶层目录扁平化、重装替换与失败回滚、zip-slip)、`local-status` 扫描、删除已安装/已下载。**下载流本身没测**(见下) |
| SVC-05 | 服务页 `/service` | L2 | 需先补 testid | P1 待定(2026-10-18;L2 页面用例需先决定是否引入浏览器测试、是否允许给前端补 `data-testid`;依赖列=需先补 testid) |

> **服务(Service)已知现状(`test_service.py` 里已用例固定,改了行为需同步改用例):**
> - **未安装时 `POST /api/service/start`、`/load-model` 返回 500**(`internal.unexpected`),而同状态下保存配置是干净的 **412**。
> - **界面正常的"读取→原样保存"会抹掉只存在于文件里的 `api_key`**:读取时被掩成 `****`,保存时按 `****` 去钥匙串找,找不到就写成 `""`。典型触发:安装包或手工写进 `service_config.json` 的 key。
> - **`forge.config` 的 `genie_service.root_path` 失效时,配置与状态会不一致**:`/api/config` 回退成默认值(保存 412,且 `using_default_config` 仍是 `false`),但 `/api/service/status` 靠扫描 `data/bin` 仍能找到真实安装。相对路径、不含配置的目录、指向文件的路径都是如此。
> - **装了更新的版本后同样分叉**:`status.exe_path` 指向最新安装目录(守护进程将从这里启动),`/api/config` 仍在编辑旧目录的 `service_config.json`。
> - **保存不做结构校验**:`routing` 可以被写成字符串;未知顶层键原样保留;损坏的文件在下一次保存时被默认值整体替换且不留备份。
> - **配置文件内容是 JSON 数组(不是对象)时,`GET /api/config` 返回 500**,而语法损坏的文件会正常回退成默认值。
> - **未覆盖(负责人已定:不起进程、不写 keyring、不关安全开关)**:真正启动守护进程。本机没装 `GenieAPIService`;用自编译的替身进程实测过,默认配置下后端的原生文件守卫会拒绝这个子进程(`WinError 5`,接口 500),只有关掉该安全开关才能起。所以以下**全部没有自动化覆盖**:运行中的日志内容与 `skip` 偏移、环形缓冲回绕、运行中清空日志、守护进程崩溃后的状态与日志流、`stop` 运行中的守护进程、后端被强杀时守护进程跟着退出。另有 `POST /api/config` 带真实 `api_key`(会写 Windows 凭据管理器):测试只发 `""`/`****`/不带 key,且当机器上已存在真实云端 key 时,写入类用例自动跳过(只读检查),模块结束时有守卫确认没有新增凭据。
> - **已知不可达的存活变异(已核实,非测试漏洞)**:`root_path` 去空白在三处各做一次(仓储层、DI 层读库、DI 层读文件),实测只有 DI 读库那处会被用例抓到;仓储层和 DI 读文件两处单独去掉仍会被其他路径兜住(用例经由 HTTP 设置的值总是先走读库路径);`load` 对"文件刚好消失"的分支(定位时已用 `is_file()` 过滤);保存用例里的"未安装则拒绝"(仓储层 `save` 抛同一个 412);请求体的深拷贝(请求字典是刚解析出来的新对象,无法观察)。

这些问题的完整记录(现象、复现、影响、建议)见 `KNOWN-ISSUES.md`。

> **服务版本管理已知现状(`test_service_versions.py` 里已用例固定,改了行为需同步改用例):**
> - **安全:`DELETE /api/versions/download/{version}` 可路径穿越**。`%2e%2e` 会删掉下载目录上一级里的所有 `*.zip`;Windows 下反斜杠形式也能走出去(如 `a\..\keep`)。版本段拼到下载目录后没有任何校验(编码的正斜杠 `%2F` 不会匹配路由,所以这条不行)。
> - **`DELETE /api/versions/install/{version}` 按目录名子串匹配,不是按解析出的版本**:`2.44.0`(目录名里的 QAIRT 版本)会删掉 2.3.7 的安装;`73`(驱动标签)同理;`Genie` 或 `e` 会删掉所有安装。只对数字/点的边界做了限制(`2.3`、`3.7` 不会误删)。
> - **`GET /api/versions/local-status` 有写副作用**:`genie_service.root_path` 未设置时会把数值上最新的安装写进 `forge_config.json`(10.0 胜过 9.0),而这个值决定 Service 页编辑哪份 `service_config.json`(见 SVC-03)。该指针从不修复:删掉那个安装后它仍指向不存在的目录。
> - **`PUT /api/versions/download-settings` 是整体替换,不是补丁**:省略的字段回到默认值;空 URL 读回来是 GitHub 默认地址;`fetch_timeout_seconds: 0` 读回 15;负超时、非 http URL 都原样保存。这些设置存在 `forge_config.json`,而 `GET /api/forge-config` 读的是另一个存储,永远看不到 `version_check`/`download`。
> - **发布清单的 `tags` 填字符串会被拆成单个字符**(`"beta"` → `["b","e","t","a"]`)。
> - **安装会删掉调用方传入的源 zip**(以及变空的父目录)。
> - **未覆盖**:(1) 真正的下载。引擎优先走 aria2c,会自动从互联网下载 `aria2c.exe` 并在 6800 端口起 RPC 守护进程,因此只测了在引擎之前就报错的 400 校验。进度帧、取消、校验和不匹配、断点续传、停滞看门狗都没有自动化覆盖。(2) "服务正在运行"的检测和"杀进程后重试"的删除兜底:两者都用 `wmic` 枚举进程,而新版 Windows 11 已不再带 `wmic`(本机没有),**所以这个功能在这台机器上实际已失效**:`/install/{v}/running` 永远返回 `false`,删除运行中的服务时不会杀进程。(3) 文件被锁(WinError 5)、`.bak` 在锁定重命名时的回滚、超 260 字符长路径。
> - **已知不可达的存活变异(已核实,非测试漏洞)**:`stop_running` 触发的优雅停止钩子是否被调用(这里没有运行中的服务,调用与否结果相同);`POST /download` 成功路径的 SSE 响应头(需要一次合法下载);`download_url` 的大小写不敏感检查(接受大写 `HTTP://` 就意味着进入下载引擎,会拉起 aria2c 并联网);`is_running` 里的版本边界检查(缺失的 `wmic` 短路,始终返回 `false`)。

### 3.8 安全与权限(`/api/security/*`,`/security`)

| ID | 功能 | 层 | 依赖 | 优先级 |
|---|---|---|---|---|
| SECU-01 | 路径白名单/授权:`path-grants` 增删查、`path_patterns` 读写、`permission/check` 决策、审计 `audit/recent` | L1 | - | P0 ✅ 已实现(`test_secu_paths.py`) |
| SECU-02 | 危险命令策略(内置 9 条不可移除的底线 + 只增不减的 extra)、自动审批(`auto_approve` 两套) | L1 | - | P0 ✅ 已实现(`test_secu_commands.py`);仅覆盖配置面,未验证运行时拦截 |
| SECU-03 | 权限请求/审批流(permission) | L1/L2 | - | P0 ✅ L1 已实现(`test_secu_permission.py`,93 条):请求创建/列表(新的在前)、approve/reject/cancel 的状态机与 412/404、原子性(校验失败不半应用、不写审计)、各 scope(`once`/`session`/`process`/`permanent`)与 range(`file`/`directory`/`program`)留下什么、记住的拒绝以 deny grant 存储、批量 cancel、审计行。**真实 ASK 阻塞路径没测**(需要工具调用/原生钩子),所以 `session`/`process` 只验证"存了一行带会话 id/boot id 的记录",没验证它之后真的匹配;L2(审批弹窗)未做 |
| SECU-04 | 运行时配置、Skills 安全设置 | L1 | - | P1 ✅ 已实现(`test_secu_runtime.py` 91 条、`test_secu_skills.py` 47 条、`test_secu_runtime_restart.py` 4 条、`test_secu_templates_restart.py` 5 条):`runtime-config` 读写/校验/热生效 vs 需重启/落盘、重启后保留、退出码 75;`mode`、`health`、`policy`(版本/规则/开关)、`templates`、`channels`;`skill-discovery`/`skills/register|list_active|DELETE`/`skill_policy`。**只读写开关和状态,不关守卫**(见下) |
| SECU-05 | 安全页 `/security` | L2 | - | P1 待定(2026-10-18;L2 页面用例需先决定是否引入浏览器测试、是否允许给前端补 `data-testid`;依赖列=-) |

> **安全(SECU-03/04)已知现状(用例里已固定,改了行为需同步改用例;完整记录见 `KNOWN-ISSUES.md` 的 SEC-ISS-01 至 SEC-ISS-12):**
> - **应用 `demo` / `development` 策略模板会让后端约 8 秒后以退出码 75 退出**,响应却报 `needs_reboot: false`。手工 `PUT /policy` 改规则则相反(报 true 但不重启)。
> - **注册 skill 或保存 `skill_policy` 的路径没有任何范围检查,且对所有主体生效**:`read_paths=["C:\\"]` 会让任何人读整个 C 盘。
> - **`DELETE /skills/{name}` 能把内置功能(`app-builder`)注销掉**,返回 204。
> - **空 `requested_mask` 在 `permission/request` 和 `permission/check` 上都是 500**(领域 `ValueError` 未映射成 4xx)。
> - **`GET /path-grants` 不返回 `effect`/`scope_kind`/`is_directory`**:记住的拒绝和允许在线上长得一样。
> - **未覆盖(负责人已定:不关安全开关)**:把 FileGuard 关掉(`runtime-config.file_guard_enabled=false`、`PUT /mode disabled`、`PUT /policy enabled=false`),因此 `health` 的 `test_mode`/`degraded`、原生钩子原地停止都没测。另:真实 ASK 阻塞路径、通道 ASK 限流真的被消费、skill 的 `trusted_binaries`/exec 侧(存储了,但 `permission/check` 仍 deny,见下)。
> - **已知不可达的存活变异(已核实,非测试漏洞)**:`permission` 模块默认 TTL 表(DI 总是传入 `ttl_resolver`,模块默认永远不被查);`RequestPermissionUseCase` 的空 mask 守卫(实体构造先抛同一个 `ValueError`);`RequestId` 空白检查(值对象先拦);`set_mode` 的非法值(路由的 `Literal` 先拦);`runtime-config` 里 `dependency_approval_deny_args` 的 `list()` 拷贝(模型已是 `list[str]`);`global_proxy` 的热生效(需要观察一个真实的出站请求);`health` 的 `degraded`/`test_mode`(需要关守卫)。

### 3.9 通道、定时任务、技能、AI Coding、其他

| ID | 功能 | 层 | 依赖 | 优先级 |
|---|---|---|---|---|
| CH-01 | 通道(微信/飞书)配置读写;页面 `/channels` | - | 外部账号 | **不自动化**(负责人已定):注册会把凭据写进 keyring,而 HTTP 路由里没找到删除通道实例的接口,做不到"自建自删" |
| CH-02 | 通道真实收发 | - | 外部账号 | **不自动化** |
| TASK-01 | 定时任务 列表/详情/编辑/暂停/恢复/立即运行/删除、`catalog`、运行历史、通知中心 | L1 | - | P1 ✅ 已实现(`test_scheduled_tasks.py`);**创建路径未覆盖**:HTTP 无创建接口(只能由智能体 `scheduled_task` 工具创建,需 LLM),用例直接往临时库插行 |
| SKILL-01 | 技能列表/启用/禁用;页面 `/skills` | L1/L2 | UI 需先补 testid | P1 ✅ L1 已实现(`test_skills.py` 67 条 + `test_skills_restart.py` 2 条):发现、默认模式(`ppt-gen`/`code-assist` 默认关)、toggle、单技能 mode、NPU 模式拒绝、重启后保留、安全页的技能发现。**L2 页面未做**。发现 SKILL-ISS-01..04(**01:先 set_mode 再 toggle 会被无声忽略**) |
| SKILL-02 | 内置技能可加载(browser-automation、weather、read-arxiv-paper 等) | L1 | 部分 NET→本地替身 | P2 ✅ 已实现(在 `test_skills.py` 里):假 LLM 发 `[tool skill {...}]`,逐个验证 9 个内置技能通过 `skill` 工具加载、文本与 SKILL.md 一致;禁用后从系统提示词里消失。**没测**:按技能说明去调 wttr.in/arxiv 真的能不能用。发现 SKILL-ISS-03(“关”只是从提示词里隐藏,按名字仍能加载) |
| CODE-01 | AI Coding(Claude Code / OpenCode)面板:配置、会话、权限弹窗 | L2 | 外部 CLI | P2 待定(2026-10-18;L2 页面用例需先决定是否引入浏览器测试、是否允许给前端补 `data-testid`;依赖列=外部 CLI) |
| SET-01 | 设置页:读写偏好 `user_prefs`(`/api/preferences`、`/api/forge-config`)、语言(三语目录与归一化) | L1/L2 | - | P0 ✅ L1 已实现(`test_settings.py`);语言切换本身是前端行为,后端只有消息目录,L2 未做 |
| SET-02 | 全局代理配置,下载走代理 | L1 | - | P1 ✅ 配置面已实现(`test_settings.py`,不写 keyring);"下载真正走代理"未验证 |
| SET-03 | TLS 校验开关热生效 | L1 | - | P2 ✅ 已实现(`test_set_tls_verify.py` 8 条):用一个自签证书的本地 TLS 服务器转发到假 LLM,同一个后端不重启即可在“拒绝证书”和“正常回复”之间来回切换。证书用产品的 Python 子进程生成,不装任何东西。**只观察了聊天的 LLM 路径**;模型下载、MCP、渠道等其它出站客户端是否同样读这个开关没验证 |
| SRCH-01 | 搜索引擎**管理接口** `/api/search/engines`(引擎列表、手动开关、重置评分、凭据槽、月度额度)及列表里的 `browser` 类型引擎 | L1 | -(原写 NET:这组接口只读写本地库和密钥库,不联网、不跑搜索,属误标) | P2 ✅ 已实现(`test_search_engines.py` 67 条 + `test_search_engines_restart.py` 3 条,约 1.5 分钟,不联网):20 个引擎的行结构与默认状态(`google_browser`/`mojeek` 默认关闭,有凭据槽且无密钥的显示 `needs_config`,无凭据槽的显示 `enabled`)、`manual_state` 三个合法值与各种非法值(422/400)、强制开启默认关闭的引擎、重置评分、凭据接口里**在写密钥库之前就被拒绝的分支**(未知引擎 404、无凭据槽 400、空/空白密钥 400、缺字段 400)、额度的读写/下限(<1 为 422)/通知开关、重启后状态仍在、全新数据目录回到默认值、日志里没有任何搜索引擎的外联。变异检查 10 个(改产品路由/配置的**副本**,以 pytest 退出码 1 为准)全部被抓住。**没测**:真正执行一次搜索(HTTP 抓取/带密钥 API/Playwright 的 `google_browser`/结果融合/截止时间)、**保存和删除一个真实密钥**(会写 Windows 凭据管理器,按已有决定不做)、评分随搜索成败变化、浏览器运行时不可用时跳过该引擎。发现 SRCH-ISS-01(修改类接口接受任意引擎 id) |
| REM-01 | 远程部署(`/api/remote-deploy`) | L1 | 开发板 | P2(多半不做) |
| GALL-01 | Gallery 提交 `/api/gallery/*`(**仅本地部分**:scan、submit 的校验失败分支、progress、open-file/delete-file 的拒绝分支、generate-description 的前置检查) | L1 | 本地部分:-(上传地址写死为外部看板,无法换成替身) | P2 ✅ 部分(`test_gallery.py` 71 条,约 15 秒,不联网):`scan`(递归、只列 `.dlc/.bin/.md/.py`、按路径排序、模型名/量化方式/SDK 版本推断、空目录、非目录 400)、`submit` 在校验阶段的全部拒绝(无 `.dlc`、路径不存在或是目录、大小写、必填字段、类型错误)、`submit-progress` 未知 id 为 404、`open-file` 的拒绝分支、`delete-file`(只删文件、不删目录、二次删除 404)、`generate-description` 的前置检查(目录不存在、无文档、文档为空、文档选择优先级、无云端模型时的提示)。**安全底线**:所有 `submit` 请求都经一个受保护的辅助函数发出并断言为 4xx,另有静态用例保证本模块只有这一处 POST,且断言后端日志里没有看板域名或 `gallery.dashboard_*`。变异检查 20 个(改产品路由**副本**)全部被抓住(其中 1 个先存活,补了"第一个匹配而非最后一个"的用例后抓住)。**没测(也不能测)**:任何通过校验的提交——打包、5 个脚本的压缩阈值、`uploading` 阶段、`done`/`error` 进度、`gallery.dashboard_http_error`/`dashboard_unreachable`/`upload_failed`,它们都会向外部主机发请求;`open-file` 打开真实文件(调用 `os.startfile`);`generate-description` 真正调用云端模型(本地回环替身会被产品故意拒绝,非回环就是真实服务)。发现 GALL-ISS-01(`category` 恒为 `non_genai`)、GALL-ISS-02(`delete-file` 无目录边界) |
| EVT-01 | 事件流 `/events` | L1 | - | P1 ✅ 已实现(`test_events.py` 25 条、`test_events_auth_gate.py` 21 条,需要测试 venv 里有 `websockets`):SSE 握手头、权限请求/批准/拒绝/撤销四类事件的负载、`op` 推导优先级、事件顺序、失败不发事件、多客户端广播与晚加入无回放、WebSocket 与 SSE 负载一致、空闲 30 秒心跳;登录门开启时 SSE 被挡而 WebSocket 不被挡。**没测**:长任务(下载/运行/转换)事件、256 条有界队列的丢弃、半开连接 |
| GO-01 | GoMaster / MB-Pro 会话 | - | 内部服务 | **内部,不纳入** |

### 3.10 安装、构建、启动脚本

| ID | 功能 | 层 | 依赖 | 优先级 |
|---|---|---|---|---|
| INST-08 | `qai.bat --help` / `qai config provider list` | L1 | - | P1 ✅ 已实现(`test_cli.py`,48 条):顶层 `--help`、各命令组帮助、用法错误退出码 2、`config provider list`(空库/已存在的 provider/密钥掩码/排序/stdout 纯 JSON)、`qai.bat` 启动器(横幅、退出码透传)。CLI 一律跑在隔离数据目录里,并检查产品 `data/` 和钥匙串没变。**刻意没调用** `install-pack-deps --help`(它会真装依赖,见 KNOWN-ISSUES INST-ISS-01)。没测:`provider add/edit/remove/test` 和 `auth`(写钥匙或联网)、`--arch`/`host_arch` 选择、交互向导 |

## 4. UI(L2)的范围与维护成本

编译产物可直接测:后端托管 `frontend/dist`,Playwright 打的就是编译后的页面。
但需要保证 dist 与源码同步(先 `Build.bat`)。

**侧边栏共 7 个视图**:chat、channels、skills、downloads、service、security、settings。其中 channels 不自动化,实际覆盖 6 个。
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
- 聊天、安全、布局、AI Coding 已有较多标识,适合写 UI 用例。通道虽然有标识(66 个),但**通道不做自动化**,不写 UI 用例。
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

- **L1 先行**:SYS-01/05/06、AUTH-03、CHAT-01、AB-01/02/09/10(已做)、AB-14、SECU-01/02、SET-01、TASK-01、MB-03/06(已做)、MH-01/03(已做)、SVC-02(部分)/03/04(部分)(已做)。
  这批不需要 NPU、不需要联网,价值/成本比最高。
- **L2 小而精**:页面加载与 6 个视图可进入(不含 channels)、聊天发消息(用替身模型)、安全页授权、设置页语言切换。
- **L3**:real-esrgan 一条推理(AB-05)、zipformer 一条(AB-06),在本机 X Elite 上 opt-in。
- **L4**:2026-10-18 决定不做(`Setup.bat`/`Start.bat`/`Build.bat`/`Uninstall.bat` 相关用例已从清单删除;`qai.bat` 的 INST-08 仍保留,已实现)。

### 5.1 各类用例的平台适用性(初判,未在 win-x64 / linux-arm64 上核对)

| 类别 | win-arm64 | win-x64 | linux-arm64 |
|---|---|---|---|
| L1 API(不依赖 NPU/联网) | ✅ 首批 | 预计可复用 | 预计可复用 |
| L2 UI(Playwright) | ✅ 首批 | 预计可复用 | 需桌面或 headless 浏览器,未核对 |
| L3 NPU(AB-05/06/07、SVC-01) | ✅ 首批 | ❌ 无 NPU | 需 FastRPC,未核对 |
| AUTH 登录门默认状态 | 开 | 开 | 随 `DISPLAY`/`WAYLAND_DISPLAY` |

## 6. 开工前必须验证的不确定项

已解决/已定:
- ~~关登录门~~:登录问题已解决,不再作为风险。
- ~~端口~~:E2E 固定用 8989,被占用就报错退出(见 §2.2)。直连入口的占用报错已实测:退出码 1。
- ~~数据隔离~~:数据文件可隔离,keyring 不隔离(见 §2.1.4);"不能隔离也没关系"的前提下已满足。
- ~~JWT 读不到~~:按负责人决定,读不到即判测试失败(见 §2.1.2 第 3 点)。

- ~~keyring 写操作的防护~~:负责人已定 —— JWT 读真实的,**不测登出**;其他 keyring 写操作直接改真实环境,不防护(见 §2.1.4)。

仍待验证:
1. **数据目录之外的写入**:AI Coding 的 `edit_trash`、浏览器缓存、`~\.config` 等写死路径(见 §2.1.4),
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

真实 Okta 登录、**通道(微信/飞书)的一切自动化**(含配置读写;注册后没有已知的删除接口)、内部 GoMaster/MB-Pro 服务、远程开发板部署、
需要付费账号的云端模型调用。
