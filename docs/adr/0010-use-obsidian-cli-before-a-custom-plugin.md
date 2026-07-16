# 优先使用 Obsidian CLI

MyOutBrain 第一版使用 Python 后台，并通过 Obsidian 官方 CLI 适配器完成 Vault 的界面跳转及无需参与事务的交互，暂缓开发 TypeScript 插件。需要与演变事件保持一致的 Vault 创建和修改由 Python 后台在项目锁内原子写入（见 ADR-0013）；当产品需要 Obsidian 内嵌侧边栏、候选卡片或复杂交互时，再添加只负责界面的薄插件，核心知识逻辑仍保留在后台。
