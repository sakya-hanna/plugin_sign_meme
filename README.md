# 举牌表情包（第一版）

这是一个独立 AstrBot 插件，为 `meme_manager` 提供举牌模板和文字透视渲染能力。

第一版能力：

- WebUI 上传模板图片；
- 使用默认居中的矩形牌面，并调整 X/Y/宽度/高度；
- 同时只激活一个模板；
- 通过 Python 服务接口接收 `sign_text` 并生成临时 PNG；
- 生成失败不影响主文字回复；
- 生成结果默认临时保存，支持显式收藏；
- 记录阶段化日志和 request_id。

当前版本暂不提供聊天指令、AI 自动创建模板或多模板语义选择。

## 目录结构（2026-09-10 重构，对齐 meme_manager 的 backend/tests 布局）

```text
astrbot_plugin_sign_meme/
├── main.py                  # 插件入口(AstrBot 加载器约定,必须在根目录)
├── metadata.yaml            # 插件元数据(加载器约定)
├── _conf_schema.json        # 配置 schema(加载器约定)
├── pages/                   # 插件页(加载器约定,根目录名硬编码)
├── backend/                 # 核心业务逻辑
│   ├── service.py           #   模板管理/渲染服务/镜像与语义池调度
│   ├── meme_manager_mirror.py  # meme_manager 镜像目录事务同步
│   ├── semantic_pool.py     #   对接模式语义池同步(upsert/remove/reconcile)
│   └── standalone_events.py #   standalone 模式事件链路(协议注入/渲染/清理)
├── tests/                   # 测试(pytest 或直跑均可)
│   ├── conftest.py          #   路径引导(对齐 meme_manager/tests 同名约定)
│   ├── _bootstrap.py        #   直跑模式 sys.path 引导
│   └── test_*.py
├── fonts/                   # 运行时字体(gitignore,部署时自备 NotoSansSC)
└── data/                    # 运行时数据(gitignore)
```

防回归要点：
- `main.py`/`metadata.yaml`/`_conf_schema.json`/`pages/` 的位置是 AstrBot
  加载器与 Plugin Pages 的硬编码约定，不可移动；
- `backend/semantic_pool.py` 与 `backend/service.py` 中存在基于
  `__file__` 的相对路径计算(插件根 = `parent.parent`)，移动文件时必须
  同步修改；
- 测试统一 `from backend.xxx import`，路径引导只依赖 `conftest.py`
  (pytest)与 `_bootstrap.py`(直跑)，不要再在测试文件里散写 sys.path。

## meme_manager 目录镜像（2026-09-10）

每个举牌模板现在会自动镜像到 meme_manager 的既有目录机制：

```text
目录 ID：sign-meme-templates
显示名称：举牌模板
镜像目录：plugin_data/meme_manager/packs/sign-meme-templates/
```

`sign_meme` 仍是唯一写入源：创建、更新、切换激活模板、删除都会同步镜像图片和 `sign_meme_mirror.json`。meme_manager 的目录页只用于浏览预览；不得在此目录中编辑、移动、删除、建立语义索引或把它设为默认/会话/persona 资源包。

这是硬隔离：`sign-meme-templates` 绝不能进入普通表情包运行时解析、向量检索或直接发送，否则空白举牌底图会被误当作普通 meme。默认普通目录必须继续是 `manosaba-001`。部署/回填后必须同时核对源模板数、镜像记录数、host/container 文件 SHA-256、registry entry 和 selection rule。

### 隔离实现位置

- `backend/pack_resolver.py` 中 `NON_RUNTIME_PACK_IDS = frozenset({"sign-meme-templates"})`；
- `_is_pack_enabled` 对受控 pack 一律返回 False，即便 registry 中 enabled；
- 实测：把 selection_rules 恶意改成指向 sign-meme-templates 后，resolve 仍回退到 manosaba-001，不会发送空白底图。

## 零修改对接官方 meme_manager（feature 分支，2026-09-10）

integrated 模式重写为**只依赖官方原版 meme_manager**，不要求用户打任何补丁：

### 依赖面（全部为官方公开函数/数据，v4.15.5 逐文件 diff 核实）

