# PrivAgent frontend — Vite landing page + agent workspace.
#
# Build from the repository root:
#   docker build -f Dockerfile -t privagent-frontend .
# Run standalone:
#   docker run --rm -p 127.0.0.1:5173:5173 privagent-frontend
#
# The workspace talks to the backend at http://localhost:8000 from the
# user's browser (hardcoded in src/agentApp.ts), so the backend must be
# reachable on the host loopback too — see docker-compose.yml, which
# publishes the backend on 127.0.0.1:8000 for exactly this reason.
# This image only serves the built static files with `vite preview`;
# it contains no secrets and no backend code.

FROM node:22-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY index.html agent.html vite.config.ts tsconfig.json ./
COPY src ./src
COPY public ./public
COPY tests ./tests
RUN npm run build

FROM node:22-slim
ENV NODE_ENV=production
WORKDIR /app
# Reuse the exact node_modules that built the app (vite is a devDependency
# but `vite preview` needs it at runtime).
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/dist ./dist
COPY --from=build /app/package.json ./package.json
RUN groupadd -r privagent && useradd -r -g privagent -d /app privagent \
    && chown -R privagent:privagent /app
USER privagent
EXPOSE 5173
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD node -e "fetch('http://127.0.0.1:5173/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["npx", "vite", "preview", "--port", "5173", "--host", "0.0.0.0"]
