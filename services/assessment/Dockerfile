FROM golang:1.25.7-alpine AS build
WORKDIR /src
COPY packages/platform /src/packages/platform
COPY services/assessment /src/services/assessment
WORKDIR /src/services/assessment
RUN go mod download && CGO_ENABLED=0 go build -trimpath -o /app .
FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /app /app
EXPOSE 8080
ENTRYPOINT ["/app"]
