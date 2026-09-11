# 举牌表情包 (sign_meme)

AstrBot 插件：举牌模板管理与牌面文字渲染。为聊天机器人提供"举牌"能力——机器人回复时附带一张由激活模板渲染出牌面文字的图片。

- **零依赖可用**：standalone 模式不依赖 meme_manager，装上即用；
- **可选深度对接**：integrated 模式对接官方原版 meme_manager（零修改），举牌模板进入语义检索池与普通表情包同池竞争；
- **结构安全**：任何失败路径下空白底图都不会发出去。

## 功能一览

### 模板管理 WebUI（插件页「举牌模板」）

- 资产库工作台：模板卡片网格，支持按名称/备注/caption/tags/文件名实时搜索、按激活状态筛选、按最近更新/名称/版本排序；
- 两阶段上传：图片先暂存换取 token，表单提交时绑定，避免孤儿文件；
- Canvas 牌面编辑：拖拽调整牌面矩形，支持矩形（rect）与四点透视（quad）两种牌面；
- 模板语义：caption、多个自由 tags、visible_text（可空）、名称、备注；
- 单模板激活机制；当前激活模板不可直接删除（先激活其他模板）；
- 测试生成：任选模板即时生成成品图预览，验证字体与牌面位置；
- 收藏：临时生成的图片可保存为持久文件；
- 全部操作右上角 Toast 反馈；删除/模式切换使用自绘 confirmBox（iframe sandbox 下原生弹窗静默失效，页面禁用 window.confirm/alert/prompt）。

### 渲染引擎

- Pillow 离屏渲染：牌面区域按模板存储的 rect/quad 做透视投影（homography）贴字；
- 单行自适应排版：按牌面宽度自动缩放字号，带描边，中文使用 NotoSansSC；
- 生成产物为临时文件，带 request_id 全链路日志；TTL 兜底清扫（默认 24h），失败路径立即丢弃，不泄漏磁盘。

### 双运行模式（sign_mode 配置项）

**standalone 独立模式（默认）**——由本插件自有事件钩子驱动完整命中链路：

```text
on_llm_request        协议注入（告知模型可主动举牌）
on_llm_response       解析模型输出的 sign_text
on_decorating_result  用激活模板渲染成品图并追加发送
after_message_sent    清理临时文件
```

不依赖 meme_manager 存在。meme_manager 旧链路检测到 `self_managed_sign_events=True` 后自动让位。

**integrated 对接模式**——举牌模板进入 meme_manager 语义检索池：

```text
模板语义记录同步进主检索池（semantic_pool）
  → 语义检索命中举牌模板（category == "举牌模板"）
  → 二次 LLM 生成牌面文字（provider/model 可配置，留空复用本轮回复模型）
  → render_for_meme_manager 渲染
  → 成品图二级发送
```

- 只依赖官方公开函数/数据（semantic_storage / semantic_models / semantic_index / provider_selection.json），官方 meme_manager v4.15.x 零修改对接；
- 发送门禁：渲染或二次 LLM 任何失败 → 候选丢弃并记录 `sign_send_skipped`，空白底图结构性不可出网（validate 对 sign 记录永远返回 None）；
- 上游探测（upstream_watch）：安装、语义模块、镜像 pack、钩子存活逐项检查，任何一项不满足自动按 standalone 运行并在插件页红色提示；
- 向量索引：模式切换/语义编辑后按 entry_id 单条增量重建 FAISS，文本未变且向量已完成时不重复消耗 embedding API。

**模式切换**：AstrBot 插件配置页改 `sign_mode` 保存后重启插件生效；插件页「同步语义池」按钮（`POST /mode/reconcile`）触发双向 reconcile（integrated 补缺 / standalone 全清）；插件启动时自动幂等 reconcile 一次。

## 安装部署

