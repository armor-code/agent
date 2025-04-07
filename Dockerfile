FROM alpine:3.21.3

RUN apk update && \
    apk upgrade && \
	apk add --no-cache autossh supervisor
RUN mkdir /etc/armorcode

ENTRYPOINT ["supervisord", "--nodaemon", "--configuration", "/etc/armorcode/supervisord.conf"]