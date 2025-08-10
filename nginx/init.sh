#!/bin/sh

# Exit on any error
set -e

envsubst < /etc/nginx/nginx.conf.template > /etc/nginx/nginx.conf

# The default command is to run nginx in the foreground
exec /docker-entrypoint.sh

exec "$@"