# --- build stage ---
FROM node:20-alpine AS build
WORKDIR /app

# Install deps (prefer npm ci when lockfile exists)
COPY package.json package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

# Copy source
COPY . .

# Build-time env (Vite reads VITE_* at build time)
ARG VITE_API_BASE=/api
ENV VITE_API_BASE=$VITE_API_BASE

RUN npm run build


# --- runtime stage ---
FROM nginx:1.27-alpine

# Nginx config (SPA fallback)
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Static build output
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD wget -qO- http://127.0.0.1/ >/dev/null || exit 1
