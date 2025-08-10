#!/bin/sh

# Exit on any error
set -e

envsubst < /etc/nginx/nginx.conf.template > /etc/nginx/nginx.conf

# Run this script since it is mentioned as entrypoint for nginx base image
exec /docker-entrypoint.sh