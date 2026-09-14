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

async function submitProposalAndApproveRun(page: import("@playwright/test").Page): Promise<string> {
  // 提议卡片带着策略引擎的裁决 —— 点「提交」之前就该知道这条会被拦下来审批
  await page.getByRole("button", { name: /提交并等待审批|执行这条提议/ }).click();
  const result = page.locator("article", { hasText: "执行结果" });
  await expect(result).toBeVisible();
  await expect(result.getByText(/需人工审批/)).toBeVisible();

  const runLink = result.getByRole("link", { name: /去执行详情审批这一步/ });
  const href = await runLink.getAttribute("href");
  expect(href, "等审批时要给出执行详情入口").toBeTruthy();
  const runUid = (href as string).split("/").pop() as string;

  // 策略层拦下来的写操作,业务表此时必须一行都没动
  await page.goto(`/governance/${runUid}`);
  await expect(page.getByText(/策略裁决 REQUIRE_APPROVAL/)).toBeVisible();
  await page.getByRole("button", { name: /批准第 \d+ 步/ }).click();
  await expect(page.getByRole("button", { name: /批准第 \d+ 步/ })).toHaveCount(0);

  await page.getByRole("button", { name: "推进执行" }).click();
  // 终态下按钮会变成「已结束」,用它判断 run 真的推进完了
  await expect(page.getByRole("button", { name: "已结束" })).toBeVisible();
  return runUid;
}

test("提议 → 策略拦下 → 审批 → 执行 → 退款 全链路", async ({ page, request }) => {
  const order = await pickRefundableOrder(request);

  // --- 1. 操作台:生成提议 ---
  await page.goto("/console");
  await page.getByLabel("你想做什么").fill(`订单 ${order.order_no} 质量有问题,帮我退 1 元`);
  await page.getByRole("button", { name: "生成提议" }).click();

  const proposal = page.locator("article", { hasText: "提议卡片" });
  await expect(proposal).toBeVisible();
  // 计划卡片 + 步骤卡片上都会标出来,取第一个即可
  await expect(proposal.getByText("create_refund", { exact: true }).first()).toBeVisible();
  await expect(proposal.getByText("需人工审批", { exact: true }).first()).toBeVisible();
  await expect(proposal.getByText(/策略裁决 REQUIRE_APPROVAL/)).toBeVisible();
  await expect(proposal.getByText(/质量问题/)).toBeVisible();
  await expect(proposal.getByText("¥1.00")).toBeVisible();
  await expect(page.getByText("参数校验")).toHaveCount(0); // 还没提交

  // --- 2. 提交 → 被策略拦下 → 在执行详情里批准并推进 ---
  await submitProposalAndApproveRun(page);

  // 批准之后业务表才真的变了:工单处于待审批
  const created = await request.get(`${API}/api/tickets?status=PENDING&limit=50`);
  const pending = (await created.json()) as { items: { ticket_no: string; order_id: number }[] };
  const ticket = pending.items.find((item) => item.order_id === order.order_id);
  expect(ticket, "推进 run 之后应该落一张待审批工单").toBeTruthy();
  const ticketNo = (ticket as { ticket_no: string }).ticket_no;

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

  // 1. 在操作台提交一次写入:网关→run→策略裁决,全程同一条受治理路径
  await page.goto("/console");
  await page.getByLabel("你想做什么").fill(`订单 ${order.order_no} 破损了,退 2 元`);
  await page.getByRole("button", { name: "生成提议" }).click();
  await submitProposalAndApproveRun(page);

  // 2. 执行与审计看板:最新一条审计必须是这次写入,并且带前后值
  await page.goto("/governance");
  const latest = page.locator("article").filter({ hasText: "create_refund" }).first();
  await expect(latest).toBeVisible();
  await expect(latest.getByText("成功")).toBeVisible();
  // 这一步是策略要求审批、人批了之后才执行的 —— 审计要能读出这两件事
  await expect(latest.getByText(/策略 REQUIRE_APPROVAL/)).toBeVisible();
  await expect(latest.getByText(/已获 human:supervisor-01 批准后执行/)).toBeVisible();
  // 订单在 seed 里可能已经有工单,所以新工单的下标不固定;
  // 断言收窄到那一行 diff,避免和「完整快照」里的原文冲突
  const change = latest.locator("li").filter({ hasText: /tickets\[\d+\]\.status/ });
  await expect(change).toBeVisible();
  await expect(change).toContainText("—"); // 前值为空:这条工单是这次操作新增的
  await expect(change).toContainText("PENDING");
});

