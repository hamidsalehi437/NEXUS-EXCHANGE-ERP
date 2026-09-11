# Shared proxy directives for every location that reaches the API.
#
# X-Forwarded-* are set here and never passed through from the client, so the
# application can trust them: the API port is not published, and uvicorn runs
# with --proxy-headers and FORWARDED_ALLOW_IPS=<compose subnet>.

proxy_http_version 1.1;

# The application's TRUSTED_HOSTS allow-list evaluates the Host it receives, so
# the public host is preserved instead of being rewritten to `api`.
proxy_set_header Host              $host;
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header X-Forwarded-Host  $host;
proxy_set_header X-Request-ID      $request_id;

# WebSocket/streaming support for the sync endpoints (PART 34) without
# buffering responses on the edge: `$connection_upgrade` is "upgrade" for a real
# upgrade request and "close" otherwise (see the map in nginx.conf).
proxy_set_header Upgrade    $http_upgrade;
proxy_set_header Connection $connection_upgrade;
proxy_buffering off;

# Do not let a slow client keep an upstream connection open indefinitely.
proxy_next_upstream error timeout http_502 http_503 http_504;
