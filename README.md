# Laravel-Dockerized-Installer (Beta)
🚢 Multi-Service Laravel Development Environment - Powered by Docker!

## Why This Exists

As a Laravel developer who absolutely **hates** installing PHP, Node.js, and Composer directly on Windows, I created this solution. Why deal with version conflicts, PATH issues, and the general Windows development environment headaches when Docker can handle everything cleanly?

I'm a **Docker stan** 🐳 and believe containerization is the future of development environments. This tool provides a complete Laravel development stack without polluting your local machine with dependencies.

## ⚠️ Beta Version Notice

This is a **beta version** that currently serves my specific development needs and workflow. It's battle-tested for my use cases, but I have plans to create a more extensible and configurable version that will serve anyone's Laravel development needs.

If you find bugs or have feature requests, please open an issue!

## What It Provides

This dockerized environment offers a complete Laravel development stack with:

### Laravel Installer Service
- ✅ **Laravel** (latest version)
- ✅ **React** frontend scaffolding
- ✅ **PHPUnit** for testing
- ✅ **NPM** package management
- ✅ **PHP 8.4** with CLI extensions
- ✅ **Node.js 24** for modern frontend tooling
- ✅ **Composer** for dependency management

### Complete Development Stack
- 🐳 **Multi-container architecture** (Laravel installer, Nginx, PHP-FPM)
- 🔧 **Nginx** web server with optimized configuration
- ⚡ **PHP-FPM 8.4** for production-ready performance
- 📦 **Modular Dockerfile structure** for easy customization

## Quick Start

### Option 1: Fresh Laravel Installation (Recommended)

1. **Clone this repository:**
   ```bash
   git clone https://github.com/arubenruben/Laravel-Dockerized-Installer.git
   cd Laravel-Dockerized-Installer
   ```

2. **Set your app name:**
   ```bash
   # Copy the environment file from the laravel directory
   cp laravel/.env.example laravel/.env
   # Edit laravel/.env and set APP_NAME=your-awesome-app
   ```

3. **Run the Laravel installer:**
   ```bash
   # Create the output directory
   mkdir -p out
   
   # Run the installer using the Laravel service directly
   docker-compose run --rm app
   ```

4. **Find your new Laravel app:**
   Your freshly installed Laravel application will be in the `./out/{APP_NAME}` directory!

### Option 2: Use Pre-built Images

If you prefer to use pre-built images instead of building locally, you can pull them directly:

```bash
# Pull the Laravel installer
docker pull ghcr.io/arubenruben/laravel-installer:latest

# Pull supporting services
docker pull ghcr.io/arubenruben/nginx-laravel-proxy:latest
docker pull ghcr.io/arubenruben/laravel-php-fpm:latest
```

## Docker Images Available

Pre-built images are available from multiple registries:

### GitHub Container Registry (GHCR)
- **Laravel Installer**: `ghcr.io/arubenruben/laravel-installer:latest`
- **Nginx Proxy**: `ghcr.io/arubenruben/nginx-laravel-proxy:latest`  
- **PHP-FPM**: `ghcr.io/arubenruben/laravel-php-fpm:latest`
- **Registry**: [https://github.com/arubenruben?tab=packages](https://github.com/arubenruben?tab=packages)

### Docker Hub
- **Registry**: [https://hub.docker.com/u/arubenruben](https://hub.docker.com/u/arubenruben)
- **Usage**: Check the registry for latest available tags

## Project Structure

```
Laravel-Dockerized-Installer/
├── docker-compose.yml         # Main orchestration file
├── laravel/                   # Laravel installer service
│   ├── Dockerfile            # Laravel + Node.js build environment
│   ├── install.sh            # Laravel installation script
│   ├── .env.example          # Configuration template
│   └── out/                  # Output directory for generated apps
├── nginx/                    # Nginx web server service
│   ├── Dockerfile           # Nginx Alpine build
│   ├── nginx.conf.template  # Nginx configuration template
│   └── init.sh              # Nginx initialization script  
├── php-fpm/                 # PHP-FPM application service
│   ├── Dockerfile          # PHP 8.4-FPM with extensions
│   ├── www.conf            # FPM pool configuration
│   ├── dev.sh              # Development setup script
│   └── prod.sh             # Production setup script
└── .github/workflows/      # CI/CD for automated image builds
```

## How It Works

### Multi-Service Architecture

1. **Laravel Installer Service** (`laravel/`):
   - Builds a container with PHP 8.4 CLI, Node.js 24, Composer, and Laravel installer
   - Executes the installation script to create a fresh Laravel application
   - Outputs the complete Laravel project to the local `./out` directory

2. **Nginx Service** (`nginx/`):
   - Provides a production-ready web server configuration
   - Serves static assets and proxies PHP requests to PHP-FPM
   - Uses Alpine Linux for minimal footprint

3. **PHP-FPM Service** (`php-fpm/`):
   - Runs PHP 8.4 with FPM (FastCGI Process Manager)
   - Optimized for production Laravel applications
   - Includes all necessary PHP extensions

### Installation Process

1. **Environment Setup**: The installer removes any existing app and creates a completely fresh Laravel installation
2. **Fresh Install**: Uses `laravel new` with React scaffolding, PHPUnit testing, and npm package management
3. **Output**: Places the complete Laravel application in your local `./out/{APP_NAME}` directory

## Configuration

### Environment Variables

Configure your installation by editing `laravel/.env`:

```bash
# Required: Your application name
APP_NAME=my-awesome-laravel-app

# Optional: Add other configuration as needed
```

### Customizing Services

Each service can be customized by modifying its respective Dockerfile and configuration files:

- **Laravel Installer**: Modify `laravel/install.sh` to add additional packages or configuration
- **Nginx**: Update `nginx/nginx.conf.template` for custom server configuration  
- **PHP-FPM**: Edit `php-fpm/www.conf` for FPM pool settings

## Future Plans

The next version will include:
- 🔧 **Configurable package selection** (Telescope, Debugbar, Blueprint, etc.)
- 🎨 **Multiple frontend framework options** (Vue, Alpine, Livewire)
- 🗄️ **Database service integration** (PostgreSQL, MySQL, Redis)
- 📦 **Custom package preset definitions**
- 🚀 **Complete development environment** with hot reloading
- 🔒 **Security hardening options**
- 🐋 **Production-ready container orchestration**
- ⚙️ **One-command setup for existing Laravel projects**

## Troubleshooting

### Common Issues

1. **Missing Dockerfile at root**: The current docker-compose.yml expects a Dockerfile at the project root. If you encounter build errors, ensure you're using the correct service configuration.

2. **Permission Issues**: On Windows with WSL2, ensure Docker has proper permissions to mount volumes.

3. **App Name Not Set**: Make sure to configure `APP_NAME` in `laravel/.env` before running the installer.

### Getting Help

- **Issues**: Report bugs on [GitHub Issues](https://github.com/arubenruben/Laravel-Dockerized-Installer/issues)
- **Discussions**: Join conversations in [GitHub Discussions](https://github.com/arubenruben/Laravel-Dockerized-Installer/discussions)

## Contributing

This is a personal project in beta, but contributions are welcome! Please:
1. Fork the repository
2. Create a feature branch
3. Submit a pull request with clear description

## License

This project is licensed under the **GNU General Public License v3.0 (GPL-3)** to limit commercial use and ensure that any derivatives remain open source. This means you're free to use, modify, and distribute this software, but any commercial use or proprietary modifications must also be released under the same GPL-3 license. Check the LICENSE file for complete details.

---
*This README was written using the Claude Sonnet 4 model.*
