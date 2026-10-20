# 已知问题(由 e2e 测试发现)

> 这里只记录**产品行为问题**,不是测试缺口。每条都有一个用例把"当前行为"固定住了(用例文档里标着 `KNOWN GAP`);
> 修复后对应用例会失败,按用例里的说明改成新行为即可。
> 测试未覆盖的范围见 `FEATURE-INVENTORY.md` 各节的"未覆盖"。
>
> **统计**(按本表编号前缀):SVC 10、SEC 12、UPL 4、EVT 1、INST 3(表里 2 行 + 说明里的小项)、MB 3、MH 7、CHAT 10、SKILL 4、SYS-ISS-13/14 两条、APP 19(编号 02 有意空缺)、TEST 2。
> 严重程度是我按影响估的,没有和产品负责人确认。**安全类优先看**:SVC-ISS-01/08、SEC-ISS-02、UPL-ISS-01、EVT-ISS-01。
> "未核实"的部分(比如开启认证后上传接口是否需要登录、绑定 0.0.0.0 后的暴露面)在各条里都单独标了。

| 编号 | 严重度 | 模块 | 一句话 | 用例 |
|---|---|---|---|---|
| SVC-ISS-01 | **高(安全)** | 版本管理 | `DELETE /api/versions/download/{version}` 可路径穿越,删除下载目录之外的 zip | `test_delete_downloaded_dot_dot_deletes_zips_outside_the_download_directory`、`test_delete_downloaded_backslash_segments_are_honoured` |
| SVC-ISS-02 | 高(误删) | 版本管理 | `DELETE /api/versions/install/{version}` 按目录名子串匹配,会删掉未点名的安装 | `test_delete_installed_matches_substrings_of_the_directory_name` |
| SVC-ISS-03 | 中 | 版本管理 | "服务是否在运行"检测依赖 `wmic`,本机没有,功能实际失效 | 无法用例固定(见下) |
| SVC-ISS-04 | 中 | 版本管理 / 服务配置 | `GET /api/versions/local-status` 有写副作用,且写入的指针永不修复 | `test_local_status_auto_configures_root_path_to_the_newest_install`、`test_root_path_is_left_dangling_after_the_install_is_deleted` |
| SVC-ISS-05 | 低 | 版本管理 | `PUT /download-settings` 是整体替换,不是补丁;值不做校验 | `test_settings_put_is_a_full_replace_not_a_patch`、`test_settings_odd_values_are_stored_without_validation` |
| SVC-ISS-06 | 低 | 版本管理 | 发布清单 `tags` 填字符串会被拆成单个字符 | `test_catalog_string_tags_are_exploded_into_characters` |
| SVC-ISS-07 | 低 | 版本管理 | 安装会删掉调用方传入的源 zip | `test_install_deletes_the_source_zip_and_its_empty_parent` |
| SVC-ISS-08 | **高(误删)** | 模型下载 | `DELETE /api/service-catalog/install/{model_id}` 按名字前缀匹配,会删掉别的模型 | `test_delete_model_deletes_directories_that_merely_start_with_the_id`、`test_delete_a_model_that_is_a_prefix_of_other_models_removes_them_all`、`test_deleting_a_base_model_also_deletes_its_instruct_variant` |
| SVC-ISS-09 | 中(误删) | 模型下载 | 同一接口的 `delete_zip` 分支会删掉服务版本的下载包 | `test_delete_model_with_zip_flag_removes_service_version_packages`、`test_delete_model_with_a_dotted_prefix_removes_a_service_package` |
| SVC-ISS-10 | 中(数据丢失) | 模型下载 | 重装名字带点的模型时,备份目录名被截断,会删掉同名的无关目录 | `test_reinstalling_a_dotted_model_deletes_an_unrelated_directory_named_like_the_truncated_backup` |
| SEC-ISS-01 | **高(可用性)** | 安全 / 策略模板 | 应用 `demo`/`development` 模板会让后端约 8 秒后以退出码 75 退出,而响应报 `needs_reboot: false` | `test_secu_templates_restart.py` |
| SEC-ISS-02 | **高(安全)** | 安全 / Skills | 注册 skill 或保存 `skill_policy` 的路径没有范围检查,且对所有主体生效:盘根即开放整个盘 | `test_registering_the_whole_drive_is_accepted_and_opens_it_to_everyone`、`test_an_override_can_open_a_whole_drive_to_every_subject`、`test_a_registered_capability_applies_to_every_subject` |
| SEC-ISS-03 | 中(安全) | 安全 / Skills | `skill_policy` 覆盖无需对应 skill 存在,保存即生效 | `test_an_override_for_a_skill_that_does_not_exist_is_kept_and_grants_access` |
| SEC-ISS-04 | 中 | 安全 / Skills | `DELETE /skills/{name}` 能注销内置功能(`app-builder`),恒返回 204 | `test_unregistering_a_built_in_really_unregisters_it_and_it_can_be_registered_back` |
| SEC-ISS-05 | 中 | 安全 / 权限 | 空 `requested_mask` 在 `permission/request` 与 `permission/check` 上返回 500 | `test_request_with_no_permission_bit_is_a_500`、`test_check_with_no_permission_bit_is_also_a_500` |
| SEC-ISS-06 | 中 | 安全 / 权限 | `GET /path-grants` 不含 `effect`/`scope_kind`/`is_directory`/`is_program`:记住的拒绝与允许在线上长得一样 | `test_a_remembered_deny_grant_looks_like_an_allow_grant_on_the_wire` |
| SEC-ISS-07 | 低 | 安全 / 运行时配置 | 手工编辑的 `forge_config.json` 里字符串 `"false"` 被读成 `True`;也能读回低于 PUT 下限的值 | `test_a_string_false_in_the_file_is_read_as_true`、`test_a_value_below_the_put_minimum_can_still_be_read_back` |
| SEC-ISS-08 | 低 | 安全 / 模式 | `PUT /mode permissive` 后 `/health.mode` 与 `/policy.mode` 仍是 `enforce` | `test_mode_is_not_reflected_in_health_or_policy` |
| SEC-ISS-09 | 低 | 安全 / Skills | skill 名无字符规则(含 `/` 的名字能注册但无法通过 HTTP 删除);skill 体里的破坏性 shell 文本不告警 | `test_odd_names_are_accepted`、`test_destructive_shell_text_in_the_body_is_not_flagged` |
| SEC-ISS-10 | 低 | 安全 / 通道 | 通道没有删除接口;保存前 `GET /channels` 为空;只接受 5 个固定名字 | `test_a_channel_cannot_be_deleted_over_http`、`test_only_the_five_known_channel_names_are_accepted` |
| SEC-ISS-11 | 低 | 安全 / 策略 | 每次 `PUT /policy` 都递增版本,即使规则完全相同 | `test_every_policy_put_bumps_the_version_but_only_a_rule_change_needs_a_reboot` |
| SEC-ISS-12 | 低 | 安全 / Skills | 已注册 skill 的 `trusted_binaries`(及其覆盖)存了但对 `permission/check` 的 exec 判定无可见效果 | `test_trusted_binaries_have_no_visible_effect_on_an_exec_check` |
| UPL-ISS-01 | **高(安全)** | 上传 | `conv_id` 直接拼到上传目录上,不校验:可写到上传根之外、数据目录之外,甚至绝对路径 | `test_conv_id_is_a_path_traversal`、`test_conv_id_dot_dot_writes_into_the_parent_of_the_category_folder`、`test_conv_id_dataset_is_equally_open`、`test_an_absolute_conv_id_is_not_rejected_as_invalid_input` |
| UPL-ISS-02 | 中 | 上传 | 文件名过长(约 150 字符起)或 `conv_id` 为空格/超长/含 `:` 时返回 500 | `test_a_very_long_file_name_is_a_500_not_a_truncation`、`test_unusable_conv_ids_are_a_500` |
| UPL-ISS-03 | 中(数据) | 上传 | 重启后索引从文件名重建:嵌套 `conv_id`(`a/b`)的上传从列表消失且无法删除;原文件名变成清洗后的形式 | `test_an_upload_under_a_nested_conv_id_vanishes_from_the_list_after_a_restart`、`test_the_original_file_name_comes_back_sanitised_after_a_restart` |
| UPL-ISS-04 | 低 | 上传 | `/api/uploads/code` 没有扩展名白名单(`.exe` 可上传),只有旧路由 `/api/upload/code` 过滤 | `test_the_generic_code_route_has_no_extension_allow_list` |
| EVT-ISS-01 | **高(安全)** | 事件流 / 登录门 | 登录门开启时 `WS /api/ws/events` 不需要登录、不检查 Origin,匿名/跨源客户端能收到真实事件;同样情况还有 3 个聊天 WebSocket | `test_events_auth_gate.py` |
| INST-ISS-01 | 中 | CLI | `qai install-pack-deps --help` 不是帮助:它直接对产品 venv 执行 `uv pip install`(缺依赖时会联网安装) | 无(测试刻意不调用;见模块文档) |
| INST-ISS-02 | 低 | CLI | `qai compile-factory` 委托的脚本不存在,任何参数都是 ImportError 回溯 | `test_compile_factory_help_crashes_with_an_import_error` |
| MB-ISS-01 | **高(可用性)** | QAIRT 环境 | `qairt_env.json` 是合法 JSON 但不是对象(`[]`/`"x"`/`null`/`5`)时**整个后端启动失败**(AttributeError,退出码 1) | `test_qairt_env_bad_file.py` |
| MB-ISS-02 | 低 | QAIRT 环境 | 非规范目录名(`02.53.0`、`2.049.0`、全角数字)会出现在版本列表里,但 `preflight` 永远报"未安装" | `test_a_non_canonical_directory_name_is_listed_but_can_never_be_preflighted` |
| MB-ISS-03 | 低 | QAIRT 环境 | `qairt_env.json` 带 UTF-8 BOM 时被当成"没有配置"(记事本保存的文件会丢掉当前版本) | `test_a_bom_in_the_env_file_is_treated_as_no_config` |
| MH-ISS-01 | 中 | 模型下载 | 下载落到后端工作目录的 `models/`,`verify` 读 `<数据目录>\blobs\...`,刚下载完就 verify 得 500 | `test_a_downloaded_file_is_not_found_by_verify_until_it_is_in_the_blob_store` |
| MH-ISS-02 | 中 | 模型下载 | 任务状态靠客户端读进度流才更新,不读就永远 `running` | `test_a_job_stays_running_until_someone_reads_its_progress` |
| MH-ISS-03 | 中 | 模型下载 | `target_filename` / `verify` 的 storage key 非法时返回 500 而不是 400 | `test_a_filename_the_domain_rejects_is_a_500_not_a_400`、`test_verify_with_a_storage_key_the_domain_rejects_is_a_500_not_a_400` |
| MH-ISS-04 | 低 | 模型下载 | 取消下载后部分文件和 `.aria2` 控制文件留在磁盘上 | `test_cancel_leaves_the_partial_file_and_its_control_file` |
| MH-ISS-05 | 中 | 模型下载 | 被拒绝/启动失败的下载请求仍留下一个永远 `queued` 的任务 | `test_a_filename_the_domain_rejects_is_a_500_not_a_400`、`test_without_aria2c_on_the_path_start_is_a_clean_503_and_the_job_stays_queued` |
| CHAT-ISS-01 | 中 | 聊天 / `/v1` | 非流式 `/v1/chat/completions` 在模型不可用时返回 **200 + 空 content + 全 0 usage**,而流式调用正确报错。调用方无法区分"模型回答空"和"根本没调成" | `test_non_streaming_with_no_model_is_a_200_with_empty_content`、`test_a_keyless_provider_model_is_reported_as_a_missing_key` |
| CHAT-ISS-02 | 低 | 聊天 / `/v1` | `/v1` 的校验错误和 403/404 用项目自己的错误信封,不是 OpenAI 的 `{"error": {...}}`;OpenAI SDK 解析不了 | `test_validation_errors_use_the_project_envelope_not_openais` |
| CHAT-ISS-03 | 低 | 聊天 / `/v1` | `/v1/models` 只列模型目录条目和两个写死的名字,不列聊天界面模型选择器里的 provider 模型;OpenAI 客户端发现不了它们 | `test_the_provider_models_the_chat_ui_offers_are_not_listed` |
| CHAT-ISS-04 | 低(待确认是否有意) | 聊天 | 本地(回环)端点收不到任何 `temperature` / `top_p` / `max_tokens`,云端路径收得到;界面上两者用同一组滑块 | `test_an_on_device_endpoint_receives_no_sampling_parameters_at_all` |
| CHAT-ISS-05 | 中(我的失误引出) | 聊天 / MCP | `POST /api/chat/mcp/servers` 带 `headers` 时,每个头的值写进**系统钥匙串**(服务 `chat_mcp`,用户名 `<server>.<header>`),文件里只留占位。正式用例从不发 `headers` | 模块说明里的“NOT covered” |
| CHAT-ISS-06 | 低 | 聊天 / MCP | `POST /servers/{name}/test` 对不存在的名字返回 200 和一行编造的数据(`command: "_"`),不是 404 | `test_testing_an_unknown_server_is_a_200_with_a_made_up_row` |
| CHAT-ISS-07 | 低 | 聊天 / MCP | 自定义注册源的 URL 写成 `not a url` 也被接受(201) | `test_a_nonsense_registry_url_is_accepted` |
| CHAT-ISS-08 | 低 | 聊天 / MCP | stdio 的 `command` 里可以有换行(只拒绝 TAB/LF/CR 以外的 C0 控制字符) | `test_a_newline_in_the_command_is_accepted` |
| CHAT-ISS-09 | **中(安全)** | 聊天 / MCP | `chat_mcp_enabled` 设置的说明是“运营者必须显式打开”(stdio 服务器就是任意本地子进程),但运行时它只是持久化开关的**初始值**:设置关着时任何调用 `PATCH /api/chat/mcp/enabled` 都能打开,之后产品就会尝试启动所有已配置的服务器 | `test_the_setting_only_seeds_the_switch_and_the_api_can_override_it_in_both_directions`、`test_with_the_setting_off_the_api_can_turn_the_gate_on` |
| CHAT-ISS-10 | 低 | 聊天 / MCP | 安装一个不在精选列表里的目录 id,产品先去联网查在线注册表才拒绝,要 8–13 秒(断网时更久);一个本地就能拒绝的请求被网络决定了快慢 | `test_installing_an_unknown_catalog_entry_asks_the_online_registry_first` |
| MH-ISS-06 | 中 | aria2c 管理 | `<data>/bin` 不存在时,即使已经找到 aria2c, `POST /api/aria2c/start` 也失败(`Popen` 的 cwd 不存在,WinError 267),返回 `status: error` 且 `status` 接口没有任何提示 | `test_start_fails_when_the_bin_directory_does_not_exist_even_though_the_binary_is_found` |
| MH-ISS-07 | 低 | aria2c 管理 | 没有任何 aria2c 二进制时 `status` 仍报 `available: true`、`exe_path: ""`(“可用或可自动安装”) | `test_status_with_no_binary_says_available_and_installable_but_has_no_path` |
| SKILL-ISS-01 | **中** | 技能 | 先 `set_mode` 再 `toggle`(技能面板的开关)会被无声忽略:两个接口都返回成功,但读取方优先用 `mode` 键 | `test_an_explicit_mode_beats_a_later_toggle` |
| SKILL-ISS-02 | 低 | 技能 | `toggle` 接受不存在的技能名并永久存进覆盖表 | `test_toggle_accepts_a_skill_that_does_not_exist` |
| SKILL-ISS-03 | 低(待确认是否有意) | 技能 | “关”只是从系统提示词里隐藏技能;模型按名字仍能用 `skill` 工具加载它(未知名字的报错信息里还会列出所有名字) | `test_a_skill_that_is_off_can_still_be_loaded_by_name` |
| SKILL-ISS-04 | 低 | 技能 | front-matter 里 `tags:` 没有值时解析成 `[""]`(一个空标签) | `test_a_skill_with_an_empty_tags_value_has_one_empty_tag` |
| SYS-ISS-13 | 中 | 系统 / SPA | `frontend/dist` 存在但没有 `index.html`(构建中断的典型情况)时,`/` 只得到裸 `{"detail":"Not Found"}`;反而是完全没有 `dist` 时才有维护页 | `test_a_dist_folder_without_index_html_gives_a_bare_404_not_the_maintenance_page` |
| SYS-ISS-14 | 低 | 系统 | `GET /api/system/features` 是写死的 6 项列表,没有任何设置能改它;清单里“反映实际启用的功能”不成立 | `test_the_default_features_and_summary`、`test_the_summary_follows_the_settings` |
| APP-ISS-01 | **中** | App Builder 应用 | `DELETE /apps/{id}/run` 返回 200 `stopped` 并让管理器忘掉该进程,但 uvicorn worker 和 reloader 子进程**仍在运行、仍在应答**;之后删除应用因文件夹被占用失败(503 WinError 32)。这也解释了为什么下一次 `run` 可能返回“ready”却是旧服务器 | `test_stop_reports_stopped_and_clears_the_manager_but_not_the_server`、`test_delete_after_a_stop_fails_while_the_server_survives_and_works_once_it_is_gone`、`test_deleting_a_running_app_does_not_stop_it_either` |
| APP-ISS-03 | 低 | App Builder 应用 | `GET /apps` 是磁盘扫描,对每个应用都报 `status: stopped`/无端口,即使它正在运行;真实状态只在 `/logs` 和 `/run` 里 | `test_the_list_never_shows_a_running_app_as_running` |
| APP-ISS-04 | 低 | App Builder 应用 | `POST /apps/{id}/run` 的 `port` 超范围(70000、-1、0)返回 500 而不是 400 | `test_an_out_of_range_port_is_an_internal_error_not_a_validation_error` |
| APP-ISS-05 | **中(数据安全)** | App Builder 打包 | 打包输出目录默认在 **`C:/WoS_AI/app_builder_packages`**(用户真实的模型构建工作区),不在数据目录下。我的一次探测未覆盖工作区就调用了 `/package`,只因为先撞到另一个失败才没写进去。正式用例一律用 `QAI_WORKSPACE__MODEL_ROOT` 指到临时目录并检查 `C:/WoS_AI` 前后一致 | `test_with_no_workspace_override_the_zip_would_go_to_the_real_work_folder`(仅记录) |
| APP-ISS-06 | 低 | App Builder 打包 | zip 文件名只精确到秒,同一秒内的两次打包是同一个文件,后者静默覆盖前者 | `test_two_packages_in_the_same_second_overwrite_each_other` |
| APP-ISS-07 | 低 | App Builder 打包 | 打包进度路由只用 job id 查找,路径里的 `{app_id}` 随便写什么都行 | `test_the_progress_route_ignores_which_app_the_job_belongs_to` |
| APP-ISS-08 | 低(待确认) | App Builder 打包 | 只打包 `backend/`、`frontend/`、`vendor/` 和 6 个根文件;`data/`、`outputs/`、`static/`、`config.json` 等静默丢弃,清单里也没有警告 | `test_files_outside_the_known_folders_are_silently_dropped` |
| APP-ISS-09 | **中(安全)** | App Builder 打包 | 应用文件夹里指向**应用之外**的目录连接(junction,Windows 上不需要特权)会被当作普通文件夹遍历,目标里的文件被打进 zip(`backend/outside/...`)。应用文件夹是聊天里的 agent 写的,zip 会被分享 | `test_a_junction_that_points_outside_the_app_IS_followed_into_the_zip` |
| APP-ISS-10 | 中 | App Builder 导入 | `import/commit` 对空计划、全 skip 计划、读不到的来源、内置 id 的 add/replace 一律返回 201 + commit id,什么也没改;调用方分不出成功和空操作 | `test_commit_answers_201_for_things_that_installed_nothing` |
| APP-ISS-11 | **中(数据)** | App Builder 导入 | `rollback` 一个 add 只删数据库行,`user_models/<id>/` 和 `user_model_weights/...` 留在磁盘上(真实模型可达数 GB),且再也列不出、删不掉。只有 `DELETE /models/{id}` 才清理文件 | `test_rolling_back_an_add_removes_the_row_but_leaves_the_files` |
| APP-ISS-12 | 中(数据) | App Builder 导入 | `rollback` 一个 replace 后,数据库回到旧版本/旧标题,磁盘上的 `manifest.json` 仍是新版本,两者不一致 | `test_rolling_back_a_replace_restores_the_old_row_but_not_the_old_files` |
| APP-ISS-13 | 低(待确认) | App Builder 导入 | `dry-run` 把不存在的路径当作“模型 id 提示”:合法 id 变成对该 id 的 `add`,其它变成 `imported-0000`,后面没有任何内容 | `test_a_path_that_does_not_exist_is_treated_as_a_model_id_hint` |
| APP-ISS-14 | 中 | App Builder 导入 | 候选文件是合法 JSON 但不是对象(`[]`/`"x"`/`5`/`null`)时,`payload.get` 抛 `AttributeError`,未被捕获,**整个 dry-run(包括同批的好候选)返回 500**。语法错误的文件却能正常处理。和 MB-ISS-01 同一类 | `test_a_manifest_file_that_is_valid_json_but_not_an_object_crashes_the_dry_run` |
| APP-ISS-15 | 低(待确认) | App Builder 运行 | `POST /runs` 缺必填输入也先返回 201 `pending`,失败(`INVALID_INPUT`)要之后才能轮询到;缺必填输入本可以直接 400 | `test_a_run_is_created_pending_and_ends_failed_with_a_reason` |
| APP-ISS-16 | 中(安全) | App Builder 分享 | 分享链接**无法撤销**:响应里有 `revoked` 字段但没有任何路由能设它(DELETE/PATCH/PUT 都是 405),链接泄露后只能等过期或删除对应运行 | `test_a_share_cannot_be_revoked` |
| APP-ISS-17 | 低 | App Builder 分享 | `ttl_seconds` 没有上限,10¹⁰ 秒(约 317 年)也接受 | `test_there_is_no_upper_bound_on_the_lifetime` |
| APP-ISS-18 | 低(待确认) | App Builder 运行 | 未安装的模型(`real-esrgan-x4plus`)也能创建运行,先在输入上失败;用户从没有被告知模型没装 | `test_the_not_installed_model_is_reported_as_invalid_input_not_as_not_installed` |
| APP-ISS-19 | 低 | App Builder 运行 | `GET /runs?status=乱写` 不是 400,返回未过滤列表 | `test_a_status_filter_with_an_unknown_value_is_ignored` |
| APP-ISS-20 | 低 | App Builder 分享 | `ttl_seconds: true` 被请求模型先转成 1,于是生成一个 1 秒后过期的链接(用例层本有显式拒绝 bool 的代码) | `test_a_boolean_ttl_is_taken_as_one_second` |
| SRCH-ISS-01 | 低(待确认是否有意) | 搜索引擎配置 | 修改类接口接受**任何**引擎 id:`POST /no-such-engine/manual_state` 返回 200 并写入一行,`DELETE /no-such-engine/score` 返回 200,`PUT /no-such-engine/quota` 返回 200;这个 id 不会出现在列表和 `/status` 里,但会出现在 `/quota`。凭据接口对未知 id 则正确返回 404 | `test_the_mutating_routes_accept_an_engine_id_that_is_not_in_the_roster`、`test_an_unknown_engine_id_never_shows_up_in_the_list_or_status` |
| GALL-ISS-01 | 低(待确认) | Gallery 提交 | `GET /gallery/scan` 的 `category` 判据是"存在类型为 `model` 的文件",而可扫描的扩展名只有 `.dlc/.bin/.md/.py`,类型永远不会是 `model`,所以结果恒为 `non_genai`,`genai` 分支走不到 | `test_scan_category_is_non_genai_for_every_scannable_set_of_files` |
| GALL-ISS-02 | **中(误删)** | Gallery 提交 | `POST /gallery/delete-file` 对**任意绝对路径**上的已存在文件执行删除,不校验它是否位于某个工作目录之内(目录会被拒绝,返回 404)。同机其他本地调用方可借此删除后端进程有权限删除的任何文件;`open-file` 同理(但限定了扩展名) | `test_delete_file_has_no_workdir_boundary` |
| TEST-ISS-01 | 低(测试环境) | 测试基础设施 | 后端带着 `qairt_env.json` 启动时会预启动 App Builder sticky worker;强停后端后偶尔留下“已终止但进程对象被句柄钉住”的 worker 和一个锁定的 2 KB `.pyc` 临时目录 | 见下文 |
| TEST-ISS-02 | 中(**已修复 2026-10-20**) | 后端(未处理异常的 500 响应) | 未处理异常返回 500 后 Starlette 重新抛出异常,uvicorn 关掉连接,客户端偶尔收到连接重置(WinError 10053/10054)而不是 500;测试里表现为偶发 `httpx` 连接错误。已新增 `UnexpectedErrorMiddleware` 自己返回 500、不重新抛出 | 见下文 |

