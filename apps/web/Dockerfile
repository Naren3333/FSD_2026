FROM node:22.17.0-alpine AS build
WORKDIR /app
COPY apps/web/package*.json ./
RUN npm ci
COPY apps/web .
RUN npm run build
# nginx-unprivileged runs every nginx process as the nginx user, so it listens on 8080 rather than 80.
FROM nginxinc/nginx-unprivileged:1.28.0-alpine
# nginx's entrypoint writes templates/default.conf.template to conf.d/default.conf with these two values
# filled in, and only these, so nginx's own $variables are left alone.
ENV GATEWAY_URL=http://localhost:8000 KEYCLOAK_URL=http://localhost:8081 NGINX_ENVSUBST_FILTER=^(GATEWAY|KEYCLOAK)_URL$
COPY apps/web/nginx.conf /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist/web/browser /usr/share/nginx/html
EXPOSE 8080
