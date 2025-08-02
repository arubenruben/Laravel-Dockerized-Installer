# Laravel-Dockerized-Installer (Beta)
🚢 Install a Fresh Laravel App Without PHP/Node/Composer - The Docker Way!

## Why This Exists

As a Laravel developer who absolutely **hates** installing PHP, Node.js, and Composer directly on Windows, I created this solution. Why deal with version conflicts, PATH issues, and the general Windows development environment headaches when Docker can handle everything cleanly?

I'm a **Docker stan** 🐳 and believe containerization is the future of development environments. This tool lets you bootstrap Laravel projects without polluting your local machine with dependencies.

## ⚠️ Beta Version Notice

This is a **beta version** that currently serves my specific development needs and workflow. It's battle-tested for my use cases, but I have plans to create a more extensible and configurable version that will serve anyone's Laravel development needs.

If you find bugs or have feature requests, please open an issue!

## What It Does

This dockerized installer creates a fresh Laravel application with:

- ✅ **Laravel** (latest version)
- ✅ **React** frontend scaffolding
- ✅ **PHPUnit** for testing
- ✅ **NPM** package management
- ✅ **Laravel Telescope** (development debugging)
- ✅ **L5-Swagger** (API documentation)
- ✅ **Laravel Shift Blueprint** (rapid prototyping)
- ✅ **Laravel Debugbar** (development debugging)
- ✅ **PHP 8.4** with all necessary extensions
- ✅ **Node.js 24** for modern frontend tooling
- ✅ **PostgreSQL** and **MySQL** database support

## Quick Start

1. **Clone this repository:**
   ```bash
   git clone https://github.com/arubenruben/Laravel-Dockerized-Installer.git
   cd Laravel-Dockerized-Installer
   ```

2. **Set your app name:**
   ```bash
   cp .env.example .env
   # Edit .env and set APP_NAME=your-awesome-app
   ```

3. **Run the installer:**
   ```bash
   docker-compose up --build
   ```

4. **Find your new Laravel app:**
   Your freshly installed Laravel application will be in the `./out/{APP_NAME}` directory!

## Docker Images Available

You can also pull pre-built images instead of building locally:

### GitHub Container Registry (GHCR)
- **Registry**: [https://github.com/arubenruben?tab=packages](https://github.com/arubenruben?tab=packages)
- **Usage**: `docker pull ghcr.io/arubenruben/laravel-dockerized-installer:latest`

### Docker Hub
- **Registry**: [https://hub.docker.com/u/arubenruben](https://hub.docker.com/u/arubenruben)
- **Usage**: `docker pull arubenruben/laravel-dockerized-installer:latest`

## Project Structure

```
Laravel-Dockerized-Installer/
├── Dockerfile              # Multi-stage build with PHP 8.4 + Node 24
├── docker-compose.yml      # Simple service definition
├── install.sh             # Laravel installation script
├── .env.example           # Configuration template
├── out/                   # Output directory for generated apps
└── .github/workflows/     # CI/CD for automated image builds
```

## How It Works

1. **Docker Build**: Creates a container with PHP 8.4, Node.js 24, Composer, and Laravel installer
2. **Fresh Install**: Removes any existing app and creates a completely fresh Laravel installation
3. **Package Installation**: Automatically installs and configures essential Laravel packages
4. **Output**: Places the complete Laravel application in your local `./out` directory

## Future Plans

The next version will include:
- 🔧 Configurable package selection
- 🎨 Multiple frontend framework options (Vue, Alpine, etc.)
- 🗄️ Database selection and setup automation
- 📦 Custom package preset definitions
- 🚀 One-command deployment options
- 🔒 Security hardening options

## Contributing

This is a personal project in beta, but contributions are welcome! Please:
1. Fork the repository
2. Create a feature branch
3. Submit a pull request with clear description

## License

This project is licensed under the **GNU General Public License v3.0 (GPL-3)** to limit commercial use and ensure that any derivatives remain open source. This means you're free to use, modify, and distribute this software, but any commercial use or proprietary modifications must also be released under the same GPL-3 license. Check the LICENSE file for complete details.

---
*This README was written using the Claude Sonnet 4 model.*
