FROM golang:1.26-alpine AS builder
WORKDIR /app
COPY go.mod ./
COPY *.go ./
RUN go build -o canary .

FROM alpine:3.20
WORKDIR /app
COPY --from=builder /app/canary .
CMD ["./canary"]
