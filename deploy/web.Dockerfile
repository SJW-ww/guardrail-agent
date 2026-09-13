# syntax=docker/dockerfile:1

FROM node:20-alpine AS base
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1

COPY package.json package-lock.json* ./
COPY apps/web/package.json apps/web/package.json
COPY packages/contracts/package.json packages/contracts/package.json
RUN npm install --no-audit --no-fund

# 开发镜像:源码由 compose 挂载,node_modules 留在镜像里
FROM base AS dev
COPY . .
EXPOSE 3000
CMD ["npm", "run", "dev", "-w", "@guardrail/web", "--", "--hostname", "0.0.0.0", "--port", "3000"]

FROM base AS builder
COPY . .
RUN npm run build

# 生产镜像:直接跑 build 产物(体积优先不是 W1 目标,先保证可用)
FROM base AS prod
ENV NODE_ENV=production
COPY --from=builder /app ./
EXPOSE 3000
CMD ["npm", "run", "start", "-w", "@guardrail/web", "--", "-H", "0.0.0.0", "-p", "3000"]

