#!/bin/sh

# Exit on any error
set -e

envsubst < /etc/nginx/nginx.conf.template > /etc/nginx/nginx.conf

exec "$@"
