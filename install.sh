#!/bin/bash

# Exit on any error
set -e

# Remove any existing application directory. Force a fresh install.
rm -rf ./out/${APP_NAME}

cd ./out

# Install Laravel with React, PHPUnit, and npm
laravel new ${APP_NAME} --react --phpunit --npm --no-interaction

cd ${APP_NAME}

# Install Telescope
composer require laravel/telescope --dev && php artisan telescope:install

# Install L5-Swagger
composer require "darkaonline/l5-swagger" && php artisan vendor:publish --provider "L5Swagger\L5SwaggerServiceProvider"

# Install Laravel Shift Blueprint
composer require -W --dev laravel-shift/blueprint
composer require --dev jasonmccreary/laravel-test-assertions

echo '/draft.yaml' >> .gitignore
echo '/.blueprint' >> .gitignore

# Install Laravel Debugbar
composer require --dev barryvdh/laravel-debugbar