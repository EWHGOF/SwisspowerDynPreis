ARG BUILD_FROM=ghcr.io/home-assistant/base:3.19
FROM ${BUILD_FROM}

COPY run.sh /run.sh
COPY custom_components /root/custom_components

RUN chmod +x /run.sh

CMD ["/run.sh"]
