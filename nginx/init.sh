#!/bin/sh

# Exit on any error
set -e

echo "Substituting environment variables in Nginx configuration..."

envsubst < /etc/nginx/nginx.conf.template > /etc/nginx/nginx.conf

# Run this script since it is mentioned as entrypoint for nginx base image
echo "Running Nginx entrypoint script..."
sh /docker-entrypoint.sh

echo "Running Nginx..."
exec "$@"