---

## SVC-ISS-01 下载版本的删除接口可路径穿越(安全)

**位置**:`DELETE /api/versions/download/{version}` → `FileSystemArtifactInstaller.delete_downloaded_service`,
`DownloadPaths.version_save_dir`(`self.download_dir / version`)。

**现象**:`version` 原样拼到下载目录后面,没有任何校验。
- `DELETE /api/versions/download/%2e%2e`:`..` 指向下载目录的**上一级**,接口删除那里所有 `*.zip`,返回 200。
- Windows 下反斜杠形式同样有效:`a%5C..%5Ckeep` 被解析为 `downloads/keep`,没有出现字面 `..` 段。
- 编码的正斜杠 `%2F` 不匹配该路由,所以走不通(这条不是漏洞)。

**复现**(在临时数据目录里):
1. 在数据根目录放一个 `stray.zip`,再保证 `downloads/` 目录存在。
2. `DELETE /api/versions/download/%2e%2e`。
3. `stray.zip` 被删除。

**影响**:能调用该接口的调用方可以删除下载目录之外(至多上一级)的 zip 文件。下载目录可通过
`download-settings.save_dir` 改到任意位置,此时"上一级"就是用户所选目录的上一级。

**尚未验证的部分(请勿当作结论)**:实际可利用性取决于谁能调用这个接口。测试后端是**关闭认证**的临时实例,
请求带了 CSRF 令牌;我没有测过开启认证时该接口是否需要登录、缺少 CSRF 令牌时是否被拒绝、
以及绑定到 `0.0.0.0` 后的暴露面(设置里 `bind_host` 默认是 `127.0.0.1`,可改为 `0.0.0.0`)。评估严重程度前建议先补这三项。
"高(安全)"这个等级是基于"服务端对路径段完全不校验"这一事实给的,不是基于已演示的远程利用。