test("大额退款要两个不同身份签字,签够之前业务表一行不动", async ({ page, request }) => {
  // 不传金额 = 全额退款,所以挑一张金额超过双人复核阈值的订单
  const ordersResponse = await request.get(`${API}/api/orders?status=PAID&limit=100`);
  const orders = (await ordersResponse.json()) as { items: OrderSummary[] };
  const ticketsResponse = await request.get(`${API}/api/tickets?limit=100`);
  const tickets = (await ticketsResponse.json()) as { items: TicketSummary[] };
  // 有过任何工单的订单都不选:可退额度可能已经被占,金额就不好判断了
  const touched = new Set(tickets.items.map((ticket) => ticket.order_id));
  const order = orders.items.find(
    (item) => !touched.has(item.order_id) && item.total_amount_cents > 10_000,
  );
  expect(order, "需要一张大额已支付订单,请先执行 make seed-reset").toBeTruthy();
  const target = order as OrderSummary;

  // 网关提交:策略层算出来要两个不同角色的签名
  const invoked = await request.post(`${API}/api/tools/create_refund/invoke`, {
    data: { arguments: { order_id: target.order_id, reason_code: "QUALITY_ISSUE" } },
    headers: { "X-Actor": "operator-07" },
  });
  expect(invoked.status()).toBe(202);
  const runUid = ((await invoked.json()) as { run_uid: string }).run_uid;

  await page.goto(`/governance/${runUid}`);
  await expect(page.getByText(/要求 2 人复核/)).toBeVisible();

  // 第一票:进度变成「还差 1 人」,而不是直接放行
  await page.getByRole("button", { name: /批准第 1 步/ }).click();
  await expect(page.getByRole("button", { name: /还差 1 人/ })).toBeVisible();

  // 没签够之前,业务表一行都不该多
  const midway = await request.get(`${API}/api/tickets?limit=100`);
  const midwayTickets = (await midway.json()) as { items: TicketSummary[] };
  expect(midwayTickets.items.filter((item) => item.order_id === target.order_id)).toHaveLength(0);

  // 第二票:换个身份才签得齐
  await page.getByLabel("审批人身份").selectOption("finance-01");
  await page.getByRole("button", { name: /批准第 1 步/ }).click();
  await expect(page.getByRole("button", { name: /还差 1 人/ })).toHaveCount(0);

  await page.getByRole("button", { name: "推进执行" }).click();
  await expect(page.getByRole("button", { name: "已结束" })).toBeVisible();

  // 签够之后才真的落一张工单,金额是订单的可退全额
  const created = await request.get(`${API}/api/tickets?status=PENDING&limit=100`);
  const pending = (await created.json()) as { items: TicketSummary[] };
  const ticket = pending.items.find((item) => item.order_id === target.order_id);
  expect(ticket, "两个人都签完之后才允许写业务表").toBeTruthy();
});

test("多步计划:先查可退额度,再按查到的金额退款", async ({ page, request }) => {
  const ordersResponse = await request.get(`${API}/api/orders?status=PAID&limit=100`);
  const orders = (await ordersResponse.json()) as { items: OrderSummary[] };
  const ticketsResponse = await request.get(`${API}/api/tickets?limit=100`);
  const tickets = (await ticketsResponse.json()) as { items: TicketSummary[] };
  const touched = new Set(tickets.items.map((ticket) => ticket.order_id));
  // 挑一张小额订单:这一版重点是"多步 + 引用",不是双人复核(那有单独的用例)
  const order = orders.items.find(
    (item) => !touched.has(item.order_id) && item.total_amount_cents < 10_000,
  );
  expect(order, "需要一张小额已支付订单,请先执行 make seed-reset").toBeTruthy();
  const target = order as OrderSummary;

  await page.goto("/console");
  await page.getByLabel("你想做什么").fill(`订单 ${target.order_no} 质量有问题,帮我退全款`);
  await page.getByRole("button", { name: "生成提议" }).click();

  const proposal = page.locator("article", { hasText: "提议卡片" });
  await expect(proposal).toBeVisible();
  await expect(proposal.getByText("2 步计划")).toBeVisible();
  await expect(proposal.getByText("query_refundable", { exact: true }).first()).toBeVisible();
  await expect(proposal.getByText("依赖第 1 步")).toBeVisible();
  // 第二步的金额是引用,不是编出来的数字 —— 页面上要看得出来
  await expect(proposal.getByText("← 上游步骤的产出")).toBeVisible();

  await submitProposalAndApproveRun(page);

  // 两步都要跑完,而且退款金额来自第一步查到的可退额度
  const created = await request.get(`${API}/api/tickets?status=PENDING&limit=100`);
  const pending = (await created.json()) as {
    items: { order_id: number; refund_amount_cents: number }[];
  };
  const ticket = pending.items.find((item) => item.order_id === target.order_id);
  expect(ticket, "整份计划跑完之后才该落工单").toBeTruthy();
  expect((ticket as { refund_amount_cents: number }).refund_amount_cents).toBe(
    target.total_amount_cents,
  );
});