| 用途 | 官方入口 |
|---|---|
| 语义记录读写 | `semantic_storage.load_metadata / save_metadata / scan_images / semantic_metadata_is_complete` |
| entry_id/文本哈希 | `semantic_models.semantic_entry_id / text_hash / normalize_tags` |
| FAISS 建索引 | `semantic_index.build_index`（force / target_entry_ids） |
| 嵌入 provider | 官方数据文件 `provider_selection.json`（只读）→ AstrBot 核心 `get_provider_by_id` |

### 四个发送口子的拦截（时序依据：官方钩子 priority=99999/0，AstrBot 按 priority 降序执行）

| 口子 | 场景 | 拦截点 |
|---|---|---|
| A | tool 模式 `&&meme:id&&` 标记 | response(100000) 剥标记 |
| B | tool 模式 default_id 自动回退 | response(100000) 清空 |
| C | llm/emotion 模式 selected_ids | response(99998)（官方 99999 检索之后）移除 |
| D | 流式兼容路径 | decorating(100000) 兜底清空 + 成品图追加 |

识别依据：官方候选 extra（`meme_manager_semantic_candidates`）中的 `category == "举牌模板"`。**不得**依赖 `is_sign_template` 等自定义字段——官方 `SemanticImage.from_dict/to_dict` 白名单会静默丢弃它们。

### 已实测的官方行为（v4.15.5）

- 语义化任务对 `manual_override=True` / `provenance∈{manual,mixed}` 的记录**无条件跳过 caption 覆盖**（含 force），`semantic_task.py:1847`；本插件写入即带这些标记；
- 官方 `category_analysis_is_current` 对 manual 记录放行，索引不受影响；
- 占位描述 vs 真实描述对检索分数影响：5 组查询平均分差 -0.007、方向不一致、全部高于 0.25 召回阈值 → **写入时自带真实 category_description 仅为最佳实践，非硬性要求**。

### 兼容声明与降级

- 声明兼容：官方 meme_manager v4.15.x（`backend/semantic_*` 模块函数签名与 extras key 未变区间）；
- 启动探测失败 → integrated 自动按 standalone 运行，插件页红色提示；
- 嵌入 provider 与索引 manifest 三元组不一致时拒绝重建向量（宁缺勿错）。

### 上游升级风险与自检

若未来官方版本改动钩子 priority 语义、extras key 名、`search_index` 候选字段，拦截可能失效（最坏情况：发出空白举牌底图）。升级官方 meme_manager 后请发一条"评价"类消息自检：文字回复若附带**带牌面文字**的举牌图即正常；若收到**空白底图**请立即切回 standalone 并反馈 issue。


### 2026-09-10 完整验收结果（全部通过）

```text
容器内测试：sign_meme 5 service/page + 2 mirror tests passed；
           meme_manager 370 passed + 78 subtests（5 个收集错误为容器缺 tqdm/boto3 的既有问题，与镜像无关）
运行状态：  meme_manager(4.15.4) 与 sign_meme 均加载 guard 版本；
           selection_rules 仍指向 manosaba-001；manosaba 112 张不变
Dashboard：管理表情包下拉框可见"举牌模板 (sign-meme-templates) · 1 张 · 未语义化"；
           切换后目录显示"举牌模板"分类，qqq 预览卡片正常渲染
真实链路：  API 上传→创建→HTTP 201，template_mirror_succeeded 日志；
           host/container 镜像文件 SHA-256 一致；emoji 列表 API 返回新卡片；
           删除→HTTP 200，template_mirror_succeeded(operation=delete)；
           源/镜像/元数据两侧全部清除，qqq 完整保留
运行时隔离：_is_pack_enabled(sign-meme-templates)=False；
           resolve(default)=manosaba-001；
           恶意 selection rule 指向 sign-meme-templates 时被拒（hostile_rule_rejected=PASS）
```


### meme_manager 浏览界面只读透出模板语义（2026-09-10）

`meme_manager` 的图片预览弹层（`meme_image_semantic` 端点）浏览
`sign-meme-templates` 目录时，现在会从镜像的 `sign_meme_mirror.json`
读取 caption/tags/模板名/激活状态用于展示：

- 实现位于 `backend/sign_meme_view.py`（纯函数，不落盘）；
- `can_edit_semantic` 恒为 False——前端"修改图片语义"按钮自动禁用，
  受控包的语义永远只能在举牌模板页编辑；