**建议**:在 `_require_non_empty` 之外校验 `version`:只允许 `^\d+(\.\d+)*$`(与清单解析里的 `_VERSION_RE` 一致);
同时在 `version_save_dir` 里 `resolve()` 后确认结果仍在 `download_dir` 之内。`install/{version}` 也一并加。

**同类检查结果**:模型下载的删除接口(`DELETE /api/service-catalog/install/{model_id:path}`,
我之前把路径误写成了 `/api/model-catalog/...`,已更正)**没有这个问题**,见下面"已核对、没有复现"一节。
但它有另外的误删问题,见 SVC-ISS-08 到 SVC-ISS-10。

---

## SVC-ISS-02 删除已安装版本按目录名子串匹配

**位置**:`DELETE /api/versions/install/{version}` → `delete_installed_service`,匹配规则
`(?<![.\d])<version>(?![.\d])`,作用对象是**整个目录名**。

**现象**:目录 `GenieAPIService_v2.3.7_QAIRT_v2.44.0_v73` 与 `GenieAPIService_v2.48.40_GenieService-win-arm64` 同时存在时:

| 请求 | 结果 |
|---|---|
| `install/2.44.0`(目录名里的 QAIRT 版本) | 删除 2.3.7 的安装 |
| `install/73`(驱动标签) | 删除 2.3.7 的安装 |
| `install/Genie` | 两个都删 |
| `install/e` | 全部删光 |
| `install/2.3`、`3.7`、`2`、`2.3.`、`2.3.7.1` | 404(数字/点边界挡住了) |

