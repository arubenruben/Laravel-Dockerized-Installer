FROM nginx:alpine

COPY ./nginx/nginx.conf.template /etc/nginx/nginx.conf.template

# Expose port 80 and start Nginx
EXPOSE 80

# Use envsubst to substitute environment variables and start nginx
ENTRYPOINT ["/bin/sh", "-c", "envsubst < /etc/nginx/nginx.conf.template > /etc/nginx/nginx.conf && nginx -g 'daemon off;'"]