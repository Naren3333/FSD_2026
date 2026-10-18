FROM golang:1.25.7-alpine AS build
WORKDIR /src
# Modules first, so a code change reuses the downloaded-modules layer. The replace in go.mod points at
# packages/platform, which only needs its go.mod for the download.
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