**影响**:界面只传真实版本号时一般没事;但接口本身可以被任意字符串误删,且与 `local-status` 里"从目录名正则解析版本"
的口径不一致。叠加 SVC-ISS-03(删除前不会真正停掉服务),误删正在使用的安装的后果更重。

**建议**:删除时复用 `_INSTALLED_VER_RE` 解析出目录的版本,按**解析出的版本等于请求版本**来匹配;
并要求请求值符合版本格式。

---

## SVC-ISS-03 "服务运行中"检测依赖 `wmic`,在本机已失效

**位置**:`filesystem_installer._processes_under`(调用 `wmic process where "ExecutablePath like ..."`),
被 `is_installed_service_running`(`GET /install/{v}/running`)和 `_kill_processes_under`
(删除/重命名失败后的"杀进程再重试")使用。

**现象**:本机 `wmic` 不存在(`Get-Command wmic` 为空)。函数捕获 `OSError` 后返回空列表,于是:
- `/api/versions/install/{v}/running` **永远返回 `false`**;
- 删除一个**正在运行**的服务时,"杀占用进程再重试"这个兜底**永远不会触发**,只会得到 WinError 5 的失败。

**影响**:`wmic` 在新版 Windows 11 上默认已移除(本机即如此)。界面"服务正在运行,删除前将停止"的提示不会出现;
`stop_running=true` 仍可走优雅停止钩子(`service_stopper`),但对该钩子管不到的孤儿进程没有兜底。

**为什么没有用例固定**:要证明它,需要一个真正运行在安装目录下的进程;这需要关掉后端的原生文件守卫才能起进程,
负责人已定不这样做。目前只有 `test_installed_version_running_probe_shape` 固定了"无进程时返回 false"。

**建议**:改用不依赖 `wmic` 的方式枚举进程(例如 `psutil.process_iter(['pid','exe'])`),再按 `exe` 路径前缀过滤。
注意:产品代码里已有 `import psutil`(`exec.py`、`background_process/kill.py`),但我没有在产品依赖清单里找到它,
采用前需确认它是否是正式依赖;否则可用 PowerShell `Get-CimInstance Win32_Process` 或 `taskkill` 的过滤能力。

---

## SVC-ISS-04 `local-status` 是读接口,却会写 `forge_config.json`

**位置**:`GET /api/versions/local-status` → `FileSystemLocalStatusScanner.scan_versions`,
`on_auto_configure=write_genie_root_path`。

**现象**:
- 当 `genie_service.root_path` 未设置、且 `data/bin` 下有安装时,**第一次读**就把"数值上最新"的安装写进 `forge_config.json`。
  比较按数字(10.0 胜过 9.0),不是按安装时间;第二次读不再写。
- 该指针**永不修复**:删除这个安装后,`root_path` 仍指向已不存在的目录。

**影响**:这个值决定 Service 页编辑哪份 `service_config.json`(见 `test_service.py` 中
`test_unusable_forge_root_masks_the_real_install`)。指针失效后,`/api/config` 回退成默认值(保存返回 412),
而 `/api/service/status` 仍能靠扫描 `data/bin` 找到真实安装,两者互相矛盾。

**建议**:删除安装时,若 `root_path` 指向被删目录就清空(或重新指向剩余的最新版本);
`local-status` 保持只读,自动配置挪到明确的"安装完成"动作里。

---

## SVC-ISS-05 下载设置:整体替换且无校验

**位置**:`PUT /api/versions/download-settings`。

**现象**:
- 请求体里省略的字段回到默认值(`PUT {}` 会把所有字段重置),不是补丁语义。
- `fetch_timeout_seconds: 0` 读回为 15;`download_timeout_seconds: -5`、`version_list_url: "ftp://x"`、
  `catalog_url: "not a url"` 都原样保存。
- 空 URL 读回为 GitHub 默认地址(`v2.34.0/release_manifest.json`)。
- 这些设置存放在 `forge_config.json`,而 `GET /api/forge-config` 读的是另一个存储,**永远看不到**
  `version_check` / `download` 段。

**影响**:UI 只改一项就保存会把其它项重置;非法 URL 要到下次拉取目录时才以 503 报错。

**建议**:改为补丁语义或让 UI 总是整体提交(并在文档里写明);对超时做范围校验、对 URL 做 http(s) 校验。

---

## SVC-ISS-06 发布清单 `tags` 为字符串时被拆成字符

**位置**:`http_catalog_source._parse_release_manifest`,`tuple(str(t) for t in raw.get("tags", []) if isinstance(t, str))`。

**现象**:清单里 `"tags": "beta"` 解析成 `["b","e","t","a"]`;列表形式正常。模型目录的 `tags` 解析同样写法。

**影响**:清单是远端提供的,格式出错时界面会显示一串单字符标签。

**建议**:先判断 `isinstance(raw.get("tags"), list)`,否则当作空。

---

## SVC-ISS-07 安装会删掉调用方传入的源 zip

**位置**:`FileSystemArtifactInstaller.install_service` → `_delete_zip_after_install`。

**现象**:安装成功后源 zip 被删除;若其父目录因此变空,父目录也被删除(父目录里还有别的文件则保留)。
接口返回 `zip_deleted: true`。

**影响**:如果 `save_path` 指向用户自己的文件(而不是下载中心下载的文件),文件会被删。
这是有意设计的行为(与 V1 一致),记在这里是因为接口并不限制 `save_path` 必须位于下载目录之内。

**建议**:至少只在 `save_path` 位于下载目录内时才删除;或在界面文案中明确。

---

## SVC-ISS-08 删除模型按名字前缀匹配,会删掉别的模型

**位置**:`DELETE /api/service-catalog/install/{model_id:path}` → `FileSystemArtifactInstaller.delete_model` →
`_model_dirs_matching`。规则:目录名等于 `model_id`,**或**以 `model_id` 开头且下一个字符是 `-`、`_`、`.`。
代码注释说明这是有意的,为了兼容旧的"每个平台一个目录"布局(如 `qwen3-8b-8480-qnn2.44`)。

**现象**(临时数据目录里实测):
- `models/` 下有 `qwen3`、`qwen3-8b`、`qwen3-vl-8b`、`qwen3_x`、`qwen3.y`、`qwen30`、`qwen`、`other`、`qwen3-8b.bak`。
  `DELETE install/qwen3` 之后,前五个全被删,剩下 `other`、`qwen`、`qwen30`、`qwen3-8b.bak` 和一个同名前缀的普通文件。
- `models/` 下有 `llama`、`llama-2-7b`、`llama-3-8b`、`llama-3-8b-instruct`。`DELETE install/llama` 后四个全被删,返回 200。
- 按同一条规则可推出:删除 `llama-3-8b` 会连带删掉 `llama-3-8b-instruct`(已用 `test_deleting_a_base_model_also_deletes_its_instruct_variant` 实测确认)。

**影响**:模型是用户下载的大文件,删除不可恢复。目录名是"模型 id + 变体后缀"的命名越常见,误删越容易发生。
界面传的是目录卡片对应的 id,所以关键看目录卡片上的 id 与其它目录的前缀关系;接口本身没有任何保护。

**建议**:只在"旧版每平台一个目录"这种已知后缀形态下才按前缀匹配(例如后缀必须形如 `-<数字>-qnn<版本>`),
或者先精确匹配,精确匹配到了就不再做前缀匹配。

---

## SVC-ISS-09 删除模型时 `delete_zip` 会删到服务版本的下载包

**位置**:同一接口的 `delete_zip=true` 分支(默认值),对 `downloads/` 用同样的前缀规则。

