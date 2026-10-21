FROM golang:1.25.7-alpine AS build
WORKDIR /src
# modules first, so a code change reuses the downloaded-modules layer.
COPY packages/platform/go.mod packages/platform/go.sum /src/packages/platform/
COPY services/identity/go.mod services/identity/go.sum /src/services/identity/
WORKDIR /src/services/identity
RUN go mod download
COPY packages/platform /src/packages/platform
COPY services/identity /src/services/identity
RUN CGO_ENABLED=0 go build -trimpath -o /app .
FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /app /app
EXPOSE 8080
ENTRYPOINT ["/app"]
