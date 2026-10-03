FROM grafana/grafana:11.2.0

USER root
RUN mkdir -p /opt/grafana-plugins && chown 472:0 /opt/grafana-plugins

USER 472
RUN grafana cli --pluginsDir /opt/grafana-plugins plugins install grafana-clickhouse-datasource

ENV GF_PATHS_PLUGINS=/opt/grafana-plugins
