FROM golang:1.25.7-alpine AS build
WORKDIR /src
COPY packages/platform/go.mod packages/platform/go.sum /src/packages/platform/
COPY infra/monitoring/go.mod infra/monitoring/go.sum /src/infra/monitoring/
WORKDIR /src/infra/monitoring
RUN go mod download
COPY packages/platform /src/packages/platform
COPY infra/monitoring /src/infra/monitoring
RUN CGO_ENABLED=0 go build -trimpath -o /app .
FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /app /app
EXPOSE 8080
ENTRYPOINT ["/app"]
