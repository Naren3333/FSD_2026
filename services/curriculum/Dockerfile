FROM golang:1.25.7-alpine AS build
WORKDIR /src
# Modules first, so a code change reuses the downloaded-modules layer. The replace in go.mod points at
# packages/platform, which only needs its go.mod for the download.
COPY packages/platform/go.mod packages/platform/go.sum /src/packages/platform/
COPY services/curriculum/go.mod services/curriculum/go.sum /src/services/curriculum/
WORKDIR /src/services/curriculum
RUN go mod download
COPY packages/platform /src/packages/platform
COPY services/curriculum /src/services/curriculum
RUN CGO_ENABLED=0 go build -trimpath -o /app .
FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /app /app
EXPOSE 8080
ENTRYPOINT ["/app"]