- `embedding_status` 恒为 `not_indexed`，不写 meme_manager 语义元数据、
  不建索引，运行时隔离与 guard 完全不变；
- 普通包（manosaba-001 等）走原逻辑，行为零变化。

防回归：`tests/test_sign_meme_readonly_semantic.py`（4 用例）。
坑：web_api.py 位于 mixins/ 包内，相对导入必须写
`from ..backend.sign_meme_view import ...`（两层）；写成一层 `.backend`
会静默 ImportError，分支永远不执行且无任何报错。meme_manager 的
web_api 路由在进程启动时注册，改 handler 后插件 reload 无效，必须
重启 AstrBot 容器。


## 双模式架构（2026-09-10）

`sign_mode` 配置（AstrBot 插件配置页）二选一，默认 `standalone`：

### standalone 独立模式（默认）
- 命中链路由**本插件自有事件钩子**驱动（standalone_events.py）：
  on_llm_request 协议注入 → on_llm_response 解析 sign_text →
  on_decorating_result 激活模板渲染 → after_message_sent 清理；
- 不依赖 meme_manager 存在；
- meme_manager 旧链路（event_handlers 内的注入/解析/发送三函数）检测
  `self_managed_sign_events=True` 后让位，代码保留不删（兼容旧版 sign_meme）。

### integrated 对接模式
- 模板语义记录写入主 pack（selection_rules default）检索池
  （semantic_pool.py：第三镜像文件 + is_sign_template 标记条目）；
- 语义检索与普通表情同池竞争，命中举牌模板时：
  validate_selected_id 按设计拒绝并标记 extra →
  sign_pipeline.try_handle_sign_candidate：
  **二次 LLM** 生成牌面文字（provider/model 可配置，留空复用本轮回复模型）
  → render_for_meme_manager(模板ID) 渲染 → 成品图走二级发送；
- 发送门禁：渲染/LLM 任何失败 → 候选丢弃 + `sign_send_skipped` 日志，
  **空白底图结构性不可出网**（validate 对 sign 记录永远返回 None）；
- 向量：模式切换/同步后 build_index(target_entry_ids) 单条增量。

### 模式切换
- AstrBot 插件配置改 `sign_mode` → 保存后重启插件生效；
- 举牌模板页顶部"同步语义池"按钮或 `POST /sign_meme/mode/reconcile`
  触发双向 reconcile（integrated 补缺 / standalone 全清）；
- 插件启动时自动 reconcile 一次（幂等）。

### 防回归要点（本日新增）
1. meme_manager `SemanticImage` dataclass 新增 is_sign_template/
   sign_template_id/sign_template_name 三字段——`to_dict()` 是手写字典,
   加字段必须同步加 to_dict,否则规范化时静默丢弃（已踩坑）;
2. candidate ID 是 ≥12 hex 的小写前缀（parse_meme_id 校验），测试用
   entry_id[:16] 构造;
3. sign_pipeline 二次 LLM 通过 host.context.llm_generate 调用;
4. web_api 改动需重启容器（路由启动时注册）;
5. semantic_pool upsert/remove 必须重算 metadata 顶层 file_total/
   unique_total/content_unique_total 快照（semantic_storage 1772-1781
   行的同款约定）——只改 images 字典会让
   semantic_metadata_is_complete() 永远 False，挡住 search_memes 门禁
   （2026-09-10 已修复,测试 test_upsert_keeps_pack_totals_and_gate_consistent）;
6. upsert 对同 entry_id 且 caption/tags 未变的模板保留原 embedding_status/
   text_hash 等——启动 reconcile 无条件覆盖会把已完成的向量状态打回
   pending（测试 test_upsert_preserves_done_embedding_on_unchanged_text）;
   caption/tags 变了才重置 pending（旧向量对应旧文本）;
7. 空 caption/tags 的模板在拷贝镜像文件**之前**拒绝入池——此类记录过不了
   semantic_caption_is_complete,会连带挡住整个 pack 的检索门禁,且拷贝后
   拒绝会留下无记录的孤儿镜像文件（测试
   test_upsert_rejects_blank_caption_without_orphan_file）;
