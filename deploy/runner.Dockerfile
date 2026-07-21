# 多阶段:先构建 Vue Console dist,再装入 Python runner(展厅 compose / 集成测试共用)
FROM node:22-bookworm AS vue-build
WORKDIR /ui
COPY console-ui/package.json console-ui/package-lock.json ./
RUN npm ci
COPY console-ui/ ./
RUN npm run build && node scripts/check-dist.mjs

FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends curl gnupg2 \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
       | gpg --dearmor -o /usr/share/keyrings/microsoft.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" \
       > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 unixodbc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .
COPY --from=vue-build /ui/dist /app/console-ui/dist
ENV D2A_VUE_DIST=/app/console-ui/dist
RUN pip install --no-cache-dir -e ".[dev,connect,mcp,console,ingest,excel]" \
    && test -f /app/console-ui/dist/index.html
