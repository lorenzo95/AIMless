FROM golang:1.27-alpine AS build
# TRANSPORT selects which daemon to build: yggdrasil (default) or tailcat.
#   docker build --build-arg TRANSPORT=tailcat -f deploy/Dockerfile -t aimlessd-tailcat .
# One image per transport; give each its own /data volume so identities stay apart.
ARG TRANSPORT=yggdrasil
WORKDIR /src
COPY daemon/go.mod daemon/go.sum ./
RUN go mod download
COPY daemon/*.go ./
RUN if [ "$TRANSPORT" = "tailcat" ]; then go build -tags tailcat -trimpath -ldflags "-s -w" -o /aimlessd .; else go build -trimpath -ldflags "-s -w" -o /aimlessd .; fi

FROM alpine:3.20
RUN apk add --no-cache ca-certificates && adduser -D -u 1000 aimless
COPY --from=build /aimlessd /usr/local/bin/aimlessd
USER aimless
WORKDIR /data
VOLUME /data
ENTRYPOINT ["/usr/local/bin/aimlessd", "-datadir", "/data/daemon"]
