# rahbar-ui: static chat page served by nginx as a non-root user on 8080.
#   docker build -t rahbar-ui:dev ui
# nginx-unprivileged already runs as uid 101 and writes pid/temp files under /tmp; our
# nginx.conf does the same, so the pod can mount an emptyDir at /tmp and keep the root
# filesystem read-only (STARTER.md, "Images and ports").
FROM nginxinc/nginx-unprivileged:1.31.7-alpine

# Patch Alpine packages (OpenSSL, brotli, curl ...) that the base image lags on; the
# image runs as uid 101, so switch to root only for the upgrade.
USER root
# The date is part of the layer's cache key: a cached `apk upgrade` layer is never re-run,
# so a CVE fixed after the layer was built stays in the image (libxslt, 11 Nov 2026, caught
# by the Trivy gate). Bump the date to pull the current patches.
ARG APK_PATCH_DATE=2026-11-11
RUN echo "apk patches as of ${APK_PATCH_DATE}" && apk upgrade --no-cache

COPY nginx.conf /etc/nginx/nginx.conf
COPY index.html /usr/share/nginx/html/index.html

USER 101
EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]
