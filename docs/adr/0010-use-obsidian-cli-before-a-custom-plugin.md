# 优先使用 Obsidian CLI

MyOutBrain 第一版使用 Python 后台，并通过 Obsidian 官方 CLI 适配器完成 Vault 搜索、读取、创建、修改与界面跳转，暂缓开发 TypeScript 插件。官方 CLI 已覆盖自动化所需的基础操作，可显著减少首版范围；当产品需要 Obsidian 内嵌侧边栏、候选卡片或复杂交互时，再添加只负责界面的薄插件，核心知识逻辑仍保留在后台。