**现象**:`downloads/` 下有服务的下载包 `2.48.40/`、`2.3.7/`,以及模型包 `qwen3-8b/`、`7/`。
`DELETE /api/service-catalog/install/2` 返回 200,把 `2.48.40`、`2.3.7` 里的 zip 都删了,`qwen3-8b/` 和 `7/` 保留。
`DELETE install/2.48` 同样会删掉 `2.48.40` 里的 zip。`?delete_zip=false` 时不会碰下载目录,返回 404。

**影响**:服务版本和模型共用 `downloads/` 目录,而两边的目录命名体系不隔离。
`model_id` 恰好是某个版本号的前缀(`2`、`2.48`)时,会误删服务版本的下载包。
实际 model_id 是否会取这类值我没有核实,所以严重度给"中"。

**建议**:模型包与服务包放在不同子目录,或者删除时只匹配模型自己的目录。

---

## SVC-ISS-10 模型重装的备份目录名被截断

**位置**:`FileSystemArtifactInstaller.install_model`,`bak_dir = dest_dir.with_suffix(".bak")`。

**现象**:对名字里带点的目录,`with_suffix` 会把最后一个点之后的部分当成扩展名替换掉:
`qwen3.5` 的备份名是 `qwen3.bak`,不是 `qwen3.5.bak`。重装时 `if bak_dir.exists(): shutil.rmtree(bak_dir)`
会**先把这个同名目录整个删掉**。实测:`models/` 下有用户自己的 `qwen3.bak`(里面有文件),
重装 `qwen3.5` 之后,该目录和里面的文件都没了。

**同一个问题已在服务安装里修过**:`install_service` 里的注释写着"`with_suffix` 会把 `2.3.7` 的 `.7` 当成扩展名截断",
改成了 `dest_dir.name + ".bak"`。模型安装这一处漏改了。

**还有一个潜在问题(没有复现,只是推断)**:`model.1` 和 `model.2` 的备份名都会变成 `model.bak`,
如果两者的重装交错发生,一方的备份会被另一方覆盖。我测的"损坏 zip 重装后回滚"那一条没问题(`model.1` 内容完整恢复),
但那次没有并发。

**建议**:改成 `dest_dir.with_name(dest_dir.name + ".bak")`,与服务安装一致。

---

## 已核对、没有复现:模型删除接口的路径穿越

对应前面 SVC-ISS-01 的"同类需检查"。结论:**`DELETE /api/service-catalog/install/{model_id}` 没有路径穿越**。
原因:它不拼路径,而是遍历 `models/` 和 `downloads/` 的子项,用名字比较来挑目录。
这些结论已用回归用例固定(`test_service_catalog_models.py` 的"no path traversal"一节和 `test_install_dir_hint_is_reduced_to_one_safe_segment`)。

实测(数据根目录放了 `sentinel/f.txt` 和 `stray.zip`,临时目录里放了 `outside/`):
`..`、`.`、`../sentinel`、`sentinel/..`、`keep/..`(这些在到达处理函数前就被路由挡成 405)和
`%2e%2e`、`%2E%2E`、`..%5Csentinel`、`a%5C..%5Csentinel`、`..%2Fsentinel`、`%2e%2e/sentinel`、绝对路径
(`C:\Windows`、临时目录的 `outside`)、`%2F`、`%5C`(这些都是 404)之后,
`sentinel`、`stray.zip`、`outside` 全部完好。

`POST /api/service-catalog/install` 的 `install_dir` 也不能逃出 `models/`:
`..`、`...`、`a/..`、`../evil`、`..\evil`、`C:\evil`、`evil/..`、空白都被净化成单一合法目录名,
要么落到 `models/evil`,要么退回用 zip 文件名。临时目录里没有生成 `evil`。

`POST /install` 里压缩包内部的 `../` 条目也已测过:不会写到安装目录之外(`test_install_zip_entries_with_dot_dot_cannot_escape`)。
注意这条只证明了"文件没有逃出"。我没有查看解压细节为什么安全(`zipfile.extractall` 自带清洗路径),以后换解压方式时这条回归用例会报警。

---

## SEC-ISS-01 应用策略模板会让后端自己退出(退出码 75)

**位置**:`POST /api/security/templates` → `ApplySecurityTemplateUseCase.execute` → `UpdatePolicyUseCase.execute(..., reboot_reason=...)`,
未传 `auto_reboot`,取默认值 `True`。手工 `PUT /api/security/policy` 的路由则显式传 `auto_reboot=False`。

**现象**(自管后端实测,`_owned_backend.py`):
- 应用 `demo`(1 条规则)或 `development`(2 条):响应 200,`policy.needs_reboot` 为 **false**;约 **8.4 秒后进程以退出码 75 退出**,
  日志有 `reboot.scheduled`,原因 `applied template development`。
- 应用 `strict`(0 条规则、规则集未变):不退出。
- 手工 `PUT /policy` 换成 1 条规则:响应 `needs_reboot: true`,等 20 秒进程**不**退出。

**影响**:退出码 75 是"请监督进程重启我"的约定(`Start.bat` / `apps.cli.serve` 会重启)。不在监督进程下运行时(直接
`python -m apps.api`,或 E2E 的后端),后端就此消失;前端只弹一个成功提示,用户看不到任何重启信息。
同一次规则变更,模板路径与手工路径处理相反,且模板路径的响应字段与实际行为矛盾。

**建议**:模板用例改传 `auto_reboot=False`,响应带上 `needs_reboot`,与手工路径一致;或让两边都由前端确认后再重启。
**回归**:`test_secu_templates_restart.py` 里的 `test_applying_a_template_with_rules_exits_the_backend_with_75_although_the_response_says_no_reboot`
会在修复后失败,按修复后的行为改断言。

---

## SEC-ISS-02 skill 路径没有范围检查,且对所有主体生效(安全)

**位置**:`POST /api/security/skills/register`(`SkillCapability`)与 `PUT /api/security/skill_policy/{name}`,经
`CheckPermissionUseCase` 的"skill 能力放行"分支。

**现象**:
- 注册 `read_paths=["C:\\e2e-zone\\sub"]` 之后,**任何**主体(`user` / `preset` / `system`,任意标识)读该路径都被 allow;
  写路径同理。路径是按目录边界匹配的(名字以 `-evil` 结尾的同级目录不被放行,读路径不授予写)。
