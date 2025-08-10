#!/bin/sh

# Exit on any error
set -e

echo "Starting Laravel development environment..."

# Check if we're in a Laravel project directory
if [ ! -f "artisan" ]; then
    echo "Error: artisan file not found. Make sure you're in a Laravel project directory."
    exit 1
fi

# Always install dependencies to ensure they exist in volumes
echo "Installing npm dependencies..."
npm install
if [ $? -ne 0 ]; then
    echo "Error: npm install failed"
    exit 1
fi

echo "Installing composer dependencies (including dev dependencies)..."
composer install --optimize-autoloader
if [ $? -ne 0 ]; then
    echo "Error: composer install failed"
    exit 1
fi

# Verify installations
echo "Verifying installations..."
if [ ! -f "node_modules/.bin/vite" ]; then
    echo "Error: vite not found after npm install"
    exit 1
fi

if [ ! -f "vendor/autoload.php" ]; then
    echo "Error: vendor/autoload.php not found after composer install"
    exit 1
fi

# Verify Laravel can bootstrap before starting servers
echo "Verifying Laravel installation..."
php artisan --version
if [ $? -ne 0 ]; then
    echo "Error: Laravel artisan command failed. Check your installation."
    exit 1
fi

# Start npm run dev in the background
echo "Starting npm run dev..."
npm run dev &
NPM_PID=$!

# Wait a moment for npm to start
sleep 3

# Start php artisan serve in the background
echo "Starting Laravel development server..."
php artisan serve --host=0.0.0.0 --port=8000 &
ARTISAN_PID=$!

echo "Development environment started!"
echo "- Laravel app: http://localhost:8000"
echo "- Vite dev server will be available for hot reloading"
echo ""
echo "Press Ctrl+C to stop all services"

# Wait for both processes
wait $NPM_PID $ARTISAN_PID