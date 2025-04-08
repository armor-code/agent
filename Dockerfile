FROM alpine:3.21.3

RUN apk update && \
    apk upgrade && \
	apk add --no-cache autossh supervisor && \
	rm -rf /var/cache/apk/*
RUN mkdir /etc/armorcode

ENTRYPOINT ["supervisord", "--nodaemon", "--configuration", "/etc/armorcode/supervisord.conf"]