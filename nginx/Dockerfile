FROM nginx:alpine

WORKDIR /etc/nginx

COPY nginx.conf.template .

COPY init.sh .

RUN chmod +x init.sh

ENTRYPOINT ["/bin/sh", "-c", "./init.sh"]

# Expose port 80 and start Nginx
EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]