- `read_paths=["C:\\"]` 被接受(201)。此后任意主体读 `C:\` 下**任意**路径都是 allow(对照:未注册时 `C:\Users\Public\x.txt`、
  `D:\...`、`C:\Program Files\...` 都是 deny)。注销后恢复 deny。
- `PUT /skill_policy/<任意名字>` 同样接受 `read: ["C:\\"]`,效果相同。
- 对比:权限请求流在 `grant_range=directory` 时拒绝把过浅的目录(盘根、一层目录)扩成整棵树,这里没有同类保护。

**影响**:能调用这两个接口的调用方,一次请求即可让整个盘符对所有主体可读/可写,绕过按路径授权。
**未核实**:这两个接口开启认证时是否需要登录、缺 CSRF 令牌是否被拒、绑定 `0.0.0.0` 时的暴露面 —— 测试后端关闭了认证。严重度按
"服务端完全不校验范围"定,不是已演示的远程利用。

**建议**:对注册和覆盖的路径复用 `_dir_grant_path` 同类的深度下限;限制能力只对该 skill 自己的主体生效。

---

## SEC-ISS-03 `skill_policy` 覆盖不需要对应的 skill,保存即生效

**现象**:对不存在的名字 `PUT /skill_policy/<name>` 返回 200,条目显示 `active: false`,但 `permission/check` 已经按它放行
(读路径 allow,写路径写 allow,写路径不授予读)。仅被发现、未注册的内置技能(如 `weather`)也一样:`GET` 先是 404,保存覆盖后变 200
并开始放行,清空覆盖后回到 404 与 deny。

**影响**:覆盖是一条独立的放行通道,不依赖任何已声明的能力;界面把它标成"未激活"容易让人以为没生效。

---

## SEC-ISS-04 内置功能可以被 `DELETE /skills/{name}` 注销

**现象**:`DELETE /api/security/skills/app-builder` 返回 204,随后 `app-builder` 从 `list_active` 消失,发现列表里 `active: false`、
`read_paths` 为空,`skill_policy/app-builder` 变 404,它自己路径上的读权限也不再放行。重新注册后恢复。
未知名字、已删名字同样恒返回 204。

**影响**:一个请求就能撤销内置功能的授权;204 对不存在的名字也成立,调用方无法区分"删了"与"本来就没有"。

---

## SEC-ISS-05 空 `requested_mask` 返回 500

`POST /permission/request` 与 `POST /permission/check` 在 `requested_mask` 一个位都没置时返回 500
(`internal.unexpected`)。其余字段错误都是干净的 400。根因是领域层 `ValueError` 没有映射成 4xx(与 MH 的同类问题一致)。

---

## SEC-ISS-06 授权列表看不出允许还是拒绝

`GET /path-grants` 的字段是 `grant_id / subject / path / mask / source / created_at / expires_at`。库里有 `effect`(allow/deny)、
`scope_kind`、`scope_key`、`is_directory`、`is_program`,线上都没有。记住的拒绝(`effect=deny`)与记住的允许在界面/客户端看起来一样;
`directory`/`program` 范围也无法区分。

---

## SEC-ISS-07 运行时配置:手工编辑文件的读回

- `forge_config.json` 里 `security_runtime_config.ssl_verify = "false"`(字符串)被读成 **True**(`bool()` 转换)。
- 文件里可以存下低于 PUT 下限的值(`read_max_lines: 0`),`GET /runtime-config` 原样返回。
- 无法解析的文件回退到默认值,下一次 PUT 把文件重写成只含这次提交的内容(其他区块被丢弃,无备份)。

---

## SEC-ISS-08 至 SEC-ISS-12(低)

- **08**:`PUT /mode permissive` 后,`/health.mode` 与 `/policy.mode` 仍是 `enforce`;只有 `effective_run_mode`(PUT 响应里)和真实判定变化。
- **09**:skill 名没有字符规则;含 `/` 的名字能注册,但 `DELETE /skills/a/b` 匹配不到路由,无法经 HTTP 删除。skill 体里的破坏性
  shell 文本(如删除根目录、解码后管道给 bash)不告警,只有 prompt-injection 类的高危模式被拦截(400)。
- **10**:通道没有删除接口(405);`GET /channels` 在首次保存前为空;名字只接受 `background cli feishu web wechat`(小写)。
- **11**:`PUT /policy` 每次都递增版本,即使规则完全相同;只有规则集真的变了才报 `needs_reboot`。
- **12**:注册 skill 的 `trusted_binaries` 与覆盖里的 `trusted_binaries` 会被存储并返回,但 `permission/check` 对该程序的 exec 判定
  仍是 deny。要么 exec 侧另有前置条件(未发现),要么这个字段目前没有效果。

---

## UPL-ISS-01 上传的 `conv_id` 可路径穿越(安全)

**位置**:`POST /api/uploads/model` 与 `/dataset` 的表单字段 `conv_id` → `FilesystemUploadStore.save`:
`target_dir = self._base_dir / category.value / conv_id`,没有任何校验。

**现象**(实测,上传根 = `<data>\blobs\uploads`):

| conv_id | 落盘位置 |
|---|---|
| `../../x` | `blobs\x\`(上传根之外) |
| `..` | `blobs\uploads\`(类别目录的上一级) |
| `../../../../x` | `<工作目录>\x\`(数据目录之外) |
| `../../../../../../x` | `C:\Users\<用户>\AppData\Local\x\`(离开临时目录整整一级) |
| `C:\x` | `C:\x\`(绝对路径,直接写到 C: 根下) |

写入内容是请求体的字节,文件名是 `<ULID>_<清洗后的文件名>`,因此**无法覆盖已有文件**,但可以在任意目录下创建任意内容的文件(包括创建目录)。
`DELETE /api/uploads/{id}` 会删除这个文件(索引记的是真实路径),返回和列表里也如实显示,没有隐藏。

**影响**:调用方能选择落盘目录。写到 `Startup` 文件夹、某个模块的搜索路径、或其它会被主动加载的位置时可能升级为代码执行(文件名受限于 `<ULID>_` 前缀,
所以直接覆盖入口文件不行)。**未核实**:开启认证时这两个接口是否需要登录、缺 CSRF 令牌是否被拒,以及绑定 `0.0.0.0` 后的暴露面——测试后端关闭了认证。

**测试安全说明**:探测阶段的两次实验真的写到了临时目录之外(`C:\Users\<用户>\AppData\Local\escape7\` 与 `C:\x\`),
已确认内容和文件名都是测试自己的后手动删除。正式用例只用 `../../`(最多退到 `blobs\`,仍在一次性数据目录内),绝对路径用一个未挂载的盘符来证明"值直达文件系统"。

**建议**:`conv_id` 只允许 `[A-Za-z0-9._-]{1,64}`,且拒绝 `.` / `..`;落盘后用 `resolve()` 确认仍在上传根内。

---

## UPL-ISS-02 过长的文件名 / 不可用的 `conv_id` 返回 500

- 文件名:存储名是 `<26 位 id>_<最多 200 字符>`,但整条路径没有对 Windows 260 限制做检查。在本机的数据目录长度下,
  150 字符的文件名成功(路径 256),160 字符起返回 500(`FileNotFoundError`)。阈值随数据目录长度变化。
- `conv_id` 为空格、超过约 140 字符、或含 `:`:`mkdir` 抛 `FileNotFoundError`,返回 500。
- 后端进程不受影响,没有残留文件。错误应该是 400/413 而不是 500。

---

## UPL-ISS-03 重启后上传索引重建丢信息

索引是内存里的字典,启动时从磁盘重建(只扫 `<类别>/` 与 `<类别>/<conv_id>/` 两层):
- `conv_id="a/b"` 的文件在 `model/a/b/`,重建不扫这一层:**文件还在磁盘上,列表里没了,`DELETE` 返回 404,再也删不掉**。
- 原始文件名丢失:重建用磁盘上的清洗后名字,`My File (1).png` 变 `My_File__1_.png`,`中文名.png` 变 `___.png`。
- `created_at` 变成文件修改时间。

---

## UPL-ISS-04 通用代码上传路由没有扩展名白名单

`POST /api/upload/code`(旧路由)只接受代码/文本扩展名并限 10 MB;`POST /api/uploads/code`(通用路由)接受 `.exe`,上限 100 MB。
前端用哪个路由决定这是否重要。**已查 `frontend/src`**:聊天代码模式工具栏(`ModeFrameCoding.vue`)用旧路由 `/api/upload/code`;模型构建器工具栏
(`ModeFrameModelBuilder.vue`)用 `/api/uploads/model`、`/api/uploads/dataset`、`GET /api/uploads?conv_id=...` 和 `DELETE /api/uploads/{id}`。
所以通用代码路由的"无白名单"没有前端入口;而 UPL-ISS-01 的 `conv_id` 正是模型构建器工具栏真实使用的参数(由前端填入当前会话 id),
但任何直接调用 API 的人都能自己传任意值。

---

## EVT-ISS-01 WebSocket 不经过登录门(安全)

**位置**:`AuthMiddleware`(`interfaces/http/middleware/auth.py`)是 Starlette `BaseHTTPMiddleware`,**只处理 HTTP 作用域**,WebSocket 握手不经过它。
`csrf.py` 的文档也明确写着 WebSocket"intentionally NOT covered",并要求"需要来源保护的 WS 路径,处理函数必须自己加";
`/api/ws/events` 的处理函数没有加(整个 `interfaces/http/routes` 里只有 `mb_pro_session.py` 和 `auth.py` 用到会话校验,都不是事件流)。

**现象**(登录门开启的后端,实测):
- `GET /api/events`(SSE):无会话 401;垃圾/签名错误/过期的 cookie 401;有效会话 200。正常。
- `WS /api/ws/events`:无 cookie、垃圾 cookie、错误密钥签名、过期 cookie **全部握手成功**;`Origin` 为空、本机、`http://evil.example`、`null` 全部握手成功。
- 握手后匿名客户端**收到真实事件**:登录用户创建一个会话,客户端收到会话标题;登录用户发起权限请求,客户端收到请求人和路径。
  跨源(`Origin: http://evil.example`)同样收到。
- 其它 4 个聊天 WebSocket(同样无 cookie 、跨源探测):`/api/chat/control` 握手成功并收到 `hello`;
  `/api/chat/subagents/{id}/ws` 与 `/api/chat/active-runs/{tab}/ws` 握手成功后返回应用层错误帧(说明处理函数已执行);
  只有 `/api/chat/ws` 返回 403。HTTP 孪生接口 `GET /api/chat/conversations` 无会话是 401。
- 未探测的 WebSocket:`channels.py` 的 `qr/events`、`mb_pro_session.py` 的 `events/ws`、`model_catalog.py` 的 `download/{job_id}/ws`、
  App Builder 的 `package/{job_id}/ws`、`runs/{run_id}/ws`、`weights/download/{job_id}/ws`。同一机制下很可能同样开放,但**没有验证**。

**影响**:产品注释把部署定义为"loopback-only",暴露限于本机。但浏览器对 WebSocket 不执行同源策略:用户浏览器里打开的任意网页都能
连 `ws://127.0.0.1:<端口>/api/ws/events` 并读到事件流(会话标题、权限请求里的路径和请求人等)。控制通道 `/api/chat/control` 的具体能力没有继续探:
它返回了协议 `hello`,后续消息能否被匿名客户端执行需要单独审计。
**未核实**:绑定 `0.0.0.0` 后的暴露面;真实浏览器里跨源 WS 是否受 `canonical_host` 等其它保护影响(我用 Python 客户端设置了 `Origin` 头)。

**建议**:在 WebSocket 握手阶段统一校验会话 cookie 和 `Origin`(最省事的是一个 ASGI 级的守卫,覆盖所有 `websocket` 作用域);
修复后 `test_events_auth_gate.py` 里所有"accepted"断言会失败,按新行为改。

---

## INST-ISS-01 `qai install-pack-deps --help` 不是帮助,而是真的在安装依赖