```bash
# 1. 将本插件放入 AstrBot 的 plugins 目录（或通过 WebUI 从 repo 安装）
# 2. 安装依赖
pip install -r requirements.txt
# 3. 字体（必读）：中文牌面渲染依赖 fonts/NotoSansSC-Regular.ttf，
#    该文件不入库（gitignore），缺失时中文渲染为方框
mkdir -p fonts && cd fonts
curl -L -o NotoSansSC-Regular.ttf \
  "https://github.com/notofonts/noto-cjk/raw/main/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf"
```

容器部署需把字体同步挂载或拷入容器内同路径。校验方式：管理页任选模板点「测试生成」，牌面文字清晰无方框即正常。

## 配置项

| 配置 | 默认 | 说明 |
|---|---|---|
| `sign_mode` | `standalone` | 运行模式。`standalone`=独立命中链路；`integrated`=进入 meme_manager 语义检索池。上游缺失时 integrated 自动降级为 standalone |
| `sign_text_llm_provider` | 空 | integrated 模式生成牌面文字的 Provider；留空复用当前会话回复模型 |
| `sign_text_llm_model` | 空 | 牌面文字模型名覆盖；留空用所选 Provider 默认模型 |

## HTTP API

路由经 AstrBot `register_web_api` 注册，`sign_meme` 与 `astrbot_plugin_sign_meme` 两套前缀等价（兼容历史）。下表为相对路径：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/templates` | 列出模板 |
| POST | `/templates` | 创建模板（携带上传 token 与语义字段） |
| POST | `/upload` | 上传模板图片，返回暂存 token |
| PUT/POST | `/templates/<id>` | 更新模板（bridge 无 PUT 时用 POST） |
| POST | `/templates/<id>/activate` | 激活模板 |
| DELETE / POST | `/templates/<id>`、`/templates/<id>/delete` | 删除模板（`/delete` 后缀供 bridge 使用） |
| GET | `/image/<id>` | 模板原图 |
| GET | `/image/<id>/preview` | 预览（data URL，供 bridge） |
| POST | `/generate` | 测试生成：`{"sign_text": "..."}` |
| POST | `/generated/save` | 收藏生成的临时图 |
| GET | `/mode` | 读取运行模式与上游状态 |
| POST | `/mode/reconcile` | 切换模式并同步语义池 |

外部直调示例（完整路径，已实测可用）：

```bash
curl -X POST "$DASHBOARD/api/v1/plugins/extensions/sign_meme/generate" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"sign_text": "你好"}'
```

## 目录结构

```text
astrbot_plugin_sign_meme/
├── main.py                     # 插件入口(AstrBot 加载器约定,必须在根目录)
├── metadata.yaml               # 插件元数据
├── _conf_schema.json           # 配置 schema
├── pages/举牌模板/index.html    # 插件页(加载器约定,根目录名硬编码)
├── backend/
│   ├── service.py              # 模板管理/渲染引擎/语义池调度/TTL 清扫
│   ├── standalone_events.py    # standalone 命中链路
│   ├── integrated_events.py    # integrated 拦截/二次 LLM/渲染管线
│   ├── semantic_pool.py        # 语义池 upsert/remove/reconcile
│   ├── meme_manager_mirror.py  # 镜像目录事务同步
│   ├── upstream_watch.py       # 上游能力探测与缓存
│   └── compat.py               # 兼容层
├── tests/                      # pytest(容器内) 或 _bootstrap.py 直跑
├── fonts/                      # 运行时字体(gitignore,部署自备)
├── data/                       # 运行时数据(gitignore,固定 plugin_data/sign_meme)
└── docs/                       # 设计与评审文档
```

## 与 meme_manager 的关系

- **目录镜像**：每个模板自动镜像到 `plugin_data/meme_manager/packs/sign-meme-templates/`（含 `sign_meme_mirror.json`），meme_manager 目录页只读浏览；不得在该目录编辑、删除或建立语义索引。`sign_meme` 是唯一写入源，创建/更新/激活/删除均同步镜像。
- **硬隔离**：`sign-meme-templates` 被列为 NON_RUNTIME_PACK（实现于 meme_manager 侧部署版 `backend/pack_resolver.py` 的 `NON_RUNTIME_PACK_IDS` 与只读语义视图 `sign_meme_view.py`），绝不进入普通表情包运行时解析、向量检索或直接发送——即使 selection rule 被恶意指向该目录也会被拒。默认普通表情目录必须仍是 `manosaba-001`。
- **零补丁的拦截链路**：integrated 模式本身只依赖官方公开函数/数据（semantic_storage / semantic_models / semantic_index / provider_selection.json），不要求用户修改 meme_manager 即可工作；上述镜像只读视图与隔离 guard 是本环境部署版的增强。
- **升级自检**：官方 meme_manager 升级后发一条"评价"类消息——若收到带牌面文字的举牌图即正常；若收到空白底图，立即切回 standalone 并排查钩子 priority / extras key 是否变更。

## 测试

```bash
# 容器内全量（当前 74 passed）
docker exec astrbot sh -c \
  'cd /AstrBot/data/plugins/astrbot_plugin_sign_meme && python -m pytest tests/ -q'

