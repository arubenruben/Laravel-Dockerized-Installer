#!/bin/bash

# Exit on any error
set -e

# Remove any existing application directory. Force a fresh install.
rm -rf ./out/${APP_NAME}

cd ./out

# Install Laravel with React, PHPUnit, and npm
laravel new ${APP_NAME} --react --phpunit --npm --no-interaction

cd ${APP_NAME}