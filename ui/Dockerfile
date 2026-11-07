# rahbar-ui: static chat page served by nginx as a non-root user on 8080.
#   docker build -t rahbar-ui:dev ui
# nginx-unprivileged already runs as uid 101 and writes pid/temp files under /tmp; our
# nginx.conf does the same, so the pod can mount an emptyDir at /tmp and keep the root
# filesystem read-only (STARTER.md, "Images and ports").
FROM nginxinc/nginx-unprivileged:1.27-alpine

COPY nginx.conf /etc/nginx/nginx.conf
COPY index.html /usr/share/nginx/html/index.html

USER 101
EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]
