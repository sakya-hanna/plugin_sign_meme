# 举牌模板 WebUI：测试方案

日期：2026-09-11

## 自动化验证

1. 页面静态契约测试：必需的 bridge SDK、Canvas/上传 DOM、asset-library 搜索/筛选/排序控件、无匹配空状态、模板结果计数、`confirmBox`、无原生 modal API、DELETE→POST `/delete` 转换。
2. Node 语法检查：从 HTML 提取 inline JavaScript，移除外部 bridge 标签后运行 `node --check`。
3. 既有 Python 单元测试：运行 `pytest -q`。
4. host 与容器 `compileall`：确认未意外影响 Python 插件代码。
5. 插件工作区与 `meme_manager` 工作区均保持无未提交修改（本次新 WebUI/docs 文件除外；不得触碰后者）。
6. 容器启动日志与未认证 API 探针：证明插件仍加载、认证边界仍存在。

## 手工浏览器验收（用户确认后才执行）

1. Dashboard 登录后进入“举牌模板”页；桌面与窄屏检查无横向溢出。
2. 搜索名称/caption/tags，切换全部/激活/未激活，依次测试三种排序和“清除筛选”。
3. 选择真实图片：Canvas 显示默认矩形，拖动/缩放后创建模板；确认 Toast、卡片、统计及预览同步。
4. 编辑已有模板语义、切换激活模板、删除非激活模板；确认自绘弹窗可操作且请求进入后端日志。
5. 切换/同步运行模式；确认 busy、成功/失败 Toast 和状态说明更新。

说明：浏览器测试是独立验收层；静态或后端测试不能替代真实 bridge、认证及 iframe 行为验证。
