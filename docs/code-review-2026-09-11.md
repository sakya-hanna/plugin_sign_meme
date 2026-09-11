# sign_meme 代码审查报告（2026-09-11）

- 范围：main 分支 5c7ad42 + 当前工作树（pages/举牌模板/index.html 未提交重构 +199/-224，docs/、tests/test_webui_asset_library.py 未跟踪）
- 方法：全量通读 main.py / backend/*.py / 前端 index.html / tests / 配置，关键疑点均在容器内实机验证（pytest 62 passed 基线一致）
- 结论先行：架构与安全面整体扎实，但有 1 个已造成实际数据泄漏的确认 bug、1 个降级链路整体失效的设计矛盾、以及若干一致性/健壮性问题

---

## 一、确认问题（按严重级别）

### H1 ｜ integrated 临时成品图必然泄漏（confirmed，已有实证）

- 位置：backend/integrated_events.py:330（置空）vs :354-370（清理读取同一 key）
- 链路：`on_decorating_result_first` 在追加图片**之前**执行 `event.set_extra(EXTRA_RENDERED_PATH, None)`；而 `after_message_sent` 靠读同一个 `EXTRA_RENDERED_PATH` 找临时文件清理 → 永远读到空 → 永不清理。
- 实证：`plugin_data/sign_meme/generated/` 现存 5 个孤儿 PNG 共 5.7MB（09-10 11:47~23:22，均为 ~1.1MB 成品图），与当日 integrated 实测轮次吻合；日志中 integrated 路径无一条 `temporary_cleanup_succeeded`。
- 影响：每轮 integrated 举牌泄漏 ~1.1MB，无 TTL 兜底（见 H3），磁盘慢性耗尽。
- 修复建议：不要在 decorating 里提前清 `EXTRA_RENDERED_PATH`（追加时改用局部变量即可），或改用独立的 pending key：decorating 写 `EXTRA_RENDERED_PATH_PENDING`，追加成功后转正为待清理 key，`after_message_sent` 消费。修复时同步清理现存 5 个孤儿文件（request_id 均已消费完，可安全删除）。

### H2 ｜ 上游降级后消息链路整体静默失效（confirmed，guard 自相矛盾）

- 位置：main.py:624-633 / 651-656 入口按 `_effective_sign_mode()` 派发；但 standalone_events.py:89、:137 内部 guard 却用**配置值** `plugin.sign_mode != "standalone": return`
- 场景：配置 integrated + 上游 missing/degraded → effective=standalone → main 派发进 standalone 分支 → 内部 guard 看到 integrated → 直接 return。
- 后果：降级期间协议注入（on_llm_request）与渲染（on_decorating_result）全部跳过，举牌功能静默消失——「上游停用 60s 内自动降级」这个特性对消息链路实际是坏的；且因从未实测过降级场景而未被发现（测试只覆盖了 upstream_watch 单模块降级，无端到端降级链路测试）。
- 修复建议：事件模块内部 guard 统一改读 `plugin._effective_sign_mode()`（或 main 不做双重分支，把模式判断完全下沉到事件模块）。补一条端到端降级测试：配置 integrated + 模拟上游 missing → 注入/剥离/渲染仍按 standalone 工作。

### H3 ｜ staging/ 与 generated/ 无 TTL 兜底清扫（confirmed）

- 位置：backend/service.py:194（staging 写入）、:551-553（generated 写入）；只在成功路径消费（consume_upload:207、cleanup:593）
- 失败路径永久残留：上传后放弃保存 → staging 残留（实测现存 2 个陈旧文件）；进程中断/钩子未触发 → generated 残留（H1 是主因，但 standalone 的 after_message_sent 未触发时同样漏）。
- 修复建议：启动时（`_migrate` 或插件 `__init__`）+ 定期（如每次 after_message_sent 顺带）做 TTL sweep：staging >24h、generated >24h 直接删。实现放 service 层一个 `sweep_expired(max_age_seconds)` 即可，测试容易覆盖。

### M1 ｜ api_generate 未捕获 SignMemeError → text_overflow 时 500（confirmed，实机复现）

- 位置：main.py:578-587；service.py:557-558（`except SignMemeError: raise` 穿透）→ `_render_text_layer` 抛 `text_overflow`（service.py:472）无任何一层接住。
- 实证：容器内调用 `_render_text_layer('...'*2, 16, 16)` 抛 `SignMemeError: text_overflow`；`generate()` 源码不含 `except SignMemeError`。
- 触发：管理员 API 传入 40 字以内文本 + 小牌面模板（≤40 字限制不保证放得下 16×16 牌面）。
- 修复：api_generate 包 `except SignMemeError → 422`（与 skipped 响应同构）。

### M2 ｜ update/set_active 镜像失败后 DB 不回滚 → 四方不一致（confirmed）

- 位置：service.py:298-314（update 先写 DB 后 sync 镜像，失败 raise 但 DB 已变）、:320-341（set_active 同样，且循环中途失败=部分同步）
- 对比：create_template:276-283 有完整回滚，update/activate 没有。
- 后果：DB version/caption/active 已更新而镜像 metadata、语义池记录停留在旧值，直到下次成功保存才收敛；期间 integrated 检索命中旧语义。
- 修复：与 create 对齐——先 sync 成功再写 DB，或失败时按快照回滚 DB 字段。

### M3 ｜ Web API 渲染在事件循环线程做 PIL 合成（性能/一致性）

- 位置：main.py:584（api_generate 同步调 service.generate）；对比 main.py:601-609 对 meme_manager 的接口用了 `asyncio.to_thread`
- 影响：~1MB PNG 合成 + 磁盘写入阻塞整个 event loop 数百 ms；与 Python 接口路径行为不一致。
- 修复：api_generate 改 `await asyncio.to_thread(...)`。

### M4 ｜ 字体未入库 → 发布后中文渲染退化风险（confirmed）

- 位置：fonts/NotoSansSC-Regular.ttf 存在于工作树但被 .gitignore 忽略（git ls-files 28 个文件无 ttf）；service.py:385 回退链是 DejaVuSans（无中文字形）→ default 位图字体。
- 后果：本机正常（回退 candidates[1]=插件目录字体存在），但按 repo 发布/重装后中文牌面变豆腐块。另外 candidates[0]（plugin_data/fonts）永远 miss，是死路径。
- 修复：字体随仓库分发（解除忽略或下载脚本），或 README 明确安装步骤；顺带清理死路径。

### M5 ｜ metadata.yaml 描述过时（confirmed）

- metadata.yaml: `desc` 仍写「第一版由 meme_manager 调用，不提供独立聊天指令」——现已双模式 + 完整 WebUI + 管理 API。对外元数据失真，发布前必须更新。

### L1 ｜ 前端预览全量 base64，未用已有直链（性能）

- main.py:455-461 已有 `GET /image/<id>` file_response，list_templates 也返回了 `image_url`（service.py:111-114），但前端 0 处使用，卡片缩略图与大图预览全部走 `/preview` 的 base64 JSON（1.1MB 图 → ~1.5MB base64 × 模板数）。建议缩略图/大图改用 image_url 直链，`/preview` 保留给需要 request_id 审计的场景。

### L2 ｜ main.py 重复定义 cleanup_generated（无害但混乱）

- main.py:611 与 :664 定义了两个同名方法（功能相同），后者覆盖前者；api_reconcile_mode:561-569 与 _after_mode_switch:234-237 逻辑重复。建议合并。

### L3 ｜ mode API 访问 upstream_watch 私有属性 `_cache`

- main.py:491、542-547 直接读 `watch._cache.status`。建议 upstream_watch 暴露 `current_status()` 公共接口。

### L4 ｜ 字体渲染无缓存（微优化）

- service.py:434/448 每次渲染对每个字号重新 `ImageFont.truetype()`（最多 ~40 次文件加载/渲染）。量小影响低，可加 dict 缓存。

### L5 ｜ handle_llm_response 的快速跳过启发式窗口

- standalone_events.py:114-117 只看前 200 字符是否有 `{`，JSON 在 200 字符后出现的协议残留会漏剥离（罕见，且 marker 命中时不受影响）。可放宽为全文查找首个 `{`。

---

## 二、安全评估（总体良好）

- 路径安全一致且到位：`_resolve_path` 防穿越（service.py:179-185）、upload token `Path().name` 校验（:199-201）、cleanup/save_generated 均限定 generated/saved 目录内（:593-597、main.py:670-677 destination 二次 resolve+relative_to）。
- 所有 API 路由统一 `_admin_required` 检查；`_error` 的 status_code keyword 注释体现了 4.27.x 版本适配意识。
- 前端 XSS 面控制良好：所有插值经 escapeHtml，无 innerHTML 裸插，无 window.confirm/alert（sandbox 约束遵守正确），DELETE 经 bridge 改写 POST …/delete 与后端双注册路由精确匹配。
- 次要：/preview 对 32MB 模板返回 ~43MB JSON（已认证管理员场景，风险低，L1 修复后自然缓解）；api_upload 无频率限制（单管理员自用可接受，TTL sweep 后风险封顶）。

## 三、值得保留的优点

1. MemeManagerMirror 的事务性设计（snapshot/rollback/atomic json/fsync，meme_manager_mirror.py:121-163）是同类同步代码里的样板。
2. semantic_pool.upsert 的幂等保护（语义文本未变时保留向量状态字段，semantic_pool.py:227-252）有效避免了每次 reconcile 重复 embedding 的 API 成本——与用户成本优先的要求一致。
3. upstream_watch 状态机 + TTL 缓存 + 可注入时钟，测试友好、职责干净。
4. 关键回归都有测试固化（decorating gate、钩子顺序敏感、接口缺失降级），62 测试全绿。
5. 结构化 `event=` 日志风格全链一致，可 grep 可追溯。

## 四、测试缺口

1. 无端到端降级链路测试（H2 的直接后果）——建议：配置 integrated + 上游 missing → 断言协议注入/渲染仍工作。
2. 无 api_generate 500 路径测试（M1）。
3. 无临时文件生命周期测试（H1/H3）——建议直接断言 integrated 渲染轮结束后 generated/ 目录为空。

## 五、建议修复顺序

| 优先级 | 项 | 理由 |
|---|---|---|
| 1 | H1 泄漏 + 清理现存 5 孤儿文件 | 已在持续产生损害 |
| 2 | H2 降级 guard | 特性承诺与实际行为不符 |
| 3 | H3 TTL sweep | 兜底，防未来同类泄漏 |
| 4 | M1 500 路径、M2 回滚缺失 | 正确性 |
| 5 | M4 字体入库 + M5 metadata | 发布阻塞项 |
| 6 | 其余 L 级 | 顺手修 |

## 六、当前未提交改动的处置建议

- pages/举牌模板/index.html（+199/-224）：资产库重构质量良好（搜索/筛选/排序/大图对话框/自绘确认框均正确），建议在 H1/H2 修复验证后一并提交。
- docs/ 两个设计文档 + tests/test_webui_asset_library.py：随上提交。
- 本报告建议保存于 docs/ 并随提交入库。
