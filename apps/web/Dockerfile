FROM node:22.17.0-alpine AS build
WORKDIR /app
COPY apps/web/package*.json ./
RUN npm ci
COPY apps/web .
RUN npm run build
FROM nginx:1.28.0-alpine
COPY --from=build /app/dist/web/browser /usr/share/nginx/html
EXPOSE 80
