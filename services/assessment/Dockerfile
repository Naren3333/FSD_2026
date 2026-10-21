FROM golang:1.25.7-alpine AS build
WORKDIR /src
# modules first, so a code change reuses the downloaded-modules layer.
COPY packages/platform/go.mod packages/platform/go.sum /src/packages/platform/
COPY services/assessment/go.mod services/assessment/go.sum /src/services/assessment/
WORKDIR /src/services/assessment
RUN go mod download
COPY packages/platform /src/packages/platform
COPY services/assessment /src/services/assessment
RUN CGO_ENABLED=0 go build -trimpath -o /app .
FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /app /app
EXPOSE 8080
ENTRYPOINT ["/app"]
