#!/bin/bash

# Exit on any error
set -e

# Remove any existing application directory. Force a fresh install.
rm -rf ./out/${APP_NAME}

cd ./out

# Install Laravel with React, PHPUnit, and npm
laravel new ${APP_NAME} --react --phpunit --npm --no-interaction

# Install Telescope
composer require laravel/telescope --dev && php artisan telescope:install

# Install L5-Swagger
composer require "darkaonline/l5-swagger" && php artisan vendor:publish --provider "L5Swagger\L5SwaggerServiceProvider"