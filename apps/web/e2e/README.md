# 端到端测试

需要整个栈已经启动,并且演示数据是干净的:

```bash
make up            # postgres + redis + api + web
make e2e           # 先 seed-reset,再跑 Playwright
```

首次运行需要装浏览器:

```bash
npm run e2e:install -w @guardrail/web
```

覆盖的主链路:操作台生成提议 → 执行 → 审批中心批准 → 执行退款,
每一步都同时校验界面与后端状态(只验界面绿灯不算通过)。

