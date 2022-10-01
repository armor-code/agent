FROM alpine:3.16.2

RUN apk add --no-cache autossh supervisor

RUN mkdir /etc/armorcode

ENV USER=ssh-user
ENV UID=12345
ENV GID=23456

RUN addgroup -g "$GID" ssh-user

RUN adduser \
    --disabled-password \
    --gecos "" \
    --home "/home/ssh-user" \
    --ingroup "$USER" \
    --uid "$UID" \
    "$USER"

ENTRYPOINT ["supervisord", "--nodaemon", "--configuration", "/etc/armorcode/supervisord.conf"]