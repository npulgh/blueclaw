# S3: Unix Socket 跨容器验证（可选）

> 状态：**已完成** ✓ — 容器间通信 PASS，宿主端 Windows 平台不支持 AF_UNIX
> 优先级：低 — 此项为 Phase 3 Proxy Sidecar 的前提，非 Phase 1 阻塞项。
> 执行结果记录在 [findings.md](findings.md) § S3

---

## 验证目标

确认 `--network none` 容器通过 Docker Volume 挂载的 Unix Socket 能与宿主通信。

## 验证方法

宿主启动一个简单的 Unix Socket HTTP 代理，容器通过 `HTTP_PROXY=socks5h://...` 发起请求。

## 判定标准

| 指标 | 通过 | 失败 |
| ---- | ---- | ---- |
| 容器 → 宿主 Socket 通信 | 请求到达宿主代理 | 连接被拒或超时 |
| Windows Docker Desktop | 行为与 Linux 一致 | Socket 文件不可见或不可连接 |

## 失败决策矩阵

| 失败项 | 替代方案 |
| ---- | ---- |
| Unix Socket 不可用 | 改用 `--network` 自定义网络 + iptables 白名单（安全性降级，需更新 ADR-004） |

## 执行清单

- [x] 执行 S3 Unix Socket 验证 → 记录到 findings.md
