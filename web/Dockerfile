ARG NGINX_BASE=nginx:stable-alpine@sha256:583b7f2fce1cdb8da5ad5c39ebd3c33b98b87d493190e548bdee1fa8c619c311
FROM ${NGINX_BASE}

RUN mkdir -p /app/tmp/web /app/web \
    && chown 10001:10001 /app/tmp/web \
    && chmod 0700 /app/tmp/web
COPY web/nginx.conf /etc/nginx/nginx.conf
COPY web/index.html web/app.js web/style.css web/data-updates.js web/data-updates.css web/template-layout.js web/template-layout.css /app/web/

USER 10001:10001
EXPOSE 8080
ENTRYPOINT []
CMD ["nginx", "-g", "daemon off;"]
