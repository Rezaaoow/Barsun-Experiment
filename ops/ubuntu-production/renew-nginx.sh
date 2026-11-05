#!/bin/sh
set -eu
/usr/sbin/nginx -t
/bin/systemctl reload nginx
/usr/bin/logger -t barsan-tls-renewal "Nginx configuration verified and reloaded after certificate renewal"
