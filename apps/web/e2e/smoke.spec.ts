import { expect, test, type APIRequestContext } from "@playwright/test";

/**
 * 端到端冒烟:把「提议 → 执行 → 审批 → 退款」这条主链路在浏览器里跑一遍。
 *
 * 前置:整个栈已启动,且演示数据是干净的(先跑 `make seed-reset`)。
 * 断言同时覆盖 UI 与后端状态 —— 只验 UI 绿灯不算通过。
 */

const API = process.env.E2E_API_BASE_URL ?? "http://localhost:8080";

type OrderSummary = {
  order_id: number;
  order_no: string;
  status: string;
  total_amount_cents: number;
};

type TicketSummary = { order_id: number; status: string; ticket_id: number };

async function pickRefundableOrder(request: APIRequestContext): Promise<OrderSummary> {
  const ordersResponse = await request.get(`${API}/api/orders?status=PAID&limit=50`);
  expect(ordersResponse.ok(), "订单接口不可用,检查后端是否已启动").toBeTruthy();
  const orders = (await ordersResponse.json()) as { items: OrderSummary[] };

  const ticketsResponse = await request.get(`${API}/api/tickets?limit=100`);
  const tickets = (await ticketsResponse.json()) as { items: TicketSummary[] };
  const busy = new Set(
    tickets.items
      .filter((ticket) => ticket.status === "PENDING" || ticket.status === "APPROVED")
      .map((ticket) => ticket.order_id),
  );

  const target = orders.items.find(
    (order) => !busy.has(order.order_id) && order.total_amount_cents >= 1000,
  );
  expect(target, "没有可用的已支付订单,请先执行 make seed-reset 重置演示数据").toBeTruthy();
  return target as OrderSummary;
}

test("提议 → 执行 → 审批 → 退款 全链路", async ({ page, request }) => {
  const order = await pickRefundableOrder(request);

  // --- 1. 操作台:生成提议 ---
  await page.goto("/console");
  await page.getByLabel("你想做什么").fill(`订单 ${order.order_no} 质量有问题,帮我退 1 元`);
  await page.getByRole("button", { name: "生成提议" }).click();

  const proposal = page.locator("article", { hasText: "提议卡片" });
  await expect(proposal).toBeVisible();
  await expect(proposal.getByText("create_refund")).toBeVisible();
  await expect(proposal.getByText("需人工审批")).toBeVisible();
  await expect(proposal.getByText(/质量问题/)).toBeVisible();
  await expect(proposal.getByText("¥1.00")).toBeVisible();

  // --- 2. 执行提议 ---
  await page.getByRole("button", { name: "执行这条提议" }).click();
  const result = page.locator("article", { hasText: "执行结果" });
  await expect(result).toBeVisible();
  // 收窄到 note 段落:JSON 里也有同名字段,不限定会触发 strict mode
  await expect(result.locator("p").filter({ hasText: /已创建待审批退款工单/ })).toBeVisible();

  const payload = (await result.locator("pre").innerText()).trim();
  const ticketNo = /"ticket_no":\s*"([^"]+)"/.exec(payload)?.[1];
  expect(ticketNo, "执行结果里应该带工单号").toBeTruthy();

  // 数据库真的变了:工单处于待审批
  const created = await request.get(`${API}/api/tickets?status=PENDING&limit=50`);
  const pending = (await created.json()) as { items: { ticket_no: string }[] };
  expect(pending.items.map((item) => item.ticket_no)).toContain(ticketNo);

  // 轨迹被记录
  await expect(page.getByText("参数校验")).toBeVisible();

  // --- 3. 审批中心:批准并退款 ---
  await page.goto("/approvals");
  const row = page.getByRole("row", { name: new RegExp(order.order_no) });
  await expect(row).toBeVisible();
  await expect(row.getByText("待审批")).toBeVisible();

  await row.getByRole("button", { name: "批准" }).click();
  await expect(row.getByText("已批准")).toBeVisible();

  await row.getByRole("button", { name: "执行退款" }).click();
  await expect(row.getByText("已退款")).toBeVisible();

  // --- 4. 后端状态核对 ---
  const refunded = await request.get(`${API}/api/tickets?status=REFUNDED&limit=50`);
  const refundedBody = (await refunded.json()) as { items: { ticket_no: string }[] };
  expect(refundedBody.items.map((item) => item.ticket_no)).toContain(ticketNo);
});

test("已发货订单不允许取消,错误理由可见", async ({ page, request }) => {
  const ordersResponse = await request.get(`${API}/api/orders?status=SHIPPED&limit=1`);
  const orders = (await ordersResponse.json()) as { items: OrderSummary[] };
  expect(orders.items.length, "需要至少一条已发货订单,请先 make seed-reset").toBeGreaterThan(0);
  const order = orders.items[0];

  await page.goto(`/orders/${order.order_id}`);
  await expect(page.getByText("已发货")).toBeVisible();
  await expect(page.getByRole("button", { name: "取消订单" })).toHaveCount(0);
});

test("UI 点的写操作会进治理链路:审计里能看到 before/after", async ({ page, request }) => {
  const order = await pickRefundableOrder(request);

  // 1. 在操作台执行一次写入(只读/写走的是同一条受治理路径)
  await page.goto("/console");
  await page.getByLabel("你想做什么").fill(`订单 ${order.order_no} 破损了,退 2 元`);
  await page.getByRole("button", { name: "生成提议" }).click();
  await page.getByRole("button", { name: "执行这条提议" }).click();
  await expect(page.locator("article", { hasText: "执行结果" })).toBeVisible();

  // 2. 执行与审计看板:最新一条审计必须是这次写入,并且带前后值
  await page.goto("/governance");
  const latest = page.locator("article").filter({ hasText: "create_refund" }).first();
  await expect(latest).toBeVisible();
  await expect(latest.getByText("成功")).toBeVisible();
  // 订单在 seed 里可能已经有工单,所以新工单的下标不固定;
  // 断言收窄到那一行 diff,避免和「完整快照」里的原文冲突
  const change = latest.locator("li").filter({ hasText: /tickets\[\d+\]\.status/ });
  await expect(change).toBeVisible();
  await expect(change).toContainText("—"); // 前值为空:这条工单是这次操作新增的
  await expect(change).toContainText("PENDING");
});