test("失败执行触发补偿:撤不干净时停在等人点,点了才真的撤", async ({ page, request }) => {
  // 挑订单不能靠"有没有工单":种子数据里已经有 200 张工单,列表还有分页。
  // 直接问只读工具"这张单还能退多少",拿真实额度说话。
  const ordersResponse = await request.get(`${API}/api/orders?status=PAID&limit=100`);
  const orders = (await ordersResponse.json()) as { items: OrderSummary[] };

  let target: OrderSummary | undefined;
  for (const candidate of [...orders.items].reverse()) {
    const probe = await request.post(`${API}/api/tools/query_refundable/invoke`, {
      data: { arguments: { order_id: candidate.order_id } },
      headers: { "X-Actor": "operator-01" },
    });
    if (!probe.ok()) continue;
    const body = (await probe.json()) as { result?: { available_refund_cents?: number } };
    if ((body.result?.available_refund_cents ?? 0) >= 100) {
      target = candidate;
      break;
    }
  }
  expect(target, "需要一张还有可退额度的已支付订单,请先执行 make seed-reset").toBeTruthy();
  const order = target as OrderSummary;

  // 第 1 步会成功(退款申请工单),第 2 步必然失败 —— 失败出口要按声明把第 1 步撤回来
  const created = await request.post(`${API}/api/runs`, {
    data: {
      goal: "补偿链路端到端验收",
      steps: [
        {
          seq: 1,
          tool: "create_refund",
          args: {
            order_id: order.order_id,
            reason_code: "QUALITY_ISSUE",
            amount_cents: 100,
            description: "补偿验收",
          },
        },
        {
          seq: 2,
          tool: "query_order",
          args: { order_no: "E2E-NOT-EXIST" },
          depends_on: [1],
        },
      ],
    },
    headers: { "X-Actor": "agent:guardrail" },
  });
  expect(created.ok(), await created.text()).toBeTruthy();
  const runUid = ((await created.json()) as { run_uid: string }).run_uid;

  // 第 1 步是高风险写操作:先按 L2 的规矩签字,再推进执行
  await request.post(`${API}/api/runs/${runUid}/steps/1/approve`, {
    headers: { "X-Actor": "supervisor-01" },
  });
  const executed = await request.post(`${API}/api/runs/${runUid}/execute`);
  expect(executed.ok(), await executed.text()).toBeTruthy();

  // 补偿动作 close_ticket 在默认信任等级下要人工确认:自动补偿**一步都没执行**,停在等人点
  const parked = (await (await request.get(`${API}/api/runs/${runUid}`)).json()) as {
    status: string;
    checkpoint: { compensation?: { status?: string } };
  };
  expect(parked.status).toBe("FAILED");
  expect(parked.checkpoint.compensation?.status).toBe("NEEDS_APPROVAL");

  await page.goto(`/governance/${runUid}`);
  await expect(page.getByText(/补偿 NEEDS_APPROVAL/)).toBeVisible();
  await expect(
    page.getByText("补偿动作在当前权限下需要人工确认:由有权限的人点「撤销这次执行」继续。"),
  ).toBeVisible();
  // 失败原因本身也留在页面上:补偿是"为什么失败"的一部分,不是另一件事
  await expect(page.getByText(/E2E-NOT-EXIST/).first()).toBeVisible();

  // 点按钮 = 有人点头:补偿动作记名执行,工单被关掉
  await expect(page.getByRole("button", { name: "撤销这次执行" })).toBeVisible();
  await expect(page.getByRole("button", { name: "撤销这次执行" })).toHaveClass(/bg-rose-700/);
  await page.getByRole("button", { name: "撤销这次执行" }).click();

  await expect(page.getByText(/补偿 COMPENSATED/)).toBeVisible();
  await expect(page.getByText("这次执行的写操作已按声明逆序撤销完毕。")).toBeVisible();
  await expect(page.getByText(/close_ticket 撤销第 1 步/)).toBeVisible();
  await expect(page.getByRole("button", { name: "撤销这次执行" })).toHaveCount(0);

  const after = (await (await request.get(`${API}/api/tickets?limit=100`)).json()) as {
    items: TicketSummary[];
  };
  const ticket = after.items.find((item) => item.order_id === order.order_id);
  expect(ticket?.status).toBe("CLOSED");
});

test("登录后审批以本人名义签署,登出即失效", async ({ page }) => {
  // 审批是记名动作 —— 签之前必须能在界面上核对"我现在是谁"
  await page.goto("/login");
  await page.getByLabel("用户名").fill("finance-01");
  await page.getByLabel("口令").fill("guardrail-demo");
  await page.getByRole("button", { name: "登录" }).click();

  await expect(page.getByText("周财务")).toBeVisible();
  await expect(page.getByText("human:finance-01")).toBeVisible();

  await page.goto("/approvals");
  await expect(page.getByText(/以 human:finance-01 的名义签署/)).toBeVisible();

  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(page).toHaveURL(/\/login$/);
});
