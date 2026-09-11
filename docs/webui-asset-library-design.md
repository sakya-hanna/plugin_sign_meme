# 举牌模板 WebUI：浅色现代资产库设计

日期：2026-09-11
状态：用户已确认实施；本次不提交 Git。

## 目标

将现有功能型单文件 Plugin Page 升级为浅色、紧凑、以模板预览为中心的资产管理工作台。保留 Canvas 牌面矩形编辑、上传两阶段提交、模板语义编辑、激活/删除安全约束、模式切换、语义池同步和 Toast；不改变后端、API、数据模型、依赖或 `meme_manager`。

## 信息架构

1. 顶部工作区：页面标题、用途说明、模板总数、激活模板数、当前运行模式，以及切换模式/同步语义池操作。
2. 新建工作区：左侧结构化表单（名称、备注、caption、tags、图片和提交）；右侧 Canvas 预览与牌面矩形编辑，显示未选择图片的明确空状态。
3. 模板资产库：搜索、状态筛选、排序、结果数；响应式卡片网格。每张卡展示缩略图、名称、状态、caption、tags、尺寸、版本、更新时间与操作。

## 视觉系统

- 浅灰蓝页面底色，白色和微灰卡片分层；蓝紫色仅用于主操作/焦点，绿色仅用于激活成功状态，红色仅用于危险操作。
- 本地 system font，不访问 CDN；CSS 变量定义色彩、间距、圆角、阴影和焦点环。
- 8px 间距节奏，卡片采用 12–16px 圆角、细描边和克制阴影。
- 所有控制项的 hover、focus-visible、disabled、busy 状态一致；不以颜色作为唯一状态提示。

## 交互约束

- 搜索只前端过滤：模板名称、备注、caption、tags、原始文件名；输入实时更新结果数。
- 状态筛选：全部、当前激活、未激活。
- 排序：最近更新（默认，`updated_at` 降序）、名称（中文 locale 排序）、版本号（降序）。后端已返回 `updated_at`、`version`，不伪造时间。
- 未匹配筛选时提供专用空状态与“清除筛选”按钮。
- 新建/读取/预览/修改/模式操作继续使用现有 bridge/API；按钮在操作过程中禁用，并使用 Toast 显示处理中、成功或失败。
- 删除、模式切换只走既有 `confirmBox`，禁止原生 `confirm`/`alert`/`prompt`。
- 对于当前激活模板，维持“不可直接删除”的现有安全约束。

## API/数据契约（不变）

- `GET /templates`：读取模板数组；前端使用 `id,name,description,caption,tags,original_name,width,height,active,updated_at,version`。
- `POST /templates`：上传令牌 + 创建数据。
- `POST /templates/<id>/activate`：激活。
- `POST /templates/<id>/delete`：删除兼容路由。
- `POST /templates/<id>`：语义更新（bridge 不支持 PUT 时保持现有 POST 兼容用法）。
- `GET /image/<id>/preview`：通过 bridge 取得 data URL。
- `GET /mode`、`POST /mode/reconcile`：模式状态/同步。

## 非目标

不添加后端分页、服务端搜索、批量删除/批量启停、外部组件库、React/Tailwind/Bootstrap/CDN、数据迁移或对 `meme_manager` 的任何修改。