8. integrated 渲染必须调用 main.py 真实公开接口
   `render_for_meme_manager(template_id, sign_text, request_id=)`——
   不要调用 service 层的 generate_with_template 或写不存在的方法名
   （2026-09-10 22:04 QQ 实测回归: AttributeError 导致本轮无图,且模型
   模仿历史样本输出的 {"reply","sign_text"} JSON 协议无人剥离,原样发给
   用户。修复三件套: 方法名+getattr 防护降级 / standalone_events 兜底
   剥离 JSON 并把 sign_text 写入 sign_meme_integrated_model_sign_text /
   main.py 100000 钩子内剥离先于拦截管线执行）。回归测试:
   test_incident_20260910_json_leak_and_render_crash、
   test_render_interface_missing_degrades_cleanly;
9. 钩子派发顺序敏感: handle_llm_response(剥离)必须先于
   on_llm_response_first(拦截渲染)——渲染管线要从剥离产物里读
   sign_text,顺序颠倒会让复用永远落空（第 8 条修复的一部分,
   test_main_hooks 的矩阵断言不覆盖顺序,改动时对照 main.py 内注释）;
10. 模板语义编辑链路(2026-09-10): PUT /templates/<id> →
   service.update_template(镜像同步+语义池 upsert) →
   main._schedule_pool_vector_flush 异步消费 pending 队列增量重建
   FAISS。upsert 的 needs_vector 语义: caption/tags 变了或向量未完成
   →True;文本未变且向量 done→False(幂等,省 embedding API)。
   向量重建必须异步(create_task+集合持引用),不能在请求线程内同步做
   (大索引重建会卡住 HTTP 响应);
11. 编辑语义实机验收结论(2026-09-10 23:21): 编辑→pending→done 约
   0.4-0.6s;新语义检索 top1 立即命中;相同文本重复编辑不触发重建。
   检索返回的候选 id 是 "meme:"+entry_id[:12] 前缀,与 metadata 全长
   entry_id 比较时必须截断(验收脚本踩坑)。

## 开发与回归注意事项

### bridge 不支持 DELETE/PUT：删除必须走 /delete 后缀（2026-09-10 修复）

Plugin Page Bridge 只有 `apiGet`/`apiPost` 两个方法。此前页面 `api()` 封装
把所有非 GET 请求一律发 POST 且路径不变，导致 `DELETE /templates/<id>` 实际
变成 `POST /templates/<id>`——命中后端"更新模板"路由（PUT/POST 共用），
空请求体触发 `invalid_name`，用户删除模板时报
"模板名称不能为空且不能超过80个字符"。

修复后的前端封装：method==='DELETE' 时自动改写为
`apiPost('<endpoint>/delete', body)`，命中后端专门注册的
`POST /templates/<id>/delete` 路由。

防回归要点：
1. 页面里新增 DELETE/PUT 类操作时，禁止直接把 method 传给 bridge——bridge
   只有 GET/POST，DELETE 必须改写到 `/delete` 后缀端点；
2. 修改 `api()` 封装后必须跑 `node --check`（曾因替换文本多带一个 `};`
   导致语法错误）；
3. 回归用例：创建临时模板 → `POST /templates/<id>/delete` → 确认列表中
   消失（本轮已跑通：BRIDGE-DELETE-REGRESSION=PASS）。

### iframe sandbox 禁用原生弹窗：确认必须用 confirmBox（2026-09-11 修复）

Dashboard 插件页 iframe 固定
`sandbox="allow-scripts allow-forms allow-downloads"`（无 `allow-modals`，
改在宿主侧注入不现实——sandbox 由 Dashboard 源码写死）。sandbox 下
`window.confirm`/`alert`/`prompt` 被浏览器**静默拦截**：不弹窗、confirm
直接返回 false。此前模式切换按钮用 `window.confirm` 做二次确认，表现为
"点击没反应"（JS 在确认处 return，`/mode/reconcile` 请求根本不发）。

修复：页面提供通用自绘确认弹窗 `confirmBox(message, okText, danger)`，
遮罩点击可关闭；`confirmDelete` 也收敛到它。

防回归要点：
1. 举牌模板页（及所有插件页）**禁止再引入 window.confirm/alert/prompt**，
   一律用 `confirmBox()`——sandbox 不会报错，只会静默失败，极难排查；