**位置**:`apps/cli/commands/install.py` 把 `install-qairt` / `install-pack-deps` / `uninstall` / `compile-factory` / `api` 都注册为 `add_help=False` +
`REMAINDER`,把所有参数转给脚本。`scripts/setup/install_app_builder_deps.py` 的 `main(argv)` **不解析参数**(`argv` 根本没用到)。

**现象**:`qai install-pack-deps --help` 直接执行 `uv pip install --python <产品 venv> ...`,输出 `Using Python 3.13 environment ...`、
`[INFO] ...uv.exe pip install ...`、`[OK] App Builder Pack deps installed: 14 packages from 3 Packs`,退出码 0。本机依赖已齐,uv 只报
`Checked N packages`(已验证 `site-packages` 没有任何变化),所以没造成伤害;但在缺依赖的机器上它会**联网下载并安装**。
按惯例用户会把 `--help` 当成只读操作。所以这条已经在测试里列为"绝不调用",我是在探测阶段跑了一次才发现的。

**建议**:`main(argv)` 至少要有一个 `argparse` 解析器(`--help`/`--dry-run`);或者让 `install-pack-deps` 自己处理 `-h`。
同类需检查:另外几个委托命令里 `install-qairt`、`uninstall` 已核实会先走 `argparse`(`--help` 安全);`api` 同理(打印 `usage: python -m apps.api`)。

---

## INST-ISS-02 `qai compile-factory` 已经不能运行

`qai --help` 列出 `compile-factory`("compile factory_source/* into factory/* seeds"),但它委托的 `scripts.build.compile_factory` 不存在
(`scripts/build/` 里只有 `model_builder_cli.py`)。任何参数(包括 `--help`)都得到原始 Python traceback 和退出码 1:
`ImportError: cannot import name 'compile_factory' from 'scripts.build'`。

---

## INST-ISS-03 其它观察(低)

- `qai config show --help` 退出码 2:`show` 不是子命令(真正的是 `get` / `set` / `setup` / `provider`)。没有需要修的,记在这里避免后人再踩。
- `qai config provider list` 的日志走 stderr,stdout 是纯 JSON(测试已固定这一点,脚本可以直接管道给 `jq`)。**只验证了这一个命令**(另有 `pack list`、`skill list`、`config get` 的 stdout 也解析为 JSON),其它命令没检查。
- 默认直接跑 `qai config provider list`(不设 `QAI_DATA__DATA_DIR`)会读写产品自己的 `data/`:它会初始化数据库、创建密钥环后端等。所以测试一律设置隔离目录并检查产品 `data/` 无变化。

---

## MB-ISS-01 `qairt_env.json` 不是 JSON 对象时,整个后端起不来

**位置**:`qai.platform.qairt_versions.resolve_qairt_installation`,`payload = json.loads(...)` 后直接 `payload.get("qairt_root")`;
`except (OSError, ValueError, TypeError)` **没有 `AttributeError`**。它由 `apps/api/di.py::_build_qairt_switch_service` 在构建 DI 容器时调用。

**现象**(自管后端实测):文件内容为 `[1, 2]`、`"just a string"`、`5`、`null`、`[{"qairt_sdk_root": ...}]` 时,后端启动即退出(退出码 1),
日志末行 `AttributeError: 'list' object has no attribute 'get'`(各类型换成对应名字)。对比:内容为 `{not json`(语法错误)或空文件时,
后端正常启动,版本列表回退到默认根目录。

**影响**:一个手误写成数组的配置文件会让整个应用无法启动,而不是像损坏文件那样降级。该文件由安装脚本生成,也有人会手改。
**建议**:`payload` 先 `isinstance(payload, dict)`,否则回退;或把 `AttributeError` 加进 `except`。
**回归**:`test_qairt_env_bad_file.py`(修复后会失败,按新行为改断言)。

---

## MB-ISS-02 / MB-ISS-03 QAIRT 版本目录的两个小问题

- **02**:`QairtVersion.parse` 接受前导零和全角数字(`02.53.0`→`2.53.0`、`2.049.0`、`２.４８.４０`),`GET /qairt-versions` 返回**原始目录名**;
  `preflight` 却拿**规范化后的** `str(target)` 和这份原始名单比较。结果:这类名字出现在选择器里,选了之后永远报"未安装"。
  真实 SDK 目录都是规范命名的,所以只会被手工建的文件夹触发。
- **03**:`read_text(encoding="utf-8")` 保留 BOM,`json.loads` 拒绝,于是带 BOM 的(记事本保存的)`qairt_env.json` 被静默当成"没有配置",
  当前版本变成 `null`。同类文件在本仓库别处(如 `service_config.json`)都用 `utf-8-sig` 读取。

---

## MH-ISS-01..05 模型下载(MH-02)

调查方式:本地 `http.server` 替身 + 仓库自带的 `data/bin/aria2c/aria2c.exe`,后端在临时 `cwd` 下运行。

- **MH-ISS-01(中,功能)下载落到的位置和 `verify` 读的位置不同。** 下载引擎没有配置 `download_root`,aria2c 把文件写到
  **后端进程工作目录**下的 `models/<target_filename>`(产品目录或你启动它的任何地方);`verify` 和块存储读的是
  `<数据目录>\blobs\<category>\<name>`。所以刚下载完的文件立刻 `verify` 会 500(`FileNotFoundError: blob not found`,没有映射成 404)。
  `_model_catalog_di.py` 里 `Aria2cRpcDownloadEngine(...)` 没有传 `download_root`,而引擎文档明确写着这个参数存在。
  **已核实**:我的一次探测默认以仓库为 `cwd` 运行,在真实的 `models/` 下留了 `e2e-mh02.bin` 和 `m1.bin`(已删除,内容和大小都核对过)。
  **未核实**:前端真实使用流程里是否有别的步骤把文件移过去(模型中心安装流程可能不走这条路径)。
- **MH-ISS-02(中)任务状态靠客户端读进度流才会更新。** 文件已完整下载,`GET /download-jobs` 仍报 `running`、`bytes_downloaded=0`、
  `total_bytes=null`,直到有人打开 `/download/{id}/progress`。不读流的话任务永远停在 `running`。列表接口因此不能当作进度来源。
- **MH-ISS-03(中)领域校验失败返回 500。** `target_filename`(`../x`、`a/b`、`a\b`、`x..y`)和 `verify` 的 `storage_category/name`
  (大写、空格、`..`、分隔符)触发 `StorageKey` 的 `ValueError`,没有映射,客户端看到 `internal.unexpected`。路径防护本身是生效的
  (没有文件写到工作目录之外),只是状态码不对,应该是 400。
- **MH-ISS-05(中,数据)被拒绝的下载请求仍留下一个 `queued` 任务。** `StartDownloadUseCase` 先创建/保存 job 再校验 `StorageKey`;
  上面那类 500 每次都留下一个永远不会执行也不会被清理的 `queued` 任务(会出现在 `download-jobs`)。同样,系统里没有 aria2c 时
  `POST /download` 返回干净的 503,任务也停在 `queued`。没有工具能清除它们(取消接口对 `queued` 的表现没有测)。
- **MH-ISS-04(低)取消不清理部分文件。** `DELETE /download/{id}` 停止传输(已用服务端计数验证),但 `models/<name>` 和
  `models/<name>.aria2` 控制文件都留在磁盘上。这也可以是有意设计(便于续传),但没有文档说明。

**测试环境上的注意点**:
- aria2c 守护进程监听固定端口 6810;其它程序占用时模块自动 skip。
- 为了不弄脏仓库 `models/`,自管后端现在支持 `start(cwd=..., path_prepend=...)`;模块起止都检查 `models/` 列表没变、没有 aria2c 进程残留。
- 运行过程中有两次测试结束时报"端口 8989 被一个 apps.api 占用"的中止(pid 不同)。**我没查明原因**:当时所有测试都已通过,
  之后重跑不再出现,端口随后也是空的。很可能是结束时某个继任的后端先于前一个释放端口就被下一个模块的检查看到,但这只是猜测。

---

## CHAT-ISS-01..04 聊天与 OpenAI 兼容接口

调查方式:后端在环境变量 `QAI_CHAT__LLM_BASE_URL` 指向本地假 OpenAI 服务(`tests/e2e/_fake_llm.py`),不写钥匙串。

- **CHAT-ISS-01(中)非流式 `/v1` 吞掉错误。** `StreamChatUseCase.collect_completion` 只读 CHUNK 和 END 帧,忽略 ERROR 帧。所以模型没有上下文长度、
  provider 没有 key 等情况,非流式调用得到 `200 {"choices":[{"message":{"content":""}}],"usage":{0,0,0}}`;同样的请求加 `stream:true` 则得到
  `data: {"error": {"code": "chat.model_context_length_missing"...}}`。客户端看不到失败。
- **CHAT-ISS-02(低)错误信封不是 OpenAI 的。** 这三个路由按 `08-business-capabilities.md` 是要与 OpenAI 一比一对齐的对外契约,但错误体是
  `{"type","code","message","details"}`。
