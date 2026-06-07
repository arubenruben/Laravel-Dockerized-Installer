import io
import zipfile

# ---------------------------------------------------------------------------
# Docker scaffold file contents
# ---------------------------------------------------------------------------

DOCKERFILE_CONTENT = """\
FROM php:8.3-fpm-alpine

RUN apk add --no-cache \\
        bash curl zip unzip git libpng-dev libjpeg-turbo-dev freetype-dev \\
        oniguruma-dev libxml2-dev && \\
    docker-php-ext-configure gd --with-freetype --with-jpeg && \\
    docker-php-ext-install pdo pdo_mysql mbstring gd xml bcmath opcache

COPY --from=composer:2 /usr/bin/composer /usr/bin/composer

WORKDIR /var/www/html
COPY . .

RUN composer install --no-dev --optimize-autoloader --no-interaction && \\
    chown -R www-data:www-data /var/www/html/storage /var/www/html/bootstrap/cache

EXPOSE 9000
CMD ["php-fpm"]
"""

DOCKER_COMPOSE_CONTENT = """\
services:
  app:
    build: .
    container_name: laravel_app
    restart: unless-stopped
    working_dir: /var/www/html
    volumes:
      - .:/var/www/html
    depends_on:
      - db
      - redis
    environment:
      APP_ENV: local
      APP_KEY: ""
      DB_CONNECTION: mysql
      DB_HOST: db
      DB_PORT: 3306
      DB_DATABASE: laravel
      DB_USERNAME: laravel
      DB_PASSWORD: secret
      REDIS_HOST: redis
      CACHE_DRIVER: redis
      SESSION_DRIVER: redis
      QUEUE_CONNECTION: redis

  nginx:
    image: nginx:alpine
    container_name: laravel_nginx
    restart: unless-stopped
    ports:
      - "8080:80"
    volumes:
      - .:/var/www/html
      - ./docker/nginx/default.conf:/etc/nginx/conf.d/default.conf
    depends_on:
      - app

  db:
    image: mysql:8.0
    container_name: laravel_db
    restart: unless-stopped
    environment:
      MYSQL_DATABASE: laravel
      MYSQL_USER: laravel
      MYSQL_PASSWORD: secret
      MYSQL_ROOT_PASSWORD: rootsecret
    ports:
      - "3306:3306"
    volumes:
      - db_data:/var/lib/mysql

  redis:
    image: redis:7-alpine
    container_name: laravel_redis
    restart: unless-stopped
    ports:
      - "6379:6379"

volumes:
  db_data:
"""

NGINX_CONF_CONTENT = """\
server {
    listen 80;
    server_name localhost;
    root /var/www/html/public;
    index index.php index.html;

    location / {
        try_files $uri $uri/ /index.php?$query_string;
    }

    location ~ \\.php$ {
        fastcgi_pass app:9000;
        fastcgi_index index.php;
        fastcgi_param SCRIPT_FILENAME $realpath_root$fastcgi_script_name;
        include fastcgi_params;
    }

    location ~ /\\.(?!well-known).* {
        deny all;
    }
}
"""

ENV_DOCKER_CONTENT = """\
APP_NAME=Laravel
APP_ENV=local
APP_KEY=
APP_DEBUG=true
APP_URL=http://localhost:8080

LOG_CHANNEL=stack

DB_CONNECTION=mysql
DB_HOST=db
DB_PORT=3306
DB_DATABASE=laravel
DB_USERNAME=laravel
DB_PASSWORD=secret

BROADCAST_DRIVER=log
CACHE_DRIVER=redis
FILESYSTEM_DISK=local
QUEUE_CONNECTION=redis
SESSION_DRIVER=redis
SESSION_LIFETIME=120

REDIS_HOST=redis
REDIS_PASSWORD=null
REDIS_PORT=6379
"""


# ---------------------------------------------------------------------------
# Public helper
# ---------------------------------------------------------------------------


def build_docker_zip(upstream_zip_bytes: bytes) -> io.BytesIO:
    """
    Re-packages the upstream Laravel source zip, injecting Docker scaffold files:
      - Dockerfile
      - docker-compose.yml
      - docker/nginx/default.conf
      - .env.docker
    Returns a seeked BytesIO ready for streaming.
    """
    output_buffer = io.BytesIO()

    with zipfile.ZipFile(io.BytesIO(upstream_zip_bytes), "r") as upstream_zip:
        with zipfile.ZipFile(output_buffer, "w", compression=zipfile.ZIP_DEFLATED) as out_zip:
            for item in upstream_zip.infolist():
                out_zip.writestr(item, upstream_zip.read(item.filename))

            # Detect root folder name (e.g. "laravel-laravel-abc123/")
            root = upstream_zip.namelist()[0].split("/")[0] + "/"

            out_zip.writestr(root + "Dockerfile", DOCKERFILE_CONTENT)
            out_zip.writestr(root + "docker-compose.yml", DOCKER_COMPOSE_CONTENT)
            out_zip.writestr(root + "docker/nginx/default.conf", NGINX_CONF_CONTENT)
            out_zip.writestr(root + ".env.docker", ENV_DOCKER_CONTENT)

    output_buffer.seek(0)
    return output_buffer
