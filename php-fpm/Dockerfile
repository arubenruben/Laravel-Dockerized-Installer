FROM composer:latest AS composer

FROM php:8.4-fpm

# Set the working directory to Laravel standard location
WORKDIR /var/www

# Create www-data user and group if they don't exist
RUN groupadd -g 1000 www-data || true && \
    useradd -u 1000 -ms /bin/bash -g www-data www-data || true

# Copy the composer stage
COPY --from=composer /usr/bin/composer /usr/bin/composer
# Set the PATH for Composer
ENV PATH="/root/.composer/vendor/bin:${PATH}"

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    unzip \
    libzip-dev \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install PHP extensions. Postgresql is needed
RUN docker-php-ext-install zip pdo pdo_pgsql

# Install only runtime libraries needed in production
# libfcgi-bin and procps are required for the php-fpm-healthcheck script
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    libicu-dev \
    libzip-dev \
    libfcgi-bin \
    procps \
    && apt-get autoremove -y && apt-get clean && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# Download and install php-fpm health check script
RUN curl -o /usr/local/bin/php-fpm-healthcheck \
    https://raw.githubusercontent.com/renatomefi/php-fpm-healthcheck/master/php-fpm-healthcheck \
    && chmod +x /usr/local/bin/php-fpm-healthcheck

# Install Node 24
RUN curl -fsSL https://deb.nodesource.com/setup_24.x | bash - && \
    apt-get install -y nodejs

# Copy 
COPY ./dev.sh .
RUN chmod +x ./dev.sh

# Copy production script
COPY ./prod.sh .
RUN chmod +x ./prod.sh

# Copy PHP-FPM configuration
COPY ./www.conf /usr/local/etc/php-fpm.d/www.conf

EXPOSE 9000