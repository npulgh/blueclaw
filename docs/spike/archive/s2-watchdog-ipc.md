# S2: watchdog + Docker Volume 验证

> 状态：**已完成** ✓ — 四项全部 PASS，结论 GO
> 执行结果记录在 [findings.md](findings.md) § S2

---

## 验证目标

确认 watchdog 文件事件在 Docker Volume 挂载场景下的可靠性和延迟。

## 验证方法

创建临时脚本 `spike/ipc_verify.py`：

**宿主端**：启动 watchdog Observer 监听 `spike/test_outbox/` 目录，记录每个文件创建事件的时间戳。

**容器端**：启动一个最小 Docker 容器，挂载 `spike/test_outbox/`，执行以下测试：

| 测试 | 方法 | 预期 |
| ---- | ---- | ---- |
| 基础事件 | 容器内 `echo > outbox/test.json` | 宿主 watchdog 收到事件 |
| 延迟测量 | 容器写入 100 个文件，记录写入时间和宿主接收时间 | P99 延迟 < 100ms |
| 高频并发 | 5 个容器同时各写 20 个文件（模拟 stream_chunk） | 零丢失，顺序正确 |
| 原子性 | 容器用 write-tmp + rename 模式写入 | 宿主不读到半写文件 |
| 平台差异 | 在 Windows Docker Desktop (WSL2) 上重复以上测试 | 结果与 Linux 一致 |

## 判定标准

| 指标 | 通过 | 失败 |
| ---- | ---- | ---- |
| 事件丢失率 | 0% | > 0% |
| P99 延迟 | < 100ms | > 500ms |
| 高频并发 | 100 个文件全部收到 | 有遗漏 |
| 原子性 | 未读到半写内容 | 读到不完整 JSON |

## 失败决策矩阵

| 失败项 | 影响范围 | 替代方案 | 决策 |
| ---- | ---- | ---- | ---- |
| 事件偶尔丢失（< 1%） | IPC 可靠性 | 增加 5 秒定时扫描兜底（已纳入 T1.7 设计修订） | 可继续，双保险模式 |
| 事件频繁丢失（> 5%） | IPC 不可用 | 回退到 NanoClaw 的轮询模式（每 1-2 秒扫描） | 可继续，放弃事件驱动 |
| 延迟过高（> 500ms） | 流式体验 | 轮询模式 + 更短间隔（500ms） | 可继续，流式体验降级 |
| Windows 特有问题 | 跨平台承诺 | 文档标注 Windows 需要轮询兜底 | 可继续，平台差异化处理 |

## 执行清单

- [x] 执行 S2 watchdog 基础事件 → 记录到 findings.md
- [x] 执行 S2 延迟测量 → 记录到 findings.md
- [x] 执行 S2 高频并发 → 记录到 findings.md
- [x] 执行 S2 原子性 → 记录到 findings.md
- [x] 在 Windows Docker Desktop (WSL2) 上重复以上测试 → 记录到 findings.md
