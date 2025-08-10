#!/bin/sh

# Exit on any error
set -e

echo "Starting Laravel production environment..."

# Check if we're in a Laravel project directory
if [ ! -f "artisan" ]; then
    echo "Error: artisan file not found. Make sure you're in a Laravel project directory."
    exit 1
fi

# Create Laravel directories if they don't exist
echo "Creating Laravel directories..."
mkdir -p /var/www/storage/app/public \
    /var/www/storage/framework/cache \
    /var/www/storage/framework/sessions \
    /var/www/storage/framework/testing \
    /var/www/storage/framework/views \
    /var/www/storage/logs \
    /var/www/bootstrap/cache

# Set proper ownership and permissions for Laravel
echo "Setting proper ownership and permissions..."
chown -R www-data:www-data /var/www
find /var/www -type f -exec chmod 644 {} \;
find /var/www -type d -exec chmod 755 {} \;
chmod -R 775 /var/www/storage
chmod -R 775 /var/www/bootstrap/cache
chmod +x /var/www/artisan
chmod +x /var/www/dev.sh
chmod +x /var/www/prod.sh

# Always install dependencies first
echo "Installing npm dependencies..."
npm install
if [ $? -ne 0 ]; then
    echo "Error: npm install failed"
    exit 1
fi

echo "Installing composer dependencies (production only)..."
composer install --no-dev --optimize-autoloader
if [ $? -ne 0 ]; then
    echo "Error: composer install failed"
    exit 1
fi

# Verify installations
echo "Verifying installations..."
if [ ! -f "vendor/autoload.php" ]; then
    echo "Error: vendor/autoload.php not found after composer install"
    exit 1
fi

# Build assets for production
echo "Building assets for production..."
npm run build
if [ $? -ne 0 ]; then
    echo "Error: npm run build failed"
    exit 1
fi

# Cache Laravel configuration for production
echo "Optimizing Laravel for production..."
php artisan config:cache
php artisan route:cache
php artisan view:cache

echo "Production environment setup completed!"
echo "Ready to serve Laravel application in production mode."

# Run php-fpm in the foreground
echo "Starting PHP-FPM..."
exec php-fpm