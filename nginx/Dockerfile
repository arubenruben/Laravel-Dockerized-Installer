FROM nginx:alpine

# Set default PHP-FPM connection details
ENV PHP_FPM_HOST=php-fpm
ENV PHP_FPM_PORT=9000

COPY nginx.conf.template /etc/nginx/nginx.conf.template

COPY ./init.sh /etc/nginx/init.sh

RUN chmod +x /etc/nginx/init.sh

ENTRYPOINT ["/etc/nginx/init.sh"]

WORKDIR /var/www/public

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]