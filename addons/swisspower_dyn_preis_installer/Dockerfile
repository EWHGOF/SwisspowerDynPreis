FROM ghcr.io/home-assistant/base:3.19

COPY run.sh /run.sh
COPY custom_components /root/custom_components

RUN chmod +x /run.sh

CMD ["/run.sh"]