- **CHAT-ISS-03(低)`/v1/models` 和模型选择器不一致。** 列表是「模型目录条目 + `qai-default`/`qai-fast`」,不包含 provider 配置里的模型。
  另外每次 `/v1` 调用都会在聊天列表里留下一个会话和一个标签(有测试固定)。
- **CHAT-ISS-04(低,待确认)本地端点不转发采样参数。** 原因是本地端点走 `LocalModelStreamAdapter`,云端走 `HttpOpenAICompatibleLLMStream`;
  前者不带这三个字段。我没查清这是不是有意设计(GenieAPIService 自己决定采样很可能),所以标"待确认"。

**没有做的真实环境测试**:唯一候选 `qai-service` 需要 SSO 登录;本机钥匙串里没有任何云端 key,也没装 GenieAPIService。

**测试过程中的一个失误(我的责任,不是产品问题)**:FEATURE-INVENTORY 的 Model Hub 节早就写明了"`PUT /providers` 带 `api_key` 会写 Windows 凭据管理器",负责人也已决定不写 keyring。
我在探测时仍然发了一次带 `api_key` 的 `PUT /api/model-catalog/providers/e2e-llm`,于是产品把它存进了系统钥匙串(`qai.model_catalog.provider` / `e2e-llm`)。
我核对过那个值正是我自己的探测字符串后删除了;用户原有的 `generic_cloud` / `qgenie` 两条凭据没动(我只读了它们的长度来确认可读,这也没必要)。
所有聊天模块都在结束时检查钥匙串里没有新增云端 key;正式用例里从不发带 key 的 provider 配置。

---

## TEST-ISS-01 带 `qairt_env.json` 的后端被强停后偶尔残留 worker 和一个锁定的临时目录

**这是测试基础设施的问题,不是产品缺陷;我只查清了一部分。**

- 事实:后端只要有 `qairt_env.json`,启动时(`lifespan._spawn_sticky_worker`)就会预启动 App Builder sticky worker
  (`_runner_bootstrap.py --persistent`,约 78 MB)。`preflight` 里 `sticky_worker_running: true` 就是它。没有该文件的后端不起 worker。
- 事实:MB-07 的几次运行后,留下 2 个 worker 进程(启动于 23:01 和 23:12,父进程都已不存在,环境变量证明是 e2e 后端的),
  以及一个只含单个 2 KB `.pyc` 的 `qai-e2e-*` 临时目录。进程已**终止**(退出码 0、1 个线程、CPU 不再变化),但进程对象还在,
  `Remove-Item` 报文件被占用。`taskkill /F` 说"进程不存在"。它们在 Job Object 里(`IsProcessInJob=True`)。
- 事实:之后连续 4 轮"启动→停止"(分别停在启动后 0/3/8/14 秒)**没有再产生任何残留**,所以不是每次都发生。
- **没查清**:是谁持有这两个进程对象的句柄。候选是 VS Code 派生的常驻 pwsh,我没法确认。也没确定为什么只有这两次。
- 已采取的缓解:`owned_backend` 现在对临时目录做带重试的删除(`_remove_tree`),并且永不抛错;进程对象本身我杀不掉,
  会在持有句柄的进程退出后由系统回收。
- **留给你**:那个残留目录 `%TEMP%\qai-e2e-c_wusuve`(2 KB)和两个已终止的进程对象会在持有句柄的进程退出后消失,重启 VS Code 或重启机器可以清掉。

---

## TEST-ISS-02 偶发的连接重置 / 读超时(**已定位并修复,2026-10-20**)

**根因是产品缺陷,不是测试基础设施:未处理异常返回 500 之后,Starlette 把异常重新抛出,uvicorn 随即关掉连接,正在读响应的客户端偶尔收到连接重置而不是 500。**

**事实(2026-10-18,win-arm64):**

- 一次整套运行加上之后的几次重跑,共出现 **10 次**失败,全是 `httpx` 连接层错误,没有断言失败:
  `ReadError [WinError 10054]` 7 次、`[WinError 10053]` 2 次、`ReadTimeout`(30 秒)1 次。
- **9 次发生在“发一个会让服务端抛未捕获异常、并断言返回 500 `internal.unexpected`”的用例上**
  (对应 MH-ISS-03 和 APP-ISS-14)。另 1 次是 `test_qairt_switch` 的 preflight 用例(读超时,请求本身是正常的 200 路径),与本根因无关,**没有单独查**。

**定位(2026-10-20):**

- 失败当时后端存活,服务端日志里 500 已经正常记录;客户端读响应时连接被切断。
- `add_exception_handler(Exception, ...)` 在 Starlette 里挂在最外层的 `ServerErrorMiddleware` 上:它先发出处理器的 500 响应,**随后把异常重新抛出**,uvicorn 看到后关闭连接。
- 把 `test_a_manifest_file_that_is_valid_json_but_not_an_object_crashes_the_dry_run` 单独连跑 15 轮,**2 轮失败**(约 13%),失败的参数每次不同(`string`、`list`、`number`)。
- 用一个独立的最小 FastAPI 程序复现(只有一个抛异常的路由):原写法 400 次请求里 133 次出现连接错误,改用纯 ASGI 中间件自己返回 500、不重新抛出后是 **0 次**。

**修复:** `interfaces/http/error_handlers.py` 新增 `UnexpectedErrorMiddleware`(纯 ASGI,不是 `BaseHTTPMiddleware`),`apps/api/main.py` 在所有中间件之后挂载,使它成为最外层。响应已经开始发送后(例如流式响应中途出错)无法再改响应,此时异常原样重新抛出。单元测试 `tests/unit/test_unexpected_error_middleware.py` 共 5 条;临时去掉挂载那一行会有 2 条变红,恢复后通过。

**验证:** 修复后同一条用例重复跑 30 轮,0 轮失败;随后整套(2009 条)0 失败,日志里没有任何连接重置 / 中断。

**范围与边界:**

- 这不只影响测试:产品里任何未处理的异常,都可能让真实客户端(浏览器、CLI)偶尔拿不到 500。
- **过去的怀疑“和 `guard64.dll` 崩溃有关”**:按时间对齐后没有看出相关性,根因也不是它。
- `test_qairt_switch` 那 1 次读超时没有查清,修复后整套未再出现。
- 我没有在修复前的代码上再跑完整套来对比,只做了上面的单用例频次对比和最小复现。
**(10-19 的历史记录)重跑结果:1922 条中 1921 通过、1 失败、0 跳过、没有遗留进程(1 小时 5 分)。连接重置 / 读超时当时一次都没有复现,所以当时根因仍未知;该根因已在 2026-10-20 定位并修复,见本节开头。**

**这次抓到的一条失败是另一件事**:`test_secu_runtime_restart.py::test_the_exit_code_is_75_for_every_reboot_class_field`。它期望后端改了“需重启”的字段后以退出码 75 退出,实际退出码是 `0xC0000005`(访问冲突)。留下的证据:

- 后端日志走完了正常关闭流程,最后一行是 `Finished server process [16228]`(09:26:36.429)。
- Windows 事件日志在 09:26:36.899(比那一行晚 0.47 秒)记录了 `python.exe` 崩溃,进程 id `0x3F64` = 16228,**就是这个后端进程**;故障模块 `vendor\bin\arm64\guard64.dll`,异常码 `0xc0000005`,偏移 `0x320c4`。
- 所以是后端在**退出的最后阶段**被 `guard64.dll` 里的一次访问冲突终止,真实退出码被盖掉。关闭日志里还有一行 `worker did not exit within 5.0s, killing pid=19424`(sticky worker 用了 5 秒才被杀)。
- 同一个用例单独连续跑 8 次全部通过(每次耗时约 23 秒。其中一次 40 秒),所以它是**偶发**的。这 8 次每次也有 2 条 `guard64.dll` 崩溃事件,我没查是哪些进程崩的,所以不能说“崩溃”本身就是失败原因,只能说这次崩溃的进程恰好是后端自己。

**这次的结论和边界:**

- 这不是测试基础设施的问题,像一个**产品缺陷**(guard64 在进程退出时偶发崩溃),影响是“监督进程看到的退出码不是 75”,可能让“重启即恢复”的机制误判。但我没有验证监督进程实际怎么处理 0xC0000005。
- 崩溃的根因(guard64.dll 里的哪一步)我没查,没有符号和 dump。`guard64.dll` 是预编译产物(`vendor/` 被 gitignore),修复需要它的维护者。
- 该用例没有标 `KNOWN GAP`,也没有把这个行为固定下来。目前继续保留它的失败语义(退出码不是 75 就失败),这正是它该报的。

---

## 与 SVC-03 相关的已知现状

服务配置读写(`/api/config`)那一批现状(未安装时 `start`/`load-model` 返回 500、"读取→原样保存"会抹掉只存在于
文件里的 `api_key`、`root_path` 失效时配置与状态不一致、保存不做结构校验、配置文件为 JSON 数组时 GET 返回 500)
已写在 `FEATURE-INVENTORY.md` 的 3.7 节,对应用例在 `test_service.py`。其中**"读取→原样保存会抹掉 `api_key`"**
会让用户丢失手写/安装包写入的密钥,建议与上面的高严重度项一起排期。
