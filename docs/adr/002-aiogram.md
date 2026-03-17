# ADR-002: 使用 aiogram 作为 Telegram SDK

**状态**：已接受
**日期**：2026-03-17

---

## 决策

Telegram 接入使用 **aiogram v3**，而非 python-telegram-bot 或其他 Telegram SDK。

## 背景

Blueclaw 宿主进程基于 Python asyncio 单进程模型运行，需要一个原生支持 asyncio 的 Telegram Bot SDK，且支持 Long Polling 和 Webhook 两种模式。

## 考虑的方案

| 方案 | 优势 | 劣势 |
| ---- | ---- | ---- |
| **aiogram v3** | 原生 asyncio、活跃社区、支持 Middleware/Filter | API 风格与 python-telegram-bot 不同 |
| python-telegram-bot | 使用广泛、文档丰富 | v20+ 虽引入 asyncio 但核心设计源自同步时代 |
| telethon | 支持 user account | 主要面向 userbot，bot API 支持非重点 |
| 裸 HTTP 调用 | 零依赖 | 需要自行实现 Long Polling、序列化、错误处理 |

## 理由

1. **原生 asyncio**：aiogram v3 从底层就是为 asyncio 设计的，与 Blueclaw 的 asyncio 宿主进程天然契合。
2. **Dispatcher 架构**：内置 `Dispatcher.start_polling()` 和 webhook 模式，切换只需改配置。
3. **Middleware 支持**：可插入幂等检查、日志、限流等中间件，与 Blueclaw 的路由器设计高度匹配。
4. **活跃维护**：aiogram v3 于 2023 年发布大版本，社区活跃，Bug 修复及时。

## 权衡

- python-telegram-bot 的 Stack Overflow 答案更多，但 aiogram 的官方文档和 GitHub 示例已足够完善。
- aiogram v3 是 breaking change 版本，不兼容 v2 代码，但新项目从零开始不受影响。
- aiogram 对 Telegram Bot API 新特性的跟进速度通常更快。