2. "按钮点了没反应"类问题先查三件事：请求有没有发出去（服务端日志）、
   有没有原生弹窗被 sandbox 拦截、bridge 是否就绪（页面顶部通信组件检查）；
3. 页面 HTML 由服务端每次请求现读（`read_plugin_page_text` +
   `Cache-Control: no-store`），改页面文件只需浏览器强刷，无需重启容器。

### 模板图片选择与牌面矩形预览

创建模板页面的本地图片选择链路依赖以下三个 DOM 引用：

```javascript
const fileInput = document.querySelector('#file');
const canvas = document.querySelector('#canvas');
const ctx = canvas.getContext('2d');
```

`fileInput` 的 `change` 事件会创建 `Image`，图片加载完成后调用 `draw()`；`draw()` 又依赖 `canvas` 和 `ctx` 绘制图片及牌面矩形。

曾发生过一次回归问题：修改上传 bridge/上传函数时误删了上述初始化声明，但保留了后面的 `fileInput.addEventListener(...)`。结果是：

- 可以点击文件选择器；
- 选择图片后牌面图片不显示；
- 默认牌面矩形不出现；
- 页面表现为“牌面矩形无法正确加载图片”；
- JavaScript 运行时会因为 `fileInput` 未定义而中断。

后续修改 `index.html` 的上传、bridge 或预览逻辑时必须注意：

1. 不要删除或移动 `fileInput`、`canvas`、`ctx` 的初始化，除非同步重构所有引用；
2. 修改后必须运行 JavaScript 语法检查；
3. 必须验证选择本地图片后 `Image.onload → setRect → draw` 完整执行；
4. 必须确认页面中仍存在 `#file`、`#canvas`、`#rect-summary` 和四个矩形隐藏字段；
5. 上传链路测试与牌面预览测试分开进行，上传成功不代表 canvas 预览正常；
6. 修改前端初始化区域后，必须做一次真实 WebUI 文件选择回归，不仅依赖后端服务测试。

推荐的最低检查命令：

```bash
python3 -c 'import pathlib; s=pathlib.Path("pages/举牌模板/index.html").read_text(); pathlib.Path("/tmp/sign-meme-page.js").write_text(s.split("<script>",1)[1].split("</script>",1)[0])'
node --check /tmp/sign-meme-page.js
```

## 当前开发与测试进度

更新时间：2026-09-09

### 已实现

- 独立 AstrBot 举牌模板插件；
- WebUI 创建模板页面；
- 本地图片选择后 canvas 预览；
- 矩形牌面位置、宽度、高度调整；
- 上传临时文件、令牌提交模板；
- 模板名称、描述、caption、tags 保存；
- 单模板激活机制；
- 模板预览 data URL 接口；
- 文字渲染、临时生成、收藏接口；
- 分阶段日志和 request_id；
- `sign_meme` 与 `astrbot_plugin_sign_meme` 两套路由前缀兼容；
- 路由名称与持久化数据目录分离，数据目录固定为 `plugin_data/sign_meme`。

### 已验证

- 服务测试：`2 service tests passed`；
- JavaScript `node --check`：通过；
- 宿主机和容器 `compileall`：通过；
- AstrBot 插件加载：通过；
- WebUI 启动：通过；
- Dashboard 认证后的真实 multipart 上传：HTTP 201，成功返回 token；
- Dashboard 认证后的真实模板提交：HTTP 201，成功创建模板；
- 有效 Pillow PNG 的真实上传→令牌→提交完整后端链路：通过；
- 测试模板和测试文件：已清理，模板库恢复原状；
- Chrome 已安装：Google Chrome 153.0.8010.36；
- Chrome 已实际打开 Dashboard、登录并加载举牌模板 iframe；
- 已确认 iframe 的真实插件名为 `sign_meme`。

### 当前待完成

- Chrome 中通过真实文件选择器完成一次最终的 Plugin Page bridge 上传回归；
- 确认浏览器端 `bridge.upload()` 返回值中包含 token；
- 确认浏览器端保存后页面显示“保存成功”；
- 读取同一时间段的 `template_upload_*` 和 `template_create_*` 日志作为最终证据。

当前不能把插件标记为“浏览器端全部验收完成”：后端认证链路已经通过，但浏览器端此前仍出现 `upload_response_missing_token`，最终 bridge 返回值还需要在强制刷新后的页面中再次验证。