# 页面 JS 语法检查（改动 pages/ 后必跑）
python3 -c 'import pathlib; s=pathlib.Path("pages/举牌模板/index.html").read_text(); \
  pathlib.Path("/tmp/sign-meme-page.js").write_text(s.split("<script>",1)[1].split("</script>",1)[0])'
node --check /tmp/sign-meme-page.js
```

## 开发注意事项（防回归）

以下均为实测踩坑，改动相关区域前先读：

1. **加载器硬约定**：`main.py`、`metadata.yaml`、`_conf_schema.json`、`pages/` 的位置不可移动；`backend/` 内有基于 `__file__` 的相对路径计算，移动文件须同步修改。
2. **bridge 只有 GET/POST**：DELETE 一律改写为 `POST <endpoint>/delete`；改前端 `api()` 封装后必须 `node --check`。
3. **iframe sandbox 无 allow-modals**：页面禁止引入 window.confirm/alert/prompt，确认一律自绘 `confirmBox()`——sandbox 拦截不报错，只会静默失败。页面 HTML 服务端现读，改页面文件浏览器强刷即可，无需重启容器。
4. **web_api 路由进程启动时注册**：改 `main.py` 路由/handler 后插件 reload 无效，必须重启 AstrBot 容器；meme_manager 侧 web_api 改动同理。
5. **integrated 渲染必须调 `render_for_meme_manager(template_id, sign_text, request_id=)`**：不要调 service 层 `generate_with_template` 或不存在的方法名（曾致 AttributeError 无图 + JSON 协议泄漏给用户）。
6. **钩子顺序敏感**：`handle_llm_response`（剥离协议）必须先于 `on_llm_response_first`（拦截渲染）执行，顺序颠倒会使 sign_text 复用永远落空。
7. **语义池 upsert 约定**：必须重算 metadata 顶层 `file_total/unique_total/content_unique_total` 快照，否则 `semantic_metadata_is_complete()` 恒 False 挡住整个 pack 的检索门禁；caption/tags 未变时保留原 embedding_status（幂等，不重复耗 embedding API）；空 caption/tags 的模板在拷贝镜像文件之前拒绝入池。
8. **meme_manager `SemanticImage`** 新增自定义字段必须同步手写 `to_dict()`，否则规范化时静默丢弃；识别举牌候选只依赖 `category == "举牌模板"`，不依赖自定义字段。
9. **前端 canvas 预览**：`#file`、`#canvas`、`ctx` 初始化与 `Image.onload → setRect → draw` 链路不可拆散；上传链路与牌面预览链路要分开回归。

更多设计与评审细节见 `docs/`（WebUI 资产库设计、测试方案、2026-09-11 code review 记录）。

## 环境要求

- AstrBot >= 4.17.0
- 平台：aiocqhttp（QQ/NapCat）验证可用，其他平台遵循 AstrBot 事件钩子规范
- Python 依赖见 `requirements.txt`（Pillow）
- integrated 模式额外要求：官方 meme_manager v4.15.x 及可用的 embedding provider
