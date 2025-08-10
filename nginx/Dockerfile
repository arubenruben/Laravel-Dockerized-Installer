FROM nginx:alpine

COPY nginx.conf.template /etc/nginx/nginx.conf.template

COPY ./init.sh /etc/nginx/init.sh

RUN chmod +x /etc/nginx/init.sh

ENTRYPOINT ["/bin/sh", "-c", "/etc/nginx/init.sh"]

WORKDIR /var/www/public

EